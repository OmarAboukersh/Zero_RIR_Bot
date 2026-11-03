"""Shared test helpers: build fake workout history without touching your real data."""

import datetime
import os
import sys

import pandas as pd
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import csv_trend_engine as engine  # noqa: E402

CSV_COLUMNS = ["title", "start_time", "end_time", "description", "exercise_title", "superset_id",
               "exercise_notes", "set_index", "set_type", "weight_kg", "reps", "distance_km",
               "duration_seconds", "rpe"]


def days_ago(n):
    return datetime.date.today() - datetime.timedelta(days=n)


def make_sessions(rows):
    """
    Session-level history for one exercise, the shape get_exercise_sessions() returns.
    rows: list of (days_ago, [(weight, reps), ...], rpe_or_None, notes)
    """
    out = []
    for n, (ago, sets, rpe, notes) in enumerate(rows, start=1):
        weights = [float(w) for w, _ in sets]
        reps = [int(r) for _, r in sets]
        out.append({
            "session_date": days_ago(ago),
            "title": "Push day",
            "max_weight": max(weights),
            "avg_reps": sum(reps) / len(reps),
            "total_reps": sum(reps),
            "total_volume": sum(w * r for w, r in zip(weights, reps)),
            "num_sets": len(sets),
            "reps_list": reps,
            "weights_list": weights,
            "max_rpe": float("nan") if rpe is None else rpe,
            "exercise_notes": notes,
            "session_number": n,
        })
    return pd.DataFrame(out)


def target_for(exercise, rows, config):
    sessions = make_sessions(rows)
    trend = engine.analyze_trend(sessions)
    return engine.compute_next_target(exercise, sessions, config, trend)


def write_csv(path, workouts):
    """
    Write a Hevy-style workouts.csv.
    workouts: list of (days_ago, title, {exercise: [(weight, reps, rpe), ...]})
    """
    rows = []
    for ago, title, exercises in workouts:
        when = datetime.datetime.combine(days_ago(ago), datetime.time(10, 0)).strftime("%b %d, %Y, %I:%M %p")
        for ex, sets in exercises.items():
            for i, (w, r, rpe) in enumerate(sets):
                rows.append({"title": title, "start_time": when, "end_time": when, "description": "",
                             "exercise_title": ex, "superset_id": "", "exercise_notes": "", "set_index": i,
                             "set_type": "normal", "weight_kg": w, "reps": r, "distance_km": "",
                             "duration_seconds": "", "rpe": rpe})
    pd.DataFrame(rows, columns=CSV_COLUMNS).to_csv(path, index=False)


@pytest.fixture(autouse=True)
def no_ml(monkeypatch):
    """ML hints depend on locally trained models; keep tests deterministic without them."""
    monkeypatch.setattr(engine, "get_ml_prediction", lambda *a, **k: (None, None))
