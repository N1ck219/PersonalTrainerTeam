from typing import Optional, List
from pydantic import BaseModel, Field


class DailyMetricsExtraction(BaseModel):
    """Schema per l'estrazione delle metriche fisiologiche quotidiane."""
    sleep_hours: Optional[float] = Field(None, description="Numero di ore di sonno, es. 7.5 o 8")
    resting_hr: Optional[int] = Field(None, description="Frequenza cardiaca a riposo (RHR) in bpm")
    hrv_score: Optional[int] = Field(None, description="Punteggio HRV (Heart Rate Variability)")
    steps: Optional[int] = Field(None, description="Numero di passi giornalieri")


class Macros(BaseModel):
    """Schema per il dettaglio dei macronutrienti."""
    carbs: int = Field(0, description="Grammi di carboidrati staccati o stimati")
    protein: int = Field(0, description="Grammi di proteine stimati")
    fat: int = Field(0, description="Grammi di grassi stimati")


class NutritionExtraction(BaseModel):
    """Schema per l'estrazione dei log nutrizionali."""
    raw_input: str = Field(..., description="Descrizione testuale del pasto inserito dall'atleta")
    est_calories: int = Field(..., description="Stima delle calorie totali del pasto in kcal")
    macros: Macros = Field(..., description="Suddivisione stimata dei macronutrienti")


class ManagerDecision(BaseModel):
    """Schema per la classificazione dell'intento dell'atleta e dei dati associati."""
    intent: str = Field(
        ...,
        description="Classificazione dell'intento: 'log_metrics' (salvataggio dati fisici), "
                    "'log_workout' (resoconto allenamento manuale), 'log_nutrition' (pasto mangiato), "
                    "'ask_plan' (richiesta del prossimo allenamento o piano), o 'chat' (domanda generica o saluto)."
    )
    daily_metrics: Optional[DailyMetricsExtraction] = Field(
        None,
        description="Dati fisiologici giornalieri estratti dal testo (se presenti)"
    )
    nutrition: Optional[NutritionExtraction] = Field(
        None,
        description="Pasto e macronutrienti estratti dal testo (se presenti)"
    )


# ─────────────────────────────────────────────────────────────────────────────
# TRAINER PLANNING SCHEMAS
# Used by trainer_node for structured weekly plan generation via Gemini
# ─────────────────────────────────────────────────────────────────────────────

class PlannedSession(BaseModel):
    """Una singola sessione di allenamento del piano settimanale."""
    workout_type: str = Field(
        ...,
        description="Tipo di allenamento: 'lento_corto', 'lento_lungo', 'medio', 'ripetute', 'gara'"
    )
    day_label: str = Field(
        ...,
        description="Giorno della settimana in italiano, es. 'Martedì', 'Giovedì', 'Sabato', 'Domenica'"
    )
    target_distance_km: float = Field(
        ...,
        description="Distanza target in km per questa sessione"
    )
    target_pace_description: str = Field(
        ...,
        description="Descrizione del ritmo target, es. '5:50–6:05/km' oppure '4:50–5:00/km per le frazioni'"
    )
    instructions: str = Field(
        ...,
        description="Indicazioni dettagliate per l'atleta su come svolgere l'allenamento, in italiano"
    )
    rationale: str = Field(
        ...,
        description="Breve spiegazione del perché questa sessione è stata prescritta così (adattamento ai dati storici), in italiano"
    )


class WeeklyPlan(BaseModel):
    """Il piano settimanale completo con 4 sessioni."""
    sessions: List[PlannedSession] = Field(
        ...,
        description="Lista delle 4 sessioni di allenamento della settimana, una per ogni tipologia"
    )
    weekly_volume_km: float = Field(
        ...,
        description="Volume totale stimato della settimana in km (somma delle distanze target)"
    )
    phase_label: str = Field(
        ...,
        description="Etichetta della fase di preparazione, es. 'Fase di Costruzione', 'Fase di Sviluppo', 'Fase Competitiva', 'Tapering'"
    )
    coach_notes: str = Field(
        ...,
        description="Note generali del coach per la settimana, inclusi trend di miglioramento osservati e consigli di recupero, in italiano"
    )


class HRZoneCalibration(BaseModel):
    """Schema per la ricalibrazione delle zone di frequenza cardiaca."""
    calibrate: bool = Field(..., description="True se è necessaria una ricalibrazione delle zone, False altrimenti")
    z1_max: Optional[int] = Field(None, description="Frequenza cardiaca massima per la Zona 1 (Recupero) in bpm")
    z2_max: Optional[int] = Field(None, description="Frequenza cardiaca massima per la Zona 2 (Fondo Lento) in bpm")
    z3_max: Optional[int] = Field(None, description="Frequenza cardiaca massima per la Zona 3 (Fondo Medio) in bpm")
    z4_max: Optional[int] = Field(None, description="Frequenza cardiaca massima per la Zona 4 (Soglia) in bpm")
    z5_max: Optional[int] = Field(None, description="Frequenza cardiaca massima per la Zona 5 (Massimo Sforzo) in bpm")
    rationale: Optional[str] = Field(None, description="Spiegazione fisiologica e razionale della ricalibrazione in italiano")
