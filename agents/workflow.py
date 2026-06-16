from typing import Dict, Any, List, Optional
from langgraph.graph import StateGraph, END
from agents.state import AgentState, AthleteProfile
from agents.nodes import (
    manager_router_node,
    physiologist_node,
    trainer_node,
    nutritionist_node,
    responder_node
)

def route_next(state: AgentState) -> str:
    """
    Conditional routing function. Inspects router_decision in the State to
    determine which node should execute next.
    """
    decision = state.get("router_decision", "responder")
    
    # Restrict to valid node names or default to responder
    valid_nodes = ["physiologist", "trainer", "nutritionist", "responder"]
    if decision in valid_nodes:
        return decision
    return "responder"


# Create and configure the state graph
workflow = StateGraph(AgentState)

# Add all agent nodes
workflow.add_node("manager", manager_router_node)
workflow.add_node("physiologist", physiologist_node)
workflow.add_node("trainer", trainer_node)
workflow.add_node("nutritionist", nutritionist_node)
workflow.add_node("responder", responder_node)

# Set entry point
workflow.set_entry_point("manager")

# Add conditional edges from each active node
workflow.add_conditional_edges(
    "manager",
    route_next,
    {
        "physiologist": "physiologist",
        "trainer": "trainer",
        "nutritionist": "nutritionist",
        "responder": "responder"
    }
)

workflow.add_conditional_edges(
    "physiologist",
    route_next,
    {
        "physiologist": "physiologist",
        "trainer": "trainer",
        "nutritionist": "nutritionist",
        "responder": "responder"
    }
)

workflow.add_conditional_edges(
    "trainer",
    route_next,
    {
        "physiologist": "physiologist",
        "trainer": "trainer",
        "nutritionist": "nutritionist",
        "responder": "responder"
    }
)

workflow.add_conditional_edges(
    "nutritionist",
    route_next,
    {
        "physiologist": "physiologist",
        "trainer": "trainer",
        "nutritionist": "nutritionist",
        "responder": "responder"
    }
)

# Responder node is the end node of the workflow
workflow.add_edge("responder", END)

# Compile graph
compiled_workflow = workflow.compile()


def run_agent_pipeline(
    user_id: int,
    message_text: Optional[str] = None,
    file_path: Optional[str] = None,
    file_type: Optional[str] = None,
    parsed_workout: Optional[Dict[str, Any]] = None,
    image_data: Optional[str] = None
) -> Dict[str, Any]:
    """
    Helper function to run the compiled LangGraph workflow with standard inputs
    and pre-filled athlete profile.
    """


    
    # Calculate current shoe mileage dynamically
    from database.database import SessionLocal, WorkoutExecuted
    from sqlalchemy import func
    import datetime
    
    db_session = SessionLocal()
    current_mileage = 632.5
    try:
        baseline_date = datetime.date(2026, 6, 15)
        new_km = db_session.query(func.sum(WorkoutExecuted.distance_km)).filter(
            WorkoutExecuted.date > baseline_date,
            WorkoutExecuted.shoe_used == "Asics Gel-Nimbus 27"
        ).scalar() or 0.0
        current_mileage = round(632.5 + new_km, 1)
    except Exception as e:
        print(f"Error computing shoe mileage in workflow: {e}")
    finally:
        db_session.close()

    # 24-year-old athlete profile details
    default_profile: AthleteProfile = {
        "age": 24,
        "gender": "M",
        "weight_kg": 76.0,
        "height_cm": 178,
        "transition_race_1": "Maratonina di Udine (Mezza Maratona)",
        "transition_date_1": "2026-09-20",
        "target_race": "Maratona di Roma",
        "target_date": "2027-03-14",
        "pb_half_marathon": "1:47:00",
        "pb_10k": "48:55",
        "running_experience": "Corsa regolare da Settembre 2025, con 4 uscite settimanali. Attualmente in fase di transizione e costruzione verso la distanza regina.",
        "shoe_used": "Asics Gel-Nimbus 27",
        "shoe_mileage": current_mileage,
        "hr_max": 204,
        "rhr_baseline": 56,
        "sleep_hr_baseline": 51,
        "runs_per_week": 4,
        "weekly_volume_km": 45.0,
        "easy_pace": "6:00 min/km"
    }

    # Prepare initial state
    initial_state: AgentState = {
        "user_id": user_id,
        "message_text": message_text,
        "file_path": file_path,
        "file_type": file_type,
        "image_data": image_data,
        
        "parsed_workout": parsed_workout,
        "parsed_metrics": None,
        "parsed_nutrition": None,
        
        "recent_workouts": [],
        "recent_metrics": [],
        "recent_insights": [],
        
        "readiness_data": None,
        "next_workout_plan": None,
        "nutrition_advice": None,
        
        "router_decision": "manager",
        "next_nodes": [],
        "response_message": "",
        
        "athlete_profile": default_profile
    }

    # Execute workflow
    result = compiled_workflow.invoke(initial_state)
    return result
