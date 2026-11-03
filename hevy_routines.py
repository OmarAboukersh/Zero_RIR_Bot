"""
Hevy routines — writes the bot's targets straight into your Hevy routines, so you
open Hevy, start the routine, and every set already has the right weight and reps.

What it changes in each of the 4 routines (matched by title: "Push day",
"Pull day", "push + quad", "Pull + ham"):
  - the working sets of every tracked exercise: number of sets, weight and reps
    (warm-up sets are left alone)
  - one line in the exercise notes starting with "🤖" with the coach's reason
    (your own notes are kept; only the 🤖 line is replaced each time)
  - an exercise that was permanently replaced (EXERCISE_REPLACEMENTS) is swapped
    for the new one, if you've logged the new one in Hevy at least once
Nothing else changes: exercise order, rest times, supersets, untracked exercises.

Safety: before writing, the current routines are saved to routines_backup/, and
a routine is only sent to Hevy if something in it actually changed.

Runs automatically after each sync with new workouts, after a settings change in
the web app, and from the "Send targets to Hevy" button. Turn off with
HEVY_ROUTINE_SYNC=off in .env.

Command line:
  python hevy_routines.py              update the routines
  python hevy_routines.py --dry-run    show what would change, write nothing
  python hevy_routines.py --restore routines_backup/<file>.json   put a backup back
"""

import datetime
import json
import os
import re
import sys

import requests

API = "https://api.hevyapp.com/v1"
BACKUP_DIR = "routines_backup"
KEEP_BACKUPS = 20
BOT_MARK = "🤖"

SPLIT_TITLES = ["Push day", "Pull day", "push + quad", "Pull + ham"]
# Other titles a routine might use for the same day
SPLIT_ALIASES = {
    "push day": ["push"],
    "pull day": ["pull"],
    "push + quad": ["push + quads", "push+quad", "push + quad day"],
    "pull + ham": ["pull + hams", "pull+ham", "pull + ham day"],
}


class HevyError(Exception):
    pass


def enabled():
    return os.getenv("HEVY_ROUTINE_SYNC", "on").strip().lower() not in ("off", "false", "0", "no")


# ---------------------------------------------------------------------------
# Hevy API
# ---------------------------------------------------------------------------
def _headers():
    import bot
    if not bot.HEVY_API_KEY:
        raise HevyError("No HEVY_API_KEY in .env")
    return {"api-key": bot.HEVY_API_KEY, "Accept": "application/json", "Content-Type": "application/json"}


def _request(method, path, **kw):
    try:
        r = requests.request(method, API + path, headers=_headers(), timeout=20, **kw)
    except requests.RequestException as e:
        raise HevyError(f"Couldn't reach Hevy: {e}")
    if r.status_code >= 400:
        try:
            msg = r.json().get("error") or r.text
        except ValueError:
            msg = r.text
        raise HevyError(f"Hevy said {r.status_code} on {method} {path}: {str(msg)[:200]}")
    return r.json() if r.text else {}


def list_routines():
    routines, page = [], 1
    while True:
        data = _request("GET", "/routines", params={"page": page, "pageSize": 10})
        routines += data.get("routines", [])
        if page >= int(data.get("page_count") or 1):
            return routines
        page += 1


def template_ids_from_workouts():
    """{config exercise name: exercise_template_id} from your recent Hevy workouts."""
    import bot
    data = _request("GET", "/workouts", params={"page": 1, "pageSize": 10})
    ids = {}
    for w in data.get("workouts", []):
        for ex in w.get("exercises", []):
            name = bot.match_exercise_config(str(ex.get("title", "")).strip(), fuzzy=False)
            if name and ex.get("exercise_template_id") and name not in ids:
                ids[name] = ex["exercise_template_id"]
    return ids


# ---------------------------------------------------------------------------
# Building the update (pure functions, no network)
# ---------------------------------------------------------------------------
def _clean_reason(text):
    text = re.sub(r"^[^\w(]+", "", str(text or ""), flags=re.UNICODE).strip()
    return re.sub(r"\s*[🔄✅🛑📈🚨⚠️]\s*", " ", text).strip()


def bot_note(target):
    parts = [p.strip() for p in target["rationale"].split("|")]
    reason = _clean_reason(parts[0])
    extras = [_clean_reason(p) for p in parts[1:] if "Plateau" in p or "Regression" in p]
    head = "Weight up! " if "Load increased" in target["rationale"] else ""
    line = f"{BOT_MARK} {head}{reason}" + (f" ({'; '.join(extras)})" if extras else "")
    return line[:220]


def merge_notes(existing, new_line):
    """Keep your own note lines, replace the bot's line."""
    keep = [l for l in str(existing or "").splitlines() if not l.strip().startswith(BOT_MARK)]
    keep = [l for l in keep if l.strip()]
    return "\n".join(keep + ([new_line] if new_line else []))


def _set_out(s):
    out = {"type": s.get("type") or "normal", "weight_kg": s.get("weight_kg"), "reps": s.get("reps")}
    for k in ("distance_meters", "duration_seconds", "custom_metric", "rep_range"):
        if s.get(k) is not None:
            out[k] = s[k]
    return out


def _exercise_out(ex):
    """A routine exercise as GET returns it -> the shape PUT accepts."""
    return {
        "exercise_template_id": ex["exercise_template_id"],
        "superset_id": ex.get("supersets_id", ex.get("superset_id")),
        "rest_seconds": ex.get("rest_seconds"),
        "notes": ex.get("notes") or None,
        "sets": [_set_out(s) for s in sorted(ex.get("sets", []), key=lambda s: s.get("index", 0))],
    }


def plan_routine_update(routine, targets, template_ids, replacements, today=None):
    """
    Work out the new version of one routine.
      routine:      the routine as Hevy returns it
      targets:      {config exercise name: target dict from compute_next_target}
      template_ids: {config exercise name: exercise_template_id} (for swaps)
      replacements: EXERCISE_REPLACEMENTS
    Returns (put_body or None if nothing changed, changes [str], warnings [str]).
    """
    import bot
    today = today or datetime.date.today()
    changes, warnings, seen = [], [], set()
    new_exercises = []

    for ex in sorted(routine.get("exercises", []), key=lambda e: e.get("index", 0)):
        out = _exercise_out(ex)
        name = bot.match_exercise_config(str(ex.get("title", "")).strip(), fuzzy=False)
        if name in replacements:
            new_name = replacements[name]
            if template_ids.get(new_name):
                out["exercise_template_id"] = template_ids[new_name]
                changes.append(f"{ex.get('title')} → {new_name} (replaced)")
                name = new_name
            else:
                warnings.append(f"{ex.get('title')} was replaced by {new_name}, but you haven't logged "
                                f"{new_name} in Hevy yet. Swap it in this routine once in Hevy.")
                name = None
        t = targets.get(name) if name else None
        if t:
            seen.add(name)
            warmups = [s for s in out["sets"] if s["type"] == "warmup"]
            working = [s for s in out["sets"] if s["type"] != "warmup"]
            set_type = working[0]["type"] if working else "normal"
            reps = int(str(t["target_reps"]).split("-")[0])
            weight = float(t["next_weight"])
            new_working = [{"type": set_type, "weight_kg": weight, "reps": reps} for _ in range(int(t["target_sets"]))]
            old_desc = ", ".join(f"{s.get('weight_kg') or 0:g}x{s.get('reps') or 0}" for s in working) or "no sets"
            new_desc = ", ".join(f"{s['weight_kg']:g}x{s['reps']}" for s in new_working)
            if [(s.get("weight_kg"), s.get("reps")) for s in working] != [(s["weight_kg"], s["reps"]) for s in new_working]:
                changes.append(f"{name}: {old_desc} → {new_desc}")
            out["sets"] = warmups + new_working
            out["notes"] = merge_notes(out["notes"], bot_note(t)) or None
        new_exercises.append(out)

    for name in targets:
        if name not in seen and not any(name == replacements.get(k) for k in seen):
            warnings.append(f"{name} is in your plan but not in the Hevy routine \"{routine.get('title')}\". "
                            f"Add it there once and the bot will fill it in.")

    old_exercises = [_exercise_out(e) for e in sorted(routine.get("exercises", []), key=lambda e: e.get("index", 0))]
    stamp = f"{BOT_MARK} Targets set by Zero RIR on {today:%a %d %b}"
    new_notes = merge_notes(routine.get("notes"), stamp)
    if new_exercises == old_exercises:
        return None, [], warnings
    body = {"routine": {"title": routine["title"], "notes": new_notes, "exercises": new_exercises}}
    return body, changes, warnings


# ---------------------------------------------------------------------------
# Targets for every exercise in every routine
# ---------------------------------------------------------------------------
def _targets_for_split(split_title):
    import csv_trend_engine as engine
    _msg, targets = engine.generate_full_blueprint(split_title)
    return {t["exercise"]: t for t in (targets or [])}


def _target_for_exercise(name):
    """Target for an exercise that isn't in the split's current plan (e.g. a routine extra)."""
    import bot
    import csv_trend_engine as engine
    cfg = bot.EXERCISE_CONFIG.get(name)
    if not cfg:
        return None
    df = engine.load_csv()
    sessions = engine.get_exercise_sessions(df, name, max_sessions=30)
    if sessions.empty:
        return None
    return engine.compute_next_target(name, sessions, cfg, engine.analyze_trend(sessions))


def _match_routine(routines, split_title):
    wanted = [split_title.lower()] + SPLIT_ALIASES.get(split_title.lower(), [])
    for r in routines:
        if str(r.get("title", "")).strip().lower() in wanted:
            return r
    return None


def _backup(routines):
    os.makedirs(BACKUP_DIR, exist_ok=True)
    path = os.path.join(BACKUP_DIR, f"routines_{datetime.datetime.now():%Y%m%d_%H%M%S}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(routines, f, ensure_ascii=False, indent=1)
    old = sorted(p for p in os.listdir(BACKUP_DIR) if p.startswith("routines_"))
    for p in old[:-KEEP_BACKUPS]:
        try:
            os.remove(os.path.join(BACKUP_DIR, p))
        except OSError:
            pass
    return path


def push_targets(dry_run=False):
    """Update all 4 routines. Returns a summary dict; prints a readable log."""
    import bot
    import csv_trend_engine as engine
    summary = {"updated": [], "unchanged": [], "changes": [], "warnings": [], "backup": None, "dry_run": dry_run}

    routines = list_routines()
    template_ids = template_ids_from_workouts()
    plans = []
    for split in SPLIT_TITLES:
        routine = _match_routine(routines, split)
        if not routine:
            summary["warnings"].append(f"No Hevy routine called \"{split}\" found, so it wasn't updated.")
            continue
        targets = _targets_for_split(split)
        # Exercises that are in the routine but not in the plan still get targets
        for ex in routine.get("exercises", []):
            name = bot.match_exercise_config(str(ex.get("title", "")).strip(), fuzzy=False)
            name = engine.EXERCISE_REPLACEMENTS.get(name, name)
            if name and name not in targets:
                t = _target_for_exercise(name)
                if t:
                    targets[name] = t
        body, changes, warnings = plan_routine_update(routine, targets, template_ids, engine.EXERCISE_REPLACEMENTS)
        summary["warnings"] += warnings
        if body is None:
            summary["unchanged"].append(routine["title"])
        else:
            plans.append((routine, body))
            summary["changes"] += [f"[{routine['title']}] {c}" for c in changes] or [f"[{routine['title']}] notes updated"]

    if plans and not dry_run:
        summary["backup"] = _backup(routines)
        for routine, body in plans:
            _request("PUT", f"/routines/{routine['id']}", json=body)
            summary["updated"].append(routine["title"])
    elif plans:
        summary["updated"] = [r["title"] for r, _ in plans]

    _print_summary(summary)
    return summary


def _print_summary(s):
    verb = "Would update" if s["dry_run"] else "Updated"
    if s["updated"]:
        print(f"🏋️ {verb} Hevy routines: {', '.join(s['updated'])}")
        for c in s["changes"]:
            print(f"   {c}")
    if s["unchanged"]:
        print(f"✅ Already up to date in Hevy: {', '.join(s['unchanged'])}")
    for w in s["warnings"]:
        print(f"⚠️ {w}")
    if s["backup"]:
        print(f"💾 Backup of the previous routines: {s['backup']}")


def push_targets_safely():
    """For automatic runs: never let a Hevy problem break a sync."""
    if not enabled():
        return None
    try:
        return push_targets()
    except Exception as e:
        print(f"⚠️ Couldn't update Hevy routines: {e}")
        return None


def restore(path):
    with open(path, encoding="utf-8") as f:
        routines = json.load(f)
    for r in routines:
        body = {"routine": {"title": r["title"], "notes": r.get("notes"),
                            "exercises": [_exercise_out(e) for e in sorted(r.get("exercises", []), key=lambda e: e.get("index", 0))]}}
        _request("PUT", f"/routines/{r['id']}", json=body)
        print(f"↩️ Restored {r['title']}")


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    if len(sys.argv) > 2 and sys.argv[1] == "--restore":
        restore(sys.argv[2])
    else:
        push_targets(dry_run="--dry-run" in sys.argv)
