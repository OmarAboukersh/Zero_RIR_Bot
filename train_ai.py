import pandas as pd
from sklearn.model_selection import train_test_split
from sklearn.ensemble import RandomForestRegressor
from sklearn.metrics import mean_squared_error

csv_file = "workouts.csv"

def load_and_clean_data(file_path):
    df = pd.read_csv(file_path)
    df = df.dropna(subset=['weight_kg', 'reps'])
    df['set_volume'] = df['weight_kg'] * df['reps']
    return df

def engineer_features(df, target_exercise):
    ex_data = df[df['exercise_title'] == target_exercise].copy()
    if ex_data.empty: return None, None, ex_data
    
    ex_data['start_time'] = pd.to_datetime(ex_data['start_time'])
    ex_data = ex_data.sort_values('start_time')
    
    X = ex_data[['reps', 'set_volume']] 
    y = ex_data['weight_kg']
    return X, y, ex_data

def analyze_plateau(ex_data, target_exercise):
    """AI checks the last 3 sessions and reads the text notes for context."""
    if len(ex_data) < 3: return
    
    recent_sessions = ex_data.tail(3)
    volumes = recent_sessions['set_volume'].tolist()
    
    # Safely pull the notes column (checking both common Hevy export names)
    if 'description' in ex_data.columns:
        notes_col = 'description'
    elif 'workout_notes' in ex_data.columns:
        notes_col = 'workout_notes'
    else:
        notes_col = None

    print(f"\n🔍 AI Trend Analysis for: {target_exercise}")
    print(f"Past 3 Session Volumes: {volumes[0]:.1f} ➡️ {volumes[1]:.1f} ➡️ {volumes[2]:.1f}")
    
    # Check if the word "deload" exists in the text of the most recent sessions
    is_intentional_deload = False
    if notes_col:
        recent_notes = recent_sessions[notes_col].astype(str).str.lower().tolist()
        if "deload" in recent_notes[-1] or "deload" in recent_notes[-2]:
            is_intentional_deload = True

    # The New Logic Tree
    if is_intentional_deload:
        print("🟢 VERDICT: Volume drop detected, but AI read your notes. Intentional Deload in progress. Recover well!")
    elif volumes[-1] <= volumes[0]:
        print("🚨 VERDICT: Plateau Detected. AI Recommends a 10% Deload drop for the next session to clear CNS fatigue.")
    else:
        print("📈 VERDICT: Upward momentum detected. Keep pushing the 0 RIR ceiling.")

def train_model():
    df = load_and_clean_data(csv_file)
    target = "Lat Pulldown (Cable)" 
    
    X, y, ex_data = engineer_features(df, target)
    if X is None: return

    X_train, X_test, y_train, y_test = train_test_split(X, y, test_size=0.2, random_state=42)
    
    model = RandomForestRegressor(n_estimators=100, random_state=42)
    model.fit(X_train, y_train)
    
    error = mean_squared_error(y_test, model.predict(X_test))
    print(f"✅ AI Training Complete. Accuracy variance: {error**0.5:.2f}kg")
    
    # Run the plateau check
    analyze_plateau(ex_data, target)

if __name__ == "__main__":
    train_model()