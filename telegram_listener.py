"""
Telegram Listener — Handles /next and /help commands via Telegram bot.

Uses the CSV trend engine to generate workout blueprints on demand.
"""

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
        
    # Monday=0, Tuesday=1, Wednesday=2, Thursday=3, Friday=4, Saturday=5, Sunday=6
    valid_weekdays = [1, 2, 5, 6]
    
    current_date = max(datetime.date.today(), last_date + datetime.timedelta(days=1))
    
    while current_date.weekday() not in valid_weekdays:
        current_date += datetime.timedelta(days=1)
        
    return next_title, current_date


def get_targets(next_title):
    """Generates targets for the predicted workout using the CSV trend engine."""
    from csv_trend_engine import generate_full_blueprint
    
    # Generate fresh blueprint from CSV history
    msg, targets = generate_full_blueprint(next_title)
    
    if msg:
        return msg
    
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
        f"🗓 *Your Next Scheduled Workout:*\n"
        f"*{next_title}*\n"
        f"📅 Date: {next_date.strftime('%A, %b %d')}\n\n"
        f"{targets_text}"
    )
    
    bot.reply_to(message, response, parse_mode="Markdown")

@bot.message_handler(commands=['start', 'help'])
def handle_help(message):
    bot.reply_to(message, (
        "💪 *Zero RIR Bot Commands:*\n\n"
        "/next — See your next workout, date, and full targets\n"
        "/help — Show this message"
    ), parse_mode="Markdown")

if __name__ == "__main__":
    print("🤖 Telegram Listener is running... Waiting for commands.")
    bot.infinity_polling()
