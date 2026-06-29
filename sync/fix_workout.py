from pathlib import Path
import sys

_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_ROOT))

from database.database import SessionLocal, WorkoutPlanned

def fix():
    db = SessionLocal()
    try:
        workout = db.query(WorkoutPlanned).filter(WorkoutPlanned.id == 5).first()
        if workout:
            print(f"Stato precedente per ID 5 ({workout.date}): {workout.status}")
            workout.status = "planned"
            db.commit()
            print(f"Stato aggiornato con successo a: {workout.status}")
        else:
            print("Allenamento ID 5 non trovato.")
    finally:
        db.close()

if __name__ == "__main__":
    fix()
