import pandas as pd
import json
import os
import re
from sklearn.model_selection import train_test_split
from sklearn.ensemble import RandomForestRegressor
from sklearn.metrics import mean_squared_error
import joblib

CSV_FILE = "workouts.csv"
MODELS_DIR = "models"
METADATA_FILE = os.path.join(MODELS_DIR, "model_metadata.json")

# Must match bot.py's EXERCISE_CONFIG keys
EXERCISE_CONFIG_KEYS = [
    "Bench Press (Smith Machine)", "Incline Bench Press (Smith Machine)",
    "Squat (Smith Machine)", "T Bar Row",
    "Reverse Grip Lat Pulldown (Cable)", "Lat Pulldown (Cable)",
    "Seated Cable Row - V Grip (Cable)", "Seated Shoulder Press (Machine)",
    "Lateral Raise (Cable)", "Lateral Raise (Dumbbell)",
    "Triceps Pushdown", "Triceps Extension (Dumbbell)",
    "Crunch (Weighted)", "Single Leg Extensions",
    "Rear Delt Reverse Fly (Machine)", "Back Extension (Weighted Hyperextension)",
    "Calf Press (Machine)", "Reverse Curl (Cable)",
    "Lying Leg Curl (Machine)", "Shrug (Cable)",
    "Preacher curl single arm (machine)", "Preacher Curl (Machine)"
]

MIN_SESSIONS = 6  # Minimum sessions required (need at least 5 after shifting + 1 for test)

def safe_filename(name):
    """Convert exercise name to a safe filename."""
    return re.sub(r'[^a-z0-9]+', '_', name.lower()).strip('_') + ".pkl"

def load_and_clean_data(file_path):
    """Load CSV and engineer base features. Excludes deload sessions."""
    df = pd.read_csv(file_path)
    df = df.dropna(subset=['weight_kg', 'reps'])
    df = df[df['set_type'] == 'normal']
    # Exclude deload sessions — intentionally lighter weights would distort the model
    df = df[~df['description'].fillna('').str.lower().str.contains('deload')]
    df['set_volume'] = df['weight_kg'] * df['reps']
    df['start_time'] = pd.to_datetime(df['start_time'])
    df = df.sort_values('start_time')
    return df

def find_csv_match(exercise_name, csv_exercises):
    """Case-insensitive match between config name and CSV exercise titles."""
    norm = exercise_name.lower().strip()
    for csv_name in csv_exercises:
        if csv_name.lower().strip() == norm:
            return csv_name
    for csv_name in csv_exercises:
        if norm in csv_name.lower() or csv_name.lower() in norm:
            return csv_name
    return None

def build_session_features(ex_data):
    """Aggregate set-level data into session-level features.
    
    Returns a DataFrame with one row per session:
      - avg_reps, max_weight, total_volume, num_sets, session_number
      - next_session_weight (target: what weight was used NEXT session)
    """
    ex_data = ex_data.copy()
    ex_data['session_date'] = ex_data['start_time'].dt.date
    
    sessions = ex_data.groupby('session_date').agg(
        avg_reps=('reps', 'mean'),
        max_weight=('weight_kg', 'max'),
        total_volume=('set_volume', 'sum'),
        num_sets=('reps', 'count')
    ).reset_index().sort_values('session_date')
    
    # Sequential session number
    sessions['session_number'] = range(1, len(sessions) + 1)
    
    # FORWARD-LOOKING TARGET: the max weight used in the NEXT session
    sessions['next_session_weight'] = sessions['max_weight'].shift(-1)
    
    # Drop the last row (no next session to predict)
    sessions = sessions.dropna(subset=['next_session_weight'])
    
    return sessions

def train_all_models():
    """Train a forward-looking RandomForest for every tracked exercise."""
    os.makedirs(MODELS_DIR, exist_ok=True)
    
    df = load_and_clean_data(CSV_FILE)
    csv_exercises = df['exercise_title'].unique()
    
    metadata = {}
    trained = 0
    skipped = 0
    
    print("=" * 60)
    print("  ZERO RIR BOT — FORWARD-LOOKING ML TRAINING PIPELINE")
    print("=" * 60)
    print(f"  CSV loaded: {len(df)} normal working sets across {len(csv_exercises)} exercises")
    print(f"  Training models for {len(EXERCISE_CONFIG_KEYS)} tracked exercises...")
    print(f"  Target: Predict NEXT session's weight (forward-looking)")
    print("=" * 60)
    
    for exercise_name in EXERCISE_CONFIG_KEYS:
        csv_name = find_csv_match(exercise_name, csv_exercises)
        
        if csv_name is None:
            print(f"\n  SKIP: '{exercise_name}' — not found in CSV")
            skipped += 1
            continue
        
        ex_data = df[df['exercise_title'] == csv_name].copy()
        sessions = build_session_features(ex_data)
        
        if len(sessions) < MIN_SESSIONS:
            print(f"\n  SKIP: '{exercise_name}' — only {len(sessions)} sessions (need {MIN_SESSIONS})")
            skipped += 1
            continue
        
        # Features: current session's performance
        feature_cols = ['avg_reps', 'max_weight', 'total_volume', 'num_sets', 'session_number']
        X = sessions[feature_cols]
        y = sessions['next_session_weight']
        
        X_train, X_test, y_train, y_test = train_test_split(
            X, y, test_size=0.2, random_state=42
        )
        
        model = RandomForestRegressor(n_estimators=100, random_state=42)
        model.fit(X_train, y_train)
        
        predictions = model.predict(X_test)
        mse = mean_squared_error(y_test, predictions)
        rmse = mse ** 0.5
        
        # Save model
        model_filename = safe_filename(exercise_name)
        model_path = os.path.join(MODELS_DIR, model_filename)
        joblib.dump(model, model_path)
        
        metadata[exercise_name] = {
            "model_file": model_filename,
            "csv_name": csv_name,
            "sessions": len(sessions),
            "feature_cols": feature_cols,
            "mse": round(mse, 3),
            "rmse_kg": round(rmse, 2),
            "trained_on": pd.Timestamp.now().isoformat()
        }
        
        trained += 1
        print(f"\n  OK: '{exercise_name}'")
        print(f"      Sessions: {len(sessions)} | RMSE: {rmse:.2f}kg | Saved: {model_filename}")
    
    # Save metadata
    with open(METADATA_FILE, "w", encoding="utf-8") as f:
        json.dump(metadata, f, indent=2, ensure_ascii=False)
    
    print("\n" + "=" * 60)
    print(f"  COMPLETE: {trained} models trained, {skipped} skipped")
    print(f"  Models saved to: {os.path.abspath(MODELS_DIR)}/")
    print(f"  Metadata saved to: {os.path.abspath(METADATA_FILE)}")
    print("=" * 60)

if __name__ == "__main__":
    train_all_models()