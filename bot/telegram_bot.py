import os
import logging
from typing import Optional
from telegram import Update
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters
)
from parser.parser import parse_workout_file, generate_mock_workout_data, parse_gpx_workout
from agents.workflow import run_agent_pipeline

# Enable logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    welcome_text = (
        "🏃‍♂️ **Marathon-Multi-Agent Coach** 🏃‍♂️\n\n"
        "Ciao! Sono il tuo assistente virtuale personale per la preparazione atletica.\n"
        "Ti aiuterò a preparare la **Maratonina di Udine** (20 Set 2026) e il tuo target principale: la **Maratona di Roma** (14 Mar 2027)!\n\n"
        "Cosa puoi fare:\n"
        "1️⃣ **Inviare dati quotidiani**: es. 'HRV 65, sonno 8 ore, battiti 54'\n"
        "2️⃣ **Registrare pasti**: es. 'Oggi a pranzo riso con pollo e avocado'\n"
        "3️⃣ **Caricare allenamenti**: usa l'interfaccia web della Dashboard per caricare i file GPX specificando la tipologia e i dettagli dell'allenamento!\n"
        "4️⃣ **Pianificare**: chiedimi 'Qual è il prossimo allenamento?' per generare la sessione successiva.\n\n"
        "Scarpe attuali configurate: *Asics Gel-Nimbus 27* (monitorerò la loro usura durante la preparazione)."
    )
    await update.message.reply_text(welcome_text, parse_mode="Markdown")


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Sends a help message."""
    help_text = (
        "ℹ️ **Comandi disponibili**:\n"
        "/start - Avvia l'interazione e mostra le istruzioni.\n"
        "/help - Mostra questo messaggio di aiuto.\n\n"
        "Scrivimi messaggi testuali per registrare pasti, dati fisiologici o chiedere indicazioni sul tuo piano. Per i file GPX, usa la dashboard web."
    )
    await update.message.reply_text(help_text, parse_mode="Markdown")


async def safe_reply(update: Update, text: str, parse_mode: Optional[str] = "Markdown") -> None:
    """
    Sends a message back to the user, falling back to plain text if Telegram's strict
    markdown parser raises a BadRequest error due to unbalanced formatting tags.
    """
    try:
        await update.message.reply_text(text, parse_mode=parse_mode)
    except Exception as e:
        if "can't parse entities" in str(e).lower():
            logger.warning("Markdown parsing failed, falling back to plain text reply.")
            await update.message.reply_text(text, parse_mode=None)
        else:
            raise e


async def handle_text_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Processes any incoming text message using the LangGraph agent workflow."""
    user_id = update.effective_user.id
    message_text = update.message.text
    
    await update.message.reply_chat_action("typing")
    
    try:
        # Run the agent workflow
        result = run_agent_pipeline(user_id=user_id, message_text=message_text)
        response_text = result.get("response_message", "Nessuna risposta generata.")
        
        await safe_reply(update, response_text)
    except Exception as e:
        logger.error(f"Error executing agent pipeline for user {user_id}: {e}", exc_info=True)
        await update.message.reply_text(
            f"❌ Si è verificato un errore nell'elaborazione del messaggio: {str(e)}"
        )


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles uploaded GPX workout files via Telegram, parses them as Corsa Lenta by default."""
    user_id = update.effective_user.id
    doc = update.message.document
    file_name = doc.file_name.lower() if doc.file_name else ""
    
    if not file_name.endswith(".gpx"):
        await update.message.reply_text(
            "❌ Formato non supportato su Telegram. Per favore carica un file `.gpx`.\n"
            "Puoi caricare file GPX complessi (con ripetute) direttamente sulla Dashboard web."
        )
        return

    await update.message.reply_text("📥 Ricevuto! Sto scaricando e analizzando il tuo allenamento come Corsa Lenta...")
    await update.message.reply_chat_action("typing")

    try:
        # Download the file
        os.makedirs("./data/uploads", exist_ok=True)
        local_path = os.path.join("./data/uploads", doc.file_name)
        
        tg_file = await context.bot.get_file(doc.file_id)
        await tg_file.download_to_drive(local_path)
        logger.info(f"Downloaded GPX file: {local_path}")

        # Parse workout file with fallback params (lento_corto)
        params = {"workout_type": "lento_corto"}
        parsed_workout = parse_gpx_workout(local_path, params)
        
        # Save to database
        from database.database import SessionLocal, WorkoutExecuted, WorkoutPlanned
        import datetime
        
        db = SessionLocal()
        try:
            # Check if there is an active planned workout
            planned = db.query(WorkoutPlanned).filter(
                WorkoutPlanned.date <= datetime.date.today(),
                WorkoutPlanned.status == "planned"
            ).order_by(WorkoutPlanned.date.desc()).first()
            planned_id = planned.id if planned else None
            
            executed = WorkoutExecuted(
                planned_id=planned_id,
                date=datetime.date.fromisoformat(parsed_workout["date"]),
                workout_type="lento_corto",
                distance_km=parsed_workout["distance_km"],
                avg_pace=parsed_workout["avg_pace"],
                avg_cadence=parsed_workout.get("avg_cadence"),
                shoe_used="Asics Gel-Nimbus 27", # fallback shoe
                elevation_gain=parsed_workout.get("elevation_gain"),
                elevation_loss=parsed_workout.get("elevation_loss"),
                max_hr=parsed_workout.get("max_hr"),
                laps_summary=parsed_workout.get("laps_summary"),
                rpe_score=5 # default RPE
            )
            db.add(executed)
            if planned:
                planned.status = "completed"
            db.commit()
        except Exception as dbe:
            db.rollback()
            raise dbe
        finally:
            db.close()
            
        # Run agent workflow with parsed workout
        result = run_agent_pipeline(
            user_id=user_id,
            message_text=f"[Allenamento GPX Caricato via Telegram: {doc.file_name}]",
            file_path=local_path,
            file_type="gpx",
            parsed_workout=parsed_workout
        )
        
        response_text = result.get("response_message", "Allenamento salvato con successo!")
        response_text += (
            "\n\n💡 *Nota del Coach*: Questo allenamento è stato salvato come Corsa Lenta. "
            "Se si tratta di ripetute o corsa media, ti consiglio di caricarlo tramite la Dashboard "
            "web per estrarre il passo di ciascuna frazione e recupero!"
        )
        await safe_reply(update, response_text)
        
    except Exception as e:
        logger.error(f"Error handling workout file upload: {e}", exc_info=True)
        await update.message.reply_text(
            f"❌ Errore durante il parsing del file: {str(e)}\n\n"
            "💡 Prova a caricare lo stesso file tramite l'interfaccia web della Dashboard!"
        )


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles voice notes (stubbed out for transcription / local testing)."""
    user_id = update.effective_user.id
    await update.message.reply_text(
        "🎙️ **Nota Vocale Ricevuta!**\n"
        "La trascrizione vocale locale è attiva come simulazione.\n"
        "Trascrizione stimata: *'Oggi corsa facile di 8 km, mi sentivo bene.'*\n\n"
        "Procedo ad analizzare il contenuto...",
        parse_mode="Markdown"
    )
    await update.message.reply_chat_action("typing")
    
    # Run pipeline with a mock transcription
    result = run_agent_pipeline(
        user_id=user_id,
        message_text="Corsa facile di 8 km con Asics Gel-Nimbus, mi sentivo bene, RPE 5"
    )
    
    await safe_reply(update, result.get("response_message", ""))


def setup_bot(token: str) -> Application:
    """Configures handlers and returns the bot application instance."""
    app = ApplicationBuilder().token(token).build()
    
    # Handlers
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_message))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))
    
    return app

