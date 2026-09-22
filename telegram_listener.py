import os
import json
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

def get_last_workout_info():
    """Reads workouts.csv to find the last completed workout."""
    try:
        df = pd.read_csv("workouts.csv")
        df['start_time'] = pd.to_datetime(df['start_time'])
        # Sort by date and get the absolute latest workout
        latest_workout = df.sort_values('start_time').iloc[-1]
        
        last_title = latest_workout['title'].strip()
        last_date = latest_workout['start_time'].date()
        
        return last_title, last_date
    except Exception as e:
        print(f"Error reading CSV: {e}")
        return None, None

def calculate_next_workout(last_title, last_date):
    """Calculates the next workout and predicted date based on the Tue/Wed/Sat/Sun schedule."""
    # Find next workout in sequence
    try:
        current_idx = SPLIT_SEQUENCE.index(last_title)
        next_idx = (current_idx + 1) % len(SPLIT_SEQUENCE)
        next_title = SPLIT_SEQUENCE[next_idx]
    except ValueError:
        # Fallback if the last workout wasn't in the standard split
        next_title = "Unknown (Check your schedule)"
        
    # Calculate next valid training day (Tue=1, Wed=2, Sat=5, Sun=6)
    valid_weekdays = [1, 2, 5, 6]
    
    # Start checking from either TODAY, or the day after the last workout (if they trained today)
    current_date = max(datetime.date.today(), last_date + datetime.timedelta(days=1))
    
    # Fast forward to the next valid training day
    while current_date.weekday() not in valid_weekdays:
        current_date += datetime.timedelta(days=1)
        
    return next_title, current_date

def get_targets(next_title):
    """Loads the targets for the predicted workout from JSON."""
    key = TARGET_KEYS.get(next_title)
    if not key:
        return "⚠️ Targets not found for this workout."
        
    try:
        with open("upcoming_targets.json", "r", encoding="utf-8") as f:
            targets = json.load(f)
            return targets.get(key, "⚠️ Targets currently missing in JSON memory.")
    except Exception as e:
        return f"⚠️ Error loading targets: {e}"

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

if __name__ == "__main__":
    print("🤖 Telegram Listener is running... Waiting for /next command.")
    # Use infinite polling to keep the script alive and listening
    bot.infinity_polling()
