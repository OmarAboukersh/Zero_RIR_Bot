"""
Morning Delivery — Sends today's workout blueprint via Telegram.

Generates targets LIVE from workouts.csv using the CSV trend engine.
No dependency on upcoming_targets.json or stale cache files.
"""

import os
import sys
import datetime
import requests
from dotenv import load_dotenv

# Ensure UTF-8 output on Windows
sys.stdout.reconfigure(encoding='utf-8')


def send_telegram_message(message_text):
    """Sends the formatted text to Telegram using credentials from .env."""
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    
    if not token or not chat_id:
        print("Error: Missing Telegram credentials in environment.")
        return
        
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {"chat_id": chat_id, "text": message_text, "parse_mode": "Markdown"}
    
    response = requests.post(url, json=payload)
    if response.status_code == 200:
        print("✅ Morning delivery successfully dispatched.")
    else:
        print(f"❌ Telegram Error: {response.text}")


def main():
    # Load environment variables
    load_dotenv()
    
    # 0 = Monday, 1 = Tuesday, 2 = Wednesday, 3 = Thursday, 4 = Friday, 5 = Saturday, 6 = Sunday
    today_weekday = datetime.datetime.today().weekday()
    
    # Map the current day to the workout split AND its CSV title
    schedule_map = {
        1: ("Push", "Push day"),        # Tuesday
        2: ("Pull", "Pull day"),        # Wednesday
        5: ("Push + Quads", "push + quad"),  # Saturday
        6: ("Pull + Ham", "Pull + ham"),     # Sunday
    }
    
    entry = schedule_map.get(today_weekday)
    
    if not entry:
        print(f"No workout scheduled for today (Weekday {today_weekday}). Exiting.")
        return
    
    standard_key, csv_title = entry
    print(f"📅 Today is mapped to: {standard_key} (CSV: '{csv_title}')")
    
    # Import the trend engine and generate the blueprint LIVE from CSV
    from csv_trend_engine import generate_full_blueprint
    
    message_text, targets = generate_full_blueprint(csv_title)
    
    if message_text:
        print(f"📤 Generated targets for {standard_key} from CSV trend engine. Sending to Telegram...")
        
        # Add a morning greeting header
        final_message = f"🌅 *Morning Delivery* 🌅\nHere is your blueprint for today's session:\n\n{message_text}"
        
        send_telegram_message(final_message)
    else:
        print(f"⚠️ No CSV data found for '{csv_title}'. Cannot generate targets.")


if __name__ == "__main__":
    main()
