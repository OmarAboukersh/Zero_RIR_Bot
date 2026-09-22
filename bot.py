import requests
import json
import os
import math
import datetime

def normalize_name(name):
    return name.lower().strip()

def match_exercise_config(hevy_name):
    hevy_norm = normalize_name(hevy_name)
    for config_name in EXERCISE_CONFIG.keys():
        if normalize_name(config_name) in hevy_norm or hevy_norm in normalize_name(config_name):
            return config_name
    return None

from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

# --- CREDENTIALS ---
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
HEVY_API_KEY = os.getenv("HEVY_API_KEY", "")

STATE_FILE = "exercise_history.json"

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
    "Triceps Pushdown": {"ceiling": 15, "step": 2.5},
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

def load_history():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {}

def save_history(history):
    with open(STATE_FILE, "w") as f:
        json.dump(history, f, indent=2)

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

def evaluate_exercise(name, current_weight, sets_data, config, prior_sessions, date_str):
    ceiling = config["ceiling"]
    step = config["step"]
    
    prior = prior_sessions[-1] if prior_sessions else None
    
    rirs = [s.get('rir', 0) for s in sets_data]
    last_set_rir = rirs[-1] if rirs else 0
    high_rir_count = sum(1 for r in rirs if r >= 2)
    
    total_reps = sum(s['reps'] for s in sets_data)
    regression_streak = prior.get("regression_streak", 0) if prior else 0
    
    if prior and prior.get("weight") == current_weight:
        rep_drop = sum(prior.get("reps", [])) - total_reps
        if rep_drop >= 3:
            regression_streak += 1
        else:
            regression_streak = 0
    else:
        regression_streak = 0

    deload = (last_set_rir >= 2) or (high_rir_count >= math.ceil(len(rirs) / 2)) or (regression_streak >= 2)
    
    historical_sets = len(prior.get("reps", [])) if prior else 0
    target_sets = len(sets_data)
    dropped_volume = target_sets < historical_sets

    if deload:
        next_weight = math.floor((current_weight * 0.9) / step) * step
        result = {
            "exercise": name, 
            "next_weight": next_weight, 
            "target_sets": 1,
            "target_reps": f"{ceiling} (Submaximal, 3-4 RIR)", 
            "rationale": "⚠️ Deload triggered (Fatigue or mult-session regression). Load dropped 10%, volume slashed."
        }
        regression_streak = 0
        
    elif all(s['reps'] >= ceiling and s.get('rir', 0) == 0 for s in sets_data):
        bottom_of_range = 5 if ceiling == 8 else 10
        result = {
            "exercise": name, 
            "next_weight": current_weight + step,
            "target_sets": target_sets, 
            "target_reps": str(bottom_of_range),
            "rationale": f"Ceiling ({ceiling}) cleared at 0 RIR. Load mathematically increased."
        }
        
    else:
        target_reps_list = [str(min(s['reps'] + 1, ceiling)) for s in sets_data]
        result = {
            "exercise": name, 
            "next_weight": current_weight, 
            "target_sets": target_sets,
            "target_reps": ", ".join(target_reps_list), 
            "rationale": "Ceiling not met across all sets. Hold load, add 1 rep."
        }

    if dropped_volume:
        result["rationale"] += "\n   └ ⚠️ Volume dropped — fewer sets than planned."

    new_record = {
        "date": date_str,
        "weight": current_weight, 
        "reps": [s['reps'] for s in sets_data],
        "rir": rirs,
        "regression_streak": regression_streak
    }
    
    return result, new_record

def format_telegram_message(workout_name, workout_plan):
    """Formats the final blueprint text with an increase summary."""
    increases = [item for item in workout_plan if "mathematically increased" in item['rationale']]
    
    message = f"🚨 **Next '{workout_name}' Targets (0 RIR)** 🚨\n\n"
    
    if increases:
        message += "📈 **WEIGHT INCREASES:**\n"
        for item in increases:
            message += f"• {item['exercise']} ➡️ **{item['next_weight']}kg**\n"
        message += "\n"
        message += "━━━━━━━━━━━━━━━━━━\n\n"
        
    message += "📋 **DETAILED TARGETS:**\n\n"
    
    for item in workout_plan:
        message += f"**{item['exercise']}**\n"
        message += f"🎯 Target: {item['next_weight']}kg for {item['target_sets']} sets ({item['target_reps']} reps)\n"
        message += f"💡 {item['rationale']}\n\n"
        
    return message

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

def main():
    recent_workout = get_latest_workout()
    if not recent_workout:
        return

    workout_id = str(recent_workout.get('id', ''))
    last_id = get_last_processed_id()
    
    if workout_id == last_id and workout_id != '':
        return

    workout_name = recent_workout.get('name', recent_workout.get('title', 'Workout'))
    
    # --- AI NLP: Read Hevy notes for deload commands ---
    workout_notes = str(recent_workout.get('description', '')).lower()
    is_intentional_deload = "deload" in workout_notes

    exercises = recent_workout.get('exercises', [])
    history = load_history()
    next_plan = []
    
    date_str = datetime.datetime.now().strftime("%Y-%m-%d")
    
    for ex in exercises:
        raw_name = ex.get('title', ex.get('exercise', {}).get('title', 'Unknown Exercise')).strip()
        name = match_exercise_config(raw_name)
        
        if not name:
            continue
            
        sets_data = [s for s in ex.get('sets', []) if s.get('type', 'normal') == 'normal']
        if not sets_data:
            continue
            
        current_weight = max((s.get('weight_kg', 0) for s in sets_data), default=0)
        
        past_sessions = history.get(name, [])
        if isinstance(past_sessions, dict):
            past_sessions = []
            
        # --- 1. THE MATH: Run your standard double-progression ---
        math_plan, new_record = evaluate_exercise(name, current_weight, sets_data, EXERCISE_CONFIG[name], past_sessions, date_str)
        
        past_sessions.append(new_record)
        history[name] = past_sessions[-8:]
        
        # --- 2. THE AI: Analyze JSON history for plateaus ---
        ai_verdict = ""
        
        if is_intentional_deload:
            ai_verdict = "🟢 AI: Intentional deload recognized from notes."
        elif len(history[name]) >= 4:
            try:
                def calc_vol(sess):
                    return sum(w * r for w, r in zip([sess.get('weight', 0)]*len(sess.get('reps', [])), sess.get('reps', [])))
                
                recent_avg_vol = (calc_vol(history[name][-1]) + calc_vol(history[name][-2])) / 2
                old_avg_vol = (calc_vol(history[name][-3]) + calc_vol(history[name][-4])) / 2
                
                if recent_avg_vol <= old_avg_vol and recent_avg_vol > 0:
                    ai_verdict = "🚨 AI: Plateau detected (Volume stagnation)."
                else:
                    ai_verdict = "📈 AI: Upward momentum confirmed."
            except Exception as e:
                ai_verdict = f"🔍 AI: Tracking volume trends... ({e})"
        else:
            ai_verdict = f"🔍 AI: Gathering baseline data (needs 4 sessions, has {len(history[name])})."

        math_plan['rationale'] += f"\n   └ {ai_verdict}"
        next_plan.append(math_plan)

    if next_plan:
        save_history(history)
        
        # 1. Format the blueprint text
        message_text = format_telegram_message(workout_name, next_plan)
        
        # 2. Send immediately
        send_telegram_message_text(message_text)
        
        # 3. Save to persistent memory for morning delivery
        save_upcoming_targets(workout_name, message_text)
        
        save_last_processed_id(workout_id)

if __name__ == "__main__":
    main()