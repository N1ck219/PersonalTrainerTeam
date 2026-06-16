import datetime
from typing import Optional, Any, Dict, List
from sqlalchemy import create_engine, Integer, Float, String, Date, DateTime, ForeignKey, JSON, func, text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker, relationship

DATABASE_URL = "sqlite:///./marathon_multi_agent.db"

# Create database engine
# check_same_thread=False is required for SQLite in multi-threaded/FastAPI apps
engine = create_engine(
    DATABASE_URL, connect_args={"check_same_thread": False}
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Base(DeclarativeBase):
    """Base class for SQLAlchemy declarative models."""
    pass


class DailyMetric(Base):
    """
    1. daily_metrics: date (PK), sleep_hours, resting_hr, hrv_score, steps, readiness.
    """
    __tablename__ = "daily_metrics"

    date: Mapped[datetime.date] = mapped_column(Date, primary_key=True)
    sleep_hours: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    resting_hr: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    hrv_score: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    steps: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    readiness: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    weight_kg: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    body_fat_pct: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    muscle_mass_kg: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    water_pct: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    bone_mass_kg: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    def __repr__(self) -> str:
        return f"<DailyMetric(date={self.date}, sleep={self.sleep_hours}, hr={self.resting_hr}, hrv={self.hrv_score}, readiness={self.readiness})>"


class WorkoutPlanned(Base):
    """
    2. workouts_planned: id, date, type, target_distance, prompt_text, parser_template (JSON), status.
    """
    __tablename__ = "workouts_planned"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    date: Mapped[datetime.date] = mapped_column(Date, nullable=False)
    type: Mapped[str] = mapped_column(String, nullable=False)  # e.g., "easy", "tempo", "interval", "long"
    target_distance: Mapped[float] = mapped_column(Float, nullable=False)  # in km
    prompt_text: Mapped[Optional[str]] = mapped_column(String, nullable=True)  # LLM prompt details
    parser_template: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSON, nullable=True)  # JSON structure for laps
    status: Mapped[str] = mapped_column(String, default="planned")  # "planned", "completed", "skipped"

    # Relationship to executed workouts
    executed_workouts: Mapped[List["WorkoutExecuted"]] = relationship(
        "WorkoutExecuted", back_populates="planned_workout"
    )

    def __repr__(self) -> str:
        return f"<WorkoutPlanned(id={self.id}, date={self.date}, type={self.type}, target={self.target_distance}km, status={self.status})>"


class WorkoutExecuted(Base):
    """
    3. workouts_executed: id, planned_id (FK), date, distance_km, avg_pace, avg_cadence, shoe_used, laps_summary (JSON), rpe_score, workout_type, elevation_gain, elevation_loss, max_hr.
    """
    __tablename__ = "workouts_executed"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    planned_id: Mapped[Optional[int]] = mapped_column(ForeignKey("workouts_planned.id"), nullable=True)
    date: Mapped[datetime.date] = mapped_column(Date, nullable=False)
    workout_type: Mapped[str] = mapped_column(String, nullable=False, default="lento_corto")
    distance_km: Mapped[float] = mapped_column(Float, nullable=False)
    avg_pace: Mapped[float] = mapped_column(Float, nullable=False)  # stored in seconds per km (e.g. 304s = 5:04/km)
    avg_cadence: Mapped[Optional[float]] = mapped_column(Float, nullable=True)  # steps per minute
    shoe_used: Mapped[Optional[str]] = mapped_column(String, nullable=True)  # "Asics Gel-Nimbus", "Adidas Adizero", etc.
    elevation_gain: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    elevation_loss: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    max_hr: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    laps_summary: Mapped[Optional[List[Dict[str, Any]]]] = mapped_column(JSON, nullable=True)
    rpe_score: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)  # Rate of Perceived Exertion (1-10)
    comment: Mapped[Optional[str]] = mapped_column(String, nullable=True)  # Athlete notes/sensations

    # Relationship to planned workouts
    planned_workout: Mapped[Optional[WorkoutPlanned]] = relationship(
        "WorkoutPlanned", back_populates="executed_workouts"
    )

    @property
    def pace_formatted(self) -> str:
        """Returns the average pace in MM:SS/km format."""
        if not self.avg_pace:
            return "0:00"
        minutes = int(self.avg_pace // 60)
        seconds = int(self.avg_pace % 60)
        return f"{minutes}:{seconds:02d}"

    def __repr__(self) -> str:
        return f"<WorkoutExecuted(id={self.id}, date={self.date}, dist={self.distance_km}km, pace={self.pace_formatted}, shoe={self.shoe_used})>"


class NutritionLog(Base):
    """
    4. nutrition_logs: id, date, raw_input, est_calories, macros_json (JSON), meal_type.
    """
    __tablename__ = "nutrition_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    date: Mapped[datetime.date] = mapped_column(Date, nullable=False)
    raw_input: Mapped[str] = mapped_column(String, nullable=False)  # Telegram text log
    est_calories: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    macros_json: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSON, nullable=True)  # {"carbs": x, "protein": y, "fat": z}
    meal_type: Mapped[Optional[str]] = mapped_column(String, nullable=True) # e.g. "colazione", "merenda_mattina", "pranzo", "merenda_pomeriggio", "cena"

    def __repr__(self) -> str:
        return f"<NutritionLog(id={self.id}, date={self.date}, calories={self.est_calories}, macros={self.macros_json}, meal_type={self.meal_type})>"


class AgentInsight(Base):
    """
    5. agent_insights: id, created_at, agent_name, insight_type, memory_payload.
    """
    __tablename__ = "agent_insights"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=func.now())
    agent_name: Mapped[str] = mapped_column(String, nullable=False)  # "trainer", "physiologist", "nutritionist", "manager"
    insight_type: Mapped[str] = mapped_column(String, nullable=False)  # "training_load", "readiness_trend", "nutrition_status"
    memory_payload: Mapped[Dict[str, Any]] = mapped_column(JSON, nullable=False)  # Arbitrary memory dump

    def __repr__(self) -> str:
        return f"<AgentInsight(id={self.id}, agent={self.agent_name}, type={self.insight_type}, created_at={self.created_at})>"


class Shoe(Base):
    """
    6. shoes: id (PK), name, baseline_km, baseline_date, is_active.
    """
    __tablename__ = "shoes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    baseline_km: Mapped[float] = mapped_column(Float, default=0.0)
    baseline_date: Mapped[datetime.date] = mapped_column(Date, default=datetime.date(2026, 6, 15))
    is_active: Mapped[bool] = mapped_column(Integer, default=True)

    def __repr__(self) -> str:
        return f"<Shoe(id={self.id}, name={self.name}, baseline_km={self.baseline_km}, is_active={self.is_active})>"


class ChatMessage(Base):
    """
    7. chat_messages: id (PK), timestamp, role, message_text, image_path.
    """
    __tablename__ = "chat_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    timestamp: Mapped[datetime.datetime] = mapped_column(DateTime, default=func.now())
    role: Mapped[str] = mapped_column(String, nullable=False)  # "user" or "assistant"
    message_text: Mapped[str] = mapped_column(String, nullable=False)
    image_path: Mapped[Optional[str]] = mapped_column(String, nullable=True)

    def __repr__(self) -> str:
        return f"<ChatMessage(id={self.id}, role={self.role}, text={self.message_text[:20]}...)>"


class PainLog(Base):
    """
    8. pain_logs: id (PK), date, body_part, intensity, notes.
    """
    __tablename__ = "pain_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    date: Mapped[datetime.date] = mapped_column(Date, nullable=False, default=datetime.date.today)
    body_part: Mapped[str] = mapped_column(String, nullable=False)  # e.g., "Left Knee", "Lower Back"
    intensity: Mapped[int] = mapped_column(Integer, nullable=False)  # 1 to 10
    notes: Mapped[Optional[str]] = mapped_column(String, nullable=True)

    def __repr__(self) -> str:
        return f"<PainLog(id={self.id}, date={self.date}, part={self.body_part}, intensity={self.intensity})>"


def init_db() -> None:
    """Initialize the SQLite database, creating all tables if they do not exist."""
    Base.metadata.create_all(bind=engine)
    # Automatically add comment column if it doesn't exist yet
    try:
        from sqlalchemy import inspect
        inspector = inspect(engine)
        columns = [col["name"] for col in inspector.get_columns("workouts_executed")]
        if "comment" not in columns:
            with engine.begin() as conn:
                conn.execute(text("ALTER TABLE workouts_executed ADD COLUMN comment TEXT;"))
                print("[init_db] Column 'comment' successfully added to 'workouts_executed' table.")
        
        # Add meal_type column to nutrition_logs if missing
        columns_nl = [col["name"] for col in inspector.get_columns("nutrition_logs")]
        if "meal_type" not in columns_nl:
            with engine.begin() as conn:
                conn.execute(text("ALTER TABLE nutrition_logs ADD COLUMN meal_type TEXT;"))
                print("[init_db] Column 'meal_type' successfully added to 'nutrition_logs' table.")
        
        # Automatically add Garmin columns to daily_metrics if missing
        columns_dm = [col["name"] for col in inspector.get_columns("daily_metrics")]
        new_dm_cols = {
            "weight_kg": "REAL",
            "body_fat_pct": "REAL",
            "muscle_mass_kg": "REAL",
            "water_pct": "REAL",
            "bone_mass_kg": "REAL"
        }
        for col_name, col_type in new_dm_cols.items():
            if col_name not in columns_dm:
                with engine.begin() as conn:
                    conn.execute(text(f"ALTER TABLE daily_metrics ADD COLUMN {col_name} {col_type};"))
                    print(f"[init_db] Column '{col_name}' successfully added to 'daily_metrics' table.")
                    
        # Seed initial shoes if shoes table is empty
        db = SessionLocal()
        try:
            if db.query(Shoe).count() == 0:
                initial_shoes = [
                    Shoe(name="Asics Gel-Nimbus 27", baseline_km=632.5, baseline_date=datetime.date(2026, 6, 15), is_active=True),
                    Shoe(name="Adidas Adizero", baseline_km=0.0, baseline_date=datetime.date(2026, 6, 15), is_active=True),
                    Shoe(name="Nike Pegasus", baseline_km=0.0, baseline_date=datetime.date(2026, 6, 15), is_active=True)
                ]
                db.add_all(initial_shoes)
                db.commit()
                print("[init_db] Seeded initial shoes into database.")
        except Exception as e:
            print(f"[init_db] Error seeding shoes: {e}")
        finally:
            db.close()
    except Exception as e:
        # Ignore on errors
        print(f"[init_db] Error during migration/seeding: {e}")


def get_db():
    """FastAPI Dependency to get a database session."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
