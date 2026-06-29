import datetime
from pathlib import Path
import sys

_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_ROOT))

from database.database import SessionLocal, DailyMetric, init_db
from sqlalchemy import func

def recalculate():
    db = SessionLocal()
    try:
        metrics = db.query(DailyMetric).order_by(DailyMetric.date.asc()).all()
        print(f"Trovate {len(metrics)} metriche giornaliere da aggiornare...")
        
        updated = 0
        for metric in metrics:
            day = metric.date
            if not metric.sleep_hours or not metric.resting_hr:
                continue
                
            # Calcola le medie mobili degli ultimi 30 giorni fino a 'day' (escluso)
            rhr_30d = db.query(func.avg(DailyMetric.resting_hr)).filter(
                DailyMetric.resting_hr.isnot(None),
                DailyMetric.date < day,
                DailyMetric.date >= day - datetime.timedelta(days=30)
            ).scalar()
            
            hrv_30d = db.query(func.avg(DailyMetric.hrv_score)).filter(
                DailyMetric.hrv_score.isnot(None),
                DailyMetric.date < day,
                DailyMetric.date >= day - datetime.timedelta(days=30)
            ).scalar()

            sleep_30d = db.query(func.avg(DailyMetric.sleep_hours)).filter(
                DailyMetric.sleep_hours.isnot(None),
                DailyMetric.date < day,
                DailyMetric.date >= day - datetime.timedelta(days=30)
            ).scalar()

            rhr_base = float(rhr_30d) if rhr_30d else 55.0
            hrv_base = float(hrv_30d) if hrv_30d else 65.0
            sleep_base = float(sleep_30d) if sleep_30d else 8.0

            # Formule adattive
            sleep_score = min(100.0, (metric.sleep_hours / sleep_base) * 100.0)
            hrv_curr = metric.hrv_score or hrv_base
            hrv_score_val = min(100.0, max(0.0, 80.0 + (hrv_curr - hrv_base) * 2.0))
            rhr_score_val = min(100.0, max(0.0, 80.0 - (metric.resting_hr - rhr_base) * 3.0))
            
            new_readiness = round((sleep_score * 0.3) + (hrv_score_val * 0.4) + (rhr_score_val * 0.3), 1)
            
            if metric.readiness != new_readiness:
                metric.readiness = new_readiness
                updated += 1
                
        db.commit()
        print(f"Ricalcolo completato. Aggiornate {updated} righe con readiness adattiva.")
    finally:
        db.close()

if __name__ == "__main__":
    recalculate()
