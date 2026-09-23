"""
CSV Trend Engine — The brain of Zero RIR Bot.

Reads ALL workout history from workouts.csv and computes intelligent targets
based on long-term trends (50-100 sessions), recent momentum (10-20 sessions),
and ML predictions (blended with math-based double progression).

This module replaces the old exercise_history.json approach.
The CSV is the single source of truth.
"""

import os
import re
import json
import math
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
    df['start_time'] = pd.to_datetime(df['start_time'])
    df = df.sort_values('start_time')
    df['set_volume'] = df['weight_kg'] * df['reps']
    
    # Filter out deload sessions so they don't corrupt progression data
    if exclude_deloads:
        deload_mask = df['description'].fillna('').str.lower().str.contains('deload') | df['title'].fillna('').str.lower().str.contains('deload')
        n_deload = deload_mask.sum()
        if n_deload > 0:
            print(f"🔇 Excluded {n_deload} deload rows from analysis")
        df = df[~deload_mask]
    
    return df


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


def get_exercise_sessions(df, exercise_name, max_sessions=100):
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

def analyze_trend(sessions_df):
    """
    Analyze long-term trends from session-level data.
    
    Returns a TrendReport dict:
      - weight_trend_slope: kg per session (from linear regression)
      - weight_trend_label: "Rising" / "Stable" / "Declining"
      - volume_trend_slope: volume units per session
      - volume_trend_label: "Rising" / "Stable" / "Declining"
      - plateau_detected: bool
      - plateau_sessions: how many sessions the plateau has lasted
      - momentum_score: recent performance vs older performance (>1 = improving)
      - momentum_label: "Strong" / "Steady" / "Fading"
      - best_weight_ever: all-time max weight
      - recent_best_weight: best weight in last MOMENTUM_WINDOW sessions
      - sessions_analyzed: total sessions used
      - weight_change_total: total kg change over the trend window
    """
    n = len(sessions_df)
    
    if n < 3:
        return {
            "weight_trend_slope": 0, "weight_trend_label": "Insufficient data",
            "volume_trend_slope": 0, "volume_trend_label": "Insufficient data",
            "plateau_detected": False, "plateau_sessions": 0,
            "momentum_score": 1.0, "momentum_label": "N/A",
            "best_weight_ever": sessions_df['max_weight'].max() if n > 0 else 0,
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
                plateau_detected = True
                # Count how many sessions back the plateau extends
                all_weights = sessions_df['max_weight'].values
                mode_weight = np.median(recent_weights)
                for i in range(n - 1, -1, -1):
                    if abs(all_weights[i] - mode_weight) < 3.0:
                        plateau_sessions += 1
                    else:
                        break
    
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
        "best_weight_ever": float(sessions_df['max_weight'].max()),
        "recent_best_weight": float(sessions_df['max_weight'].tail(MOMENTUM_WINDOW).max()),
        "sessions_analyzed": n,
        "weight_change_total": round(weight_change, 1),
    }


# ---------------------------------------------------------------------------
# ML Prediction (blended)
# ---------------------------------------------------------------------------

def _safe_filename(name):
    return re.sub(r'[^a-z0-9]+', '_', name.lower()).strip('_') + ".pkl"


def get_ml_prediction(exercise_name, avg_reps, max_weight, total_volume, num_sets, session_number):
    """
    Load a trained RandomForest model and predict NEXT session weight.
    Returns (predicted_weight, rmse) or (None, None).
    """
    model_path = os.path.join(MODELS_DIR, _safe_filename(exercise_name))
    
    if not os.path.exists(model_path):
        return None, None
    
    try:
        rmse = None
        if os.path.exists(MODEL_METADATA_FILE):
            with open(MODEL_METADATA_FILE, "r", encoding="utf-8") as f:
                meta = json.load(f)
            if exercise_name in meta:
                rmse = meta[exercise_name].get("rmse_kg")
        
        model = joblib.load(model_path)
        X = pd.DataFrame(
            [[avg_reps, max_weight, total_volume, num_sets, session_number]],
            columns=['avg_reps', 'max_weight', 'total_volume', 'num_sets', 'session_number']
        )
        prediction = model.predict(X)[0]
        return round(prediction, 1), rmse
    except Exception as e:
        print(f"ML prediction error for {exercise_name}: {e}")
        return None, None


def blend_targets(math_weight, ml_weight, ml_rmse, step):
    """
    Blend the math-based target weight with the ML prediction.
    
    Strategy:
      - If ML RMSE is low (< 3kg), trust ML more (60% ML, 40% math)
      - If ML RMSE is moderate (3-10kg), balanced (40% ML, 60% math)
      - If ML RMSE is high (>10kg), trust math more (20% ML, 80% math)
      - Round to nearest step increment
    """
    if ml_weight is None:
        return math_weight
    
    if ml_rmse is not None and ml_rmse < 3.0:
        ml_w = 0.6
    elif ml_rmse is not None and ml_rmse < 10.0:
        ml_w = 0.4
    else:
        ml_w = 0.2
    
    math_w = 1.0 - ml_w
    blended = (math_weight * math_w) + (ml_weight * ml_w)
    
    # Round to nearest step
    blended = round(blended / step) * step
    
    return round(blended, 1)


# ---------------------------------------------------------------------------
# Target Computation (Double Progression + Trends + ML)
# ---------------------------------------------------------------------------

def compute_next_target(exercise_name, sessions_df, config, trend_report, split_last_session=None):
    """
    Compute the next session's target for one exercise.
    
    Uses double-progression logic informed by trend data and blended with ML.
    
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
    
    if sessions_df.empty:
        return None
    
    # Use split-specific session for target structure, overall last for trends
    last = sessions_df.iloc[-1]
    if split_last_session is not None:
        current_weight = split_last_session['max_weight']
        reps_list = split_last_session['reps_list']
        num_sets = split_last_session['num_sets']
    else:
        current_weight = last['max_weight']
        reps_list = last['reps_list']
        num_sets = last['num_sets']
    
    # Extract RPE and Notes
    max_rpe = last.get('max_rpe', 0)
    if pd.isna(max_rpe): max_rpe = 0
    
    notes = str(last.get('exercise_notes', '')).lower()
    is_maxed = 'max' in notes
    
    # --- Double progression logic ---
    all_hit_ceiling = all(r >= ceiling for r in reps_list)
    
    if is_maxed:
        math_weight = current_weight
        if all_hit_ceiling:
            target_reps = ", ".join(str(r + 1) for r in reps_list)
            rationale = "🛑 Max weight reached. Slowly adding reps beyond ceiling."
        else:
            if max_rpe >= 9.5:
                target_reps = ", ".join(str(r) for r in reps_list)
                rationale = f"🛑 Max weight reached. RPE high ({max_rpe}), maintaining."
            else:
                target_reps = ", ".join(str(r + 1) for r in reps_list)
                rationale = "🛑 Max weight reached. Slowly adding reps."
    else:
        if all_hit_ceiling:
            if max_rpe >= 9.5:
                math_weight = current_weight
                target_reps = ", ".join(str(ceiling) for _ in reps_list)
                rationale = f"✅ Ceiling cleared but RPE {max_rpe}. Consolidating."
            else:
                bottom_of_range = 5 if ceiling == 8 else 10
                math_weight = current_weight + step
                target_reps = str(bottom_of_range)
                rationale = "✅ Ceiling cleared. Load increased."
        else:
            math_weight = current_weight
            if max_rpe >= 9.5:
                target_reps = ", ".join(str(r) for r in reps_list)
                rationale = f"🔄 Ceiling not met, RPE {max_rpe}. Maintaining reps."
            else:
                target_reps = ", ".join(str(min(r + 1, ceiling)) for r in reps_list)
                rationale = "🔄 Ceiling not met. Add 1 rep."
    
    # --- Regression detection from trend ---
    if trend_report["momentum_label"] == "Fading" and trend_report["weight_trend_label"] == "Declining":
        # Weight is trending down AND momentum is fading - flag it
        rationale += " | ⚠️ Regression detected"
    
    # --- Plateau detection from trend ---
    if trend_report["plateau_detected"]:
        rationale += f" | 🚨 Plateau ({trend_report['plateau_sessions']} sessions)"
    elif trend_report["momentum_label"] == "Strong":
        rationale += " | 📈 Upward momentum"
    
    # --- ML blending ---
    avg_reps = last['avg_reps']
    total_volume = last['total_volume']
    session_number = last['session_number']
    
    ml_weight, ml_rmse = get_ml_prediction(
        exercise_name, avg_reps, current_weight, total_volume, int(num_sets), session_number
    )
    
    blended = blend_targets(math_weight, ml_weight, ml_rmse, step)
    
    # Build ML info string
    ml_str = ""
    if ml_weight is not None:
        confidence = "high" if (ml_rmse and ml_rmse < 3) else "med" if (ml_rmse and ml_rmse < 10) else "low"
        ml_str = f"🤖 ML: {ml_weight}kg ({confidence} conf)"
    
    # Use blended weight as the final target
    final_weight = blended
    
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
        "rationale": rationale,
        "trend_summary": trend_str,
        "ml_info": ml_str,
        "best_ever": trend_report['best_weight_ever'],
    }


# ---------------------------------------------------------------------------
# Full Blueprint Generation
# ---------------------------------------------------------------------------

def get_split_exercises(df, split_name):
    """
    Find all exercises done in recent sessions of a given split.
    Returns a list of exercise names (matched to EXERCISE_CONFIG keys).
    """
    from bot import match_exercise_config
    
    # Find all CSV rows for this split
    split_df = df[df['title'].str.strip().str.lower() == split_name.lower().strip()]
    
    if split_df.empty:
        return []
    
    # Get the most recent session of this split to determine the exercise order
    latest_date = split_df['start_time'].max()
    latest_session = split_df[split_df['start_time'] == latest_date]
    
    # Maintain exercise order from the session
    seen = set()
    exercises = []
    for raw_name in latest_session['exercise_title']:
        matched = match_exercise_config(raw_name.strip())
        if matched and matched not in seen:
            seen.add(matched)
            exercises.append(matched)
    
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
        
        # Get session history (up to 100 sessions for this exercise across ALL splits)
        sessions = get_exercise_sessions(df, ex_name, max_sessions=100)
        
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
                    'num_sets': len(last_sets),
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
            msg += f"• {inc['exercise']} ➡️ *{inc['next_weight']}kg*\n"
        msg += "\n━━━━━━━━━━━━━━━━━━\n\n"
    
    msg += "📋 *DETAILED TARGETS:*\n\n"
    
    for t in targets:
        msg += f"*{t['exercise']}*\n"
        msg += f"🎯 Target: {t['next_weight']}kg for {t['target_sets']} sets ({t['target_reps']} reps)\n"
        msg += f"💡 {t['rationale']}\n"
        msg += f"{t['trend_summary']}"
        if t['ml_info']:
            msg += f" | {t['ml_info']}"
        msg += "\n\n"
    
    return msg, targets


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
