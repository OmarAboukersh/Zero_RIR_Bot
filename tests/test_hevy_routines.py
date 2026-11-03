"""Writing targets into Hevy routines, with a fake Hevy API."""

import json
import os

import pytest

import bot
import hevy_routines as hr


def target(name, weight, reps, sets, rationale="🔄 RPE 8: reps in the tank. Aim for 7 on every set."):
    return {"exercise": name, "next_weight": weight, "target_reps": str(reps), "target_sets": sets, "rationale": rationale}


def routine(title="Push day", exercises=None, notes=None, rid="r1"):
    return {"id": rid, "title": title, "notes": notes, "folder_id": None,
            "updated_at": "x", "created_at": "x", "exercises": exercises or []}


def ex(title, template, sets, index=0, notes=None, superset=None, rest=180):
    return {"index": index, "title": title, "exercise_template_id": template, "notes": notes,
            "supersets_id": superset, "rest_seconds": rest,
            "sets": [{"index": i, "type": t, "weight_kg": w, "reps": r, "rpe": None, "rep_range": None,
                      "distance_meters": None, "duration_seconds": None, "custom_metric": None}
                     for i, (t, w, r) in enumerate(sets)]}


BENCH = "Bench Press (Smith Machine)"


def test_sets_notes_and_everything_else_are_kept():
    r = routine(exercises=[
        ex(BENCH, "T1", [("warmup", 40, 10), ("normal", 90, 8), ("normal", 90, 8)], index=0,
           notes="Pause at the bottom\n🤖 old coach line", superset=1),
        ex("Some Untracked Stretch", "T9", [("normal", None, 30)], index=1),
    ], notes="My push routine")
    body, changes, warnings = hr.plan_routine_update(
        r, {BENCH: target(BENCH, 92.5, 5, 3, "✅ Ceiling cleared. Load increased.")}, {}, {})
    b = body["routine"]
    bench, stretch = b["exercises"]
    assert [s["type"] for s in bench["sets"]] == ["warmup", "normal", "normal", "normal"]
    assert bench["sets"][0] == {"type": "warmup", "weight_kg": 40, "reps": 10}            # warm-up untouched
    assert all(s["weight_kg"] == 92.5 and s["reps"] == 5 for s in bench["sets"][1:])
    assert bench["notes"].splitlines()[0] == "Pause at the bottom"                          # your note kept
    assert bench["notes"].splitlines()[1].startswith("🤖 Weight up!")                       # bot line replaced
    assert bench["superset_id"] == 1 and bench["rest_seconds"] == 180                       # read->write field name fixed
    assert stretch["exercise_template_id"] == "T9" and stretch["sets"] == [{"type": "normal", "weight_kg": None, "reps": 30}]
    assert b["title"] == "Push day" and b["notes"].startswith("My push routine\n🤖 Targets set by Zero RIR")
    assert set(b) == {"title", "notes", "exercises"}                                        # only fields PUT accepts
    assert changes == [f"{BENCH}: 90x8, 90x8 → 92.5x5, 92.5x5, 92.5x5"] and warnings == []


def test_unchanged_routine_is_not_sent():
    t = target(BENCH, 90, 8, 2)
    first, _, _ = hr.plan_routine_update(routine(exercises=[ex(BENCH, "T1", [("normal", 90, 7)] * 2)]), {BENCH: t}, {}, {})
    # Feed Hevy's "after" state back in: nothing left to change
    after = routine(exercises=[dict(ex(BENCH, "T1", [("normal", 90, 8)] * 2), notes=first["routine"]["exercises"][0]["notes"])])
    body, changes, _ = hr.plan_routine_update(after, {BENCH: t}, {}, {})
    assert body is None and changes == []


def test_replaced_exercise_is_swapped_when_known():
    old, new = "Lying Leg Curl (Machine)", "Lying Leg Curl Single Leg (Machine)"
    r = routine("Pull + ham", [ex(old, "OLD", [("normal", 100, 15)] * 3)])
    body, changes, _ = hr.plan_routine_update(r, {new: target(new, 48.75, 8, 3)}, {new: "NEW"}, {old: new})
    e = body["routine"]["exercises"][0]
    assert e["exercise_template_id"] == "NEW" and e["sets"][0] == {"type": "normal", "weight_kg": 48.75, "reps": 8}
    assert any("replaced" in c for c in changes)


def test_replaced_exercise_without_history_is_left_and_flagged():
    old, new = "Calf Press (Machine)", "Calf Press Single Leg (Machine)"
    r = routine("Pull day", [ex(old, "OLD", [("normal", 130, 16)] * 2)])
    body, _, warnings = hr.plan_routine_update(r, {new: target(new, 75, 6, 2)}, {}, {old: new})
    assert body is None                                   # nothing safe to change
    assert any("Swap it in this routine once in Hevy" in w for w in warnings)


def test_plan_exercise_missing_from_routine_is_flagged():
    _, _, warnings = hr.plan_routine_update(routine(exercises=[]), {BENCH: target(BENCH, 90, 8, 2)}, {}, {})
    assert any("not in the Hevy routine" in w for w in warnings)


# ---------- The full run against a fake Hevy ----------

class FakeHevy:
    def __init__(self, routines, workouts):
        self.routines, self.workouts, self.puts = routines, workouts, []

    def request(self, method, url, headers, timeout, params=None, json=None):
        assert headers["api-key"] == "test-key"
        path = url.split("/v1")[1]
        if method == "GET" and path == "/routines":
            page = params["page"]
            chunk = self.routines[(page - 1) * 1: page * 1]           # 1 per page: tests pagination
            return Resp(200, {"page": page, "page_count": len(self.routines), "routines": chunk})
        if method == "GET" and path == "/workouts":
            return Resp(200, {"workouts": self.workouts})
        if method == "PUT":
            self.puts.append((path, json))
            return Resp(200, {"routine": json["routine"]})
        return Resp(404, {"error": "nope"})


class Resp:
    def __init__(self, status, payload):
        self.status_code, self._p = status, payload
        self.text = "x"

    def json(self):
        return self._p


@pytest.fixture
def fake(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(bot, "HEVY_API_KEY", "test-key")
    targets = {"Push day": {BENCH: target(BENCH, 92.5, 5, 3)}, "Pull day": {}, "push + quad": {}, "Pull + ham": {}}
    monkeypatch.setattr(hr, "_targets_for_split", lambda split: dict(targets[split]))
    monkeypatch.setattr(hr, "_target_for_exercise", lambda name: None)
    hevy = FakeHevy(
        routines=[routine("Push day", [ex(BENCH, "T1", [("normal", 90, 8)] * 2)], rid="push"),
                  routine("Pull day", [ex("Some Untracked Stretch", "T9", [("normal", None, 30)])], rid="pull")],
        workouts=[])
    monkeypatch.setattr(hr.requests, "request", hevy.request)
    return hevy


def test_push_updates_only_changed_routines_and_backs_up_first(fake):
    s = hr.push_targets()
    assert [p for p, _ in fake.puts] == ["/routines/push"]
    assert s["updated"] == ["Push day"] and "Pull day" in s["unchanged"]
    assert any("No Hevy routine called \"push + quad\"" in w for w in s["warnings"])
    backup = json.load(open(s["backup"], encoding="utf-8"))
    assert {r["id"] for r in backup} == {"push", "pull"}                 # what was there before


def test_preview_writes_nothing(fake):
    s = hr.push_targets(dry_run=True)
    assert fake.puts == [] and s["updated"] == ["Push day"] and s["backup"] is None
    assert not os.path.exists(hr.BACKUP_DIR)


def test_problems_never_break_a_sync(monkeypatch):
    def boom(*a, **k):
        raise hr.HevyError("Hevy is down")
    monkeypatch.setattr(hr, "push_targets", boom)
    assert hr.push_targets_safely() is None


def test_can_be_turned_off(monkeypatch):
    monkeypatch.setenv("HEVY_ROUTINE_SYNC", "off")
    called = []
    monkeypatch.setattr(hr, "push_targets", lambda **k: called.append(1))
    hr.push_targets_safely()
    assert called == []
