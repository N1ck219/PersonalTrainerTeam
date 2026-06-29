"""
sync/samsung_health_sync.py
============================
Scarica periodicamente i dati Samsung Health (FC a riposo, sonno, passi)
da Google Drive e li importa nella tabella DailyMetric del database SQLite.

SETUP (una-tantum):
  1. Google Cloud Console → abilita "Google Drive API"
  2. Crea un OAuth2 Client ID (tipo "Desktop app") → scarica come credentials.json
  3. Metti credentials.json nella root del progetto (d:/python/PersonalTrainerTeam/)
  4. Aggiungi il tuo account come "Utente di test" nella schermata consenso OAuth
     (Google Cloud Console → API e Servizi → Schermata consenso OAuth → Utenti di test)
  5. Prima autenticazione:
       python sync/samsung_health_sync.py --auth
     Si aprirà il browser → autorizza → verrà creato token.json (non committare)
  6. Imposta nel .env uno o più folder ID:
       # Cartella unica con tutti i file:
       SAMSUNG_DRIVE_FOLDER_ID=1aBcDeFg...
       # Oppure cartelle separate per tipo (hanno la precedenza):
       SAMSUNG_HR_FOLDER_ID=1aBcDeFg...       # Health Sync Freq. cardiaca
       SAMSUNG_STEPS_FOLDER_ID=1bCdEfGh...    # Health Sync Passi
       SAMSUNG_SLEEP_FOLDER_ID=1cDeFgHi...    # Health Sync Sonno

Formato file supportati (Samsung Health export):
  - JSON export tramite "Health Export for Samsung Health" app
  - Backup ZIP Samsung Health → scompattato localmente
  - File CSV prodotti da app di terze parti (con fallback automatico)

NOTA: Il formato esatto dei file Samsung Health varia a seconda
dell'app/metodo di esportazione. Il parser è robusto e supporta
i formati più comuni. Se il parsing fallisce, controllare i log
e aggiornare le funzioni parse_* con la struttura reale del file.
"""

import os
import io
import json
import datetime
import logging
from dataclasses import dataclass, field
from typing import Optional
from pathlib import Path

logger = logging.getLogger(__name__)

# ─── Google API token paths ────────────────────────────────────────────────────
_ROOT = Path(__file__).parent.parent  # project root
CREDENTIALS_FILE = _ROOT / "credentials.json"
TOKEN_FILE = _ROOT / "token.json"

# Google Drive API scopes (read-only is sufficient)
SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]

# Samsung Health data types we are interested in
# Map: keyword in filename → handler
SAMSUNG_FILE_PATTERNS = {
    "heart_rate": "heart_rate",
    "freq":       "heart_rate",  # "Freq. cardiaca" folder files
    "cardiaca":   "heart_rate",
    "sleep": "sleep",
    "sonno": "sleep",            # Italian folder name fallback
    "step_daily_trend": "steps",
    "step_count": "steps",       # alternative naming
    "steps": "steps",            # generic fallback
    "passi": "steps",            # Italian folder name fallback
}

# Per-type folder env variable names (have priority over SAMSUNG_DRIVE_FOLDER_ID)
FOLDER_ENV_BY_TYPE = {
    "heart_rate": "SAMSUNG_HR_FOLDER_ID",
    "steps":      "SAMSUNG_STEPS_FOLDER_ID",
    "sleep":      "SAMSUNG_SLEEP_FOLDER_ID",
}


# ─── Data structures ───────────────────────────────────────────────────────────

@dataclass
class SyncReport:
    """Report returned after a sync run."""
    files_found: int = 0
    files_processed: int = 0
    records_upserted: int = 0
    errors: list = field(default_factory=list)
    started_at: datetime.datetime = field(default_factory=datetime.datetime.now)
    finished_at: Optional[datetime.datetime] = None

    def summary(self) -> str:
        dur = ""
        if self.finished_at:
            elapsed = (self.finished_at - self.started_at).total_seconds()
            dur = f" in {elapsed:.1f}s"
        return (
            f"Sync completato{dur}: "
            f"{self.files_found} file trovati, "
            f"{self.files_processed} processati, "
            f"{self.records_upserted} righe DB inserite/aggiornate, "
            f"{len(self.errors)} errori."
        )


# ─── Google Drive Authentication ───────────────────────────────────────────────

def _get_drive_service():
    """
    Autentica con Google Drive API usando OAuth2.
    Restituisce un googleapiclient.discovery.Resource oppure None se
    le credenziali non sono configurate.
    """
    try:
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from google.auth.transport.requests import Request
        from googleapiclient.discovery import build
    except ImportError:
        logger.error(
            "[SamsungSync] Librerie Google API mancanti. "
            "Esegui: pip install google-api-python-client google-auth-httplib2 google-auth-oauthlib"
        )
        return None

    if not CREDENTIALS_FILE.exists():
        logger.warning(
            f"[SamsungSync] File credentials.json non trovato in {CREDENTIALS_FILE}. "
            "Scaricalo da Google Cloud Console (OAuth2 Desktop App) e mettilo nella root del progetto."
        )
        return None

    creds = None

    # Load existing token
    if TOKEN_FILE.exists():
        try:
            creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)
        except Exception as e:
            logger.warning(f"[SamsungSync] Impossibile caricare token.json: {e}")
            creds = None

    # Refresh or request new credentials
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
                logger.info("[SamsungSync] Token OAuth2 rinnovato automaticamente.")
            except Exception as e:
                logger.warning(f"[SamsungSync] Impossibile rinnovare il token: {e}. Richiede re-autenticazione.")
                creds = None

        if not creds:
            logger.warning(
                "[SamsungSync] Token non valido o assente. "
                "Esegui 'python sync/samsung_health_sync.py --auth' per autenticarti."
            )
            return None

        # Save refreshed credentials
        try:
            with open(TOKEN_FILE, "w") as token:
                token.write(creds.to_json())
        except Exception as e:
            logger.warning(f"[SamsungSync] Impossibile salvare token.json: {e}")

    try:
        service = build("drive", "v3", credentials=creds)
        logger.info("[SamsungSync] Connessione a Google Drive OK.")
        return service
    except Exception as e:
        logger.error(f"[SamsungSync] Errore durante la costruzione del servizio Drive: {e}")
        return None


def _run_first_auth():
    """
    Esegue il flusso OAuth2 interattivo (apre il browser).
    Da eseguire una sola volta manualmente con: python sync/samsung_health_sync.py --auth
    """
    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
        from google.oauth2.credentials import Credentials
    except ImportError:
        print("ERRORE: pip install google-api-python-client google-auth-httplib2 google-auth-oauthlib")
        return

    if not CREDENTIALS_FILE.exists():
        print(f"ERRORE: credentials.json non trovato in {CREDENTIALS_FILE}")
        print("Scaricalo da Google Cloud Console > OAuth2 > Desktop App")
        return

    flow = InstalledAppFlow.from_client_secrets_file(str(CREDENTIALS_FILE), SCOPES)
    creds = flow.run_local_server(port=0)

    with open(TOKEN_FILE, "w") as token:
        token.write(creds.to_json())

    print(f"✅ Autenticazione riuscita! Token salvato in {TOKEN_FILE}")
    print("Ora puoi avviare il server normalmente.")


# ─── Google Drive File Operations ─────────────────────────────────────────────

def _list_drive_files(service, folder_id: str, since_date: Optional[datetime.date] = None) -> list[dict]:
    """
    Elenca i file JSON nella cartella Samsung Health su Drive.
    Opzionalmente filtra per file modificati dopo since_date.
    """
    query_parts = [
        f"'{folder_id}' in parents",
        "trashed = false",
        "(mimeType = 'application/json' or mimeType = 'text/plain' or name contains '.json' or name contains '.csv')"
    ]

    if since_date:
        # Drive API usa RFC 3339 / ISO 8601
        since_str = datetime.datetime.combine(since_date, datetime.time.min).isoformat() + "Z"
        query_parts.append(f"modifiedTime >= '{since_str}'")

    query = " and ".join(query_parts)

    files = []
    page_token = None

    try:
        while True:
            resp = service.files().list(
                q=query,
                spaces="drive",
                fields="nextPageToken, files(id, name, modifiedTime, size)",
                pageToken=page_token,
                pageSize=200
            ).execute()

            files.extend(resp.get("files", []))
            page_token = resp.get("nextPageToken")
            if not page_token:
                break
    except Exception as e:
        logger.error(f"[SamsungSync] Errore elencando file su Drive: {e}")

    return files


def _download_file_content(service, file_id: str) -> Optional[bytes]:
    """Scarica il contenuto di un file da Drive come bytes."""
    try:
        from googleapiclient.http import MediaIoBaseDownload
        request = service.files().get_media(fileId=file_id)
        buf = io.BytesIO()
        downloader = MediaIoBaseDownload(buf, request)
        done = False
        while not done:
            _, done = downloader.next_chunk()
        return buf.getvalue()
    except Exception as e:
        logger.error(f"[SamsungSync] Errore download file {file_id}: {e}")
        return None


def _detect_data_type(filename: str) -> Optional[str]:
    """
    Rileva il tipo di dato Samsung Health dal nome del file.
    Restituisce: 'heart_rate', 'sleep', 'steps', o None.
    """
    name_lower = filename.lower()
    for pattern, data_type in SAMSUNG_FILE_PATTERNS.items():
        if pattern in name_lower:
            return data_type
    return None


# ─── Samsung Health JSON Parsers ───────────────────────────────────────────────

def _parse_heart_rate(data: dict | list) -> dict[datetime.date, int]:
    """
    Parsifica un file JSON di Samsung Health con dati di frequenza cardiaca.
    Restituisce: {data: resting_hr_bpm}

    Supporta diversi formati Samsung Health:
    - {"data": [{"start_time": "...", "heart_rate": 52, "type": "resting"}, ...]}
    - {"HeartRate": [{"startDate": "...", "bpm": 52, ...}]}
    - Lista diretta di record
    """
    result: dict[datetime.date, list[int]] = {}

    # Normalizza a lista di record
    records = []
    if isinstance(data, list):
        records = data
    elif isinstance(data, dict):
        # Prova varie chiavi comuni
        for key in ("data", "HeartRate", "heart_rate_data", "records", "items"):
            if key in data and isinstance(data[key], list):
                records = data[key]
                break
        if not records:
            # Formato piatto con chiavi dirette
            records = [data]

    for rec in records:
        if not isinstance(rec, dict):
            continue

        # Estrai timestamp
        ts_str = (
            rec.get("start_time") or rec.get("startDate") or
            rec.get("date") or rec.get("timestamp") or rec.get("day_time") or
            rec.get("Data") or rec.get("data")
        )
        if not ts_str:
            continue

        # Parsifica data
        try:
            # Gestisce formato con punti: "2026.06.17 00:00:00" -> "2026-06-17"
            clean_ts = str(ts_str).replace(".", "-")
            if "T" in clean_ts:
                dt = datetime.datetime.fromisoformat(clean_ts.replace("Z", "+00:00"))
                day = dt.date()
            else:
                day = datetime.date.fromisoformat(clean_ts[:10])
        except (ValueError, TypeError):
            continue

        # Estrai FC (diverse chiavi incluse quelle italiane)
        hr_val = (
            rec.get("heart_rate") or rec.get("bpm") or
            rec.get("value") or rec.get("heartRate") or
            rec.get("heart_rate_value") or rec.get("Frequenza cardiaca") or
            rec.get("frequenza_cardiaca")
        )

        if hr_val is None:
            continue

        try:
            hr_int = int(float(str(hr_val)))
        except (ValueError, TypeError):
            continue

        # Filtra valori fisiologicamente plausibili
        if not (30 <= hr_int <= 220):
            continue

        # Preferisci dati "resting" se disponibili; altrimenti accumula tutti
        rec_type = str(rec.get("type", "") or rec.get("heartRateType", "") or rec.get("Origine", "")).lower()
        if "resting" in rec_type or "rest" in rec_type or "riposo" in rec_type:
            # Sovrascrivi direttamente con valore resting
            result[day] = [hr_int]
        else:
            result.setdefault(day, []).append(hr_int)

    # Calcola la FC minima del giorno come proxy della FC a riposo
    return {day: min(vals) for day, vals in result.items() if vals}


def _parse_heart_rate_intraday(data: dict | list) -> dict[datetime.datetime, int]:
    """
    Parsifica un file JSON/CSV di Samsung Health e restituisce i valori puntuali intraday:
    {timestamp_datetime: bpm}
    """
    result: dict[datetime.datetime, int] = {}
    records = []
    if isinstance(data, list):
        records = data
    elif isinstance(data, dict):
        for key in ("data", "HeartRate", "heart_rate_data", "records", "items"):
            if key in data and isinstance(data[key], list):
                records = data[key]
                break
        if not records:
            records = [data]

    for rec in records:
        if not isinstance(rec, dict):
            continue

        ts_str = (
            rec.get("start_time") or rec.get("startDate") or
            rec.get("date") or rec.get("timestamp") or rec.get("day_time") or
            rec.get("Data") or rec.get("data")
        )
        if not ts_str:
            continue

        try:
            clean_ts = str(ts_str).replace(".", "-")
            # Prova a parsificare con orario completo
            if " " in clean_ts:
                dt = datetime.datetime.strptime(clean_ts[:19], "%Y-%m-%d %H:%M:%S")
            else:
                dt = datetime.datetime.fromisoformat(clean_ts.replace("Z", "+00:00"))
                if dt.tzinfo:
                    dt = dt.replace(tzinfo=None)
        except (ValueError, TypeError):
            continue

        hr_val = (
            rec.get("heart_rate") or rec.get("bpm") or
            rec.get("value") or rec.get("heartRate") or
            rec.get("heart_rate_value") or rec.get("Frequenza cardiaca") or
            rec.get("frequenza_cardiaca")
        )
        if hr_val is None:
            continue

        try:
            hr_int = int(float(str(hr_val)))
            if 30 <= hr_int <= 220:
                result[dt] = hr_int
        except (ValueError, TypeError):
            continue

    return result


def _parse_sleep(data: dict | list) -> dict[datetime.date, float]:
    """
    Parsifica un file JSON di Samsung Health con dati del sonno.
    Restituisce: {data: ore_di_sonno}

    Supporta:
    - {"data": [{"start_time": "...", "end_time": "...", ...}]}
    - {"sleep": [{"startDate": "...", "endDate": "...", "duration": 27540}]}
    - Record con durata già in minuti/secondi/ore
    """
    result: dict[datetime.date, float] = {}

    records = []
    if isinstance(data, list):
        records = data
    elif isinstance(data, dict):
        for key in ("data", "sleep", "SleepStage", "sleep_data", "records", "items"):
            if key in data and isinstance(data[key], list):
                records = data[key]
                break
        if not records:
            records = [data]

    for rec in records:
        if not isinstance(rec, dict):
            continue

        # Metodo 1: calcola da start/end time
        start_str = rec.get("start_time") or rec.get("startDate") or rec.get("start")
        end_str = rec.get("end_time") or rec.get("endDate") or rec.get("end")

        sleep_hours = None
        ref_date = None

        if start_str and end_str:
            try:
                clean_start = str(start_str).replace(".", "-")
                clean_end = str(end_str).replace(".", "-")
                if "T" in clean_start:
                    t_start = datetime.datetime.fromisoformat(clean_start.replace("Z", "+00:00"))
                    t_end = datetime.datetime.fromisoformat(clean_end.replace("Z", "+00:00"))
                    if t_start.tzinfo:
                        t_start = t_start.replace(tzinfo=None)
                    if t_end.tzinfo:
                        t_end = t_end.replace(tzinfo=None)
                    duration_hours = (t_end - t_start).total_seconds() / 3600.0
                    ref_date = t_end.date()
                    sleep_hours = duration_hours
            except (ValueError, TypeError):
                pass

        # Metodo 2: durata pre-calcolata o colonna "Durata in secondi" / "Durata"
        if sleep_hours is None:
            duration_raw = (
                rec.get("Durata in secondi") or rec.get("durata_in_secondi") or
                rec.get("duration") or rec.get("totalSleepTime") or rec.get("sleep_duration") or
                rec.get("durata") or rec.get("Durata")
            )
            date_str = (
                rec.get("date") or rec.get("day_time") or rec.get("timestamp") or
                rec.get("Data") or rec.get("data")
            )

            if duration_raw is not None:
                try:
                    dur_val = float(str(duration_raw))
                    
                    # Se la colonna indica esplicitamente "in secondi", dividi sempre per 3600
                    has_seconds_header = any(k in rec for k in ["Durata in secondi", "durata_in_secondi"])
                    
                    if has_seconds_header:
                        # Se è la fase "awake", non la contiamo come sonno effettivo
                        fase = str(rec.get("Fase del sonno") or rec.get("fase_del_sonno") or "").lower()
                        if "awake" in fase or "sveglio" in fase:
                            sleep_hours = 0.0
                        else:
                            sleep_hours = dur_val / 3600.0
                    else:
                        # Heuristica per altri formati
                        if dur_val < 24:
                            sleep_hours = dur_val
                        elif dur_val < 1440:
                            sleep_hours = dur_val / 60.0
                        else:
                            sleep_hours = dur_val / 3600.0
                except (ValueError, TypeError):
                    pass

                if date_str:
                    try:
                        clean_date = str(date_str).replace(".", "-")
                        ref_date = datetime.date.fromisoformat(clean_date[:10])
                    except (ValueError, TypeError):
                        pass

        if sleep_hours is None or ref_date is None:
            continue

        # In caso di record dettagliati per fase (es. sonno leggero, REM), sommiamo la durata totale per giorno
        result[ref_date] = result.get(ref_date, 0.0) + sleep_hours

    # Arrotonda a due cifre decimali e limita a un range realistico (es. max 16 ore)
    return {day: min(16.0, round(hours, 2)) for day, hours in result.items() if hours >= 1.0}


def _parse_sleep_stages_intraday(data: dict | list) -> list[dict]:
    """
    Parsifica un file JSON/CSV di Samsung Health e restituisce i dettagli delle singole fasi del sonno:
    [{'timestamp': datetime, 'duration_seconds': int, 'stage': stage_str}]
    """
    result = []
    records = []
    if isinstance(data, list):
        records = data
    elif isinstance(data, dict):
        for key in ("data", "sleep", "SleepStage", "sleep_data", "records", "items"):
            if key in data and isinstance(data[key], list):
                records = data[key]
                break
        if not records:
            records = [data]

    for rec in records:
        if not isinstance(rec, dict):
            continue

        date_str = (
            rec.get("date") or rec.get("day_time") or rec.get("timestamp") or
            rec.get("Data") or rec.get("data") or rec.get("start_time") or rec.get("startDate")
        )
        duration_raw = (
            rec.get("Durata in secondi") or rec.get("durata_in_secondi") or
            rec.get("duration") or rec.get("totalSleepTime") or rec.get("sleep_duration") or
            rec.get("durata") or rec.get("Durata")
        )
        stage_raw = (
            rec.get("Fase del sonno") or rec.get("fase_del_sonno") or
            rec.get("stage") or rec.get("sleep_stage") or rec.get("stage_name")
        )

        if not date_str or duration_raw is None or not stage_raw:
            continue

        try:
            clean_date = str(date_str).replace(".", "-")
            if " " in clean_date:
                dt = datetime.datetime.strptime(clean_date[:19], "%Y-%m-%d %H:%M:%S")
            else:
                dt = datetime.datetime.fromisoformat(clean_date.replace("Z", "+00:00"))
                if dt.tzinfo:
                    dt = dt.replace(tzinfo=None)
            
            dur_val = int(float(str(duration_raw)))
            # Heuristica per convertire in secondi se espresso in ore/minuti
            has_seconds_header = any(k in rec for k in ["Durata in secondi", "durata_in_secondi"])
            if not has_seconds_header:
                if dur_val < 24: # Ore
                    dur_val = int(dur_val * 3600)
                elif dur_val < 1440: # Minuti
                    dur_val = int(dur_val * 60)
            
            stage_str = str(stage_raw).strip().lower()
            
            result.append({
                "timestamp": dt,
                "duration_seconds": dur_val,
                "stage": stage_str
            })
        except (ValueError, TypeError):
            continue

    return result


def _parse_steps(data: dict | list) -> dict[datetime.date, int]:
    """
    Parsifica un file JSON di Samsung Health con i passi giornalieri.
    Restituisce: {data: passi}

    Supporta:
    - {"data": [{"day_time": "2026-06-17", "count": 8423}]}
    - {"steps": [{"date": "2026-06-17", "value": 8423}]}
    - Record con timestamp e count incrementale
    """
    result: dict[datetime.date, int] = {}

    records = []
    if isinstance(data, list):
        records = data
    elif isinstance(data, dict):
        for key in ("data", "steps", "step_count", "StepCount", "records", "items"):
            if key in data and isinstance(data[key], list):
                records = data[key]
                break
        if not records:
            records = [data]

    for rec in records:
        if not isinstance(rec, dict):
            continue

        # Estrai data
        date_str = (
            rec.get("day_time") or rec.get("date") or rec.get("timestamp") or
            rec.get("start_time") or rec.get("startDate") or
            rec.get("Data") or rec.get("data")
        )
        if not date_str:
            continue

        try:
            clean_date = str(date_str).replace(".", "-")
            day = datetime.date.fromisoformat(clean_date[:10])
        except (ValueError, TypeError):
            continue

        # Estrai passi
        steps_val = (
            rec.get("count") or rec.get("step_count") or rec.get("value") or
            rec.get("steps") or rec.get("stepCount") or rec.get("Passi") or
            rec.get("passi")
        )
        if steps_val is None:
            continue

        try:
            steps_int = int(float(str(steps_val)))
        except (ValueError, TypeError):
            continue

        if steps_int < 0:
            continue

        # Accumula se ci sono più record per lo stesso giorno
        result[day] = result.get(day, 0) + steps_int

    return result


# ─── CSV Fallback Parser ────────────────────────────────────────────────────────

def _try_parse_csv(content: bytes, data_type: str) -> dict[datetime.date, any]:
    """
    Fallback: prova a parsificare un file CSV con i dati Samsung Health.
    Usato quando il file non è JSON o il JSON non è riconosciuto.
    """
    try:
        import csv
        text = content.decode("utf-8", errors="replace")
        reader = csv.DictReader(io.StringIO(text))
        rows = list(reader)
        if not rows:
            return {}

        # Converti CSV in lista di dict e riusa i parser JSON
        if data_type == "heart_rate":
            return _parse_heart_rate(rows)
        elif data_type == "sleep":
            return _parse_sleep(rows)
        elif data_type == "steps":
            return _parse_steps(rows)
    except Exception as e:
        logger.debug(f"[SamsungSync] CSV parsing fallito: {e}")
    return {}


def _try_parse_csv_intraday(content: bytes, data_type: str) -> any:
    """
    Fallback intraday per CSV.
    """
    try:
        import csv
        text = content.decode("utf-8", errors="replace")
        reader = csv.DictReader(io.StringIO(text))
        rows = list(reader)
        if not rows:
            return {} if data_type == "heart_rate" else []

        if data_type == "heart_rate":
            return _parse_heart_rate_intraday(rows)
        elif data_type == "sleep":
            return _parse_sleep_stages_intraday(rows)
    except Exception as e:
        logger.debug(f"[SamsungSync] CSV intraday parsing fallito: {e}")
    return {} if data_type == "heart_rate" else []


# ─── Database Upsert ────────────────────────────────────────────────────────────

def _upsert_intraday_data(db_session, hr_intraday: dict, sleep_stages: list) -> int:
    """
    Salva i dati intraday ad alta risoluzione nel database.
    """
    from database.database import HeartRateIntraday, SleepStageIntraday
    upserted = 0

    # 1. Salva la frequenza cardiaca intraday
    if hr_intraday:
        # Trova timestamp esistenti per evitare inserimenti duplicati
        min_ts = min(hr_intraday.keys())
        max_ts = max(hr_intraday.keys())
        existing_ts = {
            r[0] for r in db_session.query(HeartRateIntraday.timestamp)
            .filter(HeartRateIntraday.timestamp >= min_ts, HeartRateIntraday.timestamp <= max_ts)
            .all()
        }

        for ts, bpm in hr_intraday.items():
            if ts not in existing_ts:
                record = HeartRateIntraday(timestamp=ts, bpm=bpm)
                db_session.add(record)
                upserted += 1

    # 2. Salva le fasi del sonno intraday
    if sleep_stages:
        min_ts = min(s["timestamp"] for s in sleep_stages)
        max_ts = max(s["timestamp"] for s in sleep_stages)
        existing_ts = {
            r[0] for r in db_session.query(SleepStageIntraday.timestamp)
            .filter(SleepStageIntraday.timestamp >= min_ts, SleepStageIntraday.timestamp <= max_ts)
            .all()
        }

        for stage_data in sleep_stages:
            ts = stage_data["timestamp"]
            if ts not in existing_ts:
                record = SleepStageIntraday(
                    timestamp=ts,
                    duration_seconds=stage_data["duration_seconds"],
                    stage=stage_data["stage"]
                )
                db_session.add(record)
                upserted += 1

    if upserted > 0:
        try:
            db_session.commit()
            logger.info(f"[SamsungSync] Salvati {upserted} record intraday nel DB.")
        except Exception as e:
            logger.error(f"[SamsungSync] Errore salvataggio dati intraday: {e}")
            db_session.rollback()

    return upserted


def _upsert_daily_metrics(db_session, parsed: dict[str, dict[datetime.date, any]]) -> int:
    """
    Inserisce o aggiorna le righe nella tabella DailyMetric del database.

    parsed = {
        "heart_rate": {date: resting_hr},
        "sleep":      {date: sleep_hours},
        "steps":      {date: steps},
    }

    Restituisce il numero di righe inserite/aggiornate.
    """
    from database.database import DailyMetric

    # Merge tutte le date disponibili
    all_dates: set[datetime.date] = set()
    for day_map in parsed.values():
        all_dates.update(day_map.keys())

    upserted = 0

    for day in sorted(all_dates):
        try:
            metric = db_session.query(DailyMetric).filter(DailyMetric.date == day).first()
            if not metric:
                metric = DailyMetric(date=day)
                db_session.add(metric)

            changed = False

            if "heart_rate" in parsed and day in parsed["heart_rate"]:
                new_rhr = parsed["heart_rate"][day]
                if metric.resting_hr != new_rhr:
                    metric.resting_hr = new_rhr
                    changed = True

            if "sleep" in parsed and day in parsed["sleep"]:
                new_sleep = parsed["sleep"][day]
                if metric.sleep_hours != new_sleep:
                    metric.sleep_hours = new_sleep
                    changed = True

            if "steps" in parsed and day in parsed["steps"]:
                new_steps = parsed["steps"][day]
                if metric.steps != new_steps:
                    metric.steps = new_steps
                    changed = True

            # Ricalcola readiness se abbiamo dati sufficienti usando baseline adattive basate su media 30d
            if changed and metric.sleep_hours and metric.resting_hr:
                from sqlalchemy import func
                
                # Calcola le medie mobili degli ultimi 30 giorni fino alla data corrente
                rhr_30d = db_session.query(func.avg(DailyMetric.resting_hr)).filter(
                    DailyMetric.resting_hr.isnot(None),
                    DailyMetric.date < day,
                    DailyMetric.date >= day - datetime.timedelta(days=30)
                ).scalar()
                
                hrv_30d = db_session.query(func.avg(DailyMetric.hrv_score)).filter(
                    DailyMetric.hrv_score.isnot(None),
                    DailyMetric.date < day,
                    DailyMetric.date >= day - datetime.timedelta(days=30)
                ).scalar()

                sleep_30d = db_session.query(func.avg(DailyMetric.sleep_hours)).filter(
                    DailyMetric.sleep_hours.isnot(None),
                    DailyMetric.date < day,
                    DailyMetric.date >= day - datetime.timedelta(days=30)
                ).scalar()

                rhr_base = float(rhr_30d) if rhr_30d else 55.0
                hrv_base = float(hrv_30d) if hrv_30d else 65.0
                sleep_base = float(sleep_30d) if sleep_30d else 8.0

                # Punteggio del sonno basato sulle ore di sonno rapportate alla baseline dell'atleta
                sleep_score = min(100.0, (metric.sleep_hours / sleep_base) * 100.0)
                
                # Punteggio HRV basato sullo scostamento dalla baseline (10% deviazione)
                hrv_curr = metric.hrv_score or hrv_base
                hrv_score = min(100.0, max(0.0, 80.0 + (hrv_curr - hrv_base) * 2.0))
                
                # Punteggio RHR (più battiti sopra la baseline = punteggio inferiore)
                rhr_score = min(100.0, max(0.0, 80.0 - (metric.resting_hr - rhr_base) * 3.0))
                
                metric.readiness = round((sleep_score * 0.3) + (hrv_score * 0.4) + (rhr_score * 0.3), 1)

            if changed:
                upserted += 1

        except Exception as e:
            logger.error(f"[SamsungSync] Errore upsert per data {day}: {e}")
            db_session.rollback()
            continue

    try:
        db_session.commit()
    except Exception as e:
        logger.error(f"[SamsungSync] Errore commit batch: {e}")
        db_session.rollback()

    return upserted


# ─── Main Sync Entry Point ─────────────────────────────────────────────────────

def _get_folder_ids_by_type() -> dict[str, list[str]]:
    """
    Costruisce la mappa {data_type: [folder_id, ...]} leggendo il .env.

    Priorità:
      1. Variabili per tipo (SAMSUNG_HR_FOLDER_ID, SAMSUNG_STEPS_FOLDER_ID, SAMSUNG_SLEEP_FOLDER_ID)
      2. Variabile generica SAMSUNG_DRIVE_FOLDER_ID (applicata a tutti i tipi)
    """
    fallback = os.getenv("SAMSUNG_DRIVE_FOLDER_ID", "").strip()
    result: dict[str, list[str]] = {}

    for data_type, env_var in FOLDER_ENV_BY_TYPE.items():
        specific = os.getenv(env_var, "").strip()
        if specific:
            result[data_type] = [specific]
        elif fallback:
            result[data_type] = [fallback]

    return result


def _process_folder(service, folder_id: str, forced_type: Optional[str],
                    since_date: datetime.date, report: "SyncReport",
                    aggregated: dict, intraday_hr: dict, intraday_sleep: list) -> None:
    """
    Scarica e parsifica tutti i file in una singola cartella Drive.
    Se forced_type è impostato, tutti i file vengono trattati come quel tipo
    (utile per cartelle dedicate es. "Health Sync Passi").
    """
    files = _list_drive_files(service, folder_id, since_date=since_date)
    report.files_found += len(files)
    logger.info(f"[SamsungSync] Cartella {folder_id}: {len(files)} file trovati.")

    for file_info in files:
        filename = file_info.get("name", "")
        file_id  = file_info.get("id", "")

        # Tipo: forzato dalla cartella dedicata oppure rilevato dal nome file
        data_type = forced_type or _detect_data_type(filename)
        if data_type is None:
            logger.debug(f"[SamsungSync] File ignorato (tipo non riconosciuto): {filename}")
            continue

        logger.info(f"[SamsungSync] Scarico {filename} (tipo={data_type})...")
        content = _download_file_content(service, file_id)
        if content is None:
            report.errors.append(f"Download fallito: {filename}")
            continue

        # Parsing JSON → fallback CSV
        parsed_day_map: dict = {}
        parsed_hr_intra = {}
        parsed_sleep_intra = []
        try:
            raw = json.loads(content.decode("utf-8", errors="replace"))
            if data_type == "heart_rate":
                parsed_day_map = _parse_heart_rate(raw)
                parsed_hr_intra = _parse_heart_rate_intraday(raw)
            elif data_type == "sleep":
                parsed_day_map = _parse_sleep(raw)
                parsed_sleep_intra = _parse_sleep_stages_intraday(raw)
            elif data_type == "steps":
                parsed_day_map = _parse_steps(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            logger.debug(f"[SamsungSync] {filename} non è JSON valido, provo CSV...")
            parsed_day_map = _try_parse_csv(content, data_type)
            if data_type == "heart_rate":
                parsed_hr_intra = _try_parse_csv_intraday(content, "heart_rate")
            elif data_type == "sleep":
                parsed_sleep_intra = _try_parse_csv_intraday(content, "sleep")
        except Exception as e:
            report.errors.append(f"Errore parsing {filename}: {e}")
            logger.error(f"[SamsungSync] Errore parsing {filename}: {e}")
            continue

        if not parsed_day_map:
            logger.debug(f"[SamsungSync] Nessun dato estratto da {filename}.")
            continue

        # Merge nel dizionario aggregato
        for day, value in parsed_day_map.items():
            if data_type == "heart_rate":
                if day not in aggregated["heart_rate"] or value < aggregated["heart_rate"][day]:
                    aggregated["heart_rate"][day] = value
            elif data_type == "sleep":
                if day not in aggregated["sleep"] or value > aggregated["sleep"][day]:
                    aggregated["sleep"][day] = value
            elif data_type == "steps":
                if day not in aggregated["steps"] or value > aggregated["steps"][day]:
                    aggregated["steps"][day] = value

        # Merge intraday
        if data_type == "heart_rate" and parsed_hr_intra:
            intraday_hr.update(parsed_hr_intra)
        elif data_type == "sleep" and parsed_sleep_intra:
            intraday_sleep.extend(parsed_sleep_intra)

        report.files_processed += 1
        logger.info(f"[SamsungSync] ✓ {filename}: {len(parsed_day_map)} giorni estratti.")


def run_sync(db_session, days_lookback: int = 30) -> SyncReport:
    """
    Entry point principale del sync Samsung Health → Drive → DB.

    Supporta:
    - Una cartella unica (SAMSUNG_DRIVE_FOLDER_ID)
    - Tre cartelle separate per tipo:
        SAMSUNG_HR_FOLDER_ID    → Health Sync Freq. cardiaca
        SAMSUNG_STEPS_FOLDER_ID → Health Sync Passi
        SAMSUNG_SLEEP_FOLDER_ID → Health Sync Sonno

    Args:
        db_session: SQLAlchemy session già aperta
        days_lookback: quanti giorni indietro considerare (default 30)

    Returns:
        SyncReport con il risultato dell'operazione
    """
    report = SyncReport()

    folder_map = _get_folder_ids_by_type()
    if not folder_map:
        msg = (
            "[SamsungSync] Nessun folder ID configurato nel .env. "
            "Imposta SAMSUNG_DRIVE_FOLDER_ID oppure le variabili per tipo "
            "(SAMSUNG_HR_FOLDER_ID, SAMSUNG_STEPS_FOLDER_ID, SAMSUNG_SLEEP_FOLDER_ID)."
        )
        logger.warning(msg)
        report.errors.append(msg)
        report.finished_at = datetime.datetime.now()
        return report

    service = _get_drive_service()
    if service is None:
        msg = "[SamsungSync] Impossibile connettersi a Google Drive. Controlla credentials.json e token.json."
        logger.error(msg)
        report.errors.append(msg)
        report.finished_at = datetime.datetime.now()
        return report

    since_date = datetime.date.today() - datetime.timedelta(days=days_lookback)

    aggregated: dict[str, dict[datetime.date, any]] = {
        "heart_rate": {},
        "sleep": {},
        "steps": {},
    }
    intraday_hr: dict[datetime.datetime, int] = {}
    intraday_sleep: list[dict] = []

    # Cartelle già visitate (evita duplicati se lo stesso folder_id è usato per più tipi)
    visited: set[tuple[str, str]] = set()

    for data_type, folder_ids in folder_map.items():
        for fid in folder_ids:
            key = (fid, data_type)
            if key in visited:
                continue
            visited.add(key)

            # Se il folder_id è dedicato a un tipo specifico, forzalo;
            # altrimenti usa il rilevamento automatico dal nome file.
            is_dedicated = os.getenv(FOLDER_ENV_BY_TYPE.get(data_type, ""), "").strip() == fid
            forced = data_type if is_dedicated else None

            _process_folder(service, fid, forced, since_date, report, aggregated, intraday_hr, intraday_sleep)

    # Scrivi nel database
    if any(aggregated.values()):
        upserted = _upsert_daily_metrics(db_session, aggregated)
        report.records_upserted = upserted
        logger.info(f"[SamsungSync] {upserted} righe DailyMetric inserite/aggiornate nel DB.")
        
        # Scrivi dati intraday
        _upsert_intraday_data(db_session, intraday_hr, intraday_sleep)
    else:
        logger.info("[SamsungSync] Nessun dato nuovo da scrivere nel DB.")

    report.finished_at = datetime.datetime.now()
    logger.info(f"[SamsungSync] {report.summary()}")
    return report


# ─── CLI Entry Point ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    import logging

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s"
    )

    if "--auth" in sys.argv:
        print("Avvio flusso di autenticazione OAuth2 con Google Drive...")
        _run_first_auth()

    elif "--test" in sys.argv:
        from dotenv import load_dotenv
        load_dotenv(override=True)
        print("Test connessione Google Drive...")
        service = _get_drive_service()
        if service:
            folder_map = _get_folder_ids_by_type()
            if folder_map:
                for dtype, fids in folder_map.items():
                    for fid in fids:
                        files = _list_drive_files(service, fid)
                        print(f"\n✅ [{dtype}] Cartella {fid}: {len(files)} file trovati")
                        for f in files[:5]:
                            print(f"     - {f['name']} ({f.get('size', '?')} bytes)")
            else:
                print("⚠️  Nessun folder ID configurato nel .env")
        else:
            print("❌ Connessione fallita.")

    elif "--sync" in sys.argv:
        print("Avvio sync manuale Samsung Health...")
        # Carica .env
        from dotenv import load_dotenv
        load_dotenv(override=True)

        import sys
        sys.path.insert(0, str(Path(__file__).parent.parent))

        from database.database import SessionLocal, init_db
        init_db()
        db = SessionLocal()
        try:
            report = run_sync(db, days_lookback=90)
            print(report.summary())
            if report.errors:
                print("Errori:")
                for err in report.errors:
                    print(f"  - {err}")
        finally:
            db.close()

    else:
        print(__doc__)
        print("\nUso:")
        print("  python sync/samsung_health_sync.py --auth   # Prima autenticazione (una volta sola)")
        print("  python sync/samsung_health_sync.py --test   # Test connessione e lista file per cartella")
        print("  python sync/samsung_health_sync.py --sync   # Sync manuale (90 giorni)")
        print("\nVariabili .env supportate:")
        print("  SAMSUNG_DRIVE_FOLDER_ID      # Cartella unica con tutti i file")
        print("  SAMSUNG_HR_FOLDER_ID         # Cartella dedicata FC (Health Sync Freq. cardiaca)")
        print("  SAMSUNG_STEPS_FOLDER_ID      # Cartella dedicata Passi (Health Sync Passi)")
        print("  SAMSUNG_SLEEP_FOLDER_ID      # Cartella dedicata Sonno (Health Sync Sonno)")
