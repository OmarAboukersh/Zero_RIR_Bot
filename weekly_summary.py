"""
Weekly Summary — A 7-day progress report: workouts done, volume vs the week
before, PRs, weight increases and how much RPE was logged.

Sent automatically every Monday by morning_delivery.py and on demand with /weekly.
"""

import datetime
import pandas as pd

from csv_trend_engine import load_csv, get_exercise_sessions, current_block, WEIGHT_TOLERANCE


def _week_rows(df, start, end):
    return df[(df['session_date'] >= start) & (df['session_date'] <= end)]


def _find_prs(clean, week_exercises, start, end):
    """
    Compare this week's sets with everything before it in the current training
    block (so an old gym's numbers don't count).

    Returns (prs, increases): lists of message lines.
    """
    prs, increases = [], []

    for ex in sorted(week_exercises):
        sessions = current_block(get_exercise_sessions(clean, ex, max_sessions=100))
        prior = sessions[sessions['session_date'] < start]
        week = sessions[(sessions['session_date'] >= start) & (sessions['session_date'] <= end)]
        if prior.empty or week.empty:
            continue

        prior_sets = [(w, r) for wl, rl in zip(prior['weights_list'], prior['reps_list']) for w, r in zip(wl, rl)]
        week_sets = [(w, r) for wl, rl in zip(week['weights_list'], week['reps_list']) for w, r in zip(wl, rl)]

        prior_best_weight = max(w for w, _ in prior_sets)
        week_best_weight = max(w for w, _ in week_sets)

        if week_best_weight > prior_best_weight:
            reps = max(r for w, r in week_sets if w == week_best_weight)
            prs.append(f"🏆 {ex}: *{week_best_weight:g}kg* × {reps} (heaviest yet)")
            continue

        # Rep PR: more reps than ever at this weight (or heavier)
        rep_pr = None
        for w, r in week_sets:
            prior_reps = [pr for pw, pr in prior_sets if pw >= w - WEIGHT_TOLERANCE]
            if prior_reps and r > max(prior_reps) and (rep_pr is None or w > rep_pr[0] or (w == rep_pr[0] and r > rep_pr[1])):
                rep_pr = (w, r, max(prior_reps))
        if rep_pr:
            prs.append(f"🏆 {ex}: {rep_pr[0]:g}kg × *{rep_pr[1]}* (was {rep_pr[2]})")

        # Weight up vs the last session before this week
        last_weight = prior['max_weight'].iloc[-1]
        if week_best_weight > last_weight:
            increases.append(f"📈 {ex}: {last_weight:g} → *{week_best_weight:g}kg*")

    return prs, increases


def build_weekly_summary(end_date=None, days=7):
    """Build the summary message for the `days` days ending on `end_date` (default: today)."""
    end = end_date or datetime.date.today()
    start = end - datetime.timedelta(days=days - 1)
    prev_start = start - datetime.timedelta(days=days)
    prev_end = start - datetime.timedelta(days=1)

    everything = load_csv(exclude_deloads=False)
    clean = load_csv()

    msg = f"📅 *Weekly Summary* ({start.strftime('%b %d')} – {end.strftime('%b %d')})\n\n"

    week_all = _week_rows(everything, start, end)
    if week_all.empty:
        return msg + "No workouts logged this week. Rest well and come back strong 💪"

    # --- Workouts done (deloads marked) ---
    clean_week = _week_rows(clean, start, end)
    clean_workouts = set(zip(clean_week['session_date'], clean_week['title']))
    workouts = week_all.groupby(['session_date', 'title']).size().reset_index()[['session_date', 'title']]
    n_deload = 0
    msg += f"🏋️ *Workouts: {len(workouts)}*\n"
    for date, title in zip(workouts['session_date'], workouts['title']):
        is_deload = (date, title) not in clean_workouts
        n_deload += is_deload
        msg += f"• {date.strftime('%a %d')}: {title.strip()}{' 🟢 deload' if is_deload else ''}\n"

    # --- Sets and volume vs the week before (deloads excluded) ---
    prev_week = _week_rows(clean, prev_start, prev_end)
    prev_all = _week_rows(everything, prev_start, prev_end)
    prev_had_deload = len(set(zip(prev_all['session_date'], prev_all['title'])) -
                          set(zip(prev_week['session_date'], prev_week['title']))) > 0
    sets_now, sets_prev = len(clean_week), len(prev_week)
    vol_now, vol_prev = clean_week['set_volume'].sum(), prev_week['set_volume'].sum()
    msg += f"\n📊 *Working sets:* {sets_now} (last week {sets_prev})\n"
    if vol_prev > 0:
        change = (vol_now - vol_prev) / vol_prev * 100
        arrow = "↗️" if change > 2 else "↘️" if change < -2 else "→"
        vs = "vs last week (a deload week)" if prev_had_deload else "vs last week"
        msg += f"📦 *Volume:* {vol_now:,.0f}kg {arrow} {change:+.0f}% {vs}\n"
    else:
        msg += f"📦 *Volume:* {vol_now:,.0f}kg\n"
    if n_deload:
        msg += "_Deload sessions are left out of sets, volume and PRs._\n"

    # --- PRs and weight increases ---
    prs, increases = _find_prs(clean, set(clean_week['exercise_title']), start, end)
    msg += "\n"
    if prs:
        msg += "*PRs this week:*\n" + "\n".join(prs) + "\n\n"
    if increases:
        msg += "*Weight went up:*\n" + "\n".join(increases) + "\n\n"
    if not prs and not increases:
        msg += "No new PRs this week. Consistency is what builds them 🧱\n\n"

    # --- RPE logging (the more you log, the better the rep targets) ---
    rpe_sets = week_all['rpe'].notna().sum()
    msg += f"📝 RPE logged on {rpe_sets}/{len(week_all)} sets"
    if rpe_sets < len(week_all):
        msg += " (logging it lets the bot add reps faster when you have reps in the tank)"

    return msg


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding='utf-8')
    print(build_weekly_summary())
