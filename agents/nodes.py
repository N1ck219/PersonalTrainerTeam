import os
import json
import datetime
from typing import Dict, Any, List, Optional
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_core.messages import SystemMessage, HumanMessage
from database.database import (
    SessionLocal, DailyMetric, WorkoutPlanned, WorkoutExecuted, NutritionLog, AgentInsight
)
from agents.state import AgentState
from agents.schemas import ManagerDecision, WeeklyPlan, HRZoneCalibration

# Helper to initialize the LLM
def get_llm(agent_name: Optional[str] = None):
    # Try fetching agent-specific key first
    specific_key_name = f"{agent_name.upper()}_API_KEY" if agent_name else None
    api_key = None
    if specific_key_name:
        api_key = os.getenv(specific_key_name)
    
    # Fallback to general GOOGLE_API_KEY
    if not api_key or not api_key.strip():
        api_key = os.getenv("GOOGLE_API_KEY")
        
    if not api_key or not api_key.strip():
        return None
        
    api_key = api_key.strip()
    model_name = os.getenv("GEMINI_MODEL", "gemma-4-31b-it")
    return ChatGoogleGenerativeAI(
        model=model_name,
        google_api_key=api_key,
        temperature=0.2
    )


# ─────────────────────────────────────────────────────────────────────────────
# HELPER: Build Athlete Performance Profile from DB
# ─────────────────────────────────────────────────────────────────────────────

def _fmt_pace(seconds: float) -> str:
    """Convert pace in seconds/km to mm:ss string."""
    if not seconds or seconds <= 0:
        return "N/D"
    m = int(seconds // 60)
    s = int(seconds % 60)
    return f"{m}:{s:02d}"


def build_athlete_performance_profile(db) -> Dict[str, Any]:
    """
    Analyzes the last 60 days of WorkoutExecuted records grouped by workout_type
    and returns a rich performance profile dictionary.
    
    Returns per-type stats (pace, trend, distance) and weekly volume trend.
    Handles gracefully when few or no workouts exist.
    """
    today = datetime.date.today()
    cutoff_60d = today - datetime.timedelta(days=60)
    cutoff_30d = today - datetime.timedelta(days=30)
    cutoff_prev = today - datetime.timedelta(days=60)  # already is cutoff_60d

    # Load all workouts in last 60 days
    workouts = (
        db.query(WorkoutExecuted)
        .filter(WorkoutExecuted.date >= cutoff_60d)
        .order_by(WorkoutExecuted.date.asc())
        .all()
    )

    TYPES = ["lento_corto", "lento_lungo", "medio", "ripetute", "gara"]
    profile: Dict[str, Any] = {}

    for wtype in TYPES:
        recent = [w for w in workouts if w.workout_type == wtype and w.date >= cutoff_30d]
        previous = [w for w in workouts if w.workout_type == wtype and w.date < cutoff_30d]
        all_type = [w for w in workouts if w.workout_type == wtype]

        def avg_pace(ws):
            paces = [w.avg_pace for w in ws if w.avg_pace and w.avg_pace > 0]
            return sum(paces) / len(paces) if paces else None

        def avg_distance(ws):
            dists = [w.distance_km for w in ws if w.distance_km and w.distance_km > 0]
            return round(sum(dists) / len(dists), 1) if dists else None

        avg_pace_recent = avg_pace(recent)
        avg_pace_prev = avg_pace(previous)
        avg_dist_recent = avg_distance(recent)

        # Pace trend: negative means improvement (faster pace = fewer seconds/km)
        trend_sec = None
        trend_label = "stabile"
        if avg_pace_recent and avg_pace_prev:
            trend_sec = round(avg_pace_recent - avg_pace_prev, 1)
            if trend_sec < -5:
                trend_label = f"miglioramento ({abs(trend_sec):.0f}s/km più veloce)"
            elif trend_sec > 5:
                trend_label = f"leggero rallentamento ({trend_sec:.0f}s/km più lento)"
            else:
                trend_label = "stabile"

        # Last workout of this type
        last_wo = all_type[-1] if all_type else None

        # For ripetute: extract interval paces from laps_summary
        interval_paces = []
        if wtype == "ripetute":
            for w in all_type:
                if w.laps_summary:
                    for lap in w.laps_summary:
                        lap_name = (lap.get("name") or "").lower()
                        if "ripetuta" in lap_name and lap.get("avg_pace_sec"):
                            interval_paces.append(lap["avg_pace_sec"])

        type_profile: Dict[str, Any] = {
            "count_last30d": len(recent),
            "count_last60d": len(all_type),
            "avg_pace_sec_last30d": round(avg_pace_recent, 1) if avg_pace_recent else None,
            "avg_pace_formatted_last30d": _fmt_pace(avg_pace_recent) if avg_pace_recent else "N/D",
            "avg_pace_sec_prev30d": round(avg_pace_prev, 1) if avg_pace_prev else None,
            "avg_pace_formatted_prev30d": _fmt_pace(avg_pace_prev) if avg_pace_prev else "N/D",
            "trend_sec_vs_prev_month": trend_sec,
            "trend_label": trend_label,
            "avg_distance_km_last30d": avg_dist_recent,
            "last_pace_formatted": _fmt_pace(last_wo.avg_pace) if last_wo else "N/D",
            "last_distance_km": last_wo.distance_km if last_wo else None,
            "last_date": last_wo.date.isoformat() if last_wo else None,
        }

        if wtype == "ripetute" and interval_paces:
            avg_int = sum(interval_paces) / len(interval_paces)
            best_int = min(interval_paces)
            type_profile["avg_interval_pace_formatted"] = _fmt_pace(avg_int)
            type_profile["best_interval_pace_formatted"] = _fmt_pace(best_int)
            type_profile["interval_count_analyzed"] = len(interval_paces)

        profile[wtype] = type_profile

    # Weekly volume stats (last 4 weeks vs previous 4 weeks)
    def weekly_volumes(ws, num_weeks=4):
        """Returns list of (week_start, total_km) for the last num_weeks."""
        vols = []
        for i in range(num_weeks):
            wk_end = today - datetime.timedelta(days=7 * i)
            wk_start = wk_end - datetime.timedelta(days=6)
            km = sum(w.distance_km for w in ws if wk_start <= w.date <= wk_end)
            vols.append(round(km, 1))
        return vols

    all_workouts = (
        db.query(WorkoutExecuted)
        .filter(WorkoutExecuted.date >= today - datetime.timedelta(days=56))
        .all()
    )
    vols_last4 = weekly_volumes(all_workouts, 4)
    vols_prev4 = weekly_volumes(all_workouts, 8)[4:]  # weeks 4-7 back

    avg_vol_last4 = round(sum(vols_last4) / 4, 1) if vols_last4 else 0.0
    avg_vol_prev4 = round(sum(vols_prev4) / 4, 1) if vols_prev4 else 0.0

    profile["weekly_volume"] = {
        "last4_weeks_km": vols_last4,
        "avg_last4_km": avg_vol_last4,
        "avg_prev4_km": avg_vol_prev4,
        "trend_label": (
            f"in crescita (+{avg_vol_last4 - avg_vol_prev4:.1f} km/sett)"
            if avg_vol_last4 > avg_vol_prev4 + 1
            else f"in calo ({avg_vol_last4 - avg_vol_prev4:.1f} km/sett)"
            if avg_vol_last4 < avg_vol_prev4 - 1
            else "stabile"
        )
    }
    profile["total_workouts_analyzed"] = len(workouts)

    return profile


# ─────────────────────────────────────────────────────────────────────────────
# 1. MANAGER / ROUTER NODE
# ─────────────────────────────────────────────────────────────────────────────
def manager_router_node(state: AgentState) -> Dict[str, Any]:
    """
    Acts as the router and structured parser. Analyzes user message_text and decides:
    - What data needs to be extracted (daily metrics, nutrition, workout RPE).
    - Which nodes should be scheduled for execution.
    """
    message = state.get("message_text") or ""
    file_path = state.get("file_path")
    
    # Defaults
    parsed_metrics = None
    parsed_nutrition = None
    next_nodes = []
    
    llm = get_llm("manager")
    intent = "chat"
    
    if llm and message:
        try:
            # Bind LLM to Pydantic schema for structured output
            structured_llm = llm.with_structured_output(ManagerDecision)
            today = datetime.date.today()
            days_it = ["Lunedì", "Martedì", "Mercoledì", "Giovedì", "Venerdì", "Sabato", "Domenica"]
            today_day_name = days_it[today.weekday()]
            system_prompt = (
                f"Sei il Manager Router di Marathon-Multi-Agent.\n"
                f"La data e l'ora corrente sono: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M')} ({today_day_name}).\n"
                "Il tuo compito è analizzare il messaggio dell'atleta (ed eventuale immagine allegata), classificare il suo intento "
                "ed estrarre in modo strutturato i dati fisiologici o nutrizionali. Se l'utente allega una foto di cibo, stima le calorie e i macronutrienti."
            )
            
            image_data = state.get("image_data")
            human_content = [{"type": "text", "text": message}]
            if image_data:
                human_content.append({
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:image/jpeg;base64,{image_data}"
                    }
                })
                
            decision = structured_llm.invoke([
                SystemMessage(content=system_prompt),
                HumanMessage(content=human_content)
            ])
            
            intent = decision.intent
            
            if decision.daily_metrics:
                # Only include fields that are not None
                parsed_metrics = {}
                dm = decision.daily_metrics
                if dm.sleep_hours is not None: parsed_metrics["sleep_hours"] = dm.sleep_hours
                if dm.resting_hr is not None: parsed_metrics["resting_hr"] = dm.resting_hr
                if dm.hrv_score is not None: parsed_metrics["hrv_score"] = dm.hrv_score
                if dm.steps is not None: parsed_metrics["steps"] = dm.steps
                
            if decision.nutrition:
                parsed_nutrition = []
                for dn in decision.nutrition:
                    parsed_nutrition.append({
                        "raw_input": dn.raw_input,
                        "est_calories": dn.est_calories,
                        "macros": {
                            "carbs": dn.macros.carbs,
                            "protein": dn.macros.protein,
                            "fat": dn.macros.fat
                        },
                        "meal_type": dn.meal_type
                    })
        except Exception as e:
            print(f"Error in Manager Structured LLM: {e}")
            intent = "chat"
    else:
        # Fallback parsing in local testing (regex/simple keyword lookup)
        message_lower = message.lower()
        if "sonno" in message_lower or "hrv" in message_lower or "battiti" in message_lower:
            intent = "log_metrics"
            parsed_metrics = {"sleep_hours": 7.5, "resting_hr": 54, "hrv_score": 62, "steps": 10000}
        elif "mangiat" in message_lower or "cibo" in message_lower or "calor" in message_lower:
            intent = "log_nutrition"
            parsed_nutrition = [{
                "raw_input": message,
                "est_calories": 450,
                "macros": {"carbs": 50, "protein": 25, "fat": 15},
                "meal_type": None
            }]
        elif "allenament" in message_lower or "prossim" in message_lower or "correre" in message_lower:
            intent = "ask_plan"

    # Determine execution sequence based on intent and file uploads
    if file_path or intent == "log_workout" or state.get("parsed_workout"):
        next_nodes = ["trainer", "nutritionist"]
        router_decision = "physiologist"
    elif intent == "log_metrics" or parsed_metrics:
        next_nodes = []
        router_decision = "physiologist"
    elif intent == "log_nutrition" or parsed_nutrition:
        next_nodes = []
        router_decision = "nutritionist"
    elif intent == "ask_plan":
        next_nodes = []
        router_decision = "trainer"
    else:
        next_nodes = []
        router_decision = "end"

    return {
        "parsed_metrics": parsed_metrics,
        "parsed_nutrition": parsed_nutrition,
        "next_nodes": next_nodes,
        "router_decision": router_decision
    }


# ─────────────────────────────────────────────────────────────────────────────
# 2. PHYSIOLOGIST NODE
# ─────────────────────────────────────────────────────────────────────────────
def physiologist_node(state: AgentState) -> Dict[str, Any]:
    """
    Analyzes daily metrics (HRV, Resting HR, Sleep) to calculate athlete readiness
    and monitor for potential overtraining.
    """
    db = SessionLocal()
    try:
        # 1. Check if we have parsed metrics in the state, if so save them
        metrics_data = state.get("parsed_metrics")
        today = datetime.date.today()
        
        if metrics_data:
            # Upsert DailyMetric for today
            metric = db.query(DailyMetric).filter(DailyMetric.date == today).first()
            if not metric:
                metric = DailyMetric(date=today)
                db.add(metric)
            
            metric.sleep_hours = metrics_data.get("sleep_hours", metric.sleep_hours)
            metric.resting_hr = metrics_data.get("resting_hr", metric.resting_hr)
            metric.hrv_score = metrics_data.get("hrv_score", metric.hrv_score)
            metric.steps = metrics_data.get("steps", metric.steps)
            db.commit()

        # 2. Retrieve last 7 days of daily metrics to calculate baseline
        recent_metrics_db = db.query(DailyMetric).order_by(DailyMetric.date.desc()).limit(7).all()
        recent_metrics_list = []
        for m in recent_metrics_db:
            recent_metrics_list.append({
                "date": m.date.isoformat(),
                "sleep_hours": m.sleep_hours,
                "resting_hr": m.resting_hr,
                "hrv_score": m.hrv_score,
                "steps": m.steps,
                "readiness": m.readiness
            })

        # Calculate readiness score (0 - 100)
        sleep = metrics_data.get("sleep_hours", 8.0) if metrics_data else 8.0
        rhr = metrics_data.get("resting_hr", 55) if metrics_data else 55
        hrv = metrics_data.get("hrv_score", 65) if metrics_data else 65
        
        # Calculate baseline HRV (average of last 7 days or defaults to 65)
        hrv_values = [m["hrv_score"] for m in recent_metrics_list if m["hrv_score"] is not None]
        hrv_baseline = sum(hrv_values) / len(hrv_values) if hrv_values else 65.0
        
        rhr_values = [m["resting_hr"] for m in recent_metrics_list if m["resting_hr"] is not None]
        rhr_baseline = sum(rhr_values) / len(rhr_values) if rhr_values else 55.0

        # Scoring
        sleep_score = min(100.0, (sleep / 8.0) * 100.0)
        hrv_diff = hrv - hrv_baseline
        hrv_score = min(100.0, max(0.0, 80.0 + (hrv_diff * 2.0)))
        rhr_diff = rhr - rhr_baseline
        rhr_score = min(100.0, max(0.0, 80.0 - (rhr_diff * 3.0)))
        readiness = round((sleep_score * 0.3) + (hrv_score * 0.4) + (rhr_score * 0.3), 1)

        # Update readiness on DB for today
        metric_today = db.query(DailyMetric).filter(DailyMetric.date == today).first()
        if metric_today:
            metric_today.readiness = readiness
            db.commit()
            
        readiness_data = {
            "readiness_score": readiness,
            "sleep_hours": sleep,
            "resting_hr": rhr,
            "hrv_score": hrv,
            "hrv_baseline": round(hrv_baseline, 1),
            "rhr_baseline": round(rhr_baseline, 1),
            "status": (
                "eccellente" if readiness >= 85
                else "buono" if readiness >= 70
                else "affaticato" if readiness >= 50
                else "necessita recupero"
            )
        }

        # Save an agent insight about readiness trend if it drops significantly
        if readiness < 60:
            insight = AgentInsight(
                agent_name="physiologist",
                insight_type="readiness_warning",
                memory_payload={
                    "date": today.isoformat(),
                    "readiness": readiness,
                    "sleep": sleep,
                    "hrv": hrv,
                    "rhr": rhr,
                    "warning": "Readiness sotto la soglia ottimale. Consigliare riduzione di intensità."
                }
            )
            db.add(insight)
            db.commit()

        # 3. Dynamic HR Zone Revision
        workout_data = state.get("parsed_workout")
        message_text = state.get("message_text") or ""
        
        if (workout_data and message_text) or "battiti" in message_text.lower() or "zona" in message_text.lower() or "bpm" in message_text.lower():
            llm = get_llm("physiologist")
            if llm:
                try:
                    structured_llm = llm.with_structured_output(HRZoneCalibration)
                    system_prompt = (
                        "Sei il Fisiologo dello Sport del team Marathon-Multi-Agent.\n"
                        "Il tuo compito è analizzare la telemetria dell'allenamento dell'atleta e le sue note/sensazioni soggettive "
                        "(es. fatica di respirazione, percezione dello sforzo RPE, condizioni di caldo/freddo, se riesce a respirare "
                        "dal naso o a parlare facilmente ad una determinata frequenza cardiaca).\n"
                        "Se l'atleta indica che riesce a correre e parlare facilmente/respirare dal naso a 160 bpm nonostante il caldo, "
                        "significa che la sua soglia aerobica (limite superiore della Zona 2) è più alta di quella standard calcolata teoricamente (es. 137 o 140 bpm).\n"
                        "Valuta se è opportuno ricalibrare le zone cardiache dell'atleta.\n"
                        "Z1 (Recupero): fino a 60-65% della FC Max o in base alle sensazioni dell'atleta.\n"
                        "Z2 (Fondo Lento): aerobico facile, l'atleta respira facilmente e parla. Se riferisce 160 bpm, sposta il limite della Z2 a 160 bpm.\n"
                        "Z3 (Fondo Medio): aerobico medio, respirazione leggermente impegnata.\n"
                        "Z4 (Soglia): ritmo gara corto, respiro impegnato ma controllato.\n"
                        "Z5 (Massimo sforzo): VO2Max/anaerobico lattacido.\n"
                        "Se ritieni che le zone vadano ricalibrate, imposta 'calibrate' a True e calcola i nuovi massimali per ciascuna zona in bpm, "
                        "fornendo una spiegazione medica/sportiva accurata in italiano in 'rationale'."
                    )
                    
                    user_prompt = (
                        f"MESSAGGIO/NOTE DELL'ATLETA:\n{message_text}\n\n"
                        f"TELEMETRIA ALLENAMENTO:\n"
                        f"  - Distanza: {workout_data.get('distance_km') if workout_data else 'N/D'} km\n"
                        f"  - Passo: {workout_data.get('avg_pace') if workout_data else 'N/D'} s/km\n"
                        f"  - Cadenza: {workout_data.get('avg_cadence') if workout_data else 'N/D'} spm\n"
                        f"  - RPE dichiarato: {workout_data.get('rpe_score') if workout_data else 'N/D'}/10\n"
                    )
                    
                    calibration = structured_llm.invoke([
                        SystemMessage(content=system_prompt),
                        HumanMessage(content=user_prompt)
                    ])
                    
                    if calibration.calibrate:
                        # Save zone calibration insight
                        zone_insight = AgentInsight(
                            agent_name="physiologist",
                            insight_type="hr_zone_calibration",
                            memory_payload={
                                "z1_max": calibration.z1_max,
                                "z2_max": calibration.z2_max,
                                "z3_max": calibration.z3_max,
                                "z4_max": calibration.z4_max,
                                "z5_max": calibration.z5_max,
                                "rationale": calibration.rationale,
                                "date": today.isoformat()
                            }
                        )
                        db.add(zone_insight)
                        db.commit()
                        print(f"[Physiologist] Calibrated HR zones successfully: Z2 max = {calibration.z2_max} bpm. Rationale: {calibration.rationale}")
                except Exception as e:
                    print(f"Error in Physiologist Zone Calibration LLM: {e}")

        # Update state queue
        nodes_queue = state.get("next_nodes", [])
        next_decision = nodes_queue[0] if nodes_queue else "end"
        remaining_nodes = nodes_queue[1:] if len(nodes_queue) > 1 else []

        return {
            "recent_metrics": recent_metrics_list,
            "readiness_data": readiness_data,
            "router_decision": next_decision,
            "next_nodes": remaining_nodes
        }
    finally:
        db.close()


# ─────────────────────────────────────────────────────────────────────────────
# 3. TRAINER NODE
# ─────────────────────────────────────────────────────────────────────────────
def trainer_node(state: AgentState) -> Dict[str, Any]:
    """
    Analyzes athlete historical performance data and uses Gemini to generate
    a personalized 4-session weekly training plan (lento_corto, lento_lungo,
    medio, ripetute) with paces and distances adapted to the athlete's real
    historical data and current fitness level.
    """
    db = SessionLocal()
    try:
        today = datetime.date.today()
        profile = state.get("athlete_profile", {})
        readiness_data = state.get("readiness_data")
        readiness_score = readiness_data.get("readiness_score", 80.0) if readiness_data else 80.0

        # ── 1. Save executed workout if present in state ──────────────────────
        parsed_wo = state.get("parsed_workout")
        if parsed_wo:
            # Check if this workout is already saved to avoid duplicates
            wo_date = datetime.date.fromisoformat(parsed_wo["date"])
            existing_wo = db.query(WorkoutExecuted).filter(
                WorkoutExecuted.date == wo_date,
                WorkoutExecuted.distance_km == parsed_wo["distance_km"]
            ).first()
            
            if not existing_wo:
                planned = db.query(WorkoutPlanned).filter(
                    WorkoutPlanned.date <= today,
                    WorkoutPlanned.status == "planned"
                ).order_by(WorkoutPlanned.date.desc()).first()
                
                planned_id = planned.id if planned else None
                executed = WorkoutExecuted(
                    planned_id=planned_id,
                    date=wo_date,
                    distance_km=parsed_wo["distance_km"],
                    avg_pace=parsed_wo["avg_pace"],
                    avg_cadence=parsed_wo.get("avg_cadence"),
                    shoe_used=parsed_wo.get("shoe_used", profile.get("shoe_used")),
                    laps_summary=parsed_wo.get("laps_summary"),
                    rpe_score=parsed_wo.get("rpe_score", 6)
                )
                db.add(executed)
                if planned:
                    planned.status = "completed"
                db.commit()

                insight = AgentInsight(
                    agent_name="trainer",
                    insight_type="workout_completion",
                    memory_payload={
                        "date": executed.date.isoformat(),
                        "distance_km": executed.distance_km,
                        "avg_pace_sec": executed.avg_pace,
                        "rpe": executed.rpe_score
                    }
                )
                db.add(insight)
                db.commit()

        # ── 2. Build athlete performance profile from DB ──────────────────────
        perf_profile = build_athlete_performance_profile(db)

        # ── 3. Calculate days to target races ────────────────────────────────
        udine_date = datetime.date(2026, 9, 20)
        roma_date = datetime.date(2027, 3, 14)
        days_to_udine = (udine_date - today).days
        days_to_roma = (roma_date - today).days
        
        # Determine training phase based on nearest target race
        nearest_days = min(d for d in [days_to_udine, days_to_roma] if d > 0)
        weeks_to_race = nearest_days // 7
        nearest_race = "Maratonina di Udine" if days_to_udine > 0 and days_to_udine <= days_to_roma else "Maratona di Roma"
        
        if weeks_to_race > 12:
            phase = "Fase di Costruzione Base"
            phase_guidance = (
                "Priorità: aumentare il volume gradualmente (+10% max/settimana), "
                "consolidare il ritmo lento, costruire la resistenza aerobica. "
                "Le ripetute devono essere moderate (4-6x1000m o simili)."
            )
        elif weeks_to_race > 8:
            phase = "Fase di Sviluppo"
            phase_guidance = (
                "Priorità: aumentare la qualità dei medi e delle ripetute. "
                "Il lungo può crescere ancora. Introdurre progressivi alla fine dei lenti."
            )
        elif weeks_to_race > 4:
            phase = "Fase Competitiva"
            phase_guidance = (
                "Priorità: mantenere qualità elevata, ridurre leggermente il volume. "
                "Medi al ritmo gara o leggermente più veloci. Ripetute brevi e intense."
            )
        else:
            phase = "Tapering Pre-Gara"
            phase_guidance = (
                "Priorità: riduzione progressiva del volume (-20-30%), "
                "mantenere la qualità con uscite brevi e veloci. "
                "Nessun allenamento molto lungo, favorire il recupero."
            )

        # ── 4. Build fallback paces from PB if no history ────────────────────
        # PB Mezza 1:47:00 → VDOT-based paces for a 1:47 half marathoner:
        # Easy: ~6:00-6:15/km, Tempo: ~5:10-5:20/km, Interval: ~4:50-5:00/km
        fallback_lento = "6:00–6:15/km"
        fallback_medio = "5:25–5:40/km"
        fallback_ripetute = "4:50–5:05/km per le frazioni"
        fallback_lungo = "6:05–6:20/km"

        # ── 5. Assemble prompt for Gemini ─────────────────────────────────────
        def fmt_type_stats(key: str, label: str) -> str:
            t = perf_profile.get(key, {})
            if t.get("count_last60d", 0) == 0:
                return f"  {label}: nessun dato storico (usare valori di riferimento da PB)"
            lines = [
                f"  {label} ({t['count_last60d']} sessioni negli ultimi 60gg):",
                f"    - Passo medio ultimi 30gg: {t['avg_pace_formatted_last30d']}",
                f"    - Passo medio 30gg precedenti: {t['avg_pace_formatted_prev30d']}",
                f"    - Trend: {t['trend_label']}",
                f"    - Distanza media: {t.get('avg_distance_km_last30d', 'N/D')} km",
                f"    - Ultima uscita: {t.get('last_date', 'N/D')}, "
                f"{t.get('last_distance_km', 'N/D')} km a {t['last_pace_formatted']}/km",
            ]
            if key == "ripetute" and t.get("avg_interval_pace_formatted"):
                lines.append(f"    - Passo medio frazioni: {t['avg_interval_pace_formatted']}/km")
                lines.append(f"    - Miglior frazione: {t['best_interval_pace_formatted']}/km")
            return "\n".join(lines)

        vol = perf_profile.get("weekly_volume", {})
        readiness_note = ""
        if readiness_score < 60:
            readiness_note = (
                f"\n⚠️ ATTENZIONE RECUPERO: Readiness attuale {readiness_score}/100 — "
                "ridurre l'intensità delle sessioni di qualità e il volume totale della settimana."
            )

        system_prompt = (
            "Sei MarathonCoachAI, un allenatore di corsa esperto specializzato nella preparazione "
            "per la maratonina e la maratona.\n"
            "Generi piani di allenamento personalizzati basandoti ESCLUSIVAMENTE sui dati storici reali "
            "dell'atleta, NON su valori generici.\n"
            "Il piano deve sempre contenere ESATTAMENTE 4 sessioni:\n"
            "  1. lento_corto (Martedì)\n"
            "  2. medio (Giovedì)\n"
            "  3. ripetute (Sabato)\n"
            "  4. lento_lungo (Domenica)\n"
            "I ritmi target devono essere REALISTICI e basati sui dati storici forniti. "
            "Se i dati mostrano miglioramento, applica un incremento progressivo moderato (+3-8s/km). "
            "Se i dati mostrano rallentamento o bassa readiness, mantieni o riduci leggermente l'intensità. "
            "NON inventare ritmi non supportati dai dati.\n"
            "Rispondi in italiano con linguaggio motivante ma preciso."
        )

        user_prompt = (
            f"PROFILO ATLETA:\n"
            f"  - Età: {profile.get('age', 24)} anni, Peso: {profile.get('weight_kg', 76)} kg\n"
            f"  - PB Mezza Maratona: {profile.get('pb_half_marathon', '1:47:00')}\n"
            f"  - PB 10km: {profile.get('pb_10k', '48:55')}\n"
            f"  - Scarpa attuale: {profile.get('shoe_used', 'N/D')} ({profile.get('shoe_mileage', 0)} km)\n"
            f"  - Readiness oggi: {readiness_score}/100\n"
            f"{readiness_note}\n\n"
            f"GARE TARGET:\n"
            f"  - {nearest_race}: tra {nearest_days} giorni ({weeks_to_race} settimane)\n"
            f"  - Maratona di Roma (14 Mar 2027): tra {days_to_roma} giorni\n\n"
            f"FASE ATTUALE: {phase}\n"
            f"  {phase_guidance}\n\n"
            f"DATI STORICI PER TIPO DI ALLENAMENTO (ultimi 60 giorni):\n"
            f"  Totale sessioni analizzate: {perf_profile.get('total_workouts_analyzed', 0)}\n\n"
            f"{fmt_type_stats('lento_corto', 'LENTO CORTO')}\n\n"
            f"{fmt_type_stats('lento_lungo', 'LENTO LUNGO')}\n\n"
            f"{fmt_type_stats('medio', 'MEDIO / TEMPO')}\n\n"
            f"{fmt_type_stats('ripetute', 'RIPETUTE')}\n\n"
            f"VOLUME SETTIMANALE:\n"
            f"  - Media ultime 4 settimane: {vol.get('avg_last4_km', 0)} km/sett\n"
            f"  - Media 4 settimane precedenti: {vol.get('avg_prev4_km', 0)} km/sett\n"
            f"  - Trend volume: {vol.get('trend_label', 'N/D')}\n\n"
            f"VALORI DI RIFERIMENTO (usali SOLO se i dati storici sono assenti):\n"
            f"  - Ritmo lento di riferimento: {fallback_lento}\n"
            f"  - Ritmo medio di riferimento: {fallback_medio}\n"
            f"  - Ritmo ripetute di riferimento: {fallback_ripetute}\n"
            f"  - Ritmo lungo di riferimento: {fallback_lungo}\n\n"
            f"Genera il piano settimanale per la settimana che inizia {today.isoformat()} "
            f"con le 4 sessioni richieste, adattando ritmi e distanze ai dati storici reali."
        )

        # ── 6. Call Gemini with structured output ────────────────────────────
        weekly_plan_dict = None
        llm = get_llm("trainer")
        
        if llm:
            try:
                structured_llm = llm.with_structured_output(WeeklyPlan)
                result: WeeklyPlan = structured_llm.invoke([
                    SystemMessage(content=system_prompt),
                    HumanMessage(content=user_prompt)
                ])
                
                # Convert to dict for state storage
                weekly_plan_dict = {
                    "week_start": today.isoformat(),
                    "phase": result.phase_label,
                    "weekly_volume_km": result.weekly_volume_km,
                    "coach_notes": result.coach_notes,
                    "sessions": [
                        {
                            "workout_type": s.workout_type,
                            "day_label": s.day_label,
                            "target_distance_km": s.target_distance_km,
                            "target_pace_description": s.target_pace_description,
                            "instructions": s.instructions,
                            "rationale": s.rationale
                        }
                        for s in result.sessions
                    ]
                }

                # Save the 4 sessions to workouts_planned
                TYPE_DAYS = {
                    "lento_corto": 1,   # next Tuesday
                    "medio": 3,         # next Thursday
                    "ripetute": 5,      # next Saturday
                    "lento_lungo": 6    # next Sunday
                }
                today_wd = today.weekday()  # Monday=0
                for session in result.sessions:
                    target_wd = TYPE_DAYS.get(session.workout_type, 1)
                    days_ahead = (target_wd - today_wd + 7) % 7
                    if days_ahead == 0:
                        days_ahead = 7
                    session_date = today + datetime.timedelta(days=days_ahead)

                    # Avoid duplicate planned workouts for the same date/type
                    existing = db.query(WorkoutPlanned).filter(
                        WorkoutPlanned.date == session_date,
                        WorkoutPlanned.type == session.workout_type
                    ).first()
                    if not existing:
                        planned_wo = WorkoutPlanned(
                            date=session_date,
                            type=session.workout_type,
                            target_distance=session.target_distance_km,
                            prompt_text=session.instructions,
                            parser_template={"pace_target": session.target_pace_description},
                            status="planned"
                        )
                        db.add(planned_wo)
                db.commit()

                # Save insight for this planning event
                insight = AgentInsight(
                    agent_name="trainer",
                    insight_type="weekly_plan_generated",
                    memory_payload={
                        "date": today.isoformat(),
                        "phase": result.phase_label,
                        "weekly_volume_km": result.weekly_volume_km,
                        "sessions_count": len(result.sessions)
                    }
                )
                db.add(insight)
                db.commit()

            except Exception as e:
                print(f"Error in Trainer LLM (WeeklyPlan): {e}")
                weekly_plan_dict = _fallback_weekly_plan(today, readiness_score, perf_profile, phase)
        else:
            # No LLM available: use rule-based fallback
            weekly_plan_dict = _fallback_weekly_plan(today, readiness_score, perf_profile, phase)

        # ── 7. Update state queue ─────────────────────────────────────────────
        # Also keep legacy next_workout_plan populated for backward compatibility
        next_workout_plan_legacy = None
        if weekly_plan_dict and weekly_plan_dict.get("sessions"):
            s = weekly_plan_dict["sessions"][0]
            next_workout_plan_legacy = {
                "date": today.isoformat(),
                "type": s.get("workout_type"),
                "target_distance": s.get("target_distance_km"),
                "prompt_text": s.get("instructions"),
                "shoe_recommendation": profile.get("shoe_used", "N/D")
            }

        nodes_queue = state.get("next_nodes", [])
        next_decision = nodes_queue[0] if nodes_queue else "end"
        remaining_nodes = nodes_queue[1:] if len(nodes_queue) > 1 else []

        return {
            "recent_workouts": [],
            "recent_insights": [],
            "next_workout_plan": next_workout_plan_legacy,
            "weekly_plan": weekly_plan_dict,
            "performance_profile": perf_profile,
            "router_decision": next_decision,
            "next_nodes": remaining_nodes
        }
    finally:
        db.close()


def _fallback_weekly_plan(
    today: datetime.date,
    readiness: float,
    perf: Dict[str, Any],
    phase: str
) -> Dict[str, Any]:
    """Rule-based fallback when LLM is unavailable."""
    lc = perf.get("lento_corto", {})
    ll = perf.get("lento_lungo", {})
    me = perf.get("medio", {})
    ri = perf.get("ripetute", {})

    pace_lento = lc.get("avg_pace_formatted_last30d", "6:05")
    pace_medio = me.get("avg_pace_formatted_last30d", "5:30")
    pace_lungo = ll.get("avg_pace_formatted_last30d", "6:10")
    pace_rip = ri.get("avg_interval_pace_formatted", "5:00")

    # Apply readiness penalty
    dist_factor = 0.85 if readiness < 60 else 1.0

    return {
        "week_start": today.isoformat(),
        "phase": phase,
        "weekly_volume_km": round(38 * dist_factor, 1),
        "coach_notes": (
            "Piano generato automaticamente (LLM non disponibile). "
            "I ritmi sono basati sulla tua storia recente."
        ),
        "sessions": [
            {
                "workout_type": "lento_corto",
                "day_label": "Martedì",
                "target_distance_km": round(9 * dist_factor, 1),
                "target_pace_description": f"{pace_lento}/km",
                "instructions": (
                    f"Corsa lenta {round(9 * dist_factor, 0):.0f} km a ritmo confortevole. "
                    "Respirazione facile, conversazione possibile per tutta la durata."
                ),
                "rationale": f"Basato sul tuo passo lento medio: {pace_lento}/km"
            },
            {
                "workout_type": "medio",
                "day_label": "Giovedì",
                "target_distance_km": round(12 * dist_factor, 1),
                "target_pace_description": f"{pace_medio}/km",
                "instructions": (
                    "2 km riscaldamento lento, poi corpo corsa a ritmo medio costante, "
                    "2 km defaticamento."
                ),
                "rationale": f"Basato sul tuo passo medio: {pace_medio}/km"
            },
            {
                "workout_type": "ripetute",
                "day_label": "Sabato",
                "target_distance_km": round(10 * dist_factor, 1),
                "target_pace_description": f"{pace_rip}/km per le frazioni",
                "instructions": (
                    "2 km riscaldamento, 5x1000m a ritmo ripetute con 90s recupero, "
                    "2 km defaticamento."
                ),
                "rationale": f"Basato sul tuo passo medio frazioni: {pace_rip}/km"
            },
            {
                "workout_type": "lento_lungo",
                "day_label": "Domenica",
                "target_distance_km": round(16 * dist_factor, 1),
                "target_pace_description": f"{pace_lungo}/km",
                "instructions": (
                    f"Lungo {round(16 * dist_factor, 0):.0f} km a ritmo lento costante. "
                    "Gestisci l'idratazione ogni 5 km. Mantenere conversazione possibile."
                ),
                "rationale": f"Basato sul tuo passo lungo medio: {pace_lungo}/km"
            }
        ]
    }


# ─────────────────────────────────────────────────────────────────────────────
# 4. NUTRITIONIST NODE
# ─────────────────────────────────────────────────────────────────────────────
def nutritionist_node(state: AgentState) -> Dict[str, Any]:
    """
    Estimates the caloric impact and nutrient macros based on the athlete's
    workouts and food logs.
    """
    db = SessionLocal()
    try:
        today = datetime.date.today()
        profile = state.get("athlete_profile", {})
        
        # 1. Parse and Save Nutrition Log if present
        parsed_nut_list = state.get("parsed_nutrition")
        if parsed_nut_list:
            if not isinstance(parsed_nut_list, list):
                parsed_nut_list = [parsed_nut_list]
                
            for parsed_nut in parsed_nut_list:
                meal_type = parsed_nut.get("meal_type")
                if not meal_type:
                    current_hour = datetime.datetime.now().hour
                    if current_hour < 11:
                        meal_type = "colazione"
                    elif current_hour < 13:
                        meal_type = "merenda_mattina"
                    elif current_hour < 16:
                        meal_type = "pranzo"
                    elif current_hour < 19:
                        meal_type = "merenda_pomeriggio"
                    else:
                        meal_type = "cena"
                        
                log = NutritionLog(
                    date=today,
                    raw_input=parsed_nut.get("raw_input", "Pasto registrato"),
                    est_calories=parsed_nut.get("est_calories"),
                    macros_json=parsed_nut.get("macros"),
                    meal_type=meal_type
                )
                db.add(log)
                db.commit()
                
                insight = AgentInsight(
                    agent_name="nutritionist",
                    insight_type="nutrition_log",
                    memory_payload={
                        "date": today.isoformat(),
                        "meal_type": meal_type,
                        "calories": log.est_calories,
                        "macros": log.macros_json
                    }
                )
                db.add(insight)
                db.commit()

        # 2. Estimate caloric expenditure and nutritional needs
        workout_exec = state.get("parsed_workout")
        weekly_plan = state.get("weekly_plan")
        
        weight_kg = profile.get("weight_kg", 76.0)
        
        kcal_burned = 0
        if workout_exec:
            kcal_burned = int(workout_exec["distance_km"] * weight_kg * 1.0)
        elif weekly_plan and weekly_plan.get("sessions"):
            # Estimate from the hardest session this week
            hard_session = next(
                (s for s in weekly_plan["sessions"] if s["workout_type"] in ["ripetute", "medio"]),
                weekly_plan["sessions"][0]
            )
            kcal_burned = int(hard_session.get("target_distance_km", 10) * weight_kg * 1.0)

        is_hard = False
        if workout_exec and workout_exec.get("rpe_score", 0) >= 7:
            is_hard = True
        elif weekly_plan:
            types = [s.get("workout_type") for s in weekly_plan.get("sessions", [])]
            if "ripetute" in types or "medio" in types:
                is_hard = True

        carbs_g = round(weight_kg * (6.5 if is_hard else 4.0))
        protein_g = round(weight_kg * 1.6)
        fat_g = round(weight_kg * 1.0)
        total_target_kcal = int(1700 + kcal_burned)

        nutrition_advice = {
            "workout_energy_expenditure_kcal": kcal_burned,
            "recommended_daily_calories": total_target_kcal,
            "macros_target": {
                "carbs_g": carbs_g,
                "protein_g": protein_g,
                "fat_g": fat_g
            },
            "advice": (
                "Carica i carboidrati prima della corsa intensa." if is_hard
                else "Mantieni l'apporto calorico pulito e bilanciato."
            )
        }

        nodes_queue = state.get("next_nodes", [])
        next_decision = nodes_queue[0] if nodes_queue else "end"
        remaining_nodes = nodes_queue[1:] if len(nodes_queue) > 1 else []

        return {
            "nutrition_advice": nutrition_advice,
            "router_decision": next_decision,
            "next_nodes": remaining_nodes
        }
    finally:
        db.close()


# ─────────────────────────────────────────────────────────────────────────────
# 5. RESPONDER NODE
# ─────────────────────────────────────────────────────────────────────────────
def responder_node(state: AgentState) -> Dict[str, Any]:
    """
    Compiles all the inputs and agent recommendations into a single, cohesive,
    motivating, and structured Italian response to be sent back to the user.
    Now includes the full personalized weekly plan when available.
    """
    llm = get_llm("responder")
    
    readiness = state.get("readiness_data")
    weekly_plan = state.get("weekly_plan")
    next_workout = state.get("next_workout_plan")
    nutrition = state.get("nutrition_advice")
    parsed_wo = state.get("parsed_workout")
    parsed_nut = state.get("parsed_nutrition")
    parsed_met = state.get("parsed_metrics")
    perf_profile = state.get("performance_profile")
    athlete = state.get("athlete_profile", {})

    # ── Format weekly plan for text display ──────────────────────────────────
    def format_weekly_plan(plan: Dict) -> str:
        if not plan:
            return ""
        EMOJI = {
            "lento_corto": "🟢",
            "lento_lungo": "🟡",
            "medio": "🟠",
            "ripetute": "🔴",
            "gara": "🏆"
        }
        lines = [
            f"\n📅 **Piano Settimanale — {plan.get('phase', 'N/D')}**",
            f"   Volume previsto: **{plan.get('weekly_volume_km', 0)} km**\n"
        ]
        for s in plan.get("sessions", []):
            emoji = EMOJI.get(s.get("workout_type", ""), "▶")
            lines.append(
                f"{emoji} **{s.get('day_label', '')} — {s.get('workout_type', '').replace('_', ' ').upper()}**"
            )
            lines.append(f"   📏 {s.get('target_distance_km', 0)} km · ⏱ {s.get('target_pace_description', '')}")
            lines.append(f"   {s.get('instructions', '')}")
            if s.get("rationale"):
                lines.append(f"   _(↳ {s.get('rationale')})_")
            lines.append("")
        if plan.get("coach_notes"):
            lines.append(f"💬 **Note del Coach:** {plan['coach_notes']}")
        return "\n".join(lines)

    # ── Format performance trend ──────────────────────────────────────────────
    def format_trend_note(perf: Dict) -> str:
        if not perf:
            return ""
        notes = []
        for wtype, label in [("lento_corto", "lento"), ("medio", "medio"), ("ripetute", "frazioni")]:
            t = perf.get(wtype, {})
            if t.get("trend_sec_vs_prev_month") and t["trend_sec_vs_prev_month"] < -5:
                notes.append(
                    f"il tuo passo {label} è migliorato di "
                    f"{abs(t['trend_sec_vs_prev_month']):.0f}s/km nell'ultimo mese ✅"
                )
        if notes:
            return "📈 **Progressi rilevati:** " + " | ".join(notes)
        return ""

    # ── Use Gemini if available ───────────────────────────────────────────────
    if llm:
        system_prompt = (
            "Sei il team di allenatori personali MarathonCoachAI (Trainer, Fisiologo, Nutrizionista).\n"
            "Comunica con tono motivante, professionale e chiaro in ITALIANO.\n"
            "Mantieni i messaggi puliti ed evita l'uso eccessivo di emoji (rimuovi simboli non necessari o ripetitivi, usali solo se hanno una reale utilità informativa o visiva per rendere il testo leggibile).\n"
            "L'atleta si prepara per: Maratonina di Udine (20 Set 2026) e Maratona di Roma (14 Mar 2027).\n"
            "Analizza il messaggio dell'atleta e rispondi in modo cordiale, fornendo feedback su allenamenti, nutrizione e stato di forma.\n"
            "Se l'atleta invia la foto di un pasto, analizzala e stima calorie e macronutrienti per aiutarlo a tracciare la nutrizione."
        )
        # Fetch today's logged nutrition from DB to provide full context to the LLM
        db_session = SessionLocal()
        today_logs_str = "nessuno"
        try:
            today_date = datetime.date.today()
            today_logs = db_session.query(NutritionLog).filter(NutritionLog.date == today_date).all()
            if today_logs:
                today_logs_str = ", ".join([
                    f"{log.raw_input} ({log.est_calories} kcal, Carboidrati: {log.macros_json.get('carbs', 0) if log.macros_json else 0}g, Proteine: {log.macros_json.get('protein', 0) if log.macros_json else 0}g, Grassi: {log.macros_json.get('fat', 0) if log.macros_json else 0}g) per {log.meal_type}"
                    for log in today_logs
                ])
        except Exception as e:
            print(f"Error fetching today's nutrition logs in responder: {e}")
        finally:
            db_session.close()

        trend_note = format_trend_note(perf_profile)
        plan_text = format_weekly_plan(weekly_plan) if weekly_plan else ""
        
        days_it = ["Lunedì", "Martedì", "Mercoledì", "Giovedì", "Venerdì", "Sabato", "Domenica"]
        today_day_name = days_it[today_date.weekday()]
        
        prompt = (
            f"Data corrente di oggi: {today_date.isoformat()} ({today_day_name})\n"
            f"Messaggio dell'atleta: \"{state.get('message_text') or ''}\"\n"
            f"Profilo atleta: PB Mezza {athlete.get('pb_half_marathon', '1:47:00')}, PB 10k {athlete.get('pb_10k', '48:55')}, scarpa {athlete.get('shoe_used', 'N/D')} ({athlete.get('shoe_mileage', 0)} km).\n"
            f"Readiness: {readiness.get('readiness_score', 80) if readiness else 'N/D'}/100\n"
            f"Dati Nutrizionali Loggati Oggi (nel database): {today_logs_str}\n"
            f"Dati Nutrizionali Loggati in questo messaggio: {json.dumps(state.get('parsed_nutrition')) if state.get('parsed_nutrition') else 'nessuno'}\n"
            f"Workout eseguito oggi: {json.dumps(parsed_wo) if parsed_wo else 'nessuno'}\n"
            f"Trend di miglioramento: {trend_note if trend_note else 'dati insufficienti'}\n"
        )
        if plan_text:
            prompt += f"\nPiano settimanale generato:\n{plan_text}\n"

        image_data = state.get("image_data")
        human_content = [{"type": "text", "text": prompt}]
        if image_data:
            human_content.append({
                "type": "image_url",
                "image_url": {
                    "url": f"data:image/jpeg;base64,{image_data}"
                }
            })

        try:
            response = llm.invoke([
                SystemMessage(content=system_prompt),
                HumanMessage(content=human_content)
            ])
            content = response.content
            if isinstance(content, list):
                response_text = "".join(
                    (b.get("text", "") if isinstance(b, dict) else
                     b if isinstance(b, str) else
                     getattr(b, "text", ""))
                    for b in content
                )
            else:
                response_text = content.strip() if content else ""
            return {"response_message": response_text, "router_decision": "end"}
        except Exception as e:
            print(f"Error in Responder LLM: {e}")

    # ── Manual fallback formatter ─────────────────────────────────────────────
    response_parts = ["🏃 **Marathon-Multi-Agent Coach**\n"]

    trend_note = format_trend_note(perf_profile)
    if trend_note:
        response_parts.append(trend_note + "\n")

    if parsed_wo:
        pace_min = int(parsed_wo['avg_pace'] // 60)
        pace_sec = int(parsed_wo['avg_pace'] % 60)
        response_parts.append(
            f"**Analisi dell'Allenamento:**\n"
            f"• Distanza: {parsed_wo['distance_km']} km\n"
            f"• Passo Medio: {pace_min}:{pace_sec:02d}/km\n"
            f"• Cadenza Media: {parsed_wo.get('avg_cadence', 'N/D')} spm\n"
            f"• Sforzo (RPE): {parsed_wo.get('rpe_score', 'N/D')}/10\n"
        )

    if weekly_plan:
        response_parts.append(format_weekly_plan(weekly_plan))
    elif next_workout:
        response_parts.append(
            f"📅 **Prossimo Allenamento ({next_workout['date']}):**\n"
            f"• Tipo: {next_workout.get('type', '').capitalize()}\n"
            f"• Distanza: {next_workout.get('target_distance', 0)} km\n"
            f"• Indicazioni: {next_workout.get('prompt_text', '')}\n"
        )

    if readiness and not weekly_plan:
        response_parts.append(
            f"**Readiness: {readiness['readiness_score']}/100** ({readiness['status']})\n"
            f"• Sonno: {readiness['sleep_hours']}h | "
            f"RHR: {readiness['resting_hr']} bpm | HRV: {readiness['hrv_score']}\n"
        )

    if parsed_nut and not parsed_wo:
        if isinstance(parsed_nut, list):
            items_lines = []
            for item in parsed_nut:
                macros_info = ""
                m = item.get("macros", {})
                if m:
                    macros_info = f" (Carboidrati: {m.get('carbs', 0)}g, Proteine: {m.get('protein', 0)}g, Grassi: {m.get('fat', 0)}g)"
                items_lines.append(f"• **{item.get('raw_input')}** (~{item.get('est_calories', 0)} kcal){macros_info}")
            items_str = "\n".join(items_lines)
            
            response_parts.append(
                f"**Pasti Registrati nel Diario!**\n"
                f"Ho inserito correttamente i seguenti cibi:\n{items_str}\n"
            )
        else:
            macros_info = ""
            m = parsed_nut.get("macros", {})
            if m:
                macros_info = f" (Carboidrati: {m.get('carbs', 0)}g, Proteine: {m.get('protein', 0)}g, Grassi: {m.get('fat', 0)}g)"
            response_parts.append(
                f"**Pasto Registrato nel Diario!**\n"
                f"Ho inserito correttamente: **{parsed_nut.get('raw_input')}** (~{parsed_nut.get('est_calories')} kcal){macros_info}\n"
            )

    if nutrition:
        response_parts.append(
            f"**Target Giornaliero Consigliato:**\n"
            f"• Calorie: **{nutrition['recommended_daily_calories']} kcal**\n"
            f"• Macronutrienti: Carboidrati **{nutrition['macros_target']['carbs_g']}g** | Proteine **{nutrition['macros_target']['protein_g']}g** | Grassi **{nutrition['macros_target']['fat_g']}g**\n\n"
            f"**Consiglio del Team:** {nutrition['advice']}\n"
        )

    response_text = "\n".join(response_parts)
    return {"response_message": response_text, "router_decision": "end"}
