import os
import pandas as pd
from typing import Optional
import json
import asyncio
import datetime
from contextlib import asynccontextmanager
from fastapi import FastAPI, Depends, HTTPException, BackgroundTasks, Request, UploadFile, File, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from dotenv import load_dotenv

from database.database import (
    init_db, get_db, DailyMetric, WorkoutPlanned, WorkoutExecuted, NutritionLog, AgentInsight, Shoe, SessionLocal, ChatMessage, PainLog,
    HeartRateIntraday, SleepStageIntraday
)
from bot.telegram_bot import setup_bot
from parser.parser import generate_mock_workout_data, parse_gpx_workout
from agents.workflow import run_agent_pipeline
from sync.samsung_health_sync import run_sync as samsung_run_sync, SyncReport

# Load environment variables
load_dotenv(override=True)

telegram_app = None

@asynccontextmanager
async def lifespan(app: FastAPI):
    # 1. Startup: Init SQLite Tables
    logger_print("Inizializzazione Database SQLite...")
    init_db()
    
    # Ensure directories exist
    os.makedirs("./static/css", exist_ok=True)
    os.makedirs("./templates", exist_ok=True)
    os.makedirs("./data/uploads", exist_ok=True)
    
    logger_print("Database e directory locali inizializzati.")
    
    # Trigger Garmin Sync in the background at startup (90 days lookback)
    sync_garmin_history_startup(days_back=90)
    
    # Start the daily scheduled sync background task
    start_daily_sync_scheduler()

    # Trigger Samsung Health sync at startup (30 days lookback)
    sync_samsung_health_startup(days_back=30)

    # Start periodic Samsung Health sync scheduler
    start_samsung_sync_scheduler()
    
    # 2. Startup: Configure and Start Telegram Bot in Long Polling (DISABLED per user request)
    logger_print("🤖 Bot Telegram disattivato su richiesta dell'utente.")

    yield

    # 3. Shutdown: Clean up background tasks & stop the bot
    if telegram_app:
        logger_print("Arresto del Bot Telegram...")
        try:
            await telegram_app.updater.stop()
            await telegram_app.stop()
            await telegram_app.shutdown()
            logger_print("🤖 Bot Telegram arrestato.")
        except Exception as e:
            logger_print(f"❌ Errore durante l'arresto del Bot: {e}")


app = FastAPI(
    title="Marathon-Multi-Agent API & Dashboard",
    description="Backend ed interfaccia web per il tracciamento degli allenamenti della mezza maratona.",
    version="1.0.0",
    lifespan=lifespan
)

# Mount static files and templates
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")


def logger_print(message: str):
    """Utility to print messages formatted with timestamp."""
    try:
        print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {message}")
    except UnicodeEncodeError:
        safe_msg = message.encode('ascii', errors='replace').decode('ascii')
        print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {safe_msg}")


@app.get("/favicon.ico", include_in_schema=False)
@app.get("/apple-touch-icon.png", include_in_schema=False)
@app.get("/apple-touch-icon-precomposed.png", include_in_schema=False)
@app.get("/favicon.png", include_in_schema=False)
def favicon():
    from fastapi.responses import FileResponse
    favicon_path = "static/favicon.png"
    if os.path.exists(favicon_path):
        return FileResponse(favicon_path)
    from fastapi import Response
    return Response(status_code=204)


@app.get("/")
def read_root():
    """Redirect to dashboard."""
    return RedirectResponse(url="/dashboard")
import re

def extract_workout_parameters(prompt_text: Optional[str], workout_type: str) -> dict:
    """
    Parses planned workout instructions to extract warmup, cooldown, and interval specifications.
    """
    params = {
        "warmup_enabled": False,
        "warmup_type": "time",
        "warmup_value": 10.0,
        "cooldown_enabled": False,
        "cooldown_type": "distance",
        "cooldown_value": 1.0,
        "interval_type": "distance",
        "interval_value": 1000.0,
        "recovery_type": "time",
        "recovery_value": 120.0,
        "repetitions": 5
    }
    
    if not prompt_text:
        return params
        
    text = prompt_text.lower()
    
    # 1. Warmup parsing
    # "2 km riscaldamento" or "riscaldamento 2km" or "riscaldamento 2 km"
    warmup_match = re.search(r'(?:(\d+(?:\.\d+)?)\s*(?:km|chilometri)|(\d+)\s*(?:min|minuti))\s*(?:di\s*)?riscaldamento', text)
    if not warmup_match:
        warmup_match = re.search(r'riscaldamento\s*(?:di\s*)?(?:(\d+(?:\.\d+)?)\s*(?:km|chilometri)|(\d+)\s*(?:min|minuti))', text)
        
    if warmup_match:
        params["warmup_enabled"] = True
        if warmup_match.group(1):
            params["warmup_type"] = "distance"
            params["warmup_value"] = float(warmup_match.group(1))
        elif warmup_match.group(2):
            params["warmup_type"] = "time"
            params["warmup_value"] = float(warmup_match.group(2))
            
    # 2. Cooldown parsing
    cooldown_match = re.search(r'(?:(\d+(?:\.\d+)?)\s*(?:km|chilometri)|(\d+)\s*(?:min|minuti))\s*(?:di\s*)?defaticamento', text)
    if not cooldown_match:
        cooldown_match = re.search(r'defaticamento\s*(?:di\s*)?(?:(\d+(?:\.\d+)?)\s*(?:km|chilometri)|(\d+)\s*(?:min|minuti))', text)
        
    if cooldown_match:
        params["cooldown_enabled"] = True
        if cooldown_match.group(1):
            params["cooldown_type"] = "distance"
            params["cooldown_value"] = float(cooldown_match.group(1))
        elif cooldown_match.group(2):
            params["cooldown_type"] = "time"
            params["cooldown_value"] = float(cooldown_match.group(2))
            
    # If workout is "medio", warmup and cooldown are typically enabled too (e.g. 2km warmup, 2km cooldown)
    if workout_type == "medio":
        if "riscaldamento" in text:
            params["warmup_enabled"] = True
        if "defaticamento" in text:
            params["cooldown_enabled"] = True

    # 3. Ripetute details parsing
    if workout_type == "ripetute":
        # Repetitions & interval length e.g. "5x1000m", "5 x 1000m", "5x1km", "5 ripetute da 1000m"
        rep_match = re.search(r'(\d+)\s*[x*]\s*(\d+(?:\.\d+)?)\s*(m|meter|metri|km|chilometri|s|sec|secondi|min|minuti)?', text)
        if not rep_match:
            rep_match = re.search(r'(\d+)\s*(?:ripetizioni|volte|ripetute)?\s*(?:da|di)?\s*(\d+(?:\.\d+)?)\s*(m|meter|metri|km|chilometri|s|sec|secondi|min|minuti)?', text)
            
        if rep_match:
            params["repetitions"] = int(rep_match.group(1))
            val = float(rep_match.group(2))
            unit = rep_match.group(3) or "m"
            if "km" in unit or "chilometri" in unit:
                params["interval_type"] = "distance"
                params["interval_value"] = val * 1000.0
            elif "s" in unit or "sec" in unit or "secondi" in unit:
                params["interval_type"] = "time"
                params["interval_value"] = val
            elif "min" in unit or "minuti" in unit:
                params["interval_type"] = "time"
                params["interval_value"] = val * 60.0
            else:
                params["interval_type"] = "distance"
                params["interval_value"] = val
                
        # Recovery e.g. "recupero 90s", "recupero di 2 min", "90s di recupero"
        rec_match = re.search(r'(?:recupero|rec)\s*(?:da|di\s*)?\s*(\d+(?:\.\d+)?)\s*(s|sec|secondi|m|metri|km|min|minuti)', text)
        if not rec_match:
            rec_match = re.search(r'(\d+(?:\.\d+)?)\s*(s|sec|secondi|m|metri|km|min|minuti)\s*(?:di\s*)?(?:recupero|rec)', text)
            
        if rec_match:
            val = float(rec_match.group(1))
            unit = rec_match.group(2)
            if "min" in unit or "minuti" in unit:
                params["recovery_type"] = "time"
                params["recovery_value"] = val * 60.0
            elif "s" in unit or "sec" in unit or "secondi" in unit:
                params["recovery_type"] = "time"
                params["recovery_value"] = val
            elif "km" in unit:
                params["recovery_type"] = "distance"
                params["recovery_value"] = val * 1000.0
            else:
                params["recovery_type"] = "distance"
                params["recovery_value"] = val

    return params


from sqlalchemy import func

def get_current_shoe_mileage(db: Session, shoe_name: str = "Asics Gel-Nimbus 27") -> float:
    shoe = db.query(Shoe).filter(Shoe.name == shoe_name).first()
    if not shoe:
        baseline_date = datetime.date(2026, 6, 15)
        baseline_km = 632.5 if shoe_name == "Asics Gel-Nimbus 27" else 0.0
    else:
        baseline_date = shoe.baseline_date or datetime.date(2026, 6, 15)
        baseline_km = shoe.baseline_km or 0.0
        
    new_km = db.query(func.sum(WorkoutExecuted.distance_km)).filter(
        WorkoutExecuted.date > baseline_date,
        WorkoutExecuted.shoe_used == shoe_name
    ).scalar() or 0.0
    return round(baseline_km + new_km, 1)

def sync_garmin_history_startup(days_back: int = 90):
    """
    Syncs Garmin Connect scale data for the last N days in a background thread at server startup.
    """
    import threading
    
    def run_sync():
        email = os.getenv("GARMIN_EMAIL")
        password = os.getenv("GARMIN_PASSWORD")
        if not email or not password:
            logger_print(f"[Garmin Sync] Credentials not configured in .env. Skipping lookback.")
            return
            
        logger_print(f"[Garmin Sync] Connection initiating (lookback: {days_back} days)...")
        try:
            from garminconnect import Garmin
            client = Garmin(email, password)
            client.login()
            
            db = SessionLocal()
            try:
                today = datetime.date.today()
                synced_count = 0
                
                # Fetch last N days
                for i in range(days_back):
                    target_date = today - datetime.timedelta(days=i)
                    
                    # Check if already exists with weight in DB
                    existing = db.query(DailyMetric).filter(
                        DailyMetric.date == target_date,
                        DailyMetric.weight_kg.isnot(None)
                    ).first()
                    if existing:
                        continue
                        
                    try:
                        data = client.get_body_composition(target_date.isoformat())
                        if data and "totalAverage" in data and data["totalAverage"]:
                            avg = data["totalAverage"]
                            
                            metric = db.query(DailyMetric).filter(DailyMetric.date == target_date).first()
                            if not metric:
                                metric = DailyMetric(date=target_date)
                                db.add(metric)
                                
                            if avg.get("weight"):
                                metric.weight_kg = round(avg["weight"] / 1000.0, 2)
                            if avg.get("bodyFat"):
                                metric.body_fat_pct = round(avg["bodyFat"], 1)
                            if avg.get("muscleMass"):
                                metric.muscle_mass_kg = round(avg["muscleMass"] / 1000.0, 2)
                            if avg.get("waterPercent"):
                                metric.water_pct = round(avg["waterPercent"], 1)
                            if avg.get("boneMass"):
                                metric.bone_mass_kg = round(avg["boneMass"] / 1000.0, 2)
                                
                            # Standard defaults if not set
                            if not metric.sleep_hours:
                                metric.sleep_hours = 8.0
                            if not metric.resting_hr:
                                metric.resting_hr = 55
                            if not metric.hrv_score:
                                metric.hrv_score = 65
                            if not metric.steps:
                                metric.steps = 10000
                                
                            sleep_val = metric.sleep_hours if metric.sleep_hours is not None else 8.0
                            hrv_val = metric.hrv_score if metric.hrv_score is not None else 65
                            rhr_val = metric.resting_hr if metric.resting_hr is not None else 55
                            
                            sleep_score = min(100.0, (sleep_val / 8.0) * 100.0)
                            hrv_score = min(100.0, max(0.0, 80.0 + ((hrv_val - 65) * 2.0)))
                            rhr_score = min(100.0, max(0.0, 80.0 - ((rhr_val - 55) * 3.0)))
                            metric.readiness = round((sleep_score * 0.3) + (hrv_score * 0.4) + (rhr_score * 0.3), 1)
                            
                            db.commit()
                            synced_count += 1
                    except Exception as e:
                        if "429" in str(e):
                            logger_print(f"[Garmin Sync] Rate limited (429) at date {target_date}. Stopping lookback.")
                            break
                        logger_print(f"[Garmin Sync] Skipping {target_date} due to error: {e}")
                        
                logger_print(f"[Garmin Sync] lookback completed. Synced {synced_count} days.")
            finally:
                db.close()
        except Exception as e:
            logger_print(f"[Garmin Sync] Authentication or connection failed: {e}")
            
    t = threading.Thread(target=run_sync, daemon=True)
    t.start()

def start_daily_sync_scheduler():
    """
    Spawns a background daemon thread that wakes up every hour and checks
    if it is 4:00 AM, then runs a 7-day lookback sync from Garmin Connect.
    """
    import threading
    import time
    
    def scheduler_loop():
        logger_print("[Garmin Scheduler] Background scheduler loop started.")
        while True:
            # Sleep for 1 hour (3600 seconds)
            time.sleep(3600)
            now = datetime.datetime.now()
            if now.hour == 4:
                logger_print("[Garmin Scheduler] Waking up for daily scheduled scale sync...")
                # Run sync for last 7 days (lightweight and checks for missing entries)
                sync_garmin_history_startup(days_back=7)
                
    t = threading.Thread(target=scheduler_loop, daemon=True)
    t.start()


def sync_samsung_health_startup(days_back: int = 30):
    """
    Scarica i dati Samsung Health da Google Drive per gli ultimi N giorni
    in un thread daemon di background. Viene chiamato all'avvio del server.
    """
    import threading

    def _run():
        folder_id = os.getenv("SAMSUNG_DRIVE_FOLDER_ID", "").strip()
        if not folder_id:
            logger_print(
                "[Samsung Sync] SAMSUNG_DRIVE_FOLDER_ID non impostato nel .env. "
                "Imposta l'ID della cartella Drive con i dati Samsung Health."
            )
            return

        logger_print(f"[Samsung Sync] Avvio sync (lookback: {days_back} giorni)...")
        db = SessionLocal()
        try:
            report = samsung_run_sync(db, days_lookback=days_back)
            logger_print(f"[Samsung Sync] {report.summary()}")
            if report.errors:
                for err in report.errors:
                    logger_print(f"[Samsung Sync] ⚠️  {err}")
        except Exception as e:
            logger_print(f"[Samsung Sync] ❌ Errore: {e}")
        finally:
            db.close()

    t = threading.Thread(target=_run, daemon=True)
    t.start()


def start_samsung_sync_scheduler():
    """
    Spawns a background daemon thread that periodically runs the Samsung Health sync.
    The interval is configured via SAMSUNG_SYNC_INTERVAL_HOURS (default: 6 ore).
    """
    import threading
    import time

    interval_hours = int(os.getenv("SAMSUNG_SYNC_INTERVAL_HOURS", "6"))
    interval_sec = interval_hours * 3600

    def _loop():
        logger_print(f"[Samsung Scheduler] Scheduler avviato (intervallo: ogni {interval_hours}h).")
        while True:
            time.sleep(interval_sec)
            logger_print("[Samsung Scheduler] Avvio sync periodico Samsung Health...")
            sync_samsung_health_startup(days_back=7)

    t = threading.Thread(target=_loop, daemon=True)
    t.start()

def get_max_hr(db: Session) -> int:
    max_hr_val = db.query(func.max(WorkoutExecuted.max_hr)).scalar()
    return max_hr_val if (max_hr_val and max_hr_val > 0) else 196

def get_personal_records(db: Session) -> dict:
    def format_time(total_seconds: float) -> str:
        h = int(total_seconds // 3600)
        m = int((total_seconds % 3600) // 60)
        s = int(total_seconds % 60)
        if h > 0:
            return f"{h}:{m:02d}:{s:02d}"
        return f"{m}:{s:02d}"
        
    def get_best_for_distance(target_dist: float, fallback_time_str: str, fallback_pace_str: str) -> dict:
        best_wo = db.query(WorkoutExecuted).filter(
            WorkoutExecuted.distance_km >= target_dist,
            WorkoutExecuted.avg_pace > 0
        ).order_by(WorkoutExecuted.avg_pace.asc()).first()
        
        if best_wo:
            actual_dur = best_wo.distance_km * best_wo.avg_pace
            if abs(best_wo.distance_km - target_dist) / target_dist <= 0.05:
                calc_time = actual_dur
                calc_pace = best_wo.avg_pace
            else:
                calc_time = target_dist * best_wo.avg_pace
                calc_pace = best_wo.avg_pace
                
            pm = int(calc_pace // 60)
            ps = int(calc_pace % 60)
            
            return {
                "time": format_time(calc_time),
                "pace": f"{pm}:{ps:02d}/km",
                "date": best_wo.date.strftime("%d/%m/%Y") if best_wo.date else "",
                "workout_id": best_wo.id,
                "workout_name": best_wo.workout_type.replace("_", " ").title(),
                "is_real": True
            }
        return {
            "time": fallback_time_str,
            "pace": fallback_pace_str,
            "date": "N/D",
            "workout_id": None,
            "workout_name": "Valore predefinito",
            "is_real": False
        }
        
    return {
        "1k": get_best_for_distance(1.0, "4:30", "4:30/km"),
        "5k": get_best_for_distance(5.0, "23:30", "4:42/km"),
        "10k": get_best_for_distance(10.0, "48:55", "4:53/km"),
        "mezza": get_best_for_distance(21.0, "1:47:00", "5:04/km")
    }

def get_race_predictions(db: Session) -> dict:
    # 1. Calculate average weekly volume over the last 30 days
    thirty_days_ago = datetime.date.today() - datetime.timedelta(days=30)
    total_km_30d = db.query(func.sum(WorkoutExecuted.distance_km)).filter(WorkoutExecuted.date >= thirty_days_ago).scalar() or 0.0
    avg_weekly_vol = float((total_km_30d / 30.0) * 7.0)

    # 2. Adjust Riegel exponent b based on weekly volume (aerobic base)
    if avg_weekly_vol >= 40.0:
        b_val = 1.04  # High volume = less pace degradation over distance
    elif avg_weekly_vol >= 30.0:
        b_val = 1.06  # Standard baseline
    elif avg_weekly_vol >= 15.0:
        b_val = 1.09  # Medium-low volume
    else:
        b_val = 1.12  # Low volume = significant pace degradation over distance

    # 3. Estimate VO2Max using Uth formula: 15.3 * (max_hr / resting_hr)
    max_hr_db = db.query(func.max(WorkoutExecuted.max_hr)).scalar()
    if not max_hr_db or max_hr_db < 100:
        max_hr_db = 204  # Default baseline for 24-year-old athlete

    seven_days_ago = datetime.date.today() - datetime.timedelta(days=7)
    avg_resting_hr = db.query(func.avg(DailyMetric.resting_hr)).filter(
        DailyMetric.date >= seven_days_ago, 
        DailyMetric.resting_hr.isnot(None)
    ).scalar()
    if not avg_resting_hr or avg_resting_hr < 30:
        avg_resting_hr = 56.0  # Default athlete resting HR baseline

    vo2max = 15.3 * (max_hr_db / float(avg_resting_hr))

    # 4. Find the best workout of the last 60 days to base predictions on (min pace)
    workouts = db.query(WorkoutExecuted).filter(WorkoutExecuted.distance_km >= 4.5, WorkoutExecuted.avg_pace > 0).all()
    
    best_workout = None
    best_pace = 9999.0
    for w in workouts:
        if w.avg_pace < best_pace:
            best_pace = w.avg_pace
            best_workout = w
            
    if best_workout:
        d_ref = best_workout.distance_km
        t_ref = d_ref * best_workout.avg_pace
    else:
        # Fallback to 10k PB: 48:55 (2935 sec)
        d_ref = 10.0
        t_ref = 2935.0
        
    def predict_time_and_pace(d_target: float) -> tuple:
        t_target = t_ref * ((d_target / d_ref) ** b_val)
        pace_target_sec = t_target / d_target
        
        # Format time
        h = int(t_target // 3600)
        m = int((t_target % 3600) // 60)
        s = int(t_target % 60)
        if h > 0:
            time_str = f"{h}:{m:02d}:{s:02d}"
        else:
            time_str = f"{m}:{s:02d}"
            
        # Format pace
        pm = int(pace_target_sec // 60)
        ps = int(pace_target_sec % 60)
        pace_str = f"{pm}:{ps:02d}/km"
        
        return time_str, pace_str

    t_5k, p_5k = predict_time_and_pace(5.0)
    t_10k, p_10k = predict_time_and_pace(10.0)
    t_half, p_half = predict_time_and_pace(21.0975)
    t_mara, p_mara = predict_time_and_pace(42.195)
    
    return {
        "b_exponent": round(b_val, 2),
        "vo2max": round(vo2max, 1),
        "weekly_volume_avg": round(avg_weekly_vol, 1),
        "max_hr_real": max_hr_db,
        "resting_hr_avg": round(float(avg_resting_hr), 1),
        "5k": {"time": t_5k, "pace": p_5k},
        "10k": {"time": t_10k, "pace": p_10k},
        "mezza": {"time": t_half, "pace": p_half},
        "maratona": {"time": t_mara, "pace": p_mara}
    }


def calculate_workout_tss(workout: WorkoutExecuted, resting_hr: float, max_hr: float) -> float:
    """
    Calculates Training Stress Score (TSS) for a workout.
    Uses heart-rate based formula if heart rate data is present.
    Falls back to RPE-based estimation if heart rate is missing.
    """
    duration_min = (workout.distance_km * (workout.avg_pace or 300.0)) / 60.0
    if duration_min <= 0:
        duration_min = 40.0
        
    avg_hr = None
    if workout.laps_summary:
        hr_vals = [l["avg_hr"] for l in workout.laps_summary if l.get("avg_hr")]
        if hr_vals:
            avg_hr = sum(hr_vals) / len(hr_vals)
            
    if not avg_hr and workout.max_hr:
        avg_hr = workout.max_hr - 15
        
    if avg_hr and avg_hr > 0 and max_hr > resting_hr:
        reserve_pct = (avg_hr - resting_hr) / (max_hr - resting_hr)
        reserve_pct = max(0.0, min(1.2, reserve_pct))
        tss = (duration_min / 60.0) * (reserve_pct ** 2) * 100 * 1.15
        return round(tss, 1)
    else:
        rpe = workout.rpe_score or 5
        tss = duration_min * rpe * 1.4
        return round(tss, 1)


def get_training_load_history(db: Session, days_lookback: int = 60) -> dict:
    """
    Computes CTL, ATL, TSB trends for the last N days.
    """
    max_hr = get_max_hr(db)
    
    seven_days_ago = datetime.date.today() - datetime.timedelta(days=7)
    avg_resting_hr = db.query(func.avg(DailyMetric.resting_hr)).filter(
        DailyMetric.date >= seven_days_ago, 
        DailyMetric.resting_hr.isnot(None)
    ).scalar() or 56.0
    
    # Fetch all executed workouts from last 110 days to allow stable rolling averages
    start_date = datetime.date.today() - datetime.timedelta(days=days_lookback + 45)
    workouts = db.query(WorkoutExecuted).filter(WorkoutExecuted.date >= start_date).all()
    
    # Group TSS by date
    daily_tss = {}
    curr_d = start_date
    today = datetime.date.today()
    while curr_d <= today:
        daily_tss[curr_d] = 0.0
        curr_d += datetime.timedelta(days=1)
        
    for w in workouts:
        if w.date in daily_tss:
            daily_tss[w.date] += calculate_workout_tss(w, avg_resting_hr, max_hr)
            
    dates_sorted = sorted(list(daily_tss.keys()))
    
    ctl_dict = {}
    atl_dict = {}
    tsb_dict = {}
    
    for i, d in enumerate(dates_sorted):
        ctl_slice = [daily_tss[dates_sorted[j]] for j in range(max(0, i - 41), i + 1)]
        ctl_dict[d] = round(sum(ctl_slice) / 42.0, 1)
        
        atl_slice = [daily_tss[dates_sorted[j]] for j in range(max(0, i - 6), i + 1)]
        atl_dict[d] = round(sum(atl_slice) / 7.0, 1)
        
        tsb_dict[d] = round(ctl_dict[d] - atl_dict[d], 1)
        
    plot_start = today - datetime.timedelta(days=days_lookback)
    plot_dates = [d for d in dates_sorted if d >= plot_start]
    
    labels = [d.strftime("%d/%m") for d in plot_dates]
    ctl_values = [ctl_dict[d] for d in plot_dates]
    atl_values = [atl_dict[d] for d in plot_dates]
    tsb_values = [tsb_dict[d] for d in plot_dates]
    
    # ACWR (Acute-to-Chronic Workload Ratio) using weekly km volume
    d_7d_ago = today - datetime.timedelta(days=7)
    d_28d_ago = today - datetime.timedelta(days=28)
    
    acute_volume = db.query(func.sum(WorkoutExecuted.distance_km)).filter(WorkoutExecuted.date >= d_7d_ago).scalar() or 0.0
    chronic_volume_total = db.query(func.sum(WorkoutExecuted.distance_km)).filter(
        WorkoutExecuted.date >= d_28d_ago
    ).scalar() or 0.0
    chronic_volume = chronic_volume_total / 4.0 if chronic_volume_total > 0 else 0.0
    
    if chronic_volume > 0:
        acwr = round(float(acute_volume / chronic_volume), 2)
    else:
        acwr = 0.0
        
    if acwr >= 1.5:
        acwr_status = "pericolo"
        acwr_label = "Pericolo Infortunio (ACWR >= 1.50)"
        acwr_advice = "Il tuo carico acuto è cresciuto troppo velocemente! Riduci l'intensità e la distanza per evitare infortuni muscolari."
    elif acwr >= 1.3:
        acwr_status = "sovraccarico"
        acwr_label = "Sovraccarico Moderato (1.30 - 1.49)"
        acwr_advice = "Stai spingendo al limite del range sicuro. Monitora i fastidi e assicurati di recuperare bene."
    elif acwr >= 0.8:
        acwr_status = "ottimale"
        acwr_label = "Zona Ottimale (0.80 - 1.29)"
        acwr_advice = "Ottimo lavoro! Il tuo carico di allenamento sta crescendo in modo sicuro e costante (Sweet Spot)."
    else:
        acwr_status = "sottoallenamento"
        acwr_label = "Sotto-allenamento / Scarico (<0.80)"
        acwr_advice = "Il carico è molto basso. Ottimo per il recupero post-gara o tapering, ma non incrementerà il tuo fitness."
        
    return {
        "labels": labels,
        "ctl": ctl_values,
        "atl": atl_values,
        "tsb": tsb_values,
        "current_ctl": ctl_dict[today] if today in ctl_dict else 0.0,
        "current_atl": atl_dict[today] if today in atl_dict else 0.0,
        "current_tsb": tsb_dict[today] if today in tsb_dict else 0.0,
        "acwr": acwr,
        "acwr_status": acwr_status,
        "acwr_label": acwr_label,
        "acwr_advice": acwr_advice
    }


def get_tapering_advisor(db: Session) -> dict:
    """
    Determines if a planned race is scheduled and returns countdown and tapering details.
    """
    today = datetime.date.today()
    udine_date = datetime.date(2026, 9, 20)
    roma_date = datetime.date(2027, 3, 14)
    
    upcoming_races = []
    if udine_date >= today:
        upcoming_races.append(("Maratonina di Udine", udine_date, "mezza"))
    if roma_date >= today:
        upcoming_races.append(("Maratona di Roma", roma_date, "maratona"))
        
    if not upcoming_races:
        return {
            "has_race": False,
            "is_tapering": False,
            "race_name": None,
            "days_to_race": None,
            "weeks_to_taper": 0,
            "weeks_tapering": []
        }
        
    upcoming_races.sort(key=lambda x: x[1])
    race_name, race_date, race_type = upcoming_races[0]
    days_to_race = (race_date - today).days
    
    if days_to_race > 21:
        weeks_to_taper = (days_to_race - 21) // 7
        is_tapering = False
    else:
        weeks_to_taper = 0
        is_tapering = True
        
    thirty_days_ago = today - datetime.timedelta(days=30)
    total_km_30d = db.query(func.sum(WorkoutExecuted.distance_km)).filter(WorkoutExecuted.date >= thirty_days_ago).scalar() or 0.0
    avg_weekly_vol = float((total_km_30d / 30.0) * 7.0)
    if avg_weekly_vol < 15.0:
        avg_weekly_vol = 30.0
        
    w3_vol = round(avg_weekly_vol * 0.75, 1)
    w3_desc = "Riduzione lieve (-25%). Mantieni 1 o 2 sessioni di qualità brevi al passo gara stimato. Ottimo momento per massaggi e stretching."
    
    w2_vol = round(avg_weekly_vol * 0.55, 1)
    w2_desc = "Riduzione media (-45%). Ultimo richiamo di ritmo gara a metà settimana. Limita le sessioni di forza pesante."
    
    w1_vol = round(avg_weekly_vol * 0.30, 1)
    w1_desc = "Scarico massimo (-70%). Solo corse brevissime e facili con qualche allungo finale. Concentrati su sonno e carbo-loading."
    
    current_week = 3 if days_to_race > 14 else 2 if days_to_race > 7 else 1
    
    weeks = [
        {"week": 3, "volume_km": w3_vol, "description": w3_desc, "is_current": is_tapering and current_week == 3},
        {"week": 2, "volume_km": w2_vol, "description": w2_desc, "is_current": is_tapering and current_week == 2},
        {"week": 1, "volume_km": w1_vol, "description": w1_desc, "is_current": is_tapering and current_week == 1}
    ]
    
    return {
        "has_race": True,
        "is_tapering": is_tapering,
        "race_name": f"{race_name} (il {race_date.strftime('%d/%m/%Y')})",
        "days_to_race": days_to_race,
        "weeks_to_taper": weeks_to_taper,
        "weeks_tapering": weeks
    }


@app.post("/api/sync-samsung-health")
def trigger_samsung_sync(background_tasks: BackgroundTasks, days_back: int = 30, db: Session = Depends(get_db)):
    """
    Endpoint per triggering manuale del sync Samsung Health da Google Drive.
    Esegue in background e restituisce immediatamente lo stato iniziale.
    
    Query params:
      - days_back: quanti giorni di lookback (default 30)
    
    Esempio: POST /api/sync-samsung-health?days_back=7
    """
    folder_id = os.getenv("SAMSUNG_DRIVE_FOLDER_ID", "").strip()
    if not folder_id:
        raise HTTPException(
            status_code=400,
            detail=(
                "SAMSUNG_DRIVE_FOLDER_ID non configurato nel .env. "
                "Apri la cartella Drive, copia l'ID dall'URL e aggiungilo al .env."
            )
        )

    def _bg_sync():
        db_inner = SessionLocal()
        try:
            logger_print(f"[Samsung Sync] Sync manuale avviato (lookback: {days_back} giorni)...")
            report = samsung_run_sync(db_inner, days_lookback=days_back)
            logger_print(f"[Samsung Sync] {report.summary()}")
        except Exception as e:
            logger_print(f"[Samsung Sync] ❌ Errore sync manuale: {e}")
        finally:
            db_inner.close()

    background_tasks.add_task(_bg_sync)
    return {
        "status": "avviato",
        "message": f"Sync Samsung Health avviato in background (ultimi {days_back} giorni). Controlla i log del server.",
        "folder_id": folder_id[:8] + "..." if len(folder_id) > 8 else folder_id
    }


@app.post("/api/workout/{workout_id}/sweat-rate")

def save_workout_sweat_rate(workout_id: int, weight_pre: float = Form(...), weight_post: float = Form(...), fluids_ml: float = Form(...), db: Session = Depends(get_db)):
    workout = db.query(WorkoutExecuted).filter(WorkoutExecuted.id == workout_id).first()
    if not workout:
        raise HTTPException(status_code=404, detail="Workout not found")
    duration_hr = (workout.distance_km * (workout.avg_pace or 300.0)) / 3600.0
    if duration_hr <= 0:
        duration_hr = 0.75
    actual_loss_liters = weight_pre - weight_post + (fluids_ml / 1000.0)
    sweat_rate_l_h = actual_loss_liters / duration_hr
    
    # Save as an AgentInsight so we don't have to alter schema
    # Search for existing one
    existing_insight = db.query(AgentInsight).filter(
        AgentInsight.insight_type == "workout_sweat_rate"
    ).all()
    
    insight_to_update = None
    for ins in existing_insight:
        if ins.memory_payload.get("workout_id") == workout_id:
            insight_to_update = ins
            break
            
    payload = {
        "workout_id": workout_id,
        "weight_pre": weight_pre,
        "weight_post": weight_post,
        "fluids_ml": fluids_ml,
        "actual_loss_liters": round(actual_loss_liters, 2),
        "sweat_rate_l_h": round(sweat_rate_l_h, 2),
        "sodium_needed_mg": int(actual_loss_liters * 700)
    }
    
    if insight_to_update:
        insight_to_update.memory_payload = payload
    else:
        new_insight = AgentInsight(
            agent_name="nutritionist",
            insight_type="workout_sweat_rate",
            memory_payload=payload
        )
        db.add(new_insight)
    db.commit()
    return RedirectResponse(url=f"/workout/{workout_id}?sweat_calculated=1", status_code=303)


@app.post("/workout/{workout_id}/delete")
def delete_workout(workout_id: int, db: Session = Depends(get_db)):
    """
    Deletes a WorkoutExecuted record, its associated GPX file, and any associated AgentInsight records,
    then redirects the user to the history page.
    """
    workout = db.query(WorkoutExecuted).filter(WorkoutExecuted.id == workout_id).first()
    if not workout:
        raise HTTPException(status_code=404, detail="Allenamento non trovato")

    # 1. Try to delete the associated GPX file
    date_prefix = workout.date.strftime("%Y%m%d")
    for folder in ["./data/uploads", "./GPX"]:
        if os.path.exists(folder):
            for file in os.listdir(folder):
                if file.startswith(date_prefix) and file.endswith(".gpx"):
                    try:
                        os.remove(os.path.join(folder, file))
                        logger_print(f"Deleted GPX file: {file}")
                    except Exception as e:
                        logger_print(f"Could not delete GPX file {file}: {e}")
                    break

    # 2. Delete associated AgentInsight records (workout analysis, sweat rate, etc.)
    related_insights = db.query(AgentInsight).filter(
        AgentInsight.memory_payload["workout_id"].as_integer() == workout_id
    ).all()
    for insight in related_insights:
        db.delete(insight)

    # 3. Delete the workout itself
    db.delete(workout)
    db.commit()

    logger_print(f"[Delete] WorkoutExecuted ID={workout_id} eliminato dall'utente.")
    return RedirectResponse(url="/history", status_code=303)



@app.get("/dashboard", response_class=HTMLResponse)
def get_athlete_dashboard(request: Request, db: Session = Depends(get_db)):

    """
    Renders the premium Dark Mode dashboard with athletic stats,
    readiness metrics, plans, and historical logs.
    """
    today = datetime.date.today()
    
    # Calculate countdown to next races (Udine and Roma)
    now_dt = datetime.datetime.now()
    udine_dt = datetime.datetime(2026, 9, 20, 9, 30, 0)
    roma_dt = datetime.datetime(2027, 3, 14, 9, 0, 0)
    
    countdown_info = None
    upcoming_races = []
    if udine_dt >= now_dt:
        upcoming_races.append(("Maratonina di Udine", udine_dt))
    if roma_dt >= now_dt:
        upcoming_races.append(("Maratona di Roma", roma_dt))
        
    if upcoming_races:
        upcoming_races.sort(key=lambda x: x[1])
        next_race_name, next_race_dt = upcoming_races[0]
        delta = next_race_dt - now_dt
        days_left = delta.days
        countdown_info = {
            "race_name": next_race_name,
            "date_str": next_race_dt.strftime("%d/%m/%Y alle %H:%M"),
            "target_iso": next_race_dt.isoformat(),
            "days_left": days_left
        }
    
    # 1. Fetch latest daily metrics & readiness
    readiness_data = None
    latest_metric = db.query(DailyMetric).filter(DailyMetric.readiness.isnot(None)).order_by(DailyMetric.date.desc()).first()
    
    # Calcola le baseline dinamiche sugli ultimi 30 giorni con dati disponibili
    rhr_30d_avg = db.query(func.avg(DailyMetric.resting_hr)).filter(
        DailyMetric.resting_hr.isnot(None)
    ).order_by(DailyMetric.date.desc()).limit(30).scalar()
    
    hrv_30d_avg = db.query(func.avg(DailyMetric.hrv_score)).filter(
        DailyMetric.hrv_score.isnot(None)
    ).order_by(DailyMetric.date.desc()).limit(30).scalar()
    
    rhr_baseline = round(float(rhr_30d_avg), 1) if rhr_30d_avg else 55.0
    hrv_baseline = round(float(hrv_30d_avg), 1) if hrv_30d_avg else 65.0

    if latest_metric:
        readiness_val = latest_metric.readiness if latest_metric.readiness is not None else 70
        readiness_data = {
            "readiness_score": readiness_val,
            "sleep_hours": latest_metric.sleep_hours,
            "resting_hr": latest_metric.resting_hr,
            "hrv_score": latest_metric.hrv_score,
            "hrv_baseline": hrv_baseline,
            "rhr_baseline": rhr_baseline,
            "status": "eccellente" if readiness_val >= 85 else "buono" if readiness_val >= 70 else "affaticato" if readiness_val >= 50 else "necessita recupero"
        }
        
    # Last 10 days of metrics for Chart.js
    recent_metrics = db.query(DailyMetric).order_by(DailyMetric.date.desc()).limit(10).all()
    metrics_json = [
        {
            "date": m.date.strftime("%d/%m") if m.date else "",
            "readiness": m.readiness or 0,
            "hrv_score": m.hrv_score or 0
        }
        for m in recent_metrics
    ]
    
    # 2. Fetch recent workouts (executed)
    recent_workouts = db.query(WorkoutExecuted).order_by(WorkoutExecuted.date.desc()).limit(5).all()
    
    # 3. Fetch next planned workout
    next_planned = db.query(WorkoutPlanned).filter(WorkoutPlanned.status == "planned").order_by(WorkoutPlanned.date.asc()).first()
    next_workout_data = None
    normalized_type = "lento_corto"
    if next_planned:
        # Default shoe recommendation logic mapping
        shoe = "Asics Gel-Nimbus 27"
        
        # Normalize type for consistency
        raw_type = next_planned.type
        normalized_type = {
            "easy": "lento_corto",
            "tempo": "medio",
            "interval": "ripetute",
            "long": "lento_lungo",
            "race": "gara"
        }.get(raw_type, raw_type)
        
        parsed_params = extract_workout_parameters(next_planned.prompt_text, normalized_type)
        next_workout_data = {
            "id": next_planned.id,
            "date": next_planned.date.strftime("%A, %d %B %Y") if next_planned.date else "",
            "type": normalized_type,
            "target_distance": next_planned.target_distance,
            "prompt_text": next_planned.prompt_text,
            "pace_target": (next_planned.parser_template or {}).get("pace_target", ""),
            "shoe_recommendation": shoe,
            **parsed_params
        }
        
    # 4. Fetch latest nutrition recommendation
    # We estimate based on next workout or executed workout today
    kcal_burned = 0
    carbs_g = 304   # 4.0g/kg * 76kg
    protein_g = 121 # 1.6g/kg * 76kg
    fat_g = 76      # 1.0g/kg * 76kg
    advice = "Mantieni l'apporto calorico pulito e bilanciato."
    
    if next_planned:
        kcal_burned = int(next_planned.target_distance * 76.0) # 76kg weight
        is_hard = normalized_type in ["ripetute", "medio", "lento_lungo"]
        carbs_g = int(76.0 * (6.5 if is_hard else 4.0))
        advice = "Carica i carboidrati prima della corsa intensa di domani." if is_hard else advice
        
    latest_nutrition = {
        "recommended_daily_calories": 1700 + kcal_burned,
        "macros_target": {
            "carbs_g": carbs_g,
            "protein_g": protein_g,
            "fat_g": fat_g
        },
        "advice": advice
    }
    
    # Check if user logged a past meal today
    pasto_logged = db.query(NutritionLog).filter(NutritionLog.date == today).order_by(NutritionLog.id.desc()).first()
    pasto_data = None
    if pasto_logged:
        pasto_data = {
            "raw_input": pasto_logged.raw_input,
            "est_calories": pasto_logged.est_calories
        }

    # Custom stats
    shoe_mileage = get_current_shoe_mileage(db)
    predictions = get_race_predictions(db)
    from agents.nodes import build_athlete_performance_profile
    perf_profile = build_athlete_performance_profile(db)

    # 4.5 Fetch intraday metrics for the latest available date
    latest_hr_record = db.query(HeartRateIntraday).order_by(HeartRateIntraday.timestamp.desc()).first()
    intraday_hr_data = []
    selected_date_str = ""
    if latest_hr_record:
        target_day = latest_hr_record.timestamp.date()
        selected_date_str = target_day.strftime("%d/%m/%Y")
        
        # Carica tutti i record di quel giorno
        start_dt = datetime.datetime.combine(target_day, datetime.time.min)
        end_dt = datetime.datetime.combine(target_day, datetime.time.max)
        
        hr_records = db.query(HeartRateIntraday).filter(
            HeartRateIntraday.timestamp >= start_dt,
            HeartRateIntraday.timestamp <= end_dt
        ).order_by(HeartRateIntraday.timestamp.asc()).all()
        
        intraday_hr_data = [
            {"time": r.timestamp.strftime("%H:%M"), "bpm": r.bpm}
            for r in hr_records
        ]

    # Fasi del sonno per la notte più recente
    latest_sleep_record = db.query(SleepStageIntraday).order_by(SleepStageIntraday.timestamp.desc()).first()
    sleep_summary = {}
    if latest_sleep_record:
        # Prendi tutti i record delle ultime 24 ore rispetto all'ultimo record di sonno
        sleep_ref = latest_sleep_record.timestamp
        sleep_start = sleep_ref - datetime.timedelta(hours=14)
        
        sleep_records = db.query(SleepStageIntraday).filter(
            SleepStageIntraday.timestamp >= sleep_start,
            SleepStageIntraday.timestamp <= sleep_ref
        ).order_by(SleepStageIntraday.timestamp.asc()).all()
        
        if sleep_records:
            # Calcola orario di addormentamento (primo record) e sveglia (ultimo record)
            # Troviamo il primo record non sveglio come effettivo addormentamento, se possibile,
            # altrimenti usiamo il primo in assoluto.
            non_awake_records = [r for r in sleep_records if not any(k in r.stage.lower() for k in ["awake", "veglia"])]
            first_sleep = non_awake_records[0] if non_awake_records else sleep_records[0]
            last_sleep = sleep_records[-1]
            
            wake_time = last_sleep.timestamp + datetime.timedelta(seconds=last_sleep.duration_seconds)
            
            bedtime_str = first_sleep.timestamp.strftime("%H:%M")
            waketime_str = wake_time.strftime("%H:%M")
            
            # Somma minuti per fase
            durations = {"deep": 0, "rem": 0, "light": 0, "awake": 0}
            for r in sleep_records:
                stage = r.stage.lower()
                dur_min = r.duration_seconds / 60.0
                if "deep" in stage or "profondo" in stage:
                    durations["deep"] += dur_min
                elif "rem" in stage:
                    durations["rem"] += dur_min
                elif "light" in stage or "leggero" in stage:
                    durations["light"] += dur_min
                else:
                    durations["awake"] += dur_min
            
            # Arrotondamento
            for k in durations:
                durations[k] = round(durations[k])
                
            total_sleep_min = durations["deep"] + durations["rem"] + durations["light"]
            total_time_in_bed_min = total_sleep_min + durations["awake"]
            
            pct = {}
            if total_sleep_min > 0:
                pct["deep"] = round((durations["deep"] / total_sleep_min) * 100)
                pct["rem"] = round((durations["rem"] / total_sleep_min) * 100)
                pct["light"] = round((durations["light"] / total_sleep_min) * 100)
            else:
                pct = {"deep": 0, "rem": 0, "light": 0}
                
            sleep_summary = {
                "bedtime": bedtime_str,
                "waketime": waketime_str,
                "duration_hours": round(total_sleep_min / 60.0, 1),
                "durations_min": durations,
                "percentages": pct,
                "total_bedtime_hours": round(total_time_in_bed_min / 60.0, 1)
            }

    return templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context={
            "readiness": readiness_data,
            "metrics_json": metrics_json,
            "workouts": recent_workouts,
            "next_workout": next_workout_data,
            "nutrition": latest_nutrition,
            "pasto": pasto_data,
            "shoe_mileage": shoe_mileage,
            "predictions": predictions,
            "perf_profile": perf_profile,
            "countdown": countdown_info,
            "intraday_hr": intraday_hr_data,
            "sleep_summary": sleep_summary,
            "selected_date_str": selected_date_str
        }
    )


def fetch_workout_weather(db: Session, workout: WorkoutExecuted) -> dict:
    """
    Retrieves weather data (temperature, humidity, description) for a given workout.
    First tries to fetch from cached AgentInsight, otherwise requests Open-Meteo API
    by locating the GPX file, reading starting coordinates, and invoking the API.
    """
    # 1. Check cache first
    cached_insight = db.query(AgentInsight).filter(
        AgentInsight.insight_type == "workout_weather",
        AgentInsight.memory_payload["workout_id"].as_integer() == workout.id
    ).first()
    if cached_insight and cached_insight.memory_payload:
        return cached_insight.memory_payload

    # Default fallback
    weather_info = {
        "workout_id": workout.id,
        "temp": None,
        "humidity": None,
        "status": "N/D"
    }

    # 2. Find GPX file to get coordinates
    gpx_filename = None
    date_prefix = workout.date.strftime("%Y%m%d")
    for folder in ["./data/uploads", "./GPX"]:
        if os.path.exists(folder):
            for file in os.listdir(folder):
                if file.startswith(date_prefix) and file.endswith(".gpx"):
                    gpx_filename = os.path.join(folder, file)
                    break
            if gpx_filename:
                break

    if not gpx_filename:
        return weather_info

    try:
        from parser.parser import parse_gpx_to_df
        df = parse_gpx_to_df(gpx_filename)
        if not df.empty and "latitude" in df.columns and "longitude" in df.columns:
            # Get starting coordinates
            lat = round(float(df["latitude"].iloc[0]), 4)
            lon = round(float(df["longitude"].iloc[0]), 4)
            date_str = workout.date.isoformat()

            # Call Open-Meteo archive API
            import requests
            url = f"https://archive-api.open-meteo.com/v1/archive?latitude={lat}&longitude={lon}&start_date={date_str}&end_date={date_str}&hourly=temperature_2m,relative_humidity_2m,weather_code"
            resp = requests.get(url, timeout=5)
            if resp.status_code == 200:
                res_json = resp.json()
                hourly = res_json.get("hourly", {})
                temps = hourly.get("temperature_2m", [])
                humids = hourly.get("relative_humidity_2m", [])
                codes = hourly.get("weather_code", [])

                if temps:
                    # Get hour from GPX first point timestamp if available
                    workout_hour = 18 # default evening
                    if "timestamp" in df.columns and not df.empty:
                        first_ts = df["timestamp"].iloc[0]
                        if hasattr(first_ts, "hour"):
                            workout_hour = first_ts.hour
                        elif isinstance(first_ts, str):
                            try:
                                dt_parsed = datetime.datetime.fromisoformat(first_ts.replace("Z", "+00:00"))
                                workout_hour = dt_parsed.hour
                            except Exception:
                                pass

                    mid_idx = min(workout_hour, len(temps) - 1)
                    avg_temp = temps[mid_idx]
                    avg_humid = humids[mid_idx] if len(humids) > mid_idx else None
                    w_code = codes[mid_idx] if len(codes) > mid_idx else 0

                    # Map WMO weather codes to simple descriptions
                    # 0, 1: Soleggiato / Sereno
                    # 2: Parzialmente Nuvoloso
                    # 3: Nuvoloso / Coperto
                    # 45, 48: Nebbia
                    # 51-67, 80-82: Pioggia
                    # 71-77, 85-86: Neve
                    # 95+: Temporale
                    weather_desc = "Soleggiato"
                    if w_code == 2:
                        weather_desc = "Parzialmente Nuvoloso"
                    elif w_code == 3:
                        weather_desc = "Nuvoloso"
                    elif w_code in [45, 48]:
                        weather_desc = "Nebbia"
                    elif w_code in [51, 53, 55, 56, 57, 61, 63, 65, 66, 67, 80, 81, 82]:
                        weather_desc = "Pioggia"
                    elif w_code in [71, 73, 75, 77, 85, 86]:
                        weather_desc = "Neve"
                    elif w_code >= 95:
                        weather_desc = "Temporale"

                    weather_info["temp"] = round(avg_temp, 1)
                    weather_info["humidity"] = int(avg_humid) if avg_humid is not None else None
                    weather_info["status"] = weather_desc

                    # Cache in database
                    insight = AgentInsight(
                        agent_name="manager",
                        insight_type="workout_weather",
                        memory_payload=weather_info
                    )
                    db.add(insight)
                    db.commit()
    except Exception as e:
        print(f"Error fetching weather details: {e}")

    return weather_info


@app.get("/maintenance", response_class=HTMLResponse)
def get_maintenance(request: Request, db: Session = Depends(get_db)):
    """
    Renders the maintenance page to manage planned workouts and correct anomalies.
    """
    from agents.nodes import get_workout_days
    planned_workouts = db.query(WorkoutPlanned).order_by(WorkoutPlanned.date.desc()).all()
    workout_days = get_workout_days()
    return templates.TemplateResponse(
        request=request,
        name="maintenance.html",
        context={
            "planned_workouts": planned_workouts,
            "workout_days": workout_days
        }
    )


from pydantic import BaseModel
class UpdateStatusRequest(BaseModel):
    status: str

class UpdateDaysRequest(BaseModel):
    lento_corto: str
    medio: str
    ripetute: str
    lento_lungo: str

@app.post("/api/maintenance/update-status/{workout_id}")
def update_workout_status(workout_id: int, payload: UpdateStatusRequest, db: Session = Depends(get_db)):
    """
    Updates the status of a planned workout.
    """
    workout = db.query(WorkoutPlanned).filter(WorkoutPlanned.id == workout_id).first()
    if not workout:
        raise HTTPException(status_code=404, detail="Workout non trovato")
    
    if payload.status not in ["planned", "completed", "skipped"]:
        raise HTTPException(status_code=400, detail="Stato non valido")
    
    workout.status = payload.status
    db.commit()
    return {"status": "success", "workout_id": workout_id, "new_status": payload.status}


@app.post("/api/maintenance/update-days")
def update_workout_days(payload: UpdateDaysRequest):
    """
    Updates the planned workout days settings.
    """
    from agents.nodes import save_workout_days
    days_data = {
        "lento_corto": payload.lento_corto,
        "medio": payload.medio,
        "ripetute": payload.ripetute,
        "lento_lungo": payload.lento_lungo
    }
    save_workout_days(days_data)
    return {"status": "success", "workout_days": days_data}


@app.get("/history", response_class=HTMLResponse)
def get_workout_history(request: Request, db: Session = Depends(get_db)):
    """
    Renders the complete list of executed workouts on a dedicated history page.
    """
    workouts = db.query(WorkoutExecuted).order_by(WorkoutExecuted.date.desc()).all()
    
    # Enrich workouts with weather information
    enriched_workouts = []
    for w in workouts:
        weather = fetch_workout_weather(db, w)
        enriched_workouts.append({
            "obj": w,
            "weather": weather
        })

    shoe_mileage = get_current_shoe_mileage(db)
    predictions = get_race_predictions(db)
    from agents.nodes import build_athlete_performance_profile
    perf_profile = build_athlete_performance_profile(db)
    return templates.TemplateResponse(
        request=request,
        name="history.html",
        context={
            "workouts": enriched_workouts,
            "shoe_mileage": shoe_mileage,
            "predictions": predictions,
            "perf_profile": perf_profile
        }
    )


@app.post("/simulate/workout")
def simulate_workout(user_id: int, workout_type: str = "easy", db: Session = Depends(get_db)):
    """
    HTTP endpoint to simulate a workout completion without needing a physical Telegram interaction.
    Triggers the LangGraph multi-agent workflow.
    """
    # 1. Generate mock workout data using parser module
    parsed_wo = generate_mock_workout_data(workout_type)
    
    # 2. Run the agent pipeline
    result = run_agent_pipeline(
        user_id=user_id,
        message_text=f"[Simulazione: completato allenamento {workout_type}]",
        parsed_workout=parsed_wo
    )
    
    return {
        "status": "success",
        "simulated_workout": parsed_wo,
        "agent_response": result.get("response_message")
    }


@app.post("/simulate/metrics")
def simulate_metrics(user_id: int, sleep_hours: float, resting_hr: int, hrv_score: int, db: Session = Depends(get_db)):
    """
    HTTP endpoint to simulate logging daily metrics without needing a physical Telegram interaction.
    Triggers the LangGraph multi-agent workflow.
    """
    message_text = f"Oggi ho dormito {sleep_hours} ore, HRV score {hrv_score}, battiti a riposo {resting_hr}"
    
    # Run the agent pipeline
    result = run_agent_pipeline(
        user_id=user_id,
        message_text=message_text
    )
    
    return {
        "status": "success",
        "message_sent": message_text,
        "agent_response": result.get("response_message")
    }


@app.post("/upload-gpx")
async def upload_gpx(
    gpx_file: UploadFile = File(...),
    workout_type: str = Form("lento_corto"),
    warmup_enabled: bool = Form(False),
    warmup_type: str = Form("distance"),
    warmup_value: float = Form(0.0),
    cooldown_enabled: bool = Form(False),
    cooldown_type: str = Form("distance"),
    cooldown_value: float = Form(0.0),
    interval_type: str = Form("distance"),
    interval_value: float = Form(0.0),
    recovery_type: str = Form("distance"),
    recovery_value: float = Form(0.0),
    repetitions: int = Form(1),
    rpe_score: Optional[str] = Form(None),
    comment: Optional[str] = Form(None),
    shoe_used: str = Form("Asics Gel-Nimbus 27"),
    db: Session = Depends(get_db)
):
    """
    Handles uploading a GPX file and running the custom sequential segmenter.
    """
    # 1. Save GPX file
    os.makedirs("./data/uploads", exist_ok=True)
    file_path = f"./data/uploads/{gpx_file.filename}"
    with open(file_path, "wb") as buffer:
        buffer.write(await gpx_file.read())
        
    # Parse rpe_score optionally
    rpe_score_parsed = None
    if rpe_score and rpe_score.strip():
        try:
            rpe_score_parsed = int(rpe_score)
        except ValueError:
            pass

    # 2. Prepare parsing/segmentation params
    params = {
        "workout_type": workout_type,
        "warmup_enabled": warmup_enabled,
        "warmup_type": warmup_type,
        "warmup_value": warmup_value,
        "cooldown_enabled": cooldown_enabled,
        "cooldown_type": cooldown_type,
        "cooldown_value": cooldown_value,
        "interval_type": interval_type,
        "interval_value": interval_value,
        "recovery_type": recovery_type,
        "recovery_value": recovery_value,
        "repetitions": repetitions,
        "rpe_score": rpe_score_parsed,
        "shoe_used": shoe_used
    }
    
    try:
        # 3. Call parse_gpx_workout
        parsed_data = parse_gpx_workout(file_path, params)
        
        # Check if there is an active planned workout matching the workout_type to associate it
        # Priority: match by workout_type first (most specific), fallback to any planned
        db_types = [workout_type]
        reverse_map = {
            "lento_corto": "easy",
            "medio": "tempo",
            "ripetute": "interval",
            "lento_lungo": "long",
            "gara": "race"
        }
        if workout_type in reverse_map:
            db_types.append(reverse_map[workout_type])

        planned = db.query(WorkoutPlanned).filter(
            WorkoutPlanned.status == "planned",
            WorkoutPlanned.type.in_(db_types)
        ).order_by(WorkoutPlanned.date.asc()).first()
        
        # Fallback: any planned workout regardless of type
        if not planned:
            planned = db.query(WorkoutPlanned).filter(
                WorkoutPlanned.status == "planned"
            ).order_by(WorkoutPlanned.date.asc()).first()
            
        planned_id = planned.id if planned else None
        
        # 4. Insert into database
        executed = WorkoutExecuted(
            planned_id=planned_id,
            date=datetime.date.fromisoformat(parsed_data["date"]),
            workout_type=workout_type,
            distance_km=parsed_data["distance_km"],
            avg_pace=parsed_data["avg_pace"],
            avg_cadence=parsed_data.get("avg_cadence"),
            shoe_used=shoe_used,
            elevation_gain=parsed_data.get("elevation_gain"),
            elevation_loss=parsed_data.get("elevation_loss"),
            max_hr=parsed_data.get("max_hr"),
            laps_summary=parsed_data.get("laps_summary"),
            rpe_score=rpe_score_parsed,
            comment=comment
        )
        db.add(executed)
        
        if planned:
            planned.status = "completed"
            
        db.commit()
        
        # 5. Log insight for trainer
        insight = AgentInsight(
            agent_name="trainer",
            insight_type="workout_completion",
            memory_payload={
                "date": executed.date.isoformat(),
                "distance_km": executed.distance_km,
                "avg_pace_sec": executed.avg_pace,
                "rpe": executed.rpe_score,
                "comment": executed.comment,
                "workout_type": workout_type
            }
        )
        db.add(insight)
        db.commit()
        
        # 6. Run agent pipeline to analyze the workout and generate critique
        try:
            from agents.workflow import run_agent_pipeline
            notes = comment or "Nessuna nota aggiuntiva."
            user_msg = f"Analizza il mio allenamento di tipo {workout_type}. Distanza: {executed.distance_km} km. Passo medio: {executed.pace_formatted}. Note dell'atleta: {notes}"
            
            parsed_wo_payload = {
                "date": executed.date.isoformat(),
                "distance_km": executed.distance_km,
                "avg_pace": executed.avg_pace,
                "avg_cadence": executed.avg_cadence,
                "shoe_used": executed.shoe_used,
                "laps_summary": executed.laps_summary,
                "rpe_score": executed.rpe_score
            }
            
            agent_result = run_agent_pipeline(
                user_id=1,
                message_text=user_msg,
                parsed_workout=parsed_wo_payload
            )
            
            critique = agent_result.get("response_message")
            if critique:
                analysis_insight = AgentInsight(
                    agent_name="manager",
                    insight_type="workout_analysis",
                    memory_payload={
                        "workout_id": executed.id,
                        "analysis": critique
                    }
                )
                db.add(analysis_insight)
                db.commit()
                print(f"[Agent Pipeline] Successfully generated and stored workout analysis for ID: {executed.id}")
        except Exception as ae:
            print(f"Error running agent pipeline on workout upload: {ae}")
        
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=400, detail=f"Errore durante l'elaborazione del file GPX: {str(e)}")
        
    return RedirectResponse(url=f"/workout/{executed.id}?uploaded=1", status_code=303)




def send_telegram_message(message: str):
    """
    Sends a message to the Telegram chat configured in .env via the Telegram Bot API.
    """
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not token:
        logger_print("[Telegram] Missing TELEGRAM_BOT_TOKEN in environment variables.")
        return
    if not chat_id:
        logger_print("[Telegram] Missing TELEGRAM_CHAT_ID in environment variables. Message not sent.")
        return

    import requests
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": message,
        "parse_mode": "Markdown"
    }
    try:
        logger_print(f"[Telegram] Sending message to chat ID {chat_id}...")
        r = requests.post(url, json=payload, timeout=10)
        if r.status_code == 200:
            logger_print("[Telegram] Message sent successfully.")
        else:
            logger_print(f"[Telegram] Failed to send message (HTTP {r.status_code}): {r.text}")
            # Fallback to plain text in case of markdown formatting errors
            payload.pop("parse_mode", None)
            r = requests.post(url, json=payload, timeout=10)
            if r.status_code == 200:
                logger_print("[Telegram] Message sent successfully (fallback plain text).")
            else:
                logger_print(f"[Telegram] Failed to send message even with fallback: {r.text}")
    except Exception as e:
        logger_print(f"[Telegram] Error sending message: {e}")


@app.post("/request-plan")
def request_weekly_plan(request: Request, background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    """
    Triggers the full agent pipeline with intent 'ask_plan'.
    Calls build_athlete_performance_profile() then the Trainer LLM
    to generate a personalized 4-session weekly plan based on the
    athlete's real historical data. Returns JSON with the plan.
    """
    from agents.nodes import build_athlete_performance_profile
    from agents.workflow import run_agent_pipeline

    # Build performance profile directly for quick access
    perf_profile = build_athlete_performance_profile(db)

    # Run agent pipeline with ask_plan intent
    result = run_agent_pipeline(
        user_id=1,
        message_text="Generami il piano di allenamento per la prossima settimana"
    )

    weekly_plan = result.get("weekly_plan")
    response_message = result.get("response_message", "")

    if response_message:
        background_tasks.add_task(send_telegram_message, response_message)

    return {
        "status": "success",
        "weekly_plan": weekly_plan,
        "performance_profile": perf_profile,
        "agent_message": response_message
    }


@app.get("/workout/{workout_id}", response_class=HTMLResponse)
def get_workout_detail(workout_id: int, request: Request, db: Session = Depends(get_db)):
    """
    Renders the detailed telemetry page for a specific executed workout.
    """
    workout = db.query(WorkoutExecuted).filter(WorkoutExecuted.id == workout_id).first()
    if not workout:
        raise HTTPException(status_code=404, detail="Allenamento non trovato")
        
    # Format total duration
    total_sec = int(workout.distance_km * (workout.avg_pace or 0))
    hours = total_sec // 3600
    minutes = (total_sec % 3600) // 60
    seconds = total_sec % 60
    if hours > 0:
        duration_formatted = f"{hours}:{minutes:02d}:{seconds:02d}"
    else:
        duration_formatted = f"{minutes}:{seconds:02d}"
        
    # Search for GPX file in data/uploads and GPX
    gpx_filename = None
    date_prefix = workout.date.strftime("%Y%m%d")
    for folder in ["./data/uploads", "./GPX"]:
        if os.path.exists(folder):
            for file in os.listdir(folder):
                if file.startswith(date_prefix) and file.endswith(".gpx"):
                    gpx_filename = os.path.join(folder, file)
                    break
            if gpx_filename:
                break
                
    chart_points = []
    kilometric_splits = []
    hr_zones = []
    has_hr = False
    has_cadence = False
    has_elevation = False
    avg_hr_val = None
    
    if gpx_filename:
        try:
            from parser.parser import parse_gpx_to_df, clean_and_smooth_data
            df = parse_gpx_to_df(gpx_filename)
            if not df.empty:
                df_cleaned = clean_and_smooth_data(df)
                
                # Check for columns
                has_hr = "heart_rate" in df_cleaned.columns and (df_cleaned["heart_rate"] > 0).any()
                has_cadence = "cadence" in df_cleaned.columns and (df_cleaned["cadence"] > 0).any()
                has_elevation = "altitude_m" in df_cleaned.columns and df_cleaned["altitude_m"].notna().any()
                
                # Calculate time step dt between consecutive points
                df_cleaned["dt"] = df_cleaned["timestamp"].diff().dt.total_seconds().fillna(1.0)
                
                # Exclude stops/pauses:
                # 1. dt > 10.0 (indicates manual or auto-pause gaps)
                # 2. speed_m_s < 0.5 (indicates standing still / not moving)
                active_mask = (df_cleaned["dt"] <= 10.0) & (df_cleaned["speed_m_s"] >= 0.5)
                df_active = df_cleaned[active_mask].copy()
                
                # Fallback if too few active points
                if len(df_active) < 10:
                    df_active = df_cleaned.copy()
                    df_active["dt"] = 1.0
                
                # Update duration formatted based on active moving time
                total_sec = int(df_active["dt"].sum())
                hours = total_sec // 3600
                minutes = (total_sec % 3600) // 60
                seconds = total_sec % 60
                if hours > 0:
                    duration_formatted = f"{hours}:{minutes:02d}:{seconds:02d}"
                else:
                    duration_formatted = f"{minutes}:{seconds:02d}"
                
                # Downsample active points to maximum ~200 for chart rendering
                n = len(df_active)
                step = max(1, n // 200)
                df_down = df_active.iloc[::step]
                
                for idx, row in df_down.iterrows():
                    chart_points.append({
                        "dist": round(row.get("distance_m", 0.0) / 1000.0, 3),
                        "pace_sec": round(row.get("pace_sec_km", 360.0), 1) if row.get("pace_sec_km", 360.0) < 1200 else None,
                        "hr": int(row["heart_rate"]) if has_hr and row["heart_rate"] > 0 else None,
                        "cadence": int(row["cadence"]) if has_cadence and row["cadence"] > 0 else None,
                        "ele": round(row["altitude_m"], 1) if has_elevation and row["altitude_m"] is not None else None,
                        "lat": float(row["latitude"]) if "latitude" in row and pd.notna(row["latitude"]) else None,
                        "lon": float(row["longitude"]) if "longitude" in row and pd.notna(row["longitude"]) else None
                    })
                    
                # Calculate active kilometric splits
                # For 'medio' workouts: offset splits to start from end of warmup
                warmup_offset_m = 0.0
                if workout.workout_type == "medio" and workout.laps_summary:
                    for lap in workout.laps_summary:
                        lap_name = (lap.get("name") or "").lower()
                        if "riscaldamento" in lap_name:
                            warmup_offset_m = float(lap.get("distance_km", 0.0)) * 1000.0
                            break

                # Build adjusted distance column for splitting
                df_active = df_active.copy()
                df_active["distance_adj"] = df_active["distance_m"] - warmup_offset_m

                # Only include points after the warmup (and clamp negatives to 0)
                if warmup_offset_m > 0:
                    df_splits = df_active[df_active["distance_adj"] >= 0].copy()
                else:
                    df_splits = df_active.copy()
                    df_splits["distance_adj"] = df_splits["distance_m"]

                df_splits["split_idx"] = (df_splits["distance_adj"] // 1000).astype(int)
                grouped = df_splits.groupby("split_idx")
                splits = []
                for split_val, group in grouped:
                    if group.empty:
                        continue
                    
                    dist = (group["distance_adj"].max() - group["distance_adj"].min()) / 1000.0
                    if dist < 0.05 and split_val > 0 and split_val != df_splits["split_idx"].max():
                        continue
                        
                    dur = group["dt"].sum()
                    if dur <= 0:
                        dur = len(group)
                        
                    pace = dur / dist if dist > 0.05 else 0.0
                    
                    hr_split = group["heart_rate"].mean() if has_hr else 0.0
                    cad_split = group["cadence"].mean() if has_cadence else 0.0
                    
                    min_d = int(dur // 60)
                    sec_d = int(dur % 60)
                    dur_fmt = f"{min_d}:{sec_d:02d}"
                    
                    min_p = int(pace // 60)
                    sec_p = int(pace % 60)
                    pace_fmt = f"{min_p}:{sec_p:02d}"
                    
                    splits.append({
                        "lap_index": int(split_val) + 1,
                        "duration_formatted": dur_fmt,
                        "pace_formatted": pace_fmt,
                        "avg_hr": int(hr_split) if hr_split > 0 else None,
                        "avg_cadence": int(cad_split) if cad_split > 0 else None
                    })
                kilometric_splits = splits
                
                # Calculate heart rate zones using physiological constant (ATHLETE_MAX_HR = 196) or calibration
                ATHLETE_MAX_HR = get_max_hr(db)
                
                # Try to load calibrated HR zones from AgentInsight
                calibration_insight = db.query(AgentInsight).filter(
                    AgentInsight.insight_type == "hr_zone_calibration"
                ).order_by(AgentInsight.created_at.desc()).first()
                
                if calibration_insight and calibration_insight.memory_payload:
                    payload = calibration_insight.memory_payload
                    z1_limit = payload.get("z1_max", int(ATHLETE_MAX_HR * 0.60))
                    z2_limit = payload.get("z2_max", int(ATHLETE_MAX_HR * 0.70))
                    z3_limit = payload.get("z3_max", int(ATHLETE_MAX_HR * 0.80))
                    z4_limit = payload.get("z4_max", int(ATHLETE_MAX_HR * 0.90))
                    z5_limit = payload.get("z5_max", ATHLETE_MAX_HR)
                else:
                    z1_limit = int(ATHLETE_MAX_HR * 0.60)
                    z2_limit = int(ATHLETE_MAX_HR * 0.70)
                    z3_limit = int(ATHLETE_MAX_HR * 0.80)
                    z4_limit = int(ATHLETE_MAX_HR * 0.90)
                    z5_limit = ATHLETE_MAX_HR

                if has_hr:
                    avg_hr_val = df_active["heart_rate"].mean()
                    hr_values = [h for h in df_active["heart_rate"].values if h > 0]
                    
                    counts = {"z1": 0, "z2": 0, "z3": 0, "z4": 0, "z5": 0}
                    for h in hr_values:
                        if h < z1_limit:
                            counts["z1"] += 1
                        elif h < z2_limit:
                            counts["z2"] += 1
                        elif h < z3_limit:
                            counts["z3"] += 1
                        elif h < z4_limit:
                            counts["z4"] += 1
                        else:
                            counts["z5"] += 1
                            
                    total_counts = sum(counts.values())
                    
                    def format_zone_duration(seconds):
                        m = int(seconds // 60)
                        s = int(seconds % 60)
                        if m > 0:
                            return f"{m}m {s}s"
                        return f"{s}s"
                        
                    hr_zones = [
                        {"id": "z1", "name": "Zona 1 - Recupero Attivo", "range": f"< {z1_limit} bpm", "time_str": format_zone_duration(counts["z1"]), "percentage": round((counts["z1"]/total_counts)*100, 1) if total_counts else 0},
                        {"id": "z2", "name": "Zona 2 - Fondo Lento", "range": f"{z1_limit}-{z2_limit} bpm", "time_str": format_zone_duration(counts["z2"]), "percentage": round((counts["z2"]/total_counts)*100, 1) if total_counts else 0},
                        {"id": "z3", "name": "Zona 3 - Fondo Medio", "range": f"{z2_limit}-{z3_limit} bpm", "time_str": format_zone_duration(counts["z3"]), "percentage": round((counts["z3"]/total_counts)*100, 1) if total_counts else 0},
                        {"id": "z4", "name": "Zona 4 - Soglia Anaerobica", "range": f"{z3_limit}-{z4_limit} bpm", "time_str": format_zone_duration(counts["z4"]), "percentage": round((counts["z4"]/total_counts)*100, 1) if total_counts else 0},
                        {"id": "z5", "name": "Zona 5 - Massimo Sforzo", "range": f"> {z4_limit} bpm", "time_str": format_zone_duration(counts["z5"]), "percentage": round((counts["z5"]/total_counts)*100, 1) if total_counts else 0},
                    ]
        except Exception as e:
            print(f"GPX parsing error, falling back to mock: {e}")
            gpx_filename = None
            
    if not gpx_filename:
        # Fallback generated data for mock workouts
        import random
        import math
        
        has_hr = workout.max_hr is not None and workout.max_hr > 0
        has_cadence = workout.avg_cadence is not None and workout.avg_cadence > 0
        has_elevation = workout.elevation_gain is not None and workout.elevation_gain > 0
        
        dist_total = workout.distance_km
        pace_avg = workout.avg_pace or 315.0
        
        # Generate points
        num_pts = 150
        for i in range(num_pts):
            frac = i / (num_pts - 1)
            curr_dist = frac * dist_total
            pace_noise = random.uniform(-15, 15)
            curr_pace = pace_avg + pace_noise
            
            hr_base = workout.max_hr - 15 if workout.max_hr else 150
            hr_noise = random.randint(-6, 6)
            curr_hr = hr_base + hr_noise
            
            cad_base = workout.avg_cadence or 170.0
            cad_noise = random.randint(-3, 3)
            curr_cadence = cad_base + cad_noise
            
            curr_ele = 40.0 + 8.0 * math.sin(frac * 4 * math.pi) + random.uniform(-0.3, 0.3)
            
            chart_points.append({
                "dist": round(curr_dist, 3),
                "pace_sec": round(curr_pace, 1),
                "hr": int(curr_hr) if has_hr else None,
                "cadence": int(curr_cadence) if has_cadence else None,
                "ele": round(curr_ele, 1) if has_elevation else None
            })
            
        # Try to load calibrated HR zones from AgentInsight for fallback
        calibration_insight = db.query(AgentInsight).filter(
            AgentInsight.insight_type == "hr_zone_calibration"
        ).order_by(AgentInsight.created_at.desc()).first()
        
        ATHLETE_MAX_HR = get_max_hr(db)
        if calibration_insight and calibration_insight.memory_payload:
            payload = calibration_insight.memory_payload
            z1_limit = payload.get("z1_max", int(ATHLETE_MAX_HR * 0.60))
            z2_limit = payload.get("z2_max", int(ATHLETE_MAX_HR * 0.70))
            z3_limit = payload.get("z3_max", int(ATHLETE_MAX_HR * 0.80))
            z4_limit = payload.get("z4_max", int(ATHLETE_MAX_HR * 0.90))
        else:
            z1_limit = 117
            z2_limit = 137
            z3_limit = 137
            z4_limit = 156

        # Generate 1km splits
        splits = []
        for km in range(int(dist_total)):
            splits.append({
                "lap_index": km + 1,
                "duration_formatted": f"{int(pace_avg // 60)}:{int(pace_avg % 60):02d}",
                "pace_formatted": f"{int(pace_avg // 60)}:{int(pace_avg % 60):02d}",
                "avg_hr": int(workout.max_hr - 15) if has_hr else None,
                "avg_cadence": int(workout.avg_cadence) if has_cadence else None
            })
        if dist_total % 1 > 0.05:
            rem = dist_total % 1
            dur_rem = rem * pace_avg
            splits.append({
                "lap_index": len(splits) + 1,
                "duration_formatted": f"{int(dur_rem // 60)}:{int(dur_rem % 60):02d}",
                "pace_formatted": f"{int(pace_avg // 60)}:{int(pace_avg % 60):02d}",
                "avg_hr": int(workout.max_hr - 15) if has_hr else None,
                "avg_cadence": int(workout.avg_cadence) if has_cadence else None
            })
        kilometric_splits = splits
        
        if has_hr:
            hr_zones = [
                {"id": "z1", "name": "Zona 1 - Recupero Attivo", "range": f"< {z1_limit} bpm", "time_str": "3m 0s", "percentage": 8.0},
                {"id": "z2", "name": "Zona 2 - Fondo Lento", "range": f"{z1_limit}-{z2_limit} bpm", "time_str": "20m 0s", "percentage": 52.0},
                {"id": "z3", "name": "Zona 3 - Fondo Medio", "range": f"{z2_limit}-{z3_limit} bpm", "time_str": "12m 0s", "percentage": 30.0},
                {"id": "z4", "name": "Zona 4 - Soglia Anaerobica", "range": f"{z3_limit}-{z4_limit} bpm", "time_str": "4m 0s", "percentage": 10.0},
                {"id": "z5", "name": "Zona 5 - Massimo Sforzo", "range": f"> {z4_limit} bpm", "time_str": "0s", "percentage": 0.0},
            ]
            
    # Calculate cardio efficiency index (dist in km per bpm)
    efficiency_index = None
    if workout.avg_pace and workout.avg_pace > 0:
        avg_speed_m_s = 1000.0 / workout.avg_pace
        if not avg_hr_val:
            if workout.max_hr:
                avg_hr_val = workout.max_hr - 15
            elif workout.laps_summary:
                hr_vals = [l["avg_hr"] for l in workout.laps_summary if l.get("avg_hr")]
                if hr_vals:
                    avg_hr_val = sum(hr_vals) / len(hr_vals)
        
        if avg_hr_val and avg_hr_val > 0:
            efficiency_index = round((avg_speed_m_s / avg_hr_val) * 100, 2)
            
    # Calculate pace stability
    pace_stability = "N/D"
    if len(kilometric_splits) > 1:
        def get_sec(fmt):
            parts = fmt.split(":")
            if len(parts) == 2:
                return int(parts[0])*60 + int(parts[1])
            return 0
        split_secs = [get_sec(s["duration_formatted"]) for s in kilometric_splits[:-1]]
        if split_secs:
            import numpy as np
            std_dev = np.std(split_secs)
            if std_dev < 8:
                pace_stability = "Eccellente (<8s dev)"
            elif std_dev < 15:
                pace_stability = "Buona (<15s dev)"
            else:
                pace_stability = "Variabile (>15s dev)"
    elif len(kilometric_splits) == 1:
        pace_stability = "Costante"

    shoe_mileage = get_current_shoe_mileage(db)
    predictions = get_race_predictions(db)
    from agents.nodes import build_athlete_performance_profile
    perf_profile = build_athlete_performance_profile(db)
    workouts = db.query(WorkoutExecuted).order_by(WorkoutExecuted.date.desc()).all()
    
    # Fetch workout critique analysis
    workout_analysis = db.query(AgentInsight).filter(
        AgentInsight.insight_type == "workout_analysis",
        AgentInsight.memory_payload["workout_id"].as_integer() == workout_id
    ).order_by(AgentInsight.created_at.desc()).first()
    
    critique_text = None
    if workout_analysis and workout_analysis.memory_payload:
        critique_text = workout_analysis.memory_payload.get("analysis")

    # Sweat rate calculations
    weather = fetch_workout_weather(db, workout)
    temp = weather.get("temp") or 18.0
    
    weight = 76.0
    latest_metric = db.query(DailyMetric).filter(DailyMetric.weight_kg.isnot(None)).order_by(DailyMetric.date.desc()).first()
    if latest_metric:
        weight = latest_metric.weight_kg
        
    pace_min_km = (workout.avg_pace or 300.0) / 60.0
    temp_factor = 1.0 + max(-0.5, min(1.5, (temp - 15.0) / 15.0))
    est_sweat_rate = weight * 0.009 * temp_factor * (6.5 / (pace_min_km if pace_min_km > 0 else 6.5))
    est_sweat_rate = max(0.4, min(2.2, est_sweat_rate))
    
    duration_hr = (workout.distance_km * (workout.avg_pace or 300.0)) / 3600.0
    est_total_loss = est_sweat_rate * duration_hr
    
    recommended_fluid_ml_h = int(est_sweat_rate * 1000 * 0.8)
    recommended_sodium_mg = int(est_total_loss * 700)
    
    actual_sweat_data = None
    existing_insights = db.query(AgentInsight).filter(
        AgentInsight.insight_type == "workout_sweat_rate"
    ).all()
    for ins in existing_insights:
        if ins.memory_payload.get("workout_id") == workout_id:
            actual_sweat_data = ins.memory_payload
            break

    return templates.TemplateResponse(
        request=request,
        name="workout_detail.html",
        context={
            "workout": workout,
            "duration_formatted": duration_formatted,
            "chart_points": chart_points,
            "kilometric_splits": kilometric_splits,
            "hr_zones": hr_zones,
            "has_hr": has_hr,
            "has_cadence": has_cadence,
            "has_elevation": has_elevation,
            "efficiency_index": efficiency_index,
            "avg_hr_bpm": avg_hr_val or ((workout.max_hr - 15) if workout.max_hr else None),
            "pace_stability": pace_stability,
            "shoe_mileage": shoe_mileage,
            "predictions": predictions,
            "perf_profile": perf_profile,
            "workouts": workouts,
            "critique": critique_text,
            "est_sweat_rate": round(est_sweat_rate, 2),
            "est_total_loss": round(est_total_loss, 2),
            "recommended_fluid_ml_h": recommended_fluid_ml_h,
            "recommended_sodium_mg": recommended_sodium_mg,
            "actual_sweat": actual_sweat_data
        }
    )

@app.get("/profile", response_class=HTMLResponse)
def get_athlete_profile(request: Request, db: Session = Depends(get_db)):
    # Age calculation: Born on September 1, 2001
    birth_date = datetime.date(2001, 9, 1)
    today = datetime.date.today()
    age = today.year - birth_date.year - ((today.month, today.day) < (birth_date.month, birth_date.day))
    
    # Dynamic Max HR
    max_hr_val = get_max_hr(db)
    
    # Dynamic PBs
    personal_records = get_personal_records(db)
    
    # Dynamic Active Shoes from DB
    active_shoes = db.query(Shoe).filter(Shoe.is_active == True).all()
    shoes_mileage = {
        shoe.name: get_current_shoe_mileage(db, shoe.name)
        for shoe in active_shoes
    }
    shoes_list = [
        {
            "id": shoe.id,
            "name": shoe.name,
            "mileage": shoes_mileage.get(shoe.name, 0.0)
        }
        for shoe in active_shoes
    ]
    
    # Fetch latest weight/body metrics from DailyMetric
    latest_garmin_metric = db.query(DailyMetric).filter(
        DailyMetric.weight_kg.isnot(None)
    ).order_by(DailyMetric.date.desc()).first()
    
    garmin_metrics = None
    if latest_garmin_metric:
        garmin_metrics = {
            "weight_kg": latest_garmin_metric.weight_kg,
            "body_fat_pct": latest_garmin_metric.body_fat_pct,
            "muscle_mass_kg": latest_garmin_metric.muscle_mass_kg,
            "water_pct": latest_garmin_metric.water_pct,
            "bone_mass_kg": latest_garmin_metric.bone_mass_kg,
            "date": latest_garmin_metric.date.strftime("%d/%m/%Y")
        }
    
    shoe_mileage = get_current_shoe_mileage(db)
    predictions = get_race_predictions(db)
    from agents.nodes import build_athlete_performance_profile
    perf_profile = build_athlete_performance_profile(db)
    # Fetch calibrated zones from AgentInsight
    calibration_insight = db.query(AgentInsight).filter(
        AgentInsight.insight_type == "hr_zone_calibration"
    ).order_by(AgentInsight.created_at.desc()).first()
    
    calibrated_zones = None
    if calibration_insight and calibration_insight.memory_payload:
        calibrated_zones = calibration_insight.memory_payload

    return templates.TemplateResponse(
        request=request,
        name="profile.html",
        context={
            "age": age,
            "max_hr_val": max_hr_val,
            "personal_records": personal_records,
            "shoes_list": shoes_list,
            "shoes_mileage": shoes_mileage,
            "garmin_metrics": garmin_metrics,
            "shoe_mileage": shoe_mileage,
            "predictions": predictions,
            "perf_profile": perf_profile,
            "calibrated_zones": calibrated_zones
        }
    )

@app.get("/profile/weight-history", response_class=HTMLResponse)
def get_weight_history_page(request: Request, db: Session = Depends(get_db)):
    """
    Renders weight and body composition history charts and tables.
    """
    metrics_chronological = db.query(DailyMetric).filter(
        DailyMetric.weight_kg.isnot(None)
    ).order_by(DailyMetric.date.asc()).all()
    
    chart_dates = [m.date.strftime("%d/%m/%Y") for m in metrics_chronological]
    chart_weights = [m.weight_kg for m in metrics_chronological]
    chart_body_fat = [m.body_fat_pct for m in metrics_chronological]
    
    metrics_newest = list(reversed(metrics_chronological))
    
    shoe_mileage = get_current_shoe_mileage(db)
    predictions = get_race_predictions(db)
    from agents.nodes import build_athlete_performance_profile
    perf_profile = build_athlete_performance_profile(db)
    workouts = db.query(WorkoutExecuted).order_by(WorkoutExecuted.date.desc()).all()
    
    return templates.TemplateResponse(
        request=request,
        name="weight_history.html",
        context={
            "metrics_list": metrics_newest,
            "chart_dates": json.dumps(chart_dates),
            "chart_weights": json.dumps(chart_weights),
            "chart_body_fat": json.dumps(chart_body_fat),
            "shoe_mileage": shoe_mileage,
            "predictions": predictions,
            "perf_profile": perf_profile,
            "workouts": workouts
        }
    )

@app.post("/profile/sync-garmin")
def sync_garmin_scale(db: Session = Depends(get_db)):
    """
    Syncs real physiological metrics from Garmin Connect / Garmin Index Smart Scale
    using credentials set in .env.
    """
    email = os.getenv("GARMIN_EMAIL")
    password = os.getenv("GARMIN_PASSWORD")
    
    if not email or not password:
        return RedirectResponse(url="/profile?error=missing_credentials", status_code=303)
        
    try:
        from garminconnect import Garmin
        client = Garmin(email, password)
        client.login()
        
        today = datetime.date.today()
        fetched_data = None
        sync_date = today
        
        # Look back up to 7 days
        for i in range(7):
            target_date = today - datetime.timedelta(days=i)
            try:
                data = client.get_body_composition(target_date.isoformat())
                if data and "totalAverage" in data and data["totalAverage"]:
                    fetched_data = data
                    sync_date = target_date
                    break
            except Exception as e:
                logger_print(f"Skipping date {target_date} during Garmin search: {e}")
                
        if not fetched_data:
            return RedirectResponse(url="/profile?error=no_garmin_data", status_code=303)
            
        avg = fetched_data["totalAverage"]
        
        metric = db.query(DailyMetric).filter(DailyMetric.date == sync_date).first()
        if not metric:
            metric = DailyMetric(date=sync_date)
            db.add(metric)
            
        if avg.get("weight"):
            metric.weight_kg = round(avg["weight"] / 1000.0, 2)
        if avg.get("bodyFat"):
            metric.body_fat_pct = round(avg["bodyFat"], 1)
        if avg.get("muscleMass"):
            metric.muscle_mass_kg = round(avg["muscleMass"] / 1000.0, 2)
        if avg.get("waterPercent"):
            metric.water_pct = round(avg["waterPercent"], 1)
        if avg.get("boneMass"):
            metric.bone_mass_kg = round(avg["boneMass"] / 1000.0, 2)
            
        # Standard defaults for daily metrics fields if not set
        if not metric.sleep_hours:
            metric.sleep_hours = 8.0
        if not metric.resting_hr:
            metric.resting_hr = 55
        if not metric.hrv_score:
            metric.hrv_score = 65
        if not metric.steps:
            metric.steps = 10000
            
        sleep_val = metric.sleep_hours if metric.sleep_hours is not None else 8.0
        hrv_val = metric.hrv_score if metric.hrv_score is not None else 65
        rhr_val = metric.resting_hr if metric.resting_hr is not None else 55
        
        sleep_score = min(100.0, (sleep_val / 8.0) * 100.0)
        hrv_score = min(100.0, max(0.0, 80.0 + ((hrv_val - 65) * 2.0)))
        rhr_score = min(100.0, max(0.0, 80.0 - ((rhr_val - 55) * 3.0)))
        metric.readiness = round((sleep_score * 0.3) + (hrv_score * 0.4) + (rhr_score * 0.3), 1)
        
        db.commit()
        return RedirectResponse(url=f"/profile?synced=1&sync_date={sync_date.isoformat()}", status_code=303)
        
    except Exception as e:
        logger_print(f"Error authenticating or fetching from Garmin Connect: {e}")
        import urllib.parse
        err_details = urllib.parse.quote(str(e))
        return RedirectResponse(url=f"/profile?error=garmin_api_error&details={err_details}", status_code=303)

@app.post("/shoes/add")
def add_shoe(
    name: str = Form(...),
    baseline_km: float = Form(0.0),
    db: Session = Depends(get_db)
):
    existing = db.query(Shoe).filter(Shoe.name == name).first()
    if existing:
        existing.is_active = True
        existing.baseline_km = baseline_km
        existing.baseline_date = datetime.date.today()
    else:
        new_shoe = Shoe(
            name=name,
            baseline_km=baseline_km,
            baseline_date=datetime.date.today(),
            is_active=True
        )
        db.add(new_shoe)
    db.commit()
    return RedirectResponse(url="/profile?added_shoe=1", status_code=303)

@app.post("/shoes/delete/{shoe_id}")
def delete_shoe(shoe_id: int, db: Session = Depends(get_db)):
    shoe = db.query(Shoe).filter(Shoe.id == shoe_id).first()
    if not shoe:
        raise HTTPException(status_code=404, detail="Scarpa non trovata")
    db.delete(shoe)
    db.commit()
    return RedirectResponse(url="/profile?deleted_shoe=1", status_code=303)


@app.get("/predictions", response_class=HTMLResponse)
def get_predictions_page(request: Request, db: Session = Depends(get_db)):
    shoe_mileage = get_current_shoe_mileage(db)
    predictions = get_race_predictions(db)
    from agents.nodes import build_athlete_performance_profile
    perf_profile = build_athlete_performance_profile(db)
    workouts = db.query(WorkoutExecuted).order_by(WorkoutExecuted.date.desc()).all()
    tapering = get_tapering_advisor(db)
    
    return templates.TemplateResponse(
        request=request,
        name="predictions.html",
        context={
            "shoe_mileage": shoe_mileage,
            "predictions": predictions,
            "perf_profile": perf_profile,
            "workouts": workouts,
            "tapering": tapering
        }
    )


@app.get("/stretching", response_class=HTMLResponse)
def get_stretching_page(request: Request, db: Session = Depends(get_db)):
    shoe_mileage = get_current_shoe_mileage(db)
    predictions = get_race_predictions(db)
    from agents.nodes import build_athlete_performance_profile
    perf_profile = build_athlete_performance_profile(db)
    
    planned_workouts = db.query(WorkoutPlanned).filter(WorkoutPlanned.status == "planned").order_by(WorkoutPlanned.date.asc()).all()
    executed_workouts = db.query(WorkoutExecuted).order_by(WorkoutExecuted.date.desc()).all()
    
    return templates.TemplateResponse(
        request=request,
        name="stretching.html",
        context={
            "shoe_mileage": shoe_mileage,
            "predictions": predictions,
            "perf_profile": perf_profile,
            "planned_workouts": planned_workouts,
            "executed_workouts": executed_workouts
        }
    )


@app.get("/pain-log", response_class=HTMLResponse)
def get_pain_log_page(request: Request, db: Session = Depends(get_db)):
    shoe_mileage = get_current_shoe_mileage(db)
    predictions = get_race_predictions(db)
    from agents.nodes import build_athlete_performance_profile
    perf_profile = build_athlete_performance_profile(db)
    workouts = db.query(WorkoutExecuted).order_by(WorkoutExecuted.date.desc()).all()
    
    return templates.TemplateResponse(
        request=request,
        name="pain_log.html",
        context={
            "shoe_mileage": shoe_mileage,
            "predictions": predictions,
            "perf_profile": perf_profile,
            "workouts": workouts
        }
    )


@app.get("/progress", response_class=HTMLResponse)
def get_progress_page(request: Request, db: Session = Depends(get_db)):
    shoe_mileage = get_current_shoe_mileage(db)
    predictions = get_race_predictions(db)
    from agents.nodes import build_athlete_performance_profile
    perf_profile = build_athlete_performance_profile(db)
    workouts = db.query(WorkoutExecuted).order_by(WorkoutExecuted.date.desc()).all()
    training_load = get_training_load_history(db)
    
    return templates.TemplateResponse(
        request=request,
        name="progress.html",
        context={
            "shoe_mileage": shoe_mileage,
            "predictions": predictions,
            "perf_profile": perf_profile,
            "workouts": workouts,
            "training_load": training_load
        }
    )


@app.get("/nutrition", response_class=HTMLResponse)
def get_nutrition_page(request: Request, db: Session = Depends(get_db)):
    shoe_mileage = get_current_shoe_mileage(db)
    predictions = get_race_predictions(db)
    from agents.nodes import build_athlete_performance_profile
    perf_profile = build_athlete_performance_profile(db)
    
    # We pass workouts just in case we need to reference them in the sidebar
    workouts = db.query(WorkoutExecuted).order_by(WorkoutExecuted.date.desc()).all()
    
    return templates.TemplateResponse(
        request=request,
        name="nutrition.html",
        context={
            "shoe_mileage": shoe_mileage,
            "predictions": predictions,
            "perf_profile": perf_profile,
            "workouts": workouts
        }
    )


# ─────────────────────────────────────────────────────────────────────────────
# NEW API ENDPOINTS: CHAT, STRETCHING, AND PAIN LOGS
# ─────────────────────────────────────────────────────────────────────────────
import base64

@app.get("/api/chat/history")
def get_chat_history(db: Session = Depends(get_db)):
    messages = db.query(ChatMessage).order_by(ChatMessage.timestamp.asc()).all()
    return [
        {
            "id": msg.id,
            "timestamp": msg.timestamp.isoformat(),
            "role": msg.role,
            "message_text": msg.message_text,
            "image_path": msg.image_path
        }
        for msg in messages
    ]

@app.post("/api/chat")
async def post_chat_message(
    message: str = Form(...),
    image: Optional[UploadFile] = File(None),
    db: Session = Depends(get_db)
):
    # Save user message to database
    image_relative_path = None
    base64_img = None
    
    if image and image.filename:
        # Create directories if they do not exist
        os.makedirs("./static/uploads/chat", exist_ok=True)
        file_path = f"./static/uploads/chat/{datetime.datetime.now().strftime('%Y%m%d%H%M%S')}_{image.filename}"
        with open(file_path, "wb") as buffer:
            buffer.write(await image.read())
        image_relative_path = f"/static/uploads/chat/{os.path.basename(file_path)}"
        
        # Read file again to encode as base64 for Gemini
        with open(file_path, "rb") as img_file:
            base64_img = base64.b64encode(img_file.read()).decode("utf-8")
            
    # Save user message in DB
    user_msg = ChatMessage(
        role="user",
        message_text=message,
        image_path=image_relative_path
    )
    db.add(user_msg)
    db.commit()
    
    # Run agent pipeline
    try:
        result = run_agent_pipeline(
            user_id=1,
            message_text=message,
            image_data=base64_img
        )
        response_text = result.get("response_message", "Nessuna risposta ricevuta dal team.")
    except Exception as e:
        response_text = f"Errore nell'elaborazione da parte del team di coach: {str(e)}"
        
    # Save assistant message in DB
    assistant_msg = ChatMessage(
        role="assistant",
        message_text=response_text
    )
    db.add(assistant_msg)
    db.commit()
    
    return {
        "user_message": {
            "role": "user",
            "message_text": message,
            "image_path": image_relative_path
        },
        "assistant_message": {
            "role": "assistant",
            "message_text": response_text
        }
    }

@app.post("/api/pain-log")
def create_pain_log(
    body_part: str = Form(...),
    intensity: int = Form(...),
    notes: Optional[str] = Form(None),
    db: Session = Depends(get_db)
):
    log = PainLog(
        body_part=body_part,
        intensity=intensity,
        notes=notes
    )
    db.add(log)
    db.commit()
    
    # Log an agent insight for the Physiologist to review
    insight = AgentInsight(
        agent_name="physiologist",
        insight_type="pain_logged",
        memory_payload={
            "body_part": body_part,
            "intensity": intensity,
            "notes": notes,
            "date": datetime.date.today().isoformat()
        }
    )
    db.add(insight)
    db.commit()
    
    return {"status": "success", "pain_log": {"body_part": body_part, "intensity": intensity, "notes": notes}}

@app.get("/api/pain-logs")
def get_pain_logs(db: Session = Depends(get_db)):
    logs = db.query(PainLog).order_by(PainLog.date.desc()).all()
    return [
        {
            "id": l.id,
            "date": l.date.isoformat(),
            "body_part": l.body_part,
            "intensity": l.intensity,
            "notes": l.notes
        }
        for l in logs
    ]

@app.get("/api/stretching")
def get_stretching_recommendations(workout_id: Optional[str] = None, db: Session = Depends(get_db)):
    workout = None
    is_planned = False
    
    if workout_id:
        if workout_id.startswith("planned_"):
            try:
                pid = int(workout_id.split("_")[1])
                workout = db.query(WorkoutPlanned).filter(WorkoutPlanned.id == pid).first()
                is_planned = True
            except (IndexError, ValueError):
                pass
        elif workout_id.startswith("executed_"):
            try:
                eid = int(workout_id.split("_")[1])
                workout = db.query(WorkoutExecuted).filter(WorkoutExecuted.id == eid).first()
                is_planned = False
            except (IndexError, ValueError):
                pass
        else:
            try:
                eid = int(workout_id)
                workout = db.query(WorkoutExecuted).filter(WorkoutExecuted.id == eid).first()
                is_planned = False
            except ValueError:
                pass
    else:
        # Default to the next planned workout
        workout = db.query(WorkoutPlanned).filter(WorkoutPlanned.status == "planned").order_by(WorkoutPlanned.date.asc()).first()
        is_planned = True
        
        # If no planned workout, fallback to the latest executed workout
        if not workout:
            workout = db.query(WorkoutExecuted).order_by(WorkoutExecuted.date.desc()).first()
            is_planned = False
            
    if not workout:
        return {
            "workout_found": False,
            "title": "Stretching Generale di Mantenimento",
            "exercises": [
                {"name": "Allungamento Polpacci", "duration": "30 secondi per gamba", "description": "Spingiti contro un muro tenendo una gamba tesa all'indietro e il tallone a terra."},
                {"name": "Allungamento Quadricipiti", "duration": "30 secondi per gamba", "description": "In piedi, afferra la caviglia destra con la mano destra e porta il tallone al gluteo."},
                {"name": "Allungamento Bicipiti Femorali", "duration": "40 secondi", "description": "Seduto con le gambe tese in avanti, piegati dal bacino provando a toccare le punte dei piedi."}
            ]
        }
        
    if is_planned:
        raw_type = workout.type
        type_map = {
            "easy": "lento_corto",
            "tempo": "medio",
            "interval": "ripetute",
            "long": "lento_lungo",
            "race": "gara"
        }
        wtype = type_map.get(raw_type, raw_type)
        distance_km = workout.target_distance
        dur_min = int(distance_km * 5.5) # estimate 5:30/km
        rpe = 5
    else:
        wtype = workout.workout_type
        distance_km = workout.distance_km
        dur_min = int(workout.distance_km * (workout.avg_pace or 300) / 60)
        rpe = workout.rpe_score or 5
        
    exercises = []
    
    # Customize stretching based on workout details
    if wtype in ["ripetute", "gara", "medio"]:
        exercises = [
            {"name": "Dynamic Hip Flexor Stretch", "duration": "45 secondi per lato", "description": "Affondo profondo in avanti poggiando il ginocchio posteriore a terra. Spingi il bacino leggermente in avanti."},
            {"name": "Stretching Glutei (Crossover)", "duration": "30 secondi per lato", "description": "Sdraiato sulla schiena, incrocia la caviglia destra sopra il ginocchio sinistro e tira la coscia sinistra verso il petto."},
            {"name": "Allungamento Polpacci e Tendine d'Achille", "duration": "40 secondi per gamba", "description": "Spingiti contro la parete, piega leggermente il ginocchio posteriore per allungare il muscolo soleo."}
        ]
        if is_planned:
            title = f"Stretching Consigliato: Post-Qualità ({wtype.upper().replace('_',' ')})"
        else:
            title = f"Rilassamento Muscolare Post-Qualità ({wtype.upper().replace('_',' ')})"
    elif wtype == "lento_lungo":
        exercises = [
            {"name": "Posa del Bambino (Child's Pose)", "duration": "60 secondi", "description": "Inginocchiati sul pavimento, allarga le ginocchia, siediti sui talloni e distendi le braccia in avanti toccando terra con la fronte per scaricare la colonna."},
            {"name": "Allungamento Bandelletta Iliotibiale", "duration": "30 secondi per lato", "description": "In piedi, incrocia la gamba destra dietro la sinistra, piegati lateralmente verso sinistra spingendo l'anca destra in fuori."},
            {"name": "Allungamento Bicipiti Femorali con Asciugamano", "duration": "45 secondi per gamba", "description": "Sdraiato supino, solleva una gamba tesa aiutandoti con un asciugamano sotto la pianta del piede."}
        ]
        if is_planned:
            title = f"Stretching Consigliato: Post-Lungo ({distance_km} km)"
        else:
            title = f"Scarica Catena Posteriore Post-Lungo ({distance_km} km)"
    else:
        exercises = [
            {"name": "Allungamento Polpacci standard", "duration": "30 secondi per gamba", "description": "Spingiti contro un muro mantenendo il tallone posteriore premuto a terra."},
            {"name": "Allungamento Quadricipiti", "duration": "30 secondi per gamba", "description": "Porta il tallone al gluteo mantenendo le ginocchia vicine e il busto eretto."},
            {"name": "Piegamento in avanti rilassato", "duration": "45 secondi", "description": "In piedi a gambe tese ma rilassate, lascia cadere la testa e le braccia verso il basso respirando profondamente."}
        ]
        if is_planned:
            title = f"Stretching Consigliato: Post-Corsa Facile"
        else:
            title = f"Defaticamento Leggero Post-Corsa"
            
    return {
        "workout_found": True,
        "workout_type": wtype,
        "workout_distance": distance_km,
        "workout_duration_min": dur_min,
        "workout_rpe": rpe,
        "is_planned": is_planned,
        "title": title,
        "exercises": exercises
    }


# ─────────────────────────────────────────────────────────────────────────────
# NEW NUTRITION API ENDPOINTS
# ─────────────────────────────────────────────────────────────────────────────

def get_daily_nutrition_targets(date_val: datetime.date, db: Session):
    weight = 76.0 # standard weight
    kcal_base = 1700
    carbs_g = int(weight * 4.0)
    protein_g = int(weight * 1.6)
    fat_g = int(weight * 1.0)
    
    # Check executed
    workout = db.query(WorkoutExecuted).filter(WorkoutExecuted.date == date_val).first()
    if workout:
        kcal_burned = int(workout.distance_km * weight)
        is_hard = workout.workout_type in ["ripetute", "gara", "medio"]
        carbs_g = int(weight * (6.5 if is_hard else 4.0))
        return {
            "calories": kcal_base + kcal_burned,
            "carbs": carbs_g,
            "protein": protein_g,
            "fat": fat_g
        }
        
    planned = db.query(WorkoutPlanned).filter(WorkoutPlanned.date == date_val).first()
    if planned:
        kcal_burned = int(planned.target_distance * weight)
        is_hard = planned.type in ["ripetute", "gara", "medio", "interval", "tempo", "long"]
        carbs_g = int(weight * (6.5 if is_hard else 4.0))
        return {
            "calories": kcal_base + kcal_burned,
            "carbs": carbs_g,
            "protein": protein_g,
            "fat": fat_g
        }
        
    return {
        "calories": kcal_base,
        "carbs": carbs_g,
        "protein": protein_g,
        "fat": fat_g
    }


@app.get("/api/nutrition/day")
def get_nutrition_by_day(date: Optional[str] = None, db: Session = Depends(get_db)):
    if not date:
        date_val = datetime.date.today()
    else:
        try:
            date_val = datetime.date.fromisoformat(date)
        except ValueError:
            date_val = datetime.date.today()
            
    # Fetch targets
    targets = get_daily_nutrition_targets(date_val, db)
    
    # Fetch logs
    logs = db.query(NutritionLog).filter(NutritionLog.date == date_val).all()
    
    # Calculate totals
    total_cal = 0
    total_carbs = 0
    total_protein = 0
    total_fat = 0
    
    meals_dict = {
        "colazione": [],
        "merenda_mattina": [],
        "pranzo": [],
        "merenda_pomeriggio": [],
        "cena": []
    }
    
    for l in logs:
        m = l.macros_json or {}
        carbs = m.get("carbs", 0) or 0
        protein = m.get("protein", 0) or 0
        fat = m.get("fat", 0) or 0
        
        total_cal += l.est_calories or 0
        total_carbs += carbs
        total_protein += protein
        total_fat += fat
        
        meal_type = l.meal_type or "colazione"
        if meal_type not in meals_dict:
            # Fallback/normalization
            if "merenda" in meal_type and "pomeriggio" in meal_type:
                meal_type = "merenda_pomeriggio"
            elif "merenda" in meal_type and "mattina" in meal_type:
                meal_type = "merenda_mattina"
            elif "breakfast" in meal_type or "colazione" in meal_type:
                meal_type = "colazione"
            elif "lunch" in meal_type or "pranzo" in meal_type:
                meal_type = "pranzo"
            elif "dinner" in meal_type or "cena" in meal_type:
                meal_type = "cena"
            else:
                meal_type = "colazione"
                
        meals_dict[meal_type].append({
            "id": l.id,
            "raw_input": l.raw_input,
            "est_calories": l.est_calories or 0,
            "carbs": carbs,
            "protein": protein,
            "fat": fat
        })
        
    # Calculate caloric output (BMR + Workout Active Calories)
    weight_metric = db.query(DailyMetric).filter(
        DailyMetric.date == date_val,
        DailyMetric.weight_kg.isnot(None)
    ).first()
    if not weight_metric:
        weight_metric = db.query(DailyMetric).filter(
            DailyMetric.weight_kg.isnot(None)
        ).order_by(DailyMetric.date.desc()).first()
    
    weight_val = weight_metric.weight_kg if weight_metric else 76.0
    
    # BMR simplified (Mifflin-St Jeor for 24yo, 178cm, M)
    bmr = 10 * weight_val + 6.25 * 178 - 5 * 24 + 5
    
    # Active calories from executed workouts on this day
    workouts_today = db.query(WorkoutExecuted).filter(WorkoutExecuted.date == date_val).all()
    active_cal = sum(int(w.distance_km * weight_val * 1.0) for w in workouts_today)
    
    total_out = int(bmr + active_cal)
    balance = total_cal - total_out

    # Get logged dates in ISO format
    all_logged_dates = [
        d[0].isoformat() for d in db.query(NutritionLog.date).distinct().all() if d[0]
    ]
        
    return {
        "date": date_val.isoformat(),
        "totals": {
            "calories": total_cal,
            "carbs": total_carbs,
            "protein": total_protein,
            "fat": total_fat
        },
        "targets": targets,
        "meals": meals_dict,
        "logged_dates": all_logged_dates,
        "caloric_balance": {
            "bmr": int(bmr),
            "active_calories": active_cal,
            "total_in": total_cal,
            "total_out": total_out,
            "balance": balance
        }
    }


@app.post("/api/nutrition/add")
def add_nutrition_log(
    date: str = Form(...),
    meal_type: str = Form(...),
    raw_input: str = Form(...),
    est_calories: Optional[int] = Form(None),
    carbs: Optional[int] = Form(None),
    protein: Optional[int] = Form(None),
    fat: Optional[int] = Form(None),
    db: Session = Depends(get_db)
):
    try:
        date_val = datetime.date.fromisoformat(date)
    except ValueError:
        date_val = datetime.date.today()
        
    if est_calories is None:
        est_calories = 250
    if carbs is None:
        carbs = 20
    if protein is None:
        protein = 10
    if fat is None:
        fat = 5
        
    log = NutritionLog(
        date=date_val,
        raw_input=raw_input,
        est_calories=est_calories,
        macros_json={"carbs": carbs, "protein": protein, "fat": fat},
        meal_type=meal_type
    )
    db.add(log)
    db.commit()
    
    # Save nutritionist insight
    insight = AgentInsight(
        agent_name="nutritionist",
        insight_type="nutrition_log",
        memory_payload={
            "date": date_val.isoformat(),
            "meal_type": meal_type,
            "raw_input": raw_input,
            "calories": est_calories,
            "macros": {"carbs": carbs, "protein": protein, "fat": fat}
        }
    )
    db.add(insight)
    db.commit()
    
    return {"status": "success", "log_id": log.id}


@app.post("/api/nutrition/delete/{log_id}")
def delete_nutrition_log(log_id: int, db: Session = Depends(get_db)):
    log = db.query(NutritionLog).filter(NutritionLog.id == log_id).first()
    if not log:
        raise HTTPException(status_code=404, detail="Log not found")
        
    db.delete(log)
    db.commit()
    return {"status": "success"}
