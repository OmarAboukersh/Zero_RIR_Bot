"""Which exercises belong to each day's plan (skips, swaps, permanent replacements)."""

import pandas as pd
import pytest

import csv_trend_engine as engine
from conftest import days_ago


def history(sessions, title="Pull + ham"):
    """sessions: list of (days_ago, [exercise names in order])"""
    rows = []
    for ago, exercises in sessions:
        t = pd.Timestamp(days_ago(ago))
        rows += [{"title": title, "start_time": t, "exercise_title": ex} for ex in exercises]
    return pd.DataFrame(rows)


ROW = "T Bar Row"
BACK = "Back Extension (Weighted Hyperextension)"
CURL = "Preacher Curl (Machine)"


@pytest.fixture(autouse=True)
def no_replacements(monkeypatch):
    monkeypatch.setattr(engine, "EXERCISE_REPLACEMENTS", {})
    monkeypatch.setattr(engine, "SPLIT_SWAPS", [])


def test_skipping_once_keeps_the_exercise_in_place():
    df = history([(14, [ROW, BACK, CURL]), (7, [ROW, BACK, CURL]), (0, [ROW, CURL])])
    assert engine.get_split_exercises(df, "Pull + ham") == [ROW, BACK, CURL]


def test_skipping_lookback_times_in_a_row_drops_it():
    n = engine.SPLIT_LOOKBACK_SESSIONS
    df = history([(7 * (n + 1), [ROW, BACK, CURL])] + [(7 * k, [ROW, CURL]) for k in range(n)])
    assert engine.get_split_exercises(df, "Pull + ham") == [ROW, CURL]


def test_switching_equipment_is_a_swap_not_a_skip():
    df = history([(7, ["Lateral Raise (Dumbbell)", CURL]), (0, ["Lateral Raise (Cable)", CURL])])
    assert engine.get_split_exercises(df, "Pull + ham") == ["Lateral Raise (Cable)", CURL]


def test_permanent_replacement_takes_the_old_exercises_spot(monkeypatch):
    monkeypatch.setattr(engine, "EXERCISE_REPLACEMENTS",
                        {"Lying Leg Curl (Machine)": "Lying Leg Curl Single Leg (Machine)"})
    df = history([(7, [ROW, "Lying Leg Curl (Machine)", CURL]), (0, [ROW, CURL])])
    assert engine.get_split_exercises(df, "Pull + ham") == [ROW, "Lying Leg Curl Single Leg (Machine)", CURL]


def test_one_off_swap_only_applies_on_its_date(monkeypatch):
    date = str(days_ago(0))
    monkeypatch.setattr(engine, "SPLIT_SWAPS", [
        {"split": "push + quad", "date": date, "from": "Leg Press (Machine)", "to": "Squat (Smith Machine)"}])
    df = history([(0, ["Leg Press (Machine)"])], title="push + quad")
    assert engine.get_split_exercises(df, "push + quad") == ["Squat (Smith Machine)"]


def test_other_splits_are_ignored():
    df = pd.concat([history([(0, [ROW])]), history([(0, [CURL])], title="Push day")])
    assert engine.get_split_exercises(df, "Pull + ham") == [ROW]


def test_unknown_split_is_empty():
    assert engine.get_split_exercises(history([(0, [ROW])]), "Leg day") == []
