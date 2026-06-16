import os
import datetime
from database.database import init_db, SessionLocal, DailyMetric, WorkoutPlanned, WorkoutExecuted, NutritionLog, AgentInsight
from agents.workflow import run_agent_pipeline
from parser.parser import clean_and_smooth_data, detect_laps
import pandas as pd

def test_database():
    print("=== [1] Testing Database Initialization & Operations ===")
    # Initialize DB
    init_db()
    db = SessionLocal()
    try:
        # Clear database for testing
        print("[WARNING] Svuotamento di tutte le tabelle del database per scopi di test...")
        db.query(DailyMetric).delete()
        db.query(WorkoutPlanned).delete()
        db.query(WorkoutExecuted).delete()
        db.query(NutritionLog).delete()
        db.query(AgentInsight).delete()
        db.commit()
        
        # 1. Insert Daily Metric
        today = datetime.date.today()
        metric = DailyMetric(
            date=today,
            sleep_hours=7.8,
            resting_hr=52,
            hrv_score=68,
            steps=11000,
            readiness=85.0
        )
        db.add(metric)
        
        # 2. Insert Planned Workout
        planned = WorkoutPlanned(
            date=today,
            type="easy",
            target_distance=8.0,
            prompt_text="Corsa lenta di scarico",
            status="planned"
        )
        db.add(planned)
        db.commit()
        
        # 3. Insert Executed Workout referring to planned
        executed = WorkoutExecuted(
            planned_id=planned.id,
            date=today,
            workout_type="lento_corto",
            distance_km=8.1,
            avg_pace=320.0,  # 5:20 min/km in seconds
            avg_cadence=170.0,
            shoe_used="Asics Gel-Nimbus",
            elevation_gain=25.0,
            elevation_loss=25.0,
            max_hr=165,
            laps_summary=[{
                "name": "Corsa Lenta",
                "distance_km": 8.1,
                "duration_sec": 2592,
                "avg_pace_sec": 320.0,
                "avg_hr": 140.0,
                "avg_cadence": 170.0
            }],
            rpe_score=5
        )
        db.add(executed)
        planned.status = "completed"
        db.commit()
        
        # Verify relations
        fetched_planned = db.query(WorkoutPlanned).filter(WorkoutPlanned.id == planned.id).first()
        print(f"[OK] Planned workout fetched: {fetched_planned}")
        print(f"[OK] Executed workouts associated: {fetched_planned.executed_workouts}")
        
        # Verify Daily Metrics
        fetched_metric = db.query(DailyMetric).filter(DailyMetric.date == today).first()
        print(f"[OK] Daily metric fetched: {fetched_metric}")
        
    except Exception as e:
        print(f"[ERROR] Database test failed: {e}")
        raise e
    finally:
        db.close()
    print("Database test passed!\n")


def test_parser_smoothing():
    print("=== [2] Testing Parser Time-Series Smoothing & Lap Segmentation ===")
    try:
        # Generate mock time-series DataFrame (30 seconds of running)
        time_range = pd.date_range(start="2026-06-02 08:00:00", periods=40, freq="s")
        data = {
            "timestamp": time_range,
            "distance_m": [i * 3.0 for i in range(40)],  # 3 m/s constant speed
            "speed_m_s": [3.0] * 40,
            "cadence": [170] * 40,
            "heart_rate": [130] * 40
        }
        df = pd.DataFrame(data)
        
        # Apply smoothing
        df_smoothed = clean_and_smooth_data(df)
        print(f"[OK] Smoothed columns: {list(df_smoothed.columns)}")
        print(f"[OK] Average smooth pace (seconds/km): {df_smoothed['pace_smooth'].mean():.1f}")
        
        # Detect laps
        laps = detect_laps(df_smoothed)
        print(f"[OK] Detected laps count: {len(laps)}")
        print(f"[OK] First lap detail: {laps[0]}")
        
    except Exception as e:
        print(f"[ERROR] Parser test failed: {e}")
        raise e
    print("Parser test passed!\n")


def test_langgraph_pipeline():
    print("=== [3] Testing LangGraph Multi-Agent Pipeline (Gemini/Mock mode) ===")
    try:
        # 1. Test sending daily metrics via text message
        print("Invio metriche del giorno...")
        res_metrics = run_agent_pipeline(
            user_id=9999,
            message_text="HRV 72, sonno 8 ore, battiti a riposo 51"
        )
        print("[OK] Decisione router:", res_metrics.get("router_decision"))
        print("[OK] Stato di readiness:", res_metrics.get("readiness_data"))
        print("[OK] Risposta generata:")
        print("-" * 50)
        # Safe printing for console encoding
        msg = res_metrics.get("response_message", "")
        print(msg.encode('ascii', errors='replace').decode('ascii'))
        print("-" * 50)
        print()
        
        # 2. Test requesting next workout
        print("Richiesta prossimo allenamento...")
        res_workout = run_agent_pipeline(
            user_id=9999,
            message_text="Qual è il mio prossimo allenamento?"
        )
        print("[OK] Decisione router:", res_workout.get("router_decision"))
        print("[OK] Allenamento pianificato:", res_workout.get("next_workout_plan"))
        print("[OK] Risposta generata:")
        print("-" * 50)
        msg2 = res_workout.get("response_message", "")
        print(msg2.encode('ascii', errors='replace').decode('ascii'))
        print("-" * 50)
        print()

    except Exception as e:
        print(f"[ERROR] LangGraph pipeline test failed: {e}")
        raise e
    print("LangGraph pipeline test passed!\n")


def test_dashboard_endpoint():
    print("=== [4] Testing FastAPI Dashboard Endpoint ===")
    from fastapi.testclient import TestClient
    from main import app
    try:
        client = TestClient(app)
        response = client.get("/dashboard")
        print(f"[OK] Dashboard status code: {response.status_code}")
        assert response.status_code == 200
        print("[OK] Dashboard template rendered successfully.")
    except Exception as e:
        print(f"[ERROR] Dashboard test failed: {e}")
        raise e
    print("Dashboard endpoint test passed!\n")


if __name__ == "__main__":
    test_database()
    test_parser_smoothing()
    test_langgraph_pipeline()
    test_dashboard_endpoint()
    print("=== ALL TESTS PASSED SUCCESSFULLY! ===")
