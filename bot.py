import requests
import json
import os
import math

# --- CREDENTIALS ---
TELEGRAM_BOT_TOKEN = ""
TELEGRAM_CHAT_ID = ""
HEVY_API_KEY = ""

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

def evaluate_exercise(name, current_weight, sets_data, config, history):
    ceiling = config["ceiling"]
    step = config["step"]
    prior = history.get(name)

    max_rir = max(s.get('rir', 0) for s in sets_data)
    total_reps = sum(s['reps'] for s in sets_data)
    regression_streak = prior.get("regression_streak", 0) if prior else 0
    
    if prior and prior["weight"] == current_weight:
        rep_drop = sum(prior["reps"]) - total_reps
        if rep_drop >= 3:
            regression_streak += 1
        else:
            regression_streak = 0
    else:
        regression_streak = 0

    deload = max_rir >= 2 or regression_streak >= 2
    target_sets = len(sets_data)

    if deload:
        next_weight = math.floor((current_weight * 0.9) / step) * step
        result = {
            "exercise": name, 
            "next_weight": next_weight, 
            "target_sets": 1,
            "target_reps": f"{ceiling} (Submaximal, 3-4 RIR)", 
            "rationale": "⚠️ Deload triggered (RIR ≥ 2 or mult-session regression). Load dropped 10%, volume slashed."
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

    history[name] = {
        "weight": current_weight, 
        "reps": [s['reps'] for s in sets_data],
        "regression_streak": regression_streak
    }
    
    return result

def send_telegram_message(workout_name, workout_plan):
    """Formats and fires the final blueprint to Telegram with an increase summary."""
    
    # Separate the exercises that got a weight increase
    increases = [item for item in workout_plan if "mathematically increased" in item['rationale']]
    
    message = f"🚨 **Next '{workout_name}' Targets (0 RIR)** 🚨\n\n"
    
    # 1. The Top Summary (Only shows if you actually increased something)
    if increases:
        message += "📈 **WEIGHT INCREASES:**\n"
        for item in increases:
            message += f"• {item['exercise']} ➡️ **{item['next_weight']}kg**\n"
        message += "\n"
        message += "━━━━━━━━━━━━━━━━━━\n\n"
        
    message += "📋 **DETAILED TARGETS:**\n\n"
    
    # 2. The Full Breakdown ("Everything that's good")
    for item in workout_plan:
        message += f"**{item['exercise']}**\n"
        message += f"🎯 Target: {item['next_weight']}kg for {item['target_sets']} sets ({item['target_reps']} reps)\n"
        message += f"💡 {item['rationale']}\n\n"
        
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"}
    
    response = requests.post(url, json=payload)
    if response.status_code == 200:
        print("✅ Telegram notification successfully dispatched.")
    else:
        print(f"❌ Telegram Error: {response.text}")

def main():
    print("Initiating Zero RIR Progression Engine...")
    recent_workout = get_latest_workout()
    
    if not recent_workout:
        print("❌ No workout returned from Hevy.")
        return

    workout_name = recent_workout.get('name', recent_workout.get('title', 'Workout'))
    exercises = recent_workout.get('exercises', [])
    
    print(f"✅ Successfully pulled workout: '{workout_name}'")
    print(f"🔍 Found {len(exercises)} exercises in this workout. Checking them...")
    
    history = load_history()
    next_plan = []
    
    for ex in exercises:
        # Hevy API sometimes places the title directly, or nests it inside an 'exercise' object
        name = ex.get('title', ex.get('exercise', {}).get('title', 'Unknown Exercise')).strip()
        
        print(f"  -> Found in Hevy: '{name}'")
        
        if name not in EXERCISE_CONFIG:
            print(f"      ⚠️ Skipped: Name doesn't perfectly match EXERCISE_CONFIG.")
            continue
            
        sets_data = [s for s in ex.get('sets', []) if s.get('type', 'normal') == 'normal']
        if not sets_data:
            print(f"      ⚠️ Skipped: No normal working sets found.")
            continue
            
        current_weight = sets_data[0].get('weight_kg', 0)
        
        plan = evaluate_exercise(name, current_weight, sets_data, EXERCISE_CONFIG[name], history)
        next_plan.append(plan)
        print(f"      ✅ Successfully calculated progression!")

    if next_plan:
        save_history(history)
        send_telegram_message(workout_name, next_plan)
    else:
        print("\n❌ No messages to send. None of the exercises matched your config list.")

if __name__ == "__main__":
    main()