"""
Notifications — where the bot's messages go (Telegram was removed in Nov 2026).

Every message is printed, so it ends up in the log files (error_log.txt,
morning_delivery_log.txt). Everything you need is also in the web app.

Optional phone notifications with ntfy (free, no account needed):
  1. Install the ntfy app on your phone and subscribe to a long random topic
     name nobody could guess (make up your own random letters)
  2. Add to .env:   NTFY_TOPIC="zero-rir-<your-random-letters>"
     Optional:      APP_URL="https://gym.example.com"  (tapping the
                    notification opens the web app)
"""

import os
import re

import requests
from dotenv import load_dotenv

load_dotenv()


def _plain(text):
    """Drop the *bold* / _italic_ markers the messages were written with."""
    text = re.sub(r"\*([^*\n]+)\*", r"\1", text)
    return re.sub(r"(^|\s)_([^_\n]+)_(?=\s|$)", r"\1\2", text, flags=re.M)


def notify(message, title="Zero RIR"):
    """Log a message, and push it to your phone if NTFY_TOPIC is set."""
    text = _plain(message)
    print(text)

    topic = os.getenv("NTFY_TOPIC", "").strip()
    if not topic:
        return
    server = os.getenv("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
    headers = {"Title": title}
    if os.getenv("APP_URL"):
        headers["Click"] = os.getenv("APP_URL")
    try:
        r = requests.post(f"{server}/{topic}", data=text.encode("utf-8"), headers=headers, timeout=10)
        print("📲 Phone notification sent." if r.ok else f"⚠️ ntfy error {r.status_code}: {r.text[:200]}")
    except requests.RequestException as e:
        print(f"⚠️ Couldn't send phone notification: {e}")
