import os
import json
import requests
import datetime
from dotenv import load_dotenv

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
    
    # Map the current day to the requested workout keys
    schedule_map = {
        1: "Push",          # Tuesday
        2: "Pull",          # Wednesday
        5: "Push + Quads",  # Saturday
        6: "Pull + Ham"     # Sunday
    }
    
    target_key = schedule_map.get(today_weekday)
    
    if not target_key:
        print(f"No workout scheduled for today (Weekday {today_weekday}). Exiting.")
        return
        
    print(f"📅 Today is mapped to: {target_key}")
    
    target_file = "upcoming_targets.json"
    
    if not os.path.exists(target_file):
        print(f"❌ Target memory file '{target_file}' not found.")
        return
        
    try:
        with open(target_file, "r", encoding="utf-8") as f:
            targets = json.load(f)
    except Exception as e:
        print(f"❌ Failed to parse {target_file}: {e}")
        return
        
    message_text = targets.get(target_key)
    
    if message_text:
        print(f"📤 Found targets for {target_key}. Sending to Telegram...")
        
        # Add a small morning greeting header
        final_message = f"🌅 **Morning Delivery** 🌅\nHere is your blueprint for today's session:\n\n{message_text}"
        
        send_telegram_message(final_message)
    else:
        print(f"⚠️ No saved targets found for '{target_key}' in memory.")

if __name__ == "__main__":
    main()
