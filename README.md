# Zero_RIR_Bot

## Overview
Zero_RIR_Bot is a Python-based hypertrophy and strength programming engine designed for advanced lifters. It completely removes manual workout programming by applying strict mathematical double-progression and auto-regulation to your training data.

## Core Features

- **Zero-Friction Logging:** Connects directly to the Hevy API to fetch completed workout data automatically. It smartly syncs up to your last 10 missing workouts to guarantee no data is ever dropped if you skip a sync day.
- **Interactive Telegram Assistant:** A constantly running Telegram listener (`telegram_listener.py`) provides real-time access to your data.
- **Smart Autocorrect:** Don't worry about spelling. The bot uses a fuzzy matching engine (`difflib`) to instantly autocorrect typos in your commands (e.g. "dumbells" auto-corrects to "Dumbbell").
- **Burnout Set Protection:** If you do an exercise twice in the same workout (e.g. main heavy sets followed by later burnout sets), the engine strictly prioritizes your main working sets and ignores the light burnout sets for progression tracking.
- **Deload Detection:** Tag a workout with "deload" in the title or notes, and the bot will cleanly ignore the workout for progression tracking to prevent polluting the ML dataset.

## Commands
- `/next`: Predicts your next scheduled workout (based on your rolling split) and instantly outputs the generated blueprint and targets.
- `/stats <exercise>`: Generates a beautiful Excel-style grid showing your historical sets, reps, and weights for any tracked exercise to visualize your trends over time.
- `/substitute <Current Exercise>, <New Exercise>`: Swaps out a stale exercise for a new one on the fly, instantly calculating your targets and starting weights for the new exercise based on your historical data.
- `/sync`: Manually triggers a pull from the Hevy API to update the bot's local database.
- `/train`: Manually triggers a retrain of the machine learning models.

## Training Logic

- **0 RIR Double Progression:** Analyzes sets mathematically to trigger load increases only when the established rep ceiling is cleared at absolute failure.
- **Stateful Fatigue Management:** Maintains a local CSV database to track rolling performance. It automatically triggers targeted deloads (volume cuts and 10% load drops) if multi-session regression or RIR compression is detected.
- **Fixed Equipment Constraints:** Hardcoded to respect physical plate math (e.g., 2.0kg dumbbell increments and 2.5kg plate stack increments).

## Tech Stack
- Python 3
- pandas, scikit-learn (Data & Trend Engine)
- pyTelegramBotAPI (Notification Delivery & Listening)
- Hevy API (Data Ingestion)
