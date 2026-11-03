# Zero_RIR_Bot

![tests](https://github.com/OmarAboukersh/Zero_RIR_Bot/actions/workflows/tests.yml/badge.svg)

## Overview
Zero_RIR_Bot is a Python-based hypertrophy and strength programming engine designed for advanced lifters. It completely removes manual workout programming by applying strict mathematical double-progression and auto-regulation to your training data.

## Core Features

- **Zero-Friction Logging:** Connects directly to the Hevy API to fetch completed workout data automatically. It smartly syncs up to your last 10 missing workouts to guarantee no data is ever dropped if you skip a sync day.
- **Your Own Web App:** `web_app.py` serves a phone-friendly website with your targets, stats charts, weekly summary, settings and an AI coach, writes the targets into your Hevy routines, and is reachable from anywhere through a Cloudflare Tunnel behind Cloudflare Access login (see below).
- **Smart Autocorrect:** Don't worry about spelling. The bot uses a fuzzy matching engine (`difflib`) to instantly autocorrect typos in your commands (e.g. "dumbells" auto-corrects to "Dumbbell").
- **Burnout Set Protection:** If you do an exercise twice in the same workout (e.g. main heavy sets followed by later burnout sets), the engine strictly prioritizes your main working sets and ignores the light burnout sets for progression tracking.
- **Deload Detection:** Tag a workout with "deload" in the title or notes, and the bot will cleanly ignore the workout for progression tracking to prevent polluting the ML dataset. Untagged deloads are caught automatically: any lift that drops below 80% of its last working weight and then recovers the next session is excluded (a whole workout is excluded when most of its lifts do this).
- **Honest ML Hints:** Target weights come from double progression only. The ML forecast is shown as a hint only for exercises where it has been proven (walk-forward validation) to beat simply repeating last session's weight.

## Web App

Your own website for the bot: today's targets, all 4 days, stats charts, the weekly summary and settings, with buttons instead of typed commands.

**Start it:** double-click `run_web_app.bat` (first time only: `pip install flask`).

- On the laptop: open http://localhost:8000
- On your phone (same Wi-Fi): open the "On your phone" address the window prints, e.g. http://192.168.1.20:8000
- Away from home: through a Cloudflare Tunnel (route `gym.<your-domain>` → `http://localhost:8000`), protected by a Cloudflare Access email-code login
- Add it to your home screen (Share → Add to Home Screen) and it opens like an app

**Targets straight into Hevy:** after every sync with a new workout (and after any settings change), `hevy_routines.py` writes the new targets into your four Hevy routines ("Push day", "Pull day", "push + quad", "Pull + ham"): the working sets of each tracked exercise get the target weight, reps and number of sets, and a 🤖 line in the exercise notes says why. Warm-up sets, your own notes, exercise order, rest times, supersets and untracked exercises are left alone, and a permanently replaced exercise (`EXERCISE_REPLACEMENTS`) is swapped for its replacement once you've logged it in Hevy. Before writing, the routines are backed up to `routines_backup/`, and only routines that actually changed are sent. In the app: Sync → **Send targets to Hevy**, or **Preview** to see the changes without writing. Command line: `python hevy_routines.py --dry-run`, or `--restore routines_backup/<file>.json` to put a backup back. Turn it off with `HEVY_ROUTINE_SYNC=off`.

**AI coach (Coach tab):** chat with a coach that sees your current targets for all 4 days, your last 4 sessions of every exercise (sets, RPE, notes), your settings and the weekly summary, plus the progression rules. Ask about injuries, breaks, stalled lifts or RPE. Uses Google's Gemini API free tier: put `GEMINI_API_KEY` in `.env` (free key from aistudio.google.com/apikey) and restart. The provider lives in one function in `coach.py`, so it can be swapped. Your training data is sent to Gemini with each question.

**Auto-sync:** every time the app opens (or you come back to it), it pulls new workouts from Hevy in the background, at most once every 5 minutes (`AUTOSYNC_MINUTES`). If a new workout came in, the page refreshes itself. A lock file (`sync.lock`) makes sure the app and the scheduled `run_bot.bat` never sync at the same time.

**Optional PIN:** add `APP_PIN="1234"` to `.env`. Turn this on if you ever run it on a shared Wi-Fi.

**How it works:** `web_app.py` is a small Flask server. The page (`web/index.html`) asks it for data at `/api/...` and it answers with JSON, using the same engine as before (`csv_trend_engine.py`, `bot.py`, `weekly_summary.py`). `workouts.csv` is cached in memory and only re-read when the file changes, which is why it's faster than the Telegram listener.

| Old command | In the web app |
|---|---|
| `/next` | Today tab |
| `/showall` | All days tab |
| `/stats <exercise>` | Stats tab, or "Stats" on any exercise |
| `/substitute A, B` | "Swap" on any exercise |
| `/weekly` | Week tab |
| `/settings`, `/setmax`, `/setstep`, `/setceiling`, `/clearmax`, `/resetsettings` | Settings tab (clear the Max box to remove a max) |
| `/sync`, `/train` | Sync button (top right) |


## Development

**Tests:** `pip install pytest` then `pytest`. The suite (`tests/`) builds its own fake workout history, so it never touches your real data, and covers the progression maths (RPE rules, ceilings, strict ceilings, max weights, back-off sets), how each day's plan is built (skips, swaps, permanent replacements) and the web API (targets, settings validation, PIN). GitHub Actions runs it on every push.

**Docker:** `docker compose up -d --build`. The image only contains the code; this folder (workouts.csv, models/, settings, .env) is mounted at `/data`, so rebuilding never touches your data. The container restarts by itself after a reboot and syncs from Hevy every hour (`SYNC_EVERY_MINUTES`), so on the home lab you don't need Windows Task Scheduler. `docker-compose.yml` also has a ready-to-uncomment Cloudflare Tunnel service.

## Settings

Each exercise has a rep ceiling, a weight step and an optional max weight, editable in the Settings tab.
- **Max weight:** the machine is maxed out, so the weight stops going up and reps keep progressing. Clear the box to remove it.
- **Strict ceiling:** exercises with `"strict_ceiling": True` in `EXERCISE_CONFIG` (Squat, Leg Press and Triceps Pushdown) never go above the ceiling, and the weight goes up as soon as every set hits it, whatever the RPE.

Changes are saved in `exercise_settings.json` and take effect immediately; the defaults live in `EXERCISE_CONFIG` in `bot.py`.

**Permanent swaps:** when you move on from an exercise for good (e.g. a maxed-out machine), add it to `EXERCISE_REPLACEMENTS` in `csv_trend_engine.py` ("old": "new"). Skipping an exercise once doesn't remove it: it only leaves a day's plan after `SPLIT_LOOKBACK_SESSIONS` (3) sessions in a row without it.

## Notifications

`notify.py` handles the messages the bot sends (new targets after a sync, the 4 AM morning targets, the Monday weekly summary). By default they're only written to the log files; everything is also on the web app. For phone notifications, install the free ntfy app and set `NTFY_TOPIC` in `.env` (instructions at the top of `notify.py`).

## Training Logic

- **0 RIR Double Progression:** Analyzes sets mathematically to trigger load increases only when the established rep ceiling is cleared at absolute failure.
- **RPE-Aware Rep Progression:** Log RPE per set in Hevy (or write "RPE 8" in the exercise notes). RPE 9.5-10 holds your reps, RPE 8 adds 1 rep, RPE 7 or lower adds 2. With RPE 8.5-9 or no RPE logged, a new rep best has to be matched twice over about 2 weeks before another rep is added.
- **Stateful Fatigue Management:** Maintains a local CSV database to track rolling performance. It automatically triggers targeted deloads (volume cuts and 10% load drops) if multi-session regression or RIR compression is detected.
- **Fixed Equipment Constraints:** Hardcoded to respect physical plate math (e.g., 2.0kg dumbbell increments and 2.5kg plate stack increments).

## Tech Stack
- Python 3
- pandas, scikit-learn (Data & Trend Engine)
- Flask (web app), vanilla JavaScript front end (no build step)
- Google Gemini API (AI coach)
- pytest + GitHub Actions, Docker
- Cloudflare Tunnel + Access
- ntfy (optional phone notifications)
- Hevy API (reads workouts, writes routines)
