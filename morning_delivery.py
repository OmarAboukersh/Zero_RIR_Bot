"""
Morning Delivery — builds today's workout blueprint (and the weekly summary on
Mondays) and sends it through notify.py.

Without ntfy set up this just writes to morning_delivery_log.txt; the same
targets are always on the web app's Today tab.

Generates targets LIVE from workouts.csv using the CSV trend engine.
"""

import sys
import datetime
from dotenv import load_dotenv

# Ensure UTF-8 output on Windows
sys.stdout.reconfigure(encoding='utf-8')

from notify import notify


def main():
    load_dotenv()

    # 0 = Monday, 1 = Tuesday, 2 = Wednesday, 3 = Thursday, 4 = Friday, 5 = Saturday, 6 = Sunday
    today_weekday = datetime.datetime.today().weekday()

    # Every Monday: weekly summary of the previous Monday-Sunday
    if today_weekday == 0:
        from weekly_summary import build_weekly_summary
        yesterday = datetime.date.today() - datetime.timedelta(days=1)
        print("📅 Monday: weekly summary")
        notify(build_weekly_summary(end_date=yesterday), title="Weekly summary")

    # Map the current day to the workout split AND its CSV title
    schedule_map = {
        1: ("Push", "Push day"),             # Tuesday
        2: ("Pull", "Pull day"),             # Wednesday
        5: ("Push + Quads", "push + quad"),  # Saturday
        6: ("Pull + Ham", "Pull + ham"),     # Sunday
    }

    entry = schedule_map.get(today_weekday)
    if not entry:
        print(f"No workout scheduled for today (Weekday {today_weekday}). Exiting.")
        return

    standard_key, csv_title = entry
    print(f"📅 Today is mapped to: {standard_key} (CSV: '{csv_title}')")

    from csv_trend_engine import generate_full_blueprint
    message_text, targets = generate_full_blueprint(csv_title)

    if message_text:
        notify(f"🌅 Today: {standard_key}\n\n{message_text}", title=f"Today: {standard_key}")
    else:
        print(f"⚠️ No CSV data found for '{csv_title}'. Cannot generate targets.")


if __name__ == "__main__":
    main()
