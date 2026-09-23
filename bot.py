"""
Bot — Processes new Hevy workouts, appends to CSV, and generates trend-based targets.

Flow:
  1. Pull latest workout from Hevy API
  2. Append set data to workouts.csv
  3. Run the CSV trend engine to generate next targets
  4. Send targets via Telegram
  5. Auto-retrain ML models if threshold reached
"""

import requests
import json
import os
import math
import re
import datetime
import joblib

def normalize_name(name):
    return name.lower().strip()

def match_exercise_config(hevy_name):
    hevy_norm = normalize_name(hevy_name)
    # 1. Exact match first (most reliable)
    for config_name in EXERCISE_CONFIG.keys():
        if normalize_name(config_name) == hevy_norm:
            return config_name
    # 2. Substring fallback (only if no exact match found)
    for config_name in EXERCISE_CONFIG.keys():
        config_norm = normalize_name(config_name)
        if config_norm in hevy_norm or hevy_norm in config_norm:
            return config_name
    return None


from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

# --- CREDENTIALS ---
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
HEVY_API_KEY = os.getenv("HEVY_API_KEY", "")

# --- IMMUTABLE EXERCISE POOL & CONFIGURATION ---
EXERCISE_CONFIG = {
    # Compounds (5-8 Rep Range)
    "Bench Press (Smith Machine)": {"ceiling": 8, "step": 2.5},
    "Incline Bench Press (Smith Machine)": {"ceiling": 8, "step": 2.5},
    "Squat (Smith Machine)": {"ceiling": 8, "step": 2.5},
    "T Bar Row": {"ceiling": 8, "step": 2.5},
    "Reverse Grip Lat Pulldown (Cable)": {"ceiling": 8, "step": 2.5},
    "Lat Pulldown (Cable)": {"ceiling": 8, "step": 2.5},
    "Seated Cable Row - V Grip (Cable)": {"ceiling": 8, "step": 2.5},
    "Seated Shoulder Press (Machine)": {"ceiling": 8, "step": 2.5},
    
    # Isolations (10-15 Rep Range)
    "Lateral Raise (Cable)": {"ceiling": 15, "step": 2.5},
    "Lateral Raise (Dumbbell)": {"ceiling": 15, "step": 2.0},
    "Triceps Pushdown": {"ceiling": 15, "step": 5.0},
    "Triceps Extension (Dumbbell)": {"ceiling": 15, "step": 2.0},
    "Crunch (Weighted)": {"ceiling": 15, "step": 2.5},
    "Single Leg Extensions": {"ceiling": 15, "step": 2.5},
    "Rear Delt Reverse Fly (Machine)": {"ceiling": 15, "step": 2.5},
    "Back Extension (Weighted Hyperextension)": {"ceiling": 15, "step": 2.5},
    "Calf Press (Machine)": {"ceiling": 15, "step": 2.5},
    "Reverse Curl (Cable)": {"ceiling": 15, "step": 2.5},
    "Lying Leg Curl (Machine)": {"ceiling": 15, "step": 2.5},
    "Shrug (Cable)": {"ceiling": 15, "step": 2.5},
    "Preacher curl single arm (machine)": {"ceiling": 15, "step": 2.5},
    "Preacher Curl (Machine)": {"ceiling": 15, "step": 2.5}
}

def get_latest_workout():
    """Fetches the most recently completed workout from Hevy."""
    url = "https://api.hevyapp.com/v1/workouts"
    headers = {"api-key": HEVY_API_KEY, "Accept": "application/json"}
    
    response = requests.get(url, headers=headers)
    
    if response.status_code == 200:
        data = response.json()
        
        if isinstance(data, dict):
            if "workouts" in data and len(data["workouts"]) > 0:
                return data["workouts"][0]
            elif "data" in data and len(data["data"]) > 0:
                return data["data"][0]
            else:
                print(f"⚠️ Unexpected Hevy format: {data}")
                return None
                
        elif isinstance(data, list) and len(data) > 0:
            return data[0]
            
    print(f"❌ Failed to pull from Hevy. Status Code: {response.status_code}")
    return None


def send_telegram_message_text(message):
    """Fires the exact message text to Telegram."""
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"}
    
    response = requests.post(url, json=payload)
    if response.status_code == 200:
        print("✅ Telegram notification successfully dispatched.")
    else:
        print(f"❌ Telegram Error: {response.text}")


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


def append_workout_to_csv(workout_name, workout_description, exercises_data):
    """Append the current workout's set data to workouts.csv so the ML stays current."""
    csv_path = "workouts.csv"
    
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
    
    import pandas as pd
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


def main():
    recent_workout = get_latest_workout()
    if not recent_workout:
        return

    workout_id = str(recent_workout.get('id', ''))
    last_id = get_last_processed_id()
    
    if workout_id == last_id and workout_id != '':
        return

    workout_name = recent_workout.get('name', recent_workout.get('title', 'Workout'))
    workout_description = str(recent_workout.get('description', ''))
    
    # Detect intentional deload from workout notes/description or title
    is_deload = 'deload' in workout_description.lower() or 'deload' in workout_name.lower()
    
    exercises = recent_workout.get('exercises', [])
    exercises_for_csv = []
    
    for ex in exercises:
        raw_name = ex.get('title', ex.get('exercise', {}).get('title', 'Unknown Exercise')).strip()
        name = match_exercise_config(raw_name)
        
        if not name:
            continue
            
        ex_notes = str(ex.get('notes', '')).replace('\n', ' ')
        
        # Collect raw set data for CSV append
        raw_sets = ex.get('sets', [])
        exercises_for_csv.append((name, raw_sets, ex_notes))

    if exercises_for_csv:
        # 1. ALWAYS append workout data to CSV (even deloads, for complete history)
        append_workout_to_csv(workout_name, workout_description, exercises_for_csv)
        
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
            send_telegram_message_text(deload_msg)
            print(f"🟢 Deload workout detected and logged. Skipping target generation.")
        else:
            # 2. Generate trend-based targets from the full CSV history
            from csv_trend_engine import generate_full_blueprint
            message_text, targets = generate_full_blueprint(workout_name)
            
            if message_text:
                # 3. Send immediately via Telegram
                send_telegram_message_text(message_text)
                
                # 4. Save to upcoming_targets.json (as cache for telegram_listener)
                save_upcoming_targets(workout_name, message_text)
            else:
                print(f"⚠️ Trend engine returned no targets for '{workout_name}'")
        
        # 5. Auto-retrain ML models if enough new data has accumulated
        auto_retrain_if_needed()
        
        save_last_processed_id(workout_id)


if __name__ == "__main__":
    main()