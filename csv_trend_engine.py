"""
CSV Trend Engine — The brain of Zero RIR Bot.

Reads ALL workout history from workouts.csv and computes intelligent targets
based on long-term trends (50-100 sessions), recent momentum (10-20 sessions),
and math-based double progression. ML predictions are shown as a hint only,
and only for exercises where the model beats a "no change" baseline.

This module replaces the old exercise_history.json approach.
The CSV is the single source of truth.
"""

import os
import re
import json
import math
import datetime
import numpy as np
import pandas as pd
import joblib
from scipy import stats

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
CSV_FILE = "workouts.csv"
MODELS_DIR = "models"
MODEL_METADATA_FILE = os.path.join(MODELS_DIR, "model_metadata.json")

TREND_WINDOW = 60       # Sessions for linear regression trend analysis
MOMENTUM_WINDOW = 15    # Sessions for recent momentum scoring
PLATEAU_MIN_SESSIONS = 8  # Minimum flat sessions to declare a plateau
BLOCK_GAP_DAYS = 60       # A break longer than this starts a new training block
BLOCK_SHIFT_RATIO = 0.2   # A lasting weight change of 20%+ starts a new block (new machine/gym)

WORKING_SET_RATIO = 0.9   # Sets lighter than 90% of the top set are back-off sets / logging mistakes
REP_BAR_LOOKBACK = 6      # Recent sessions checked for the best reps at the current weight
WEIGHT_TOLERANCE = 0.5    # kg — treat 78.5 and 78.8 as the same weight

CONSOLIDATE_SESSIONS = 2  # A new rep best must be matched this many times...
CONSOLIDATE_DAYS = 14     # ...over at least this many days before adding another rep (unless RPE says go)

MAX_VALID_REPS = 50        # Anything above this is a logging typo (e.g. 1976 reps)
DELOAD_DROP_RATIO = 0.8    # Weight below 80% of the last working weight = deload candidate
DELOAD_RECOVER_RATIO = 0.9 # ...confirmed if it gets back to 90%+ of it
DELOAD_MAX_SESSIONS = 3    # ...within this many sessions (a deload week can have several light sessions)

SPLIT_LOOKBACK_SESSIONS = 3  # An exercise stays in a split's plan until it's skipped this many sessions in a row

# Import exercise config from bot.py
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from bot import EXERCISE_CONFIG

# Map split names (as they appear in the CSV 'title' column) to standard keys
SPLIT_NAMES = {
    "Push": ["Push day"],
    "Pull": ["Pull day"],
    "Push + Quads": ["push + quad"],
    "Pull + Ham": ["Pull + ham"],
}

# One-off swaps in a split's exercise list. The next targets for a split are
# built from its most recent session; when that session is the given date, the
# "from" exercise is replaced by "to" (targets come from the "to" exercise's own
# history). It stops applying by itself once you log that split again.
# Permanent swaps: when you move on from an exercise for good (e.g. the machine
# is maxed out), map the old one to its replacement. Every split that had the
# old exercise gets the new one in the same spot, and the old one never comes
# back to the plan. Its history stays in the CSV and in Stats.
EXERCISE_REPLACEMENTS = {
    "Lying Leg Curl (Machine)": "Lying Leg Curl Single Leg (Machine)",  # maxed at 100kg
    "Calf Press (Machine)": "Calf Press Single Leg (Machine)",          # maxed at 130kg
}

SPLIT_SWAPS = [
    # Oct 10: leg press was done by accident instead of the Smith squat
    {"split": "push + quad", "date": "2026-10-10",
     "from": "Leg Press (Machine)", "to": "Squat (Smith Machine)"},
]

# Reverse mapping: CSV title -> standard key
CSV_TITLE_TO_KEY = {}
for key, titles in SPLIT_NAMES.items():
    for t in titles:
        CSV_TITLE_TO_KEY[t.lower().strip()] = key


# ---------------------------------------------------------------------------
# Data Loading
# ---------------------------------------------------------------------------

def load_csv(exclude_deloads=True):
    """Load and clean the master CSV. Returns a DataFrame sorted by time.
    
    Args:
        exclude_deloads: If True (default), filters out any rows where the
            workout description contains 'deload'. This prevents intentionally
            lighter sessions from corrupting trend analysis.
    """
    df = pd.read_csv(CSV_FILE)
    df = df.dropna(subset=['weight_kg', 'reps'])
    df['weight_kg'] = pd.to_numeric(df['weight_kg'], errors='coerce')
    df['reps'] = pd.to_numeric(df['reps'], errors='coerce')
    if 'rpe' in df.columns:
        df['rpe'] = pd.to_numeric(df['rpe'], errors='coerce')
    else:
        df['rpe'] = np.nan
        
    df = df.dropna(subset=['weight_kg', 'reps'])
    df = df[df['set_type'] == 'normal']
    # Drop impossible values from logging typos
    df = df[(df['reps'] > 0) & (df['reps'] <= MAX_VALID_REPS) & (df['weight_kg'] >= 0)]
    df['start_time'] = pd.to_datetime(df['start_time'])
    # Hevy sometimes changes an exercise's capitalisation (e.g. "reverse curl (cable)"
    # -> "Reverse Curl (Cable)"). Merge the variants under the most recent spelling
    # so the full history is one exercise instead of an old and a new one.
    df['exercise_title'] = df['exercise_title'].str.strip()
    title_key = df['exercise_title'].str.lower()
    latest_spelling = df.sort_values('start_time').groupby(title_key)['exercise_title'].last()
    df['exercise_title'] = title_key.map(latest_spelling)
    df['set_index'] = pd.to_numeric(df['set_index'], errors='coerce').fillna(0)
    df = df.sort_values(['start_time', 'set_index'])
    df['session_date'] = df['start_time'].dt.date
    # Find the latest start_time for each exercise on a given day to avoid stitching sets from different syncs
    idx = df.groupby(['session_date', 'title', 'exercise_title'])['start_time'].transform('max') == df['start_time']
    df = df[idx]
    # Keep the first occurrence of a set_index to prioritize main working sets over later burnout sets in the same workout
    df = df.drop_duplicates(subset=['session_date', 'title', 'exercise_title', 'set_index'], keep='first')
    
    df['set_volume'] = df['weight_kg'] * df['reps']
    
    # Filter out deload sessions so they don't corrupt progression data
    if exclude_deloads:
        deload_mask = df['description'].fillna('').str.lower().str.contains('deload') | df['title'].fillna('').str.lower().str.contains('deload')
        # Also catch deloads that were never tagged in Hevy
        auto_deloads, deload_exercises = _detect_untagged_deloads(df[~deload_mask])
        if auto_deloads:
            workout_keys = pd.Series(list(zip(df['session_date'], df['title'])), index=df.index)
            deload_mask |= workout_keys.isin(auto_deloads)
            print(f"🔇 Auto-detected {len(auto_deloads)} untagged deload workout(s): "
                  f"{', '.join(f'{d} {t}' for d, t in sorted(auto_deloads))}")
        if deload_exercises:
            exercise_keys = pd.Series(list(zip(df['session_date'], df['title'], df['exercise_title'])), index=df.index)
            deload_mask |= exercise_keys.isin(deload_exercises)
            print(f"🔇 Auto-detected {len(deload_exercises)} single-exercise deload session(s)")
        n_deload = deload_mask.sum()
        if n_deload > 0:
            print(f"🔇 Excluded {n_deload} deload rows from analysis")
        df = df[~deload_mask]

    return df


def _detect_untagged_deloads(df):
    """
    Find workouts that look like deloads even though they weren't tagged.

    An exercise "votes deload" when its weight drops below DELOAD_DROP_RATIO of
    the last working weight AND it goes back up to at least DELOAD_RECOVER_RATIO
    of it within DELOAD_MAX_SESSIONS sessions (or history ends while still light).
    The recovery check stops a gym switch or a genuine reset from being mistaken
    for a deload.

    A workout is a deload when at least 2 exercises, and at least half of the
    exercises that have a previous session, vote deload. Outside such workouts,
    a single exercise is still dropped when its dip is confirmed by a recovery
    (e.g. benching 50kg for one day between 90kg days).

    Returns (set of (session_date, title) workout keys,
             set of (session_date, title, exercise_title) exercise keys).
    """
    per_ex = (df.groupby(['session_date', 'title', 'exercise_title'])['weight_kg']
                .max().reset_index().sort_values('session_date'))

    # Every session's weight for each exercise, in order (for the recovery check)
    history = {ex: list(zip(g['session_date'], g['title'], g['weight_kg']))
               for ex, g in per_ex.groupby('exercise_title')}

    last_working = {}  # exercise -> last non-deload weight
    deloads = set()
    deload_exercises = set()

    for (date, title), workout in per_ex.groupby(['session_date', 'title'], sort=False):
        compared = votes = 0
        confirmed = set()  # exercises whose dip was followed by a recovery
        for ex, weight in zip(workout['exercise_title'], workout['weight_kg']):
            ref = last_working.get(ex)
            if not ref or ref <= 0:
                continue
            compared += 1
            if weight >= DELOAD_DROP_RATIO * ref:
                continue
            ex_hist = history[ex]
            pos = next(i for i, (d, t, _) in enumerate(ex_hist) if d == date and t == title)
            recovered = _recovers_after_dip([w for _, _, w in ex_hist[pos + 1:]], ref)
            if recovered is not False:
                votes += 1
                if recovered:
                    confirmed.add(ex)

        if votes >= 2 and votes >= compared / 2:
            deloads.add((date, title))
        else:
            for ex, weight in zip(workout['exercise_title'], workout['weight_kg']):
                if ex in confirmed:
                    deload_exercises.add((date, title, ex))
                else:
                    last_working[ex] = weight

    return deloads, deload_exercises


def _recovers_after_dip(later_weights, ref):
    """
    Did the weight come back after a dip below DELOAD_DROP_RATIO of `ref`?

    Looks up to DELOAD_MAX_SESSIONS sessions ahead so a deload week with several
    light sessions is still recognised.

    Returns True (back to DELOAD_RECOVER_RATIO of ref), False (settled at a new
    lower level, or stayed low too long — not a deload), or None (history ends
    while still light: possibly an ongoing deload).
    """
    window = later_weights[:DELOAD_MAX_SESSIONS]
    for w in window:
        if w >= DELOAD_RECOVER_RATIO * ref:
            return True
        if w >= DELOAD_DROP_RATIO * ref:
            return False
    return None if len(window) < DELOAD_MAX_SESSIONS else False


def _find_csv_name(exercise_name, csv_exercises):
    """Case-insensitive match between config name and CSV exercise titles."""
    norm = exercise_name.lower().strip()
    for csv_name in csv_exercises:
        if csv_name.lower().strip() == norm:
            return csv_name
    for csv_name in csv_exercises:
        if norm in csv_name.lower() or csv_name.lower() in norm:
            return csv_name
    return None


def get_exercise_sessions(df, exercise_name, max_sessions=30):
    """
    Aggregate set-level rows into session-level features for one exercise.
    
    Groups by (session_date, workout_title) so that exercises done in
    different splits on the same day remain separate sessions.
    
    Returns a DataFrame with one row per session:
      date, max_weight, avg_reps, total_reps, total_volume, num_sets, reps_list
    
    Limited to the most recent `max_sessions` sessions.
    """
    csv_exercises = df['exercise_title'].unique()
    csv_name = _find_csv_name(exercise_name, csv_exercises)
    
    if csv_name is None:
        return pd.DataFrame()
    
    ex_data = df[df['exercise_title'] == csv_name].copy()
    
    if ex_data.empty:
        return pd.DataFrame()
    
    ex_data['session_date'] = ex_data['start_time'].dt.date
    
    # Group by BOTH date and workout title to avoid merging exercises
    # done in different splits on the same day
    sessions = ex_data.groupby(['session_date', 'title']).agg(
        max_weight=('weight_kg', 'max'),
        avg_reps=('reps', 'mean'),
        total_reps=('reps', 'sum'),
        total_volume=('set_volume', 'sum'),
        num_sets=('reps', 'count'),
        reps_list=('reps', lambda x: list(x.astype(int))),
        weights_list=('weight_kg', lambda x: list(x)),
        max_rpe=('rpe', 'max') if 'rpe' in ex_data.columns else ('weight_kg', lambda x: np.nan),
        exercise_notes=('exercise_notes', 'last') if 'exercise_notes' in ex_data.columns else ('weight_kg', lambda x: ""),
    ).reset_index().sort_values('session_date')
    
    sessions['session_number'] = range(1, len(sessions) + 1)
    
    # Return the last max_sessions
    return sessions.tail(max_sessions).reset_index(drop=True)


# ---------------------------------------------------------------------------
# Trend Analysis
# ---------------------------------------------------------------------------

def current_block(sessions_df):
    """
    Return only the sessions since the last big break or lasting weight shift.

    A new block starts after a gap of more than BLOCK_GAP_DAYS, or when the
    weight changes by more than BLOCK_SHIFT_RATIO from the previous session
    (deloads are already filtered out, so a jump this big means a new machine,
    gym, or a fresh start). Comparing against an older block would make a
    different machine's numbers look like progress or regression.
    """
    if len(sessions_df) < 2:
        return sessions_df

    dates = pd.to_datetime(sessions_df['session_date']).values
    weights = sessions_df['max_weight'].values
    start = 0
    for i in range(1, len(sessions_df)):
        gap_days = (dates[i] - dates[i - 1]) / np.timedelta64(1, 'D')
        prev = weights[i - 1]
        shifted = prev > 0 and abs(weights[i] - prev) / prev > BLOCK_SHIFT_RATIO
        if gap_days > BLOCK_GAP_DAYS or shifted:
            start = i
    return sessions_df.iloc[start:].reset_index(drop=True)


def analyze_trend(sessions_df):
    """
    Analyze trends from session-level data, using only the current block of
    training (see current_block) so old machines/gyms don't distort it.

    Returns a TrendReport dict:
      - weight_trend_slope: kg per session (from linear regression)
      - weight_trend_label: "Rising" / "Stable" / "Declining"
      - volume_trend_slope: volume units per session
      - volume_trend_label: "Rising" / "Stable" / "Declining"
      - plateau_detected: bool
      - plateau_sessions: how many sessions the plateau has lasted
      - momentum_score: recent performance vs older performance (>1 = improving)
      - momentum_label: "Strong" / "Steady" / "Fading"
      - best_weight_ever: max weight across ALL given sessions (not just the block)
      - recent_best_weight: best weight in last MOMENTUM_WINDOW sessions
      - sessions_analyzed: sessions in the current block
      - weight_change_total: total kg change over the trend window
    """
    best_weight_ever = float(sessions_df['max_weight'].max()) if len(sessions_df) > 0 else 0
    sessions_df = current_block(sessions_df)
    n = len(sessions_df)

    if n < 3:
        return {
            "weight_trend_slope": 0, "weight_trend_label": "Insufficient data",
            "volume_trend_slope": 0, "volume_trend_label": "Insufficient data",
            "plateau_detected": False, "plateau_sessions": 0,
            "momentum_score": 1.0, "momentum_label": "N/A",
            "best_weight_ever": best_weight_ever,
            "recent_best_weight": sessions_df['max_weight'].max() if n > 0 else 0,
            "sessions_analyzed": n,
            "weight_change_total": 0,
        }
    
    # --- Weight trend (linear regression over trend window) ---
    trend_data = sessions_df.tail(TREND_WINDOW)
    x = np.arange(len(trend_data))
    
    weight_slope, weight_intercept, weight_r, _, _ = stats.linregress(x, trend_data['max_weight'].values)
    volume_slope, _, volume_r, _, _ = stats.linregress(x, trend_data['total_volume'].values)
    
    # Label the trend based on slope significance
    # A slope of ~0.05 kg/session = roughly 1kg over 20 sessions = meaningful
    if weight_slope > 0.03:
        weight_label = "Rising"
    elif weight_slope < -0.03:
        weight_label = "Declining"
    else:
        weight_label = "Stable"
    
    if volume_slope > 1.0:
        volume_label = "Rising"
    elif volume_slope < -1.0:
        volume_label = "Declining"
    else:
        volume_label = "Stable"
    
    weight_change = weight_slope * len(trend_data)
    
    # --- Plateau detection ---
    # A plateau = weight has been essentially flat for PLATEAU_MIN_SESSIONS+ sessions
    # AND volume is not clearly rising (you're not progressing via reps either)
    recent_weights = sessions_df['max_weight'].tail(PLATEAU_MIN_SESSIONS).values
    plateau_detected = False
    plateau_sessions = 0
    
    if n >= PLATEAU_MIN_SESSIONS:
        weight_std = np.std(recent_weights)
        # If the standard deviation of recent weights is < 1 step size, it's flat
        # Use a generous threshold: std < 2.5kg for compounds, any flat for isolations
        if weight_std < 2.5:
            # Also check that volume isn't clearly rising (reps aren't increasing)
            recent_vols = sessions_df['total_volume'].tail(PLATEAU_MIN_SESSIONS).values
            vol_x = np.arange(len(recent_vols))
            vol_slope, _, _, _, _ = stats.linregress(vol_x, recent_vols)
            
            if vol_slope < 2.0:  # Volume not meaningfully increasing
                # Count how many sessions back the plateau extends
                all_weights = sessions_df['max_weight'].values
                mode_weight = np.median(recent_weights)
                for i in range(n - 1, -1, -1):
                    if abs(all_weights[i] - mode_weight) < 3.0:
                        plateau_sessions += 1
                    else:
                        break
                # Only a plateau if the weight has actually been flat long enough
                # (a fresh weight increase at the end breaks it)
                plateau_detected = plateau_sessions >= PLATEAU_MIN_SESSIONS
                if not plateau_detected:
                    plateau_sessions = 0
    
    # --- Momentum scoring ---
    # Compare average volume of last MOMENTUM_WINDOW sessions vs the MOMENTUM_WINDOW before that
    if n >= MOMENTUM_WINDOW * 2:
        recent_vol = sessions_df['total_volume'].tail(MOMENTUM_WINDOW).mean()
        older_vol = sessions_df['total_volume'].iloc[-(MOMENTUM_WINDOW * 2):-MOMENTUM_WINDOW].mean()
        momentum = recent_vol / older_vol if older_vol > 0 else 1.0
    elif n >= MOMENTUM_WINDOW:
        half = n // 2
        recent_vol = sessions_df['total_volume'].tail(half).mean()
        older_vol = sessions_df['total_volume'].head(half).mean()
        momentum = recent_vol / older_vol if older_vol > 0 else 1.0
    else:
        momentum = 1.0
    
    if momentum > 1.05:
        momentum_label = "Strong"
    elif momentum > 0.95:
        momentum_label = "Steady"
    else:
        momentum_label = "Fading"
    
    return {
        "weight_trend_slope": round(weight_slope, 4),
        "weight_trend_label": weight_label,
        "volume_trend_slope": round(volume_slope, 2),
        "volume_trend_label": volume_label,
        "plateau_detected": plateau_detected,
        "plateau_sessions": plateau_sessions,
        "momentum_score": round(momentum, 3),
        "momentum_label": momentum_label,
        "best_weight_ever": best_weight_ever,
        "recent_best_weight": float(sessions_df['max_weight'].tail(MOMENTUM_WINDOW).max()),
        "sessions_analyzed": n,
        "weight_change_total": round(weight_change, 1),
    }


# ---------------------------------------------------------------------------
# ML Prediction (blended)
# ---------------------------------------------------------------------------

def _safe_filename(name):
    return re.sub(r'[^a-z0-9]+', '_', name.lower()).strip('_') + ".pkl"


ML_FEATURE_COLS = ['avg_reps', 'max_weight', 'total_volume', 'num_sets']


def get_ml_prediction(exercise_name, session_row):
    """
    Load a trained RandomForest model and predict NEXT session weight.

    Only returns a prediction when training showed the model beats the
    "same weight as last time" baseline for this exercise — otherwise the
    number is noise and is hidden.

    Returns (predicted_weight, mae_kg) or (None, None).
    """
    model_path = os.path.join(MODELS_DIR, _safe_filename(exercise_name))

    if not os.path.exists(model_path) or not os.path.exists(MODEL_METADATA_FILE):
        return None, None

    try:
        with open(MODEL_METADATA_FILE, "r", encoding="utf-8") as f:
            meta = json.load(f).get(exercise_name)
        if not meta or not meta.get("beats_baseline"):
            return None, None

        model = joblib.load(model_path)
        feature_cols = meta.get("feature_cols", ML_FEATURE_COLS)
        X = pd.DataFrame([[session_row[c] for c in feature_cols]], columns=feature_cols)
        # The model predicts 'weight_change' (a delta)
        predicted_weight = session_row['max_weight'] + model.predict(X)[0]
        return round(predicted_weight, 1), meta.get("mae_kg")
    except Exception as e:
        print(f"ML prediction error for {exercise_name}: {e}")
        return None, None


# ---------------------------------------------------------------------------
# Target Computation (Double Progression + Trends + ML)
# ---------------------------------------------------------------------------

def _rpe_from_notes(notes):
    """Read an RPE written in the exercise notes, e.g. "RPE 9" or "rpe: 8.5". Returns None if absent."""
    match = re.search(r'rpe\s*[:=]?\s*(\d+(?:\.\d+)?)', str(notes or ''), re.IGNORECASE)
    if match:
        value = float(match.group(1))
        if 1 <= value <= 10:
            return value
    return None


def _rep_goal(rpe, consolidated, rep_bar, times_hit, days_at_bar, cap=None):
    """
    Decide the rep goal from the rep bar, based on how hard the last session
    felt (RPE) and how long the current best has been held.

      RPE 9.5-10  -> hold (tank was empty)
      RPE 8.5-9   -> wait until the best is consolidated (same as no RPE logged)
      RPE 7.5-8   -> +1 now (2 reps in reserve)
      RPE <= 7    -> +2 now (3+ reps in reserve)

    `cap` limits the goal (the rep ceiling), or None for no limit.
    Returns (goal_reps, explanation).
    """
    def goal(add):
        return rep_bar + add if cap is None else min(rep_bar + add, cap)

    if rpe is not None and rpe >= 9.5:
        return goal(0), f"RPE {rpe:g}: tank was empty. Hold {goal(0)} reps until it feels easier."
    if rpe is not None and rpe <= 7:
        return goal(2), f"RPE {rpe:g}: plenty in the tank. Push for {goal(2)} on every set."
    if rpe is not None and rpe <= 8:
        return goal(1), f"RPE {rpe:g}: reps in the tank. Aim for {goal(1)} on every set."

    rpe_str = f"RPE {rpe:g}. " if rpe is not None else ""
    if consolidated:
        return goal(1), f"{rpe_str}{rep_bar} reps owned ({times_hit}x over {days_at_bar} days). Aim for {goal(1)} on every set."
    sessions_left = max(CONSOLIDATE_SESSIONS - times_hit, 0)
    days_left = max(CONSOLIDATE_DAYS - days_at_bar, 0)
    wait = []
    if sessions_left:
        wait.append(f"{sessions_left} more session{'s' if sessions_left > 1 else ''}")
    if days_left:
        wait.append(f"~{days_left} more days")
    return goal(0), f"{rpe_str}Consolidating {goal(0)} reps on every set. +1 rep after {' and '.join(wait)}."


def compute_next_target(exercise_name, sessions_df, config, trend_report, split_last_session=None):
    """
    Compute the next session's target for one exercise.
    
    Uses double-progression logic informed by trend data. ML is attached as an
    optional hint and never changes the target weight.
    
    Args:
        split_last_session: Optional dict with 'reps_list', 'max_weight', 'num_sets'
            from the last session of the specific split. If provided, used for
            target sets/reps structure. Otherwise falls back to last overall session.
    
    Returns a dict:
      exercise, next_weight, target_sets, target_reps, rationale,
      trend_summary, ml_prediction, blended_weight
    """
    ceiling = config["ceiling"]
    step = config["step"]
    # strict_ceiling: reps never go above the ceiling, and hitting it on every
    # set always adds weight, even at RPE 9.5-10
    strict = config.get("strict_ceiling", False)
    
    if sessions_df.empty:
        return None
    
    # Use split-specific session for target structure, overall last for trends
    last = sessions_df.iloc[-1]
    if split_last_session is not None:
        current_weight = split_last_session['max_weight']
        reps_list = split_last_session['reps_list']
        weights_list = split_last_session['weights_list']
        num_sets = split_last_session['num_sets']
    else:
        current_weight = last['max_weight']
        reps_list = last['reps_list']
        weights_list = last['weights_list']
        num_sets = last['num_sets']
    
    # --- RPE of the session the target is built from ---
    # Hevy's per-set RPE field first; "RPE 9" written in the exercise notes as a fallback.
    if split_last_session is not None:
        rpe_values = [r for r in split_last_session.get('rpe_list', []) if not pd.isna(r)]
        last_notes = split_last_session.get('notes', '')
    else:
        rpe_values = [] if pd.isna(last.get('max_rpe')) else [last['max_rpe']]
        last_notes = last.get('exercise_notes', '')
    max_rpe = max(rpe_values) if rpe_values else _rpe_from_notes(last_notes)
    rpe_logged = max_rpe is not None
    if not rpe_logged:
        max_rpe = 0

    # "max" in the exercise notes means the machine has no more weight to add.
    # It stays in effect for as long as you're at that weight — you only need to
    # write it once, and it clears itself if you ever use a heavier weight.
    notes_at_weight = sessions_df.loc[sessions_df['max_weight'] >= current_weight, 'exercise_notes']
    is_maxed = notes_at_weight.fillna('').astype(str).str.lower().str.contains('max').any()
    # A hardcoded machine limit in EXERCISE_CONFIG does the same thing
    max_weight_limit = config.get("max_weight")
    if max_weight_limit is not None and current_weight >= max_weight_limit:
        is_maxed = True

    # --- Working sets ---
    # Only sets close to the top weight count. Much lighter sets are back-off sets
    # or Hevy pre-fill mistakes (e.g. a 40kg set left over from a deload week).
    working_reps = [r for w, r in zip(weights_list, reps_list) if w >= WORKING_SET_RATIO * current_weight]

    # --- Rep bar: best reps recently achieved at this weight or heavier ---
    # One bad day shouldn't lower the goal: if you hit 8 last week at this
    # weight, the goal stays 8 even if today you only got 5.
    recent = current_block(sessions_df).tail(REP_BAR_LOOKBACK)
    session_bests = []  # (date, best reps at this weight) for each recent session
    for date, w_list, r_list in zip(recent['session_date'], recent['weights_list'], recent['reps_list']):
        at_weight = [r for w, r in zip(w_list, r_list) if w >= current_weight - WEIGHT_TOLERANCE]
        if at_weight:
            session_bests.append((date, max(at_weight)))
    rep_bar = max([max(working_reps)] + [best for _, best in session_bests])

    # --- Consolidation: has the rep bar been "owned" for a while? ---
    # A new rep best has to be matched in CONSOLIDATE_SESSIONS sessions, starting
    # at least CONSOLIDATE_DAYS ago, before another rep is added.
    bar_dates = [d for d, best in session_bests if best >= rep_bar]
    days_at_bar = (datetime.date.today() - min(bar_dates)).days if bar_dates else 0
    consolidated = len(bar_dates) >= CONSOLIDATE_SESSIONS and days_at_bar >= CONSOLIDATE_DAYS

    rpe_for_reps = max_rpe if rpe_logged else None
    rep_args = (rpe_for_reps, consolidated, rep_bar, len(bar_dates), days_at_bar)

    # --- Double progression logic ---
    # The target is one rep goal for EVERY set (what to aim for), built from your
    # best recent set — not a copy of last session's fatigue pattern.
    # Weight only goes up when the LAST session hit the ceiling on every working set.
    all_hit_ceiling = all(r >= ceiling for r in working_reps)

    if is_maxed:
        math_weight = current_weight
        # no ceiling: reps are the only way up (unless the ceiling is strict)
        goal_reps, rep_reason = _rep_goal(*rep_args, cap=ceiling if strict else None)
        rationale = f"🛑 Max weight reached. {rep_reason}"
    elif all_hit_ceiling:
        if max_rpe >= 9.5 and not strict:
            math_weight = current_weight
            goal_reps = ceiling
            rationale = f"✅ Ceiling cleared but RPE {max_rpe:g}. Consolidating before adding weight."
        else:
            math_weight = current_weight + step
            if max_weight_limit is not None:
                math_weight = min(math_weight, max_weight_limit)
            goal_reps = 5 if ceiling == 8 else 10
            rationale = "✅ Ceiling cleared. Load increased."
    else:
        math_weight = current_weight
        goal_reps, rep_reason = _rep_goal(*rep_args, cap=ceiling)
        rationale = f"🔄 {rep_reason}"
    target_reps = str(goal_reps)

    # --- Regression detection from trend ---
    if trend_report["momentum_label"] == "Fading" and trend_report["weight_trend_label"] == "Declining":
        # Weight is trending down AND momentum is fading - flag it
        rationale += " | ⚠️ Regression detected"

    # --- Plateau detection from trend ---
    # Not useful when maxed out: a flat weight is expected there
    if is_maxed:
        pass
    elif trend_report["plateau_detected"]:
        rationale += f" | 🚨 Plateau ({trend_report['plateau_sessions']} sessions)"
    elif trend_report["momentum_label"] == "Strong":
        rationale += " | 📈 Upward momentum"
    
    # --- ML hint (informational only, never changes the target) ---
    ml_weight, ml_mae = get_ml_prediction(exercise_name, last)

    ml_str = ""
    if ml_weight is not None:
        ml_str = f"🤖 ML hint: {ml_weight:g}kg (±{ml_mae:g}kg)"

    # The target weight comes purely from double progression, stepping up from
    # the weight actually used so it lands on a real plate/stack increment
    final_weight = round(math_weight, 2)

    # --- Format Individual Sets ---
    # Every set uses the same working weight and rep goal
    target_sets_data = [f"  Set {i+1}: {final_weight:g}kg x {goal_reps}" for i in range(int(num_sets))]

    formatted_targets = "\n".join(target_sets_data)
    
    # --- Trend summary line ---
    n_sessions = trend_report['sessions_analyzed']
    weight_change = trend_report['weight_change_total']
    trend_emoji = "↗️" if weight_change > 0 else "↘️" if weight_change < 0 else "→"
    trend_str = f"📊 {trend_emoji} {'+' if weight_change > 0 else ''}{weight_change}kg over {n_sessions} sessions"
    trend_str += f" | Momentum: {trend_report['momentum_label']}"
    
    return {
        "exercise": exercise_name,
        "next_weight": final_weight,
        "math_weight": math_weight,
        "ml_weight": ml_weight,
        "target_sets": int(num_sets),
        "target_reps": target_reps,
        "formatted_targets": formatted_targets,
        "rationale": rationale,
        "trend_summary": trend_str,
        "ml_info": ml_str,
        "best_ever": trend_report['best_weight_ever'],
    }


# ---------------------------------------------------------------------------
# Full Blueprint Generation
# ---------------------------------------------------------------------------

def _session_exercises(session_rows, split_name, session_time):
    """Exercises of one logged session, in order, matched to EXERCISE_CONFIG keys."""
    from bot import match_exercise_config

    exercises = []
    for raw_name in session_rows['exercise_title']:
        matched = match_exercise_config(raw_name.strip(), fuzzy=False)
        for swap in SPLIT_SWAPS:
            if (swap["split"].lower() == split_name.lower().strip()
                    and str(session_time.date()) == swap["date"] and matched == swap["from"]):
                matched = swap["to"]
        matched = EXERCISE_REPLACEMENTS.get(matched, matched)
        if matched and matched not in exercises:
            exercises.append(matched)
    return exercises


def get_split_exercises(df, split_name):
    """
    Find the exercises that belong to a split, in the order you do them.
    Returns a list of exercise names (matched to EXERCISE_CONFIG keys).

    The list is built from the last SPLIT_LOOKBACK_SESSIONS sessions of the
    split, not just the latest one, so skipping an exercise once (injury, no
    time, machine taken) doesn't drop it from next week's plan. The latest
    session sets the order; an exercise missing from it is put back where it
    was in the older session. An exercise only leaves the plan after it's been
    skipped SPLIT_LOOKBACK_SESSIONS times in a row.
    """
    split_df = df[df['title'].str.strip().str.lower() == split_name.lower().strip()]
    if split_df.empty:
        return []

    session_times = sorted(split_df['start_time'].unique(), reverse=True)[:SPLIT_LOOKBACK_SESSIONS]
    sessions = [
        _session_exercises(split_df[split_df['start_time'] == t], split_name, pd.Timestamp(t))
        for t in session_times
    ]

    def movement(name):
        # "Lateral Raise (Cable)" and "Lateral Raise (Dumbbell)" are the same movement:
        # switching between them is a swap, not a skip, so only the newest one is kept
        return name.split('(')[0].strip().lower()

    exercises = list(sessions[0])
    for older in sessions[1:]:
        for i, name in enumerate(older):
            if name in exercises or movement(name) in {movement(e) for e in exercises}:
                continue
            # Put it right after whatever came before it in that older session
            before = next((older[j] for j in range(i - 1, -1, -1) if older[j] in exercises), None)
            pos = exercises.index(before) + 1 if before else 0
            exercises.insert(pos, name)

    return exercises


def generate_full_blueprint(split_name):
    """
    Generate a complete workout blueprint for a given split (e.g., "Pull day").
    
    Reads ALL history from CSV, runs trend analysis + double progression + ML
    for every exercise in that split.
    
    Returns: (message_text, exercise_targets_list) or (None, None) if no data.
    """
    if not os.path.exists(CSV_FILE):
        return None, None
    
    df = load_csv()
    
    # Get exercises for this split
    exercises = get_split_exercises(df, split_name)
    
    if not exercises:
        return None, None
    
    targets = []
    increases = []
    
    # Get split-specific data for target structure (sets/reps from last session of THIS split)
    split_df = df[df['title'].str.strip().str.lower() == split_name.lower().strip()]
    
    for ex_name in exercises:
        config = EXERCISE_CONFIG.get(ex_name)
        if not config:
            continue
        
        # Get session history (up to 30 sessions for this exercise across ALL splits)
        sessions = get_exercise_sessions(df, ex_name, max_sessions=30)
        
        if sessions.empty:
            continue
        
        # Get the last session of this exercise in THIS SPECIFIC SPLIT
        csv_name = _find_csv_name(ex_name, split_df['exercise_title'].unique())
        split_last = None
        if csv_name:
            ex_split = split_df[split_df['exercise_title'] == csv_name].copy()
            ex_split = ex_split[ex_split['set_type'] == 'normal']
            if not ex_split.empty:
                latest_date = ex_split['start_time'].max()
                last_sets = ex_split[ex_split['start_time'] == latest_date]
                split_last = {
                    'max_weight': float(last_sets['weight_kg'].max()),
                    'reps_list': list(last_sets['reps'].dropna().astype(int)),
                    'weights_list': list(last_sets['weight_kg'].dropna().astype(float)),
                    'num_sets': len(last_sets),
                    'rpe_list': list(last_sets['rpe']),
                    'notes': last_sets['exercise_notes'].dropna().astype(str).iloc[-1] if last_sets['exercise_notes'].notna().any() else '',
                }
        
        # Run trend analysis (uses ALL sessions across splits)
        trend = analyze_trend(sessions)
        
        # Compute target (uses split-specific last session for sets/reps structure)
        target = compute_next_target(ex_name, sessions, config, trend, split_last_session=split_last)
        
        if target:
            targets.append(target)
            if "Load increased" in target['rationale']:
                increases.append(target)
    
    if not targets:
        return None, None
    
    # --- Format the message ---
    total_sessions = max(t.get('best_ever', 0) for t in targets)  # Just for the header
    msg = f"🚨 *Next '{split_name}' Targets (0 RIR)* 🚨\n"
    msg += f"_(Computed from full CSV history)_\n\n"
    
    if increases:
        msg += "📈 *WEIGHT INCREASES:*\n"
        for inc in increases:
            msg += f"• {inc['exercise']} ➡️ *{inc['next_weight']:g}kg*\n"
        msg += "\n━━━━━━━━━━━━━━━━━━\n\n"
    
    msg += "📋 *DETAILED TARGETS:*\n\n"
    
    for t in targets:
        msg += f"*{t['exercise']}*\n"
        msg += f"🎯 Targets:\n{t['formatted_targets']}\n"
        msg += f"💡 {t['rationale']}\n"
        msg += f"{t['trend_summary']}"
        if t['ml_info']:
            msg += f" | {t['ml_info']}"
        msg += "\n\n"
    
    return msg, targets

def get_exercise_targets(ex_name):
    """Generates targets for a specific exercise independently (e.g. for /substitute)."""
    from bot import EXERCISE_CONFIG
    df = load_csv()
    
    config = EXERCISE_CONFIG.get(ex_name)
    if not config:
        return f"⚠️ '{ex_name}' is not in the active exercise pool."
        
    sessions = get_exercise_sessions(df, ex_name, max_sessions=30)
    if sessions.empty:
        return f"⚠️ No history found for '{ex_name}' in your CSV."
        
    trend = analyze_trend(sessions)
    # Don't pass split_last, it will default to the absolute last session for this exercise
    target = compute_next_target(ex_name, sessions, config, trend)
    
    if not target:
        return f"⚠️ Could not compute targets for '{ex_name}'."
        
    msg = f"🎯 Targets:\n{target['formatted_targets']}\n"
    msg += f"💡 {target['rationale']}\n"
    msg += f"{target['trend_summary']}"
    if target['ml_info']:
        msg += f" | {target['ml_info']}"
        
    return msg


def generate_all_blueprints():
    """
    Generate blueprints for all 4 splits and save to upcoming_targets.json.
    
    This is called by morning_delivery.py so that every split always has
    fresh, trend-based targets.
    """
    results = {}
    
    for standard_key, csv_titles in SPLIT_NAMES.items():
        for csv_title in csv_titles:
            msg, _ = generate_full_blueprint(csv_title)
            if msg:
                results[standard_key] = msg
                break  # Found data for this split, move on
    
    # Save to upcoming_targets.json (as cache, but the CSV is still truth)
    target_file = "upcoming_targets.json"
    with open(target_file, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=4, ensure_ascii=False)
    
    return results


# ---------------------------------------------------------------------------
# CLI Testing
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding='utf-8')
    
    print("=" * 60)
    print("  ZERO RIR BOT — CSV TREND ENGINE TEST")
    print("=" * 60)
    
    for standard_key, csv_titles in SPLIT_NAMES.items():
        for csv_title in csv_titles:
            print(f"\n{'─' * 60}")
            print(f"  Generating blueprint for: {csv_title} ({standard_key})")
            print(f"{'─' * 60}")
            
            msg, targets = generate_full_blueprint(csv_title)
            
            if msg:
                print(msg)
            else:
                print(f"  ⚠️ No data found for '{csv_title}'")
    
    print("\n" + "=" * 60)
    print("  TEST COMPLETE")
    print("=" * 60)
