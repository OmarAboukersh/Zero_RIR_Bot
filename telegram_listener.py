import os
import json
import math
import datetime
import pandas as pd
import telebot
from dotenv import load_dotenv

# Load env
load_dotenv()
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")

if not TELEGRAM_BOT_TOKEN:
    print("❌ ERROR: TELEGRAM_BOT_TOKEN missing in .env")
    exit(1)

bot = telebot.TeleBot(TELEGRAM_BOT_TOKEN)

# Standard sequence matching the user's weekly split
SPLIT_SEQUENCE = ["Push day", "Pull day", "push + quad", "Pull + ham"]

# Map exact Hevy names to upcoming_targets.json keys
TARGET_KEYS = {
    "Push day": "Push",
    "Pull day": "Pull",
    "push + quad": "Push + Quads",
    "Pull + ham": "Pull + Ham"
}

# Import EXERCISE_CONFIG from bot.py for ceiling/step values
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from bot import EXERCISE_CONFIG, match_exercise_config

def get_last_workout_info():
    """Reads workouts.csv to find the last completed workout."""
    try:
        df = pd.read_csv("workouts.csv")
        df['start_time'] = pd.to_datetime(df['start_time'])
        latest_workout = df.sort_values('start_time').iloc[-1]
        
        last_title = latest_workout['title'].strip()
        last_date = latest_workout['start_time'].date()
        
        return last_title, last_date
    except Exception as e:
        print(f"Error reading CSV: {e}")
        return None, None

def calculate_next_workout(last_title, last_date):
    """Calculates the next workout and predicted date based on the Tue/Wed/Sat/Sun schedule."""
    try:
        current_idx = SPLIT_SEQUENCE.index(last_title)
        next_idx = (current_idx + 1) % len(SPLIT_SEQUENCE)
        next_title = SPLIT_SEQUENCE[next_idx]
    except ValueError:
        next_title = "Unknown (Check your schedule)"
        
    # Tue=0, Wed=1, Sat=4, Sun=5 ... wait, Monday=0 in Python
    # Monday=0, Tuesday=1, Wednesday=2, Thursday=3, Friday=4, Saturday=5, Sunday=6
    valid_weekdays = [1, 2, 5, 6]
    
    current_date = max(datetime.date.today(), last_date + datetime.timedelta(days=1))
    
    while current_date.weekday() not in valid_weekdays:
        current_date += datetime.timedelta(days=1)
        
    return next_title, current_date

def generate_blueprint_from_csv(split_name):
    """Builds a full workout blueprint from CSV history when no real targets exist.
    
    Finds the most recent session of the given split, then for each exercise
    applies the same double-progression logic that bot.py uses to predict next targets.
    """
    try:
        df = pd.read_csv("workouts.csv")
        df['start_time'] = pd.to_datetime(df['start_time'])
        
        # Get all rows for this split, excluding deload sessions
        split_df = df[df['title'].str.strip() == split_name].copy()
        if 'description' in split_df.columns:
            split_df = split_df[~split_df['description'].fillna('').str.lower().str.contains('deload')]
        if split_df.empty:
            return None
        
        # Find the most recent NON-deload session date for this split
        latest_date = split_df['start_time'].max()
        latest_session = split_df[split_df['start_time'] == latest_date]
        
        exercises = []
        increases = []
        
        # Group by exercise within that session
        for ex_name, ex_group in latest_session.groupby('exercise_title', sort=False):
            # Only process exercises we track in EXERCISE_CONFIG
            matched_name = match_exercise_config(ex_name.strip())
            if not matched_name:
                continue
            
            config = EXERCISE_CONFIG[matched_name]
            ceiling = config["ceiling"]
            step = config["step"]
            
            # Get normal sets only
            normal_sets = ex_group[ex_group['set_type'] == 'normal']
            if normal_sets.empty:
                continue
            
            weight = normal_sets['weight_kg'].max()
            reps_list = normal_sets['reps'].dropna().astype(int).tolist()
            num_sets = len(reps_list)
            
            if not reps_list or weight == 0:
                continue
            
            # Apply double-progression logic
            all_hit_ceiling = all(r >= ceiling for r in reps_list)
            
            if all_hit_ceiling:
                next_weight = weight + step
                bottom = 5 if ceiling == 8 else 10
                target_reps = str(bottom)
                rationale = "✅ Ceiling cleared. Load increased."
                increases.append({"exercise": matched_name, "next_weight": next_weight})
            else:
                next_weight = weight
                target_reps = ", ".join(str(min(r + 1, ceiling)) for r in reps_list)
                rationale = "🔄 Ceiling not met. Add 1 rep."
            
            exercises.append({
                "name": matched_name,
                "weight": next_weight,
                "sets": num_sets,
                "reps": target_reps,
                "rationale": rationale
            })
        
        if not exercises:
            return None
        
        # Build the message
        msg = f"🚨 **Next '{split_name}' Targets (0 RIR)** 🚨\n"
        msg += f"_(Generated from your last {split_name} session)_\n\n"
        
        if increases:
            msg += "📈 **WEIGHT INCREASES:**\n"
            for inc in increases:
                msg += f"• {inc['exercise']} ➡️ **{inc['next_weight']}kg**\n"
            msg += "\n━━━━━━━━━━━━━━━━━━\n\n"
        
        msg += "📋 **DETAILED TARGETS:**\n\n"
        for ex in exercises:
            msg += f"**{ex['name']}**\n"
            msg += f"🎯 Target: {ex['weight']}kg for {ex['sets']} sets ({ex['reps']} reps)\n"
            msg += f"💡 {ex['rationale']}\n\n"
        
        return msg
        
    except Exception as e:
        print(f"Error generating blueprint from CSV: {e}")
        return None

def get_targets(next_title):
    """Loads the targets for the predicted workout from JSON.
    Falls back to generating from CSV history if no real targets exist."""
    key = TARGET_KEYS.get(next_title)
    if not key:
        return "⚠️ Targets not found for this workout."
    
    # Try loading from upcoming_targets.json first
    try:
        with open("upcoming_targets.json", "r", encoding="utf-8") as f:
            targets = json.load(f)
        stored = targets.get(key, "")
        
        # Check if stored target is real (not a test placeholder)
        if stored and "Target:" in stored:
            return stored
    except:
        pass
    
    # Fallback: generate from CSV history
    blueprint = generate_blueprint_from_csv(next_title)
    if blueprint:
        return blueprint
    
    return "⚠️ No targets available yet. Complete this workout once and the bot will compute your targets."

@bot.message_handler(commands=['next'])
def handle_next(message):
    print(f"Received /next command from chat_id: {message.chat.id}")
    
    last_title, last_date = get_last_workout_info()
    
    if not last_title:
        bot.reply_to(message, "⚠️ Couldn't read your workout history. Have you processed any workouts yet?")
        return
        
    next_title, next_date = calculate_next_workout(last_title, last_date)
    targets_text = get_targets(next_title)
    
    response = (
        f"🗓 **Your Next Scheduled Workout:**\n"
        f"**{next_title}**\n"
        f"📅 Date: {next_date.strftime('%A, %b %d')}\n\n"
        f"{targets_text}"
    )
    
    bot.reply_to(message, response, parse_mode="Markdown")

@bot.message_handler(commands=['start', 'help'])
def handle_help(message):
    bot.reply_to(message, (
        "💪 **Zero RIR Bot Commands:**\n\n"
        "/next — See your next workout, date, and full targets\n"
        "/help — Show this message"
    ), parse_mode="Markdown")

if __name__ == "__main__":
    print("🤖 Telegram Listener is running... Waiting for commands.")
    bot.infinity_polling()

