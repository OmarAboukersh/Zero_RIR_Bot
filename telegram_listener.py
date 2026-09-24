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

@bot.message_handler(commands=['sync'])
def handle_sync(message):
    print(f"Received /sync command from chat_id: {message.chat.id}")
    bot.reply_to(message, "🔄 Syncing with Hevy... This might take a moment.", parse_mode="Markdown")
    try:
        from bot import main as sync_bot
        sync_bot()
        bot.reply_to(message, "✅ Sync complete! Your workout history and targets are updated.", parse_mode="Markdown")
    except Exception as e:
        bot.reply_to(message, f"❌ Error during sync: {e}", parse_mode="Markdown")


@bot.message_handler(commands=['train'])
def handle_train(message):
    print(f"Received /train command from chat_id: {message.chat.id}")
    bot.reply_to(message, "🧠 Training AI models... Please wait.", parse_mode="Markdown")
    try:
        from train_ai import train_all_models
        train_all_models()
        bot.reply_to(message, "✅ Training complete! All machine learning models have been updated.", parse_mode="Markdown")
    except Exception as e:
        bot.reply_to(message, f"❌ Error during training: {e}", parse_mode="Markdown")


@bot.message_handler(commands=['substitute'])
def handle_substitute(message):
    print(f"Received /substitute command from chat_id: {message.chat.id}")
    args = message.text.replace("/substitute", "").strip()
    
    if "," not in args:
        bot.reply_to(message, "⚠️ Format: `/substitute [Current Exercise] , [New Exercise]`\nExample: `/substitute lateral raise cable, dumbbell`", parse_mode="Markdown")
        return
        
    ex1_raw, ex2_raw = args.split(",", 1)
    ex1_raw = ex1_raw.strip()
    ex2_raw = ex2_raw.strip()
    
    try:
        from bot import match_exercise_config
        from csv_trend_engine import get_exercise_targets
        
        ex1_name = match_exercise_config(ex1_raw)
        ex2_name = match_exercise_config(ex2_raw)
        
        if not ex1_name:
            bot.reply_to(message, f"⚠️ Couldn't autocorrect '{ex1_raw}'. Please check spelling.", parse_mode="Markdown")
            return
        if not ex2_name:
            bot.reply_to(message, f"⚠️ Couldn't autocorrect '{ex2_raw}'. Please check spelling.", parse_mode="Markdown")
            return
            
        target_str = get_exercise_targets(ex2_name)
        
        response = (
            f"🔄 *Substitution Active*\n\n"
            f"Swapping **{ex1_name}** ➡️ **{ex2_name}**\n\n"
            f"Here are your predicted targets for **{ex2_name}** based on your history:\n\n"
            f"{target_str}"
        )
        bot.reply_to(message, response, parse_mode="Markdown")
        
    except Exception as e:
        bot.reply_to(message, f"❌ Error during substitution: {e}", parse_mode="Markdown")

@bot.message_handler(commands=['stats'])
def handle_stats(message):
    print(f"Received /stats command from chat_id: {message.chat.id}")
    args = message.text.split(' ', 1)
    if len(args) < 2:
        bot.reply_to(message, "⚠️ Please specify an exercise. Example: `/stats Bench Press`", parse_mode="Markdown")
        return
    
    exercise_query = args[1].strip()
    
    try:
        from csv_trend_engine import load_csv, get_exercise_sessions, analyze_trend
        from bot import match_exercise_config
        
        exercise_name = match_exercise_config(exercise_query)
        if not exercise_name:
            bot.reply_to(message, f"⚠️ Couldn't find an exercise matching '{exercise_query}'. Check your spelling.", parse_mode="Markdown")
            return
            
        df = load_csv()
        sessions = get_exercise_sessions(df, exercise_name, max_sessions=100)
        
        if sessions.empty:
            bot.reply_to(message, f"⚠️ No session data found for '{exercise_name}'.", parse_mode="Markdown")
            return
            
        trend = analyze_trend(sessions)
        last_session = sessions.iloc[-1]
        
        weight_change = trend['weight_change_total']
        trend_emoji = "↗️" if weight_change > 0 else "↘️" if weight_change < 0 else "→"
        
        # Build vertical table for the last 10 sessions (Dates = Rows, Sets = Columns)
        recent_sessions = sessions.tail(10)
        
        max_sets = 0
        for r_list in recent_sessions['reps_list']:
            max_sets = max(max_sets, len(r_list))
            
        col_widths = [5] # "Date " is 5 chars
        for i in range(max_sets):
            col_widths.append(len(f"Set {i+1}"))
            
        cell_matrix = []
        for _, session_row in recent_sessions.iterrows():
            date_str = session_row['session_date'].strftime("%m/%d")
            row = [date_str]
            w_list = session_row['weights_list']
            r_list = session_row['reps_list']
            
            for set_idx in range(max_sets):
                if set_idx < len(r_list):
                    w = w_list[set_idx] if set_idx < len(w_list) else w_list[-1]
                    r = r_list[set_idx]
                    cell_str = f"{w:g}x{r}"
                else:
                    cell_str = "-"
                row.append(cell_str)
                col_widths[set_idx+1] = max(col_widths[set_idx+1], len(cell_str))
            cell_matrix.append(row)
            
        header_cells = ["Date".ljust(col_widths[0])]
        for i in range(max_sets):
            header_cells.append(f"Set {i+1}".ljust(col_widths[i+1]))
            
        table_rows = [" | ".join(header_cells)]
        for row in cell_matrix:
            padded_row = []
            for i, cell in enumerate(row):
                padded_row.append(cell.ljust(col_widths[i]))
            table_rows.append(" | ".join(padded_row))
            
        table_str = "\n".join(table_rows)
        
        response = (
            f"📊 **Stats: {exercise_name}**\n\n"
            f"• **All-Time Best:** {trend['best_weight_ever']}kg\n"
            f"• **Recent Trend:** {trend_emoji} {'+' if weight_change > 0 else ''}{weight_change}kg (over last {trend['sessions_analyzed']} sessions)\n"
            f"• **Momentum:** {trend['momentum_label']}\n\n"
            f"```text\n{table_str}\n```"
        )
        bot.reply_to(message, response, parse_mode="Markdown")
        
    except Exception as e:
        bot.reply_to(message, f"❌ Error fetching stats: {e}", parse_mode="Markdown")


@bot.message_handler(commands=['start', 'help'])
def handle_help(message):
    bot.reply_to(message, (
        "💪 *Zero RIR Bot Commands:*\n\n"
        "/next — See your next workout, date, and full targets\n"
        "/stats <exercise> — Check your progress and trend for a specific exercise\n"
        "/sync — Manually pull your latest workout from Hevy immediately\n"
        "/train — Force retrain the AI models with your latest data\n"
        "/help — Show this message"
    ), parse_mode="Markdown")

if __name__ == "__main__":
    print("🤖 Telegram Listener is running... Waiting for commands.")
    bot.infinity_polling()
