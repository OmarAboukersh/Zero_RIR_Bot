import pandas as pd
import numpy as np
import json
import os
from sklearn.ensemble import RandomForestRegressor
import joblib

from bot import EXERCISE_CONFIG
from csv_trend_engine import load_csv, get_exercise_sessions, _safe_filename, ML_FEATURE_COLS

MODELS_DIR = "models"
METADATA_FILE = os.path.join(MODELS_DIR, "model_metadata.json")

TRAIN_WINDOW = 60   # Most recent sessions used for training
MIN_SESSIONS = 15   # Need enough sessions for a meaningful walk-forward evaluation
MIN_HISTORY = 8     # Sessions the model sees before its first evaluated prediction
MIN_IMPROVEMENT = 0.9  # Model must cut the baseline error by 10%+ to be shown


def build_session_features(sessions):
    """Add the forward-looking target to the engine's session-level data.

    Uses the exact same sessions the trend engine sees (same deload filtering,
    burnout-set handling and outlier removal), so training matches prediction.
      - weight_change (target): change in max weight from this session to the NEXT one
    """
    sessions = sessions.tail(TRAIN_WINDOW).copy()
    sessions['weight_change'] = sessions['max_weight'].shift(-1) - sessions['max_weight']
    # Drop the last row (no next session to predict)
    return sessions.dropna(subset=['weight_change']).reset_index(drop=True)


def walk_forward_mae(sessions):
    """Evaluate the model the way it is used: train on the past, predict the next session.

    Returns (model_mae, baseline_mae), where the baseline always predicts
    "same weight as last session".
    """
    model_errors, baseline_errors = [], []
    for i in range(MIN_HISTORY, len(sessions)):
        past = sessions.iloc[:i]
        model = RandomForestRegressor(n_estimators=100, random_state=42)
        model.fit(past[ML_FEATURE_COLS], past['weight_change'])
        predicted = model.predict(sessions.iloc[[i]][ML_FEATURE_COLS])[0]
        actual = sessions.iloc[i]['weight_change']
        model_errors.append(abs(predicted - actual))
        baseline_errors.append(abs(actual))
    return float(np.mean(model_errors)), float(np.mean(baseline_errors))


def train_all_models():
    """Train a forward-looking RandomForest for every tracked exercise."""
    os.makedirs(MODELS_DIR, exist_ok=True)

    df = load_csv()

    metadata = {}
    trained = 0
    skipped = 0

    print("=" * 60)
    print("  ZERO RIR BOT — FORWARD-LOOKING ML TRAINING PIPELINE")
    print("=" * 60)
    print(f"  CSV loaded: {len(df)} normal working sets across {df['exercise_title'].nunique()} exercises")
    print(f"  Training models for {len(EXERCISE_CONFIG)} tracked exercises...")
    print(f"  Target: Predict NEXT session's weight change (forward-looking)")
    print("=" * 60)

    for exercise_name in EXERCISE_CONFIG:
        sessions = get_exercise_sessions(df, exercise_name, max_sessions=TRAIN_WINDOW + 1)

        if sessions.empty:
            print(f"\n  SKIP: '{exercise_name}' — not found in CSV")
            skipped += 1
            continue

        sessions = build_session_features(sessions)

        if len(sessions) < MIN_SESSIONS:
            print(f"\n  SKIP: '{exercise_name}' — only {len(sessions)} sessions (need {MIN_SESSIONS})")
            skipped += 1
            continue

        mae, baseline_mae = walk_forward_mae(sessions)
        beats_baseline = mae < MIN_IMPROVEMENT * baseline_mae

        # Final model is trained on everything
        model = RandomForestRegressor(n_estimators=100, random_state=42)
        model.fit(sessions[ML_FEATURE_COLS], sessions['weight_change'])

        model_filename = _safe_filename(exercise_name)
        joblib.dump(model, os.path.join(MODELS_DIR, model_filename))

        metadata[exercise_name] = {
            "model_file": model_filename,
            "sessions": len(sessions),
            "feature_cols": ML_FEATURE_COLS,
            "mae_kg": round(mae, 2),
            "baseline_mae_kg": round(baseline_mae, 2),
            "beats_baseline": beats_baseline,
            "trained_on": pd.Timestamp.now().isoformat()
        }

        trained += 1
        verdict = "used as hint" if beats_baseline else "hidden (no better than 'no change')"
        print(f"\n  OK: '{exercise_name}'")
        print(f"      Sessions: {len(sessions)} | MAE: {mae:.2f}kg vs baseline {baseline_mae:.2f}kg | {verdict}")

    # Save metadata
    with open(METADATA_FILE, "w", encoding="utf-8") as f:
        json.dump(metadata, f, indent=2, ensure_ascii=False)

    useful = sum(m["beats_baseline"] for m in metadata.values())
    print("\n" + "=" * 60)
    print(f"  COMPLETE: {trained} models trained, {skipped} skipped")
    print(f"  {useful}/{trained} models beat the 'no change' baseline and will be shown as hints")
    print(f"  Models saved to: {os.path.abspath(MODELS_DIR)}/")
    print(f"  Metadata saved to: {os.path.abspath(METADATA_FILE)}")
    print("=" * 60)


if __name__ == "__main__":
    train_all_models()
