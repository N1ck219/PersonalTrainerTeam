import datetime
from pathlib import Path
import sys

_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_ROOT))

from database.database import SessionLocal, WorkoutPlanned

def check():
    db = SessionLocal()
    try:
        today = datetime.date.today()
        print(f"Data corrente locale: {today} ({today.strftime('%A')})")
        
        workouts = db.query(WorkoutPlanned).order_by(WorkoutPlanned.date.asc()).all()
        print("\n--- Allenamenti Pianificati nel Database ---")
        for w in workouts:
            print(f"ID: {w.id} | Data: {w.date} ({w.date.strftime('%A')}) | Tipo: {w.type} | Target: {w.target_distance}km | Stato: {w.status}")
    finally:
        db.close()

if __name__ == "__main__":
    check()
