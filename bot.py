"""
Bot — Processes new Hevy workouts, appends to CSV, and generates trend-based targets.

Flow:
  1. Pull latest workout from Hevy API
  2. Append set data to workouts.csv
  3. Run the CSV trend engine to generate next targets
  4. Log the targets (and push them to your phone if ntfy is set up, see notify.py)
  5. Auto-retrain ML models if threshold reached
"""

import requests
import json
import os
import math
import re
import datetime
import joblib
import pandas as pd

def normalize_name(name):
    return name.lower().strip()

def match_exercise_config(hevy_name, fuzzy=True):
    """Match a name to an EXERCISE_CONFIG key.

    fuzzy=True autocorrects typos (for names typed in the web app). Hevy sync uses
    fuzzy=False: a fuzzy guess there once logged Leg Press as Calf Press, so an
    exercise that isn't in the list is saved under its own name instead.
    """
    hevy_norm = normalize_name(hevy_name)
    # 1. Exact match first (most reliable)
    for config_name in EXERCISE_CONFIG.keys():
        if normalize_name(config_name) == hevy_norm:
            return config_name
    # 2. Substring fallback (only if no exact match found), picking the closest name:
    #    "lat pulldown" -> "Lat Pulldown (Cable)", not "Reverse Grip Lat Pulldown (Cable)"
    inside_query = [c for c in EXERCISE_CONFIG if normalize_name(c) in hevy_norm]
    if inside_query:
        return max(inside_query, key=len)
    containing_query = [c for c in EXERCISE_CONFIG if hevy_norm in normalize_name(c)]
    if containing_query:
        return min(containing_query, key=len)
    # 3. Fuzzy match (autocorrect spelling errors)
    if not fuzzy:
        return None
    import difflib
    all_normalized = {normalize_name(k): k for k in EXERCISE_CONFIG.keys()}
    matches = difflib.get_close_matches(hevy_norm, all_normalized.keys(), n=1, cutoff=0.5)
    if matches:
        return all_normalized[matches[0]]
        
    return None


from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

# --- CREDENTIALS ---
HEVY_API_KEY = os.getenv("HEVY_API_KEY", "")

# --- IMMUTABLE EXERCISE POOL & CONFIGURATION ---
EXERCISE_CONFIG = {
    # Compounds (5-8 Rep Range)
    "Bench Press (Smith Machine)": {"ceiling": 8, "step": 2.5},
    "Incline Bench Press (Smith Machine)": {"ceiling": 8, "step": 2.5},
    # strict_ceiling: never ask for more than `ceiling` reps; once every set hits it,
    # the weight goes up no matter how hard it felt (RPE)
    "Squat (Smith Machine)": {"ceiling": 8, "step": 2.5, "strict_ceiling": True},
    "Leg Press (Machine)": {"ceiling": 8, "step": 10, "strict_ceiling": True},  # loads in 10kg (2x5kg plates)
    "T Bar Row": {"ceiling": 8, "step": 2.5},
    "Reverse Grip Lat Pulldown (Cable)": {"ceiling": 8, "step": 3.5},  # cable stack moves in 3.5kg plates
    "Lat Pulldown (Cable)": {"ceiling": 8, "step": 3.5},  # cable stack moves in 3.5kg plates
    "Seated Cable Row - V Grip (Cable)": {"ceiling": 8, "step": 3.5},  # cable stack moves in 3.5kg plates
    "Seated Shoulder Press (Machine)": {"ceiling": 8, "step": 2.5},
    
    # Isolations (10-15 Rep Range)
    "Lateral Raise (Cable)": {"ceiling": 15, "step": 2.5},
    "Lateral Raise (Dumbbell)": {"ceiling": 15, "step": 2.0},
    "Triceps Pushdown": {"ceiling": 8, "step": 3.5, "strict_ceiling": True},  # 8 reps max; 7kg stack plates + 3.5kg add-on = 3.5kg jumps
    "Triceps Extension (Dumbbell)": {"ceiling": 15, "step": 2.0},
    "Crunch (Weighted)": {"ceiling": 15, "step": 2.5},
    "Single Leg Extensions": {"ceiling": 15, "step": 2.5},
    "Rear Delt Reverse Fly (Machine)": {"ceiling": 15, "step": 2.5},
    "Back Extension (Weighted Hyperextension)": {"ceiling": 10, "step": 2.5},
    "Calf Press (Machine)": {"ceiling": 15, "step": 2.5, "max_weight": 130},  # machine maxed out
    "Calf Press Single Leg (Machine)": {"ceiling": 15, "step": 2.5},  # replaced the two-leg calf press (Oct 28)
    "Reverse Curl (Cable)": {"ceiling": 11, "step": 2.5},
    "Lying Leg Curl (Machine)": {"ceiling": 15, "step": 2.5, "max_weight": 100},  # machine maxed out
    "Lying Leg Curl Single Leg (Machine)": {"ceiling": 15, "step": 2.5},  # replaced the two-leg leg curl (Oct 25)
    "Shrug (Cable)": {"ceiling": 15, "step": 2.5, "max_weight": 120},  # machine maxed out
    "Preacher curl single arm (machine)": {"ceiling": 10, "step": 2.5},
    "Preacher Curl (Machine)": {"ceiling": 10, "step": 2.5},
    "Bicep Curl (Cable)": {"ceiling": 10, "step": 2.5}
}

# --- SETTINGS CHANGED FROM THE WEB APP ---
# The Settings tab saves overrides here; they are layered on
# top of EXERCISE_CONFIG above. A value of null removes that setting.
SETTINGS_FILE = "exercise_settings.json"
DEFAULT_EXERCISE_CONFIG = {name: dict(cfg) for name, cfg in EXERCISE_CONFIG.items()}


def _load_settings_overrides():
    if not os.path.exists(SETTINGS_FILE):
        return {}
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError) as e:
        print(f"⚠️ Couldn't read {SETTINGS_FILE}: {e}")
        return {}


def apply_settings_overrides():
    """Rebuild EXERCISE_CONFIG (in place) from the defaults plus saved overrides."""
    overrides = _load_settings_overrides()
    for name, defaults in DEFAULT_EXERCISE_CONFIG.items():
        cfg = dict(defaults)
        for key, value in overrides.get(name, {}).items():
            if value is None:
                cfg.pop(key, None)
            else:
                cfg[key] = value
        EXERCISE_CONFIG[name].clear()
        EXERCISE_CONFIG[name].update(cfg)


def update_exercise_setting(exercise_name, key, value):
    """Save one setting for an exercise (value None removes it) and apply it immediately."""
    overrides = _load_settings_overrides()
    overrides.setdefault(exercise_name, {})[key] = value
    with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(overrides, f, indent=2, ensure_ascii=False)
    apply_settings_overrides()


def reset_exercise_settings(exercise_name):
    """Drop all saved overrides for an exercise, going back to the defaults in this file."""
    overrides = _load_settings_overrides()
    overrides.pop(exercise_name, None)
    with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(overrides, f, indent=2, ensure_ascii=False)
    apply_settings_overrides()


apply_settings_overrides()

def get_recent_workouts():
    """Fetches the 10 most recently completed workouts from Hevy."""
    url = "https://api.hevyapp.com/v1/workouts"
    headers = {"api-key": HEVY_API_KEY, "Accept": "application/json"}
    
    try:
        response = requests.get(url, headers=headers, timeout=20)
    except requests.RequestException as e:
        print(f"❌ Couldn't reach Hevy: {e}")
        return []
    
    if response.status_code == 200:
        data = response.json()
        
        if isinstance(data, dict):
            if "workouts" in data and len(data["workouts"]) > 0:
                return data["workouts"]
            elif "data" in data and len(data["data"]) > 0:
                return data["data"]
            else:
                return []
                
        elif isinstance(data, list) and len(data) > 0:
            return data
            
    print(f"❌ Failed to pull from Hevy. Status Code: {response.status_code}")
    return []


from notify import notify


def save_upcoming_targets(workout_name, message_text):
    """Saves the formatted targets into a JSON memory state tagged by standard workout split."""
    target_file = "upcoming_targets.json"
    
    # Categorize exactly to match the user's weekly split keys:
    # "Push", "Pull", "Push + Quads", "Pull + Ham"
    w_lower = workout_name.lower()
    
    # Simple logic to determine the standard key:
    if "push" in w_lower and "quad" in w_lower:
        standard_key = "Push + Quads"
    elif "push" in w_lower:
        standard_key = "Push"
    elif "pull" in w_lower and "ham" in w_lower:
        standard_key = "Pull + Ham"
    elif "pull" in w_lower:
        standard_key = "Pull"
    else:
        standard_key = workout_name  # Fallback to the exact Hevy name

    # Load existing
    targets = {}
    if os.path.exists(target_file):
        try:
            with open(target_file, "r", encoding="utf-8") as f:
                targets = json.load(f)
        except:
            pass

    # Save new
    targets[standard_key] = message_text
    
    with open(target_file, "w", encoding="utf-8") as f:
        json.dump(targets, f, indent=4, ensure_ascii=False)
    
    print(f"💾 Saved targets for '{standard_key}' to {target_file}")


def get_last_processed_id():
    if os.path.exists("last_workout.txt"):
        with open("last_workout.txt", "r", encoding="utf-8") as f:
            return f.read().strip()
    return None

def save_last_processed_id(workout_id):
    with open("last_workout.txt", "w", encoding="utf-8") as f:
        f.write(workout_id)


def append_workout_to_csv(workout_name, workout_description, exercises_data, actual_time_str=None):
    """Append the current workout's set data to workouts.csv so the ML stays current."""
    csv_path = "workouts.csv"
    
    if actual_time_str:
        now_str = actual_time_str
    else:
        now_str = datetime.datetime.now().strftime("%b %d, %Y, %I:%M %p")
    rows = []
    
    for ex_name, sets_data, ex_notes in exercises_data:
        for i, s in enumerate(sets_data):
            rows.append({
                "title": workout_name,
                "start_time": now_str,
                "end_time": now_str,
                "description": workout_description or "",
                "exercise_title": ex_name,
                "superset_id": "",
                "exercise_notes": ex_notes,
                "set_index": i,
                "set_type": s.get('type', 'normal'),
                "weight_kg": s.get('weight_kg', ''),
                "reps": s.get('reps', ''),
                "distance_km": "",
                "duration_seconds": "",
                "rpe": s.get('rpe', '')
            })
    
    if not rows:
        return
    
    new_df = pd.DataFrame(rows)
    
    # Append (write header only if file doesn't exist)
    write_header = not os.path.exists(csv_path)
    new_df.to_csv(csv_path, mode='a', header=write_header, index=False)
    print(f"📝 Appended {len(rows)} sets to {csv_path}")


TRAIN_COUNTER_FILE = "train_counter.txt"
RETRAIN_EVERY_N = 10  # Retrain models after every N new workouts

def auto_retrain_if_needed():
    """Check if enough new workouts have been processed to trigger a retrain."""
    count = 0
    if os.path.exists(TRAIN_COUNTER_FILE):
        try:
            with open(TRAIN_COUNTER_FILE, "r") as f:
                count = int(f.read().strip())
        except:
            count = 0
    
    count += 1
    
    if count >= RETRAIN_EVERY_N:
        print(f"🔄 Auto-retrain triggered ({count} new workouts since last training)...")
        try:
            from train_ai import train_all_models
            train_all_models()
            count = 0  # Reset counter after successful retrain
        except Exception as e:
            print(f"⚠️ Auto-retrain failed: {e}")
    else:
        print(f"📊 ML retrain counter: {count}/{RETRAIN_EVERY_N}")
    
    with open(TRAIN_COUNTER_FILE, "w") as f:
        f.write(str(count))


SYNC_LOCK_FILE = "sync.lock"
SYNC_LOCK_STALE_SECONDS = 600  # a lock older than this is left over from a crash


def main():
    """Sync new Hevy workouts. Only one sync runs at a time, even across processes
    (the scheduled run_bot.bat and the web app's auto-sync), so a workout can't be
    appended twice."""
    import time
    try:
        if os.path.exists(SYNC_LOCK_FILE) and time.time() - os.path.getmtime(SYNC_LOCK_FILE) > SYNC_LOCK_STALE_SECONDS:
            os.remove(SYNC_LOCK_FILE)
        fd = os.open(SYNC_LOCK_FILE, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        print("⏳ Another sync is already running. Skipping this one.")
        return
    try:
        os.close(fd)
        _sync()
    finally:
        try:
            os.remove(SYNC_LOCK_FILE)
        except OSError:
            pass


def _sync():
    recent_workouts = get_recent_workouts()
    if not recent_workouts:
        return

    last_id = get_last_processed_id()
    
    workouts_to_process = []
    for w in recent_workouts:
        if str(w.get('id', '')) == last_id:
            break
        workouts_to_process.append(w)
        
    if not workouts_to_process:
        print("✅ No new workouts to process.")
        return
        
    workouts_to_process.reverse()
    
    for recent_workout in workouts_to_process:
        workout_name = recent_workout.get('name', recent_workout.get('title', 'Workout'))
        workout_description = str(recent_workout.get('description', ''))
        workout_id = str(recent_workout.get('id', ''))
        
        # Detect intentional deload from workout notes/description or title
        is_deload = 'deload' in workout_description.lower() or 'deload' in workout_name.lower()
        
        exercises = recent_workout.get('exercises', [])
        exercises_for_csv = []
        untracked = []
        
        for ex in exercises:
            raw_name = ex.get('title', ex.get('exercise', {}).get('title', 'Unknown Exercise')).strip()
            name = match_exercise_config(raw_name, fuzzy=False)
            
            if not name:
                # Not in EXERCISE_CONFIG: keep the data under its real Hevy name
                # rather than guessing a similar-looking exercise
                name = raw_name
                untracked.append(raw_name)
                
            ex_notes = str(ex.get('notes') or '').replace('\n', ' ')
            
            # Collect raw set data for CSV append
            raw_sets = ex.get('sets', [])
            exercises_for_csv.append((name, raw_sets, ex_notes))
    
        if exercises_for_csv:
            # 1. ALWAYS append workout data to CSV (even deloads, for complete history)
            w_time = pd.to_datetime(recent_workout.get('start_time'))
            actual_time_str = w_time.strftime("%b %d, %Y, %I:%M %p")
            
            append_workout_to_csv(workout_name, workout_description, exercises_for_csv, actual_time_str)
        
        if is_deload:
            # Deload detected — acknowledge it but DON'T generate targets from it
            # The deload data is in the CSV but the trend engine will filter it out
            deload_msg = (
                f"🟢 *Deload Logged: {workout_name}*\n\n"
                f"Your deload session has been recorded but will be excluded from "
                f"trend analysis and target generation.\n\n"
                f"Your next targets will be based on your last working session — "
                f"no pollution from the lighter weights. 💪"
            )
            notify(deload_msg, title="Deload logged")
            print(f"🟢 Deload workout detected and logged. Skipping target generation.")
        else:
            # 2. Generate trend-based targets from the full CSV history
            from csv_trend_engine import generate_full_blueprint
            message_text, targets = generate_full_blueprint(workout_name)
            
            if message_text:
                # 3. Log the new targets (phone notification if ntfy is set up)
                notify(message_text, title=f"Next {workout_name.strip()} targets")
                
                # 4. Save to upcoming_targets.json (cache of the latest targets)
                save_upcoming_targets(workout_name, message_text)
            else:
                print(f"⚠️ Trend engine returned no targets for '{workout_name}'")
        
        if untracked:
            names = ", ".join(sorted(set(untracked)))
            print(f"ℹ️ Not in EXERCISE_CONFIG, saved under own name: {names}")
            notify(
                f"ℹ️ Not in your exercise list (saved under its own name, no targets yet): {names}\n"
                f"Add it to EXERCISE_CONFIG in bot.py to start tracking it."
            )
        
        # 5. Auto-retrain ML models if enough new data has accumulated
        auto_retrain_if_needed()
        
        save_last_processed_id(workout_id)


if __name__ == "__main__":
    main()