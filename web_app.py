"""
Web App — Zero_RIR_Bot as your own website.

Run it with run_web_app.bat (or `python web_app.py`) and open
http://localhost:8000 on the laptop, or http://<laptop-ip>:8000 on your phone.

How it fits together:
  web/index.html   the page you see (buttons, tables, charts)
  web_app.py       this file: a small Flask server that answers the page's
                   requests (/api/...) with JSON, using the same engine as before
  csv_trend_engine / bot / weekly_summary   unchanged — all the training logic

Speed: the engine re-reads workouts.csv on every call. Here that read is cached
in memory and only redone when the file actually changes (after a sync), so
pages answer almost instantly.

Auto-sync: the page asks the server to pull from Hevy every time it opens
(at most once every AUTOSYNC_MINUTES, default 5). Set SYNC_EVERY_MINUTES to also
sync on a timer from inside the app (handy in Docker / on the home lab, where
there's no Windows Task Scheduler running run_bot.bat).

Optional PIN: put APP_PIN="1234" in .env and the page will ask for it once.
Use this if you run it on a shared Wi-Fi (like uni) so nobody else can press
Sync or change your settings.
"""

import os
import io
import sys
import datetime
import threading
import contextlib

from dotenv import load_dotenv
from flask import Flask, jsonify, request, send_from_directory, abort

# Always work from the bot folder, so workouts.csv / models/ are found no matter
# where the script is started from. DATA_DIR (used by Docker) keeps your data
# (workouts.csv, models/, settings) in a separate folder from the code.
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.abspath(os.getenv("DATA_DIR", BASE_DIR))
os.chdir(DATA_DIR)
sys.path.insert(0, BASE_DIR)
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

load_dotenv()
APP_PIN = os.getenv("APP_PIN", "").strip()
PORT = int(os.getenv("APP_PORT", "8000"))

import csv_trend_engine as engine
import bot as core

# ---------------------------------------------------------------------------
# CSV cache: load_csv() is the slow part (~0.4s+). Cache it per file version.
# ---------------------------------------------------------------------------
_original_load_csv = engine.load_csv
_csv_cache = {}
_csv_lock = threading.Lock()


def cached_load_csv(exclude_deloads=True):
    try:
        st = os.stat(engine.CSV_FILE)
        version = (st.st_mtime_ns, st.st_size)
    except OSError:
        return _original_load_csv(exclude_deloads=exclude_deloads)
    key = (version, exclude_deloads)
    with _csv_lock:
        if key not in _csv_cache:
            # Drop entries for older file versions
            for k in [k for k in _csv_cache if k[0] != version]:
                del _csv_cache[k]
            with contextlib.redirect_stdout(io.StringIO()):  # hide the deload log spam
                _csv_cache[key] = _original_load_csv(exclude_deloads=exclude_deloads)
        return _csv_cache[key].copy()


engine.load_csv = cached_load_csv
import weekly_summary  # noqa: E402  (imported after the patch on purpose)
weekly_summary.load_csv = cached_load_csv

# ---------------------------------------------------------------------------
# Split schedule (same as morning_delivery.py)
# ---------------------------------------------------------------------------
SPLITS = [
    {"key": "Push", "title": "Push day", "day": "Tue"},
    {"key": "Pull", "title": "Pull day", "day": "Wed"},
    {"key": "Push + Quads", "title": "push + quad", "day": "Sat"},
    {"key": "Pull + Ham", "title": "Pull + ham", "day": "Sun"},
]
TRAINING_WEEKDAYS = [1, 2, 5, 6]  # Tue, Wed, Sat, Sun

app = Flask(__name__, static_folder=None)
_job_lock = threading.Lock()  # only one sync/train at a time


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _split_by_key(key):
    for s in SPLITS:
        if key.lower() in (s["key"].lower(), s["title"].lower()):
            return s
    return None


def _target_to_json(t):
    """Turn the engine's target dict into clean JSON for the page."""
    parts = [p.strip() for p in t["rationale"].split("|") if p.strip()]
    reason = parts[0] if parts else ""
    return {
        "exercise": t["exercise"],
        "weight": t["next_weight"],
        "sets": t["target_sets"],
        "reps": t["target_reps"],
        "increase": "Load increased" in t["rationale"],
        "reason": reason,
        "tags": parts[1:],
        "trend": t["trend_summary"],
        "ml": t["ml_info"],
        "best": t["best_ever"],
        "ceiling": core.EXERCISE_CONFIG.get(t["exercise"], {}).get("ceiling"),
    }


def build_split(split):
    """Targets for one split, as JSON."""
    _msg, targets = engine.generate_full_blueprint(split["title"])
    return {
        "key": split["key"],
        "title": split["title"],
        "day": split["day"],
        "exercises": [_target_to_json(t) for t in (targets or [])],
    }


def next_workout():
    """Which split is next and on what date (same rules as /next)."""
    df = cached_load_csv(exclude_deloads=False)
    if df.empty:
        return None, None
    last = df.sort_values("start_time").iloc[-1]
    last_title = str(last["title"]).strip().lower()
    last_date = last["start_time"].date()

    titles = [s["title"].lower() for s in SPLITS]
    idx = titles.index(last_title) if last_title in titles else -1
    split = SPLITS[(idx + 1) % len(SPLITS)]

    day = max(datetime.date.today(), last_date + datetime.timedelta(days=1))
    while day.weekday() not in TRAINING_WEEKDAYS:
        day += datetime.timedelta(days=1)
    return split, day


def _settings_row(name):
    cfg, default = core.EXERCISE_CONFIG[name], core.DEFAULT_EXERCISE_CONFIG[name]
    return {
        "exercise": name,
        "ceiling": cfg.get("ceiling"),
        "step": cfg.get("step"),
        "max_weight": cfg.get("max_weight"),
        "strict": bool(cfg.get("strict_ceiling")),
        "changed": [k for k in ("ceiling", "step", "max_weight") if cfg.get(k) != default.get(k)],
    }


def _resolve_exercise(raw):
    name = core.match_exercise_config(raw or "")
    if not name:
        abort(400, description=f"Couldn't find an exercise matching '{raw}'. Check the spelling.")
    return name


def _run_captured(fn):
    """Run a long job, return what it printed (shown on the page as a log)."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        fn()
    return buf.getvalue().strip()


# ---------------------------------------------------------------------------
# PIN check for every /api call
# ---------------------------------------------------------------------------
@app.before_request
def check_pin():
    if APP_PIN and request.path.startswith("/api/") and request.path != "/api/ping":
        if request.headers.get("X-Pin", "") != APP_PIN:
            return jsonify(error="PIN needed", pin_required=True), 401


@app.errorhandler(400)
def bad_request(e):
    return jsonify(error=e.description), 400


@app.errorhandler(Exception)
def server_error(e):
    if hasattr(e, "code") and hasattr(e, "description"):
        return jsonify(error=e.description), e.code
    app.logger.exception(e)
    return jsonify(error=f"{type(e).__name__}: {e}"), 500


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------
WEB_DIR = os.path.join(BASE_DIR, "web")


@app.route("/")
def index():
    return send_from_directory(WEB_DIR, "index.html")


@app.route("/<path:filename>")
def static_files(filename):
    return send_from_directory(WEB_DIR, filename)


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------
@app.route("/api/ping")
def ping():
    return jsonify(ok=True, pin_required=bool(APP_PIN))


@app.route("/api/next")
def api_next():
    split, day = next_workout()
    if not split:
        abort(400, description="Couldn't read your workout history yet.")
    data = build_split(split)
    data["date"] = day.isoformat()
    data["date_label"] = day.strftime("%A, %b %d")
    data["is_today"] = day == datetime.date.today()
    return jsonify(data)


@app.route("/api/splits")
def api_splits():
    return jsonify([build_split(s) for s in SPLITS])


@app.route("/api/split/<path:key>")
def api_split(key):
    split = _split_by_key(key)
    if not split:
        abort(400, description=f"Unknown split '{key}'.")
    return jsonify(build_split(split))


@app.route("/api/exercises")
def api_exercises():
    return jsonify(list(core.EXERCISE_CONFIG.keys()))


@app.route("/api/stats")
def api_stats():
    name = _resolve_exercise(request.args.get("exercise"))
    df = cached_load_csv()
    sessions = engine.get_exercise_sessions(df, name, max_sessions=100)
    if sessions.empty:
        abort(400, description=f"No sessions logged yet for {name}.")
    trend = engine.analyze_trend(sessions)
    rows = []
    for _, s in sessions.iterrows():
        rows.append({
            "date": s["session_date"].isoformat(),
            "workout": str(s["title"]).strip(),
            "top": float(s["max_weight"]),
            "volume": round(float(s["total_volume"])),
            "sets": [[float(w), int(r)] for w, r in zip(s["weights_list"], s["reps_list"])],
        })
    return jsonify({
        "exercise": name,
        "best": trend["best_weight_ever"],
        "change": trend["weight_change_total"],
        "sessions_analyzed": trend["sessions_analyzed"],
        "momentum": trend["momentum_label"],
        "plateau": trend["plateau_sessions"] if trend["plateau_detected"] else 0,
        "sessions": rows,
    })


@app.route("/api/substitute", methods=["POST"])
def api_substitute():
    body = request.get_json(silent=True) or {}
    old = _resolve_exercise(body.get("from"))
    new = _resolve_exercise(body.get("to"))
    df = cached_load_csv()
    sessions = engine.get_exercise_sessions(df, new, max_sessions=30)
    if sessions.empty:
        abort(400, description=f"No history for {new} yet, so there's nothing to base targets on.")
    trend = engine.analyze_trend(sessions)
    target = engine.compute_next_target(new, sessions, core.EXERCISE_CONFIG[new], trend)
    if not target:
        abort(400, description=f"Couldn't compute targets for {new}.")
    return jsonify({"from": old, "to": new, "target": _target_to_json(target)})


@app.route("/api/weekly")
def api_weekly():
    return jsonify(text=weekly_summary.build_weekly_summary())


@app.route("/api/settings")
def api_settings():
    return jsonify([_settings_row(n) for n in core.EXERCISE_CONFIG])


SETTING_LIMITS = {"ceiling": (1, 50), "step": (0.25, 50), "max_weight": (1, 500)}


@app.route("/api/settings", methods=["POST"])
def api_set_setting():
    body = request.get_json(silent=True) or {}
    name = _resolve_exercise(body.get("exercise"))
    key = body.get("key")
    if key not in SETTING_LIMITS:
        abort(400, description="Unknown setting.")
    value = body.get("value")
    if value in (None, ""):
        if key != "max_weight":
            abort(400, description="Only the max weight can be cleared.")
        value = None
    else:
        try:
            value = float(value)
        except (TypeError, ValueError):
            abort(400, description="Enter a number.")
        low, high = SETTING_LIMITS[key]
        if not low <= value <= high:
            abort(400, description=f"Must be between {low:g} and {high:g}.")
        if key == "ceiling":
            if value != int(value):
                abort(400, description="The rep ceiling must be a whole number.")
            value = int(value)
    core.update_exercise_setting(name, key, value)
    return jsonify(_settings_row(name))


@app.route("/api/settings/reset", methods=["POST"])
def api_reset_settings():
    body = request.get_json(silent=True) or {}
    name = _resolve_exercise(body.get("exercise"))
    core.reset_exercise_settings(name)
    return jsonify(_settings_row(name))


@app.route("/api/sync", methods=["POST"])
def api_sync():
    if not _job_lock.acquire(blocking=False):
        abort(409, description="A sync or training run is already going.")
    try:
        log = _run_captured(core.main)
    finally:
        _job_lock.release()
    return jsonify(ok=True, log=log or "Done.")


def coach_context():
    """Everything the AI coach should know, as one text block."""
    import coach
    split, day = next_workout()
    next_split = None
    if split:
        next_split = dict(build_split(split), date_label=day.strftime("%A, %b %d"))
    splits = [build_split(s) for s in SPLITS]

    df = cached_load_csv()
    history = {}
    for s in splits:
        for e in s["exercises"]:
            name = e["exercise"]
            if name in history:
                continue
            sessions = engine.get_exercise_sessions(df, name, max_sessions=4)
            history[name] = [{
                "date": row["session_date"].isoformat(),
                "workout": str(row["title"]).strip(),
                "sets": [[float(w), int(r)] for w, r in zip(row["weights_list"], row["reps_list"])],
                "rpe": None if row["max_rpe"] != row["max_rpe"] else float(row["max_rpe"]),  # NaN check
                "notes": str(row["exercise_notes"]) if isinstance(row["exercise_notes"], str) else "",
            } for _, row in sessions.iterrows()]

    settings = [_settings_row(n) for n in core.EXERCISE_CONFIG]
    return coach.build_context(next_split, splits, settings, weekly_summary.build_weekly_summary(), history)


@app.route("/api/coach/status")
def api_coach_status():
    return jsonify(enabled=bool(os.getenv("GEMINI_API_KEY", "").strip()))


@app.route("/api/coach", methods=["POST"])
def api_coach():
    import coach
    body = request.get_json(silent=True) or {}
    try:
        reply = coach.ask_coach(body.get("messages"), coach_context())
    except coach.CoachError as e:
        if str(e) == "NO_KEY":
            return jsonify(error="The coach needs a Gemini API key. See the setup steps on the Coach tab.", no_key=True), 400
        return jsonify(error=str(e)), 400
    return jsonify(reply=reply)


AUTOSYNC_MINUTES = float(os.getenv("AUTOSYNC_MINUTES", "5"))
_last_autosync = 0.0


def _csv_version():
    try:
        st = os.stat(engine.CSV_FILE)
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return None


@app.route("/api/autosync", methods=["POST"])
def api_autosync():
    """Called by the page every time it opens. Pulls from Hevy at most once every
    AUTOSYNC_MINUTES and tells the page whether a new workout came in."""
    global _last_autosync
    import time
    if not core.HEVY_API_KEY:
        return jsonify(synced=False, changed=False, reason="no Hevy key")
    if time.time() - _last_autosync < AUTOSYNC_MINUTES * 60:
        return jsonify(synced=False, changed=False, reason="synced recently")
    if not _job_lock.acquire(blocking=False):
        return jsonify(synced=False, changed=False, reason="busy")
    try:
        before = _csv_version()
        log = _run_captured(core.main)
        _last_autosync = time.time()
    finally:
        _job_lock.release()
    return jsonify(synced=True, changed=_csv_version() != before, log=log)


def start_background_sync():
    """Sync from Hevy every SYNC_EVERY_MINUTES (off by default)."""
    minutes = float(os.getenv("SYNC_EVERY_MINUTES", "0") or 0)
    if minutes <= 0 or not core.HEVY_API_KEY:
        return

    def loop():
        import time
        while True:
            time.sleep(minutes * 60)
            if _job_lock.acquire(blocking=False):
                try:
                    core.main()
                except Exception as e:  # keep the loop alive whatever happens
                    print(f"⚠️ Background sync failed: {e}")
                finally:
                    _job_lock.release()

    threading.Thread(target=loop, daemon=True).start()
    print(f"   Background sync: every {minutes:g} min")


@app.route("/api/train", methods=["POST"])
def api_train():
    if not _job_lock.acquire(blocking=False):
        abort(409, description="A sync or training run is already going.")
    try:
        from train_ai import train_all_models
        log = _run_captured(train_all_models)
    finally:
        _job_lock.release()
    return jsonify(ok=True, log=log or "Done.")


# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import socket
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        lan_ip = s.getsockname()[0]
        s.close()
    except OSError:
        lan_ip = "your-laptop-ip"
    print("💪 Zero RIR web app is running")
    print(f"   On this laptop:  http://localhost:{PORT}")
    print(f"   On your phone:   http://{lan_ip}:{PORT}  (same Wi-Fi)")
    print("   PIN protection: " + ("on" if APP_PIN else "off (add APP_PIN to .env to turn it on)"))
    cached_load_csv()  # warm the cache so the first page load is fast
    start_background_sync()
    # Flask's built-in server is plenty for one person; threaded so a long
    # sync doesn't freeze the rest of the page
    app.run(host="0.0.0.0", port=PORT, threaded=True)
