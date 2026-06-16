import os
from database.database import init_db

DB_FILE = "marathon_multi_agent.db"

def reset_database():
    print("=== Reinizializzazione Database ===")
    if os.path.exists(DB_FILE):
        try:
            os.remove(DB_FILE)
            print(f"[OK] File del database '{DB_FILE}' eliminato.")
        except Exception as e:
            print(f"[WARNING] Impossibile eliminare il file (potrebbe essere bloccato da uvicorn): {e}")
            print("Tentativo di svuotare le singole tabelle...")
            
            from database.database import (
                SessionLocal, DailyMetric, WorkoutPlanned, 
                WorkoutExecuted, NutritionLog, AgentInsight
            )
            db = SessionLocal()
            try:
                db.query(DailyMetric).delete()
                db.query(WorkoutPlanned).delete()
                db.query(WorkoutExecuted).delete()
                db.query(NutritionLog).delete()
                db.query(AgentInsight).delete()
                db.commit()
                print("[OK] Tutte le tabelle svuotate con successo.")
            except Exception as ex:
                db.rollback()
                print(f"[ERROR] Impossibile svuotare le tabelle: {ex}")
            finally:
                db.close()
            return
            
    # Recreate empty tables
    init_db()
    print("[OK] Tabelle vuote ricreate con successo.")

if __name__ == "__main__":
    reset_database()
