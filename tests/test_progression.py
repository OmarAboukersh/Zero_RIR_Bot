"""Double progression and RPE rules: the maths that decides your next targets."""

import csv_trend_engine as engine
from conftest import target_for

CFG = {"ceiling": 8, "step": 2.5}


# ---------- Rep goals from RPE ----------

def test_rpe_10_holds_reps():
    goal, why = engine._rep_goal(10, consolidated=True, rep_bar=6, times_hit=3, days_at_bar=20, cap=8)
    assert goal == 6 and "Hold" in why


def test_rpe_8_adds_one_rep():
    assert engine._rep_goal(8, False, 6, 1, 0, cap=8)[0] == 7


def test_rpe_7_adds_two_reps():
    assert engine._rep_goal(7, False, 6, 1, 0, cap=8)[0] == 8


def test_rep_goal_never_passes_the_ceiling():
    assert engine._rep_goal(6, False, 7, 1, 0, cap=8)[0] == 8


def test_no_rpe_waits_until_consolidated():
    assert engine._rep_goal(None, consolidated=False, rep_bar=6, times_hit=1, days_at_bar=3, cap=8)[0] == 6
    assert engine._rep_goal(None, consolidated=True, rep_bar=6, times_hit=2, days_at_bar=15, cap=8)[0] == 7


def test_rpe_in_notes_is_read():
    assert engine._rpe_from_notes("felt heavy, RPE 9") == 9
    assert engine._rpe_from_notes("rpe: 8.5") == 8.5
    assert engine._rpe_from_notes("no rating") is None


# ---------- Weight increases ----------

def test_ceiling_on_every_set_adds_weight():
    t = target_for("Bench Press (Smith Machine)", [
        (10, [(90, 7), (90, 7)], 9, ""),
        (3, [(90, 8), (90, 8)], 9, ""),
    ], CFG)
    assert t["next_weight"] == 92.5
    assert t["target_reps"] == "5"
    assert "Load increased" in t["rationale"]


def test_ceiling_at_rpe_10_holds_weight():
    t = target_for("Bench Press (Smith Machine)", [
        (10, [(90, 7), (90, 7)], 9, ""),
        (3, [(90, 8), (90, 8)], 10, ""),
    ], CFG)
    assert t["next_weight"] == 90
    assert t["target_reps"] == "8"


def test_strict_ceiling_adds_weight_even_at_rpe_10():
    t = target_for("Squat (Smith Machine)", [
        (10, [(100, 7), (100, 7)], 9, ""),
        (3, [(100, 8), (100, 8)], 10, ""),
    ], {"ceiling": 8, "step": 2.5, "strict_ceiling": True})
    assert t["next_weight"] == 102.5


def test_one_missed_set_means_no_increase():
    t = target_for("Bench Press (Smith Machine)", [
        (3, [(90, 8), (90, 7)], 8, ""),
    ], CFG)
    assert t["next_weight"] == 90


def test_back_off_sets_dont_block_an_increase():
    # A much lighter back-off set doesn't count as a working set
    t = target_for("Bench Press (Smith Machine)", [
        (3, [(90, 8), (90, 8), (60, 5)], 9, ""),
    ], CFG)
    assert t["next_weight"] == 92.5


def test_increase_stops_at_max_weight():
    t = target_for("Calf Press (Machine)", [
        (3, [(128.5, 15), (128.5, 15)], 9, ""),
    ], {"ceiling": 15, "step": 2.5, "max_weight": 130})
    assert t["next_weight"] == 130


def test_maxed_machine_progresses_reps_not_weight():
    t = target_for("Lying Leg Curl (Machine)", [
        (3, [(100, 15), (100, 15)], 8, ""),
    ], {"ceiling": 15, "step": 2.5, "max_weight": 100})
    assert t["next_weight"] == 100
    assert t["target_reps"] == "16"  # no ceiling once maxed
    assert "Max weight" in t["rationale"]


def test_max_in_notes_marks_machine_as_maxed():
    t = target_for("Shrug (Cable)", [
        (3, [(120, 15), (120, 15)], 8, "max"),
    ], {"ceiling": 15, "step": 2.5})
    assert t["next_weight"] == 120


def test_one_bad_day_doesnt_lower_the_rep_goal():
    t = target_for("Bench Press (Smith Machine)", [
        (8, [(90, 7), (90, 7)], 9, ""),
        (3, [(90, 5), (90, 4)], 9, ""),
    ], CFG)
    assert t["next_weight"] == 90
    assert int(t["target_reps"]) >= 7


def test_every_set_gets_the_same_target():
    t = target_for("Bench Press (Smith Machine)", [
        (3, [(90, 7), (90, 6), (90, 5)], 8, ""),
    ], CFG)
    assert t["target_sets"] == 3
    lines = t["formatted_targets"].splitlines()
    assert len(lines) == 3 and len(set(line.split(":")[1] for line in lines)) == 1
