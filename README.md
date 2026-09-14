Overview
Zero_RIR_Bot is a Python-based hypertrophy and strength programming engine designed for advanced lifters. It completely removes manual workout programming by applying strict mathematical double-progression and auto-regulation to your training data.

Core Features

Zero-Friction Logging: Connects directly to the Hevy API to fetch completed workout data automatically.

0 RIR Double Progression: Analyzes sets mathematically to trigger load increases only when the established rep ceiling is cleared at absolute failure.

Stateful Fatigue Management: Maintains a local JSON database to track rolling performance. It automatically triggers targeted deloads (volume cuts and 10% load drops) if multi-session regression or RIR compression is detected.

Automated Delivery: Formats and dispatches a clean, actionable blueprint for the upcoming workout directly to Telegram via a custom bot.

Fixed Equipment Constraints: Hardcoded to respect physical plate math (e.g., 2.0kg dumbbell increments and 2.5kg plate stack increments).

Tech Stack

Python 3

Hevy API (Data Ingestion)

Telegram Bot API (Notification Delivery)
