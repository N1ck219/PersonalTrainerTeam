import os
import sys
import json
import datetime
import webbrowser
from pathlib import Path
from dotenv import load_dotenv

# Aggiungi root al path per importare samsung_health_sync
_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_ROOT))

from sync.samsung_health_sync import (
    _get_drive_service,
    _get_folder_ids_by_type,
    _list_drive_files,
    _download_file_content,
    _detect_data_type,
    _parse_heart_rate,
    _parse_sleep,
    _parse_steps,
    _try_parse_csv,
    FOLDER_ENV_BY_TYPE
)

def test_and_plot():
    load_dotenv(override=True)
    
    service = _get_drive_service()
    if not service:
        print("[-] Impossibile connettersi a Google Drive. Verifica token.json/credentials.json.")
        return

    folder_map = _get_folder_ids_by_type()
    if not folder_map:
        print("[!] Nessun folder ID configurato nel .env.")
        return

    # Usiamo lookback lungo per caricare quanti più dati possibile
    since_date = datetime.date.today() - datetime.timedelta(days=180)
    
    aggregated = {
        "heart_rate": {},
        "sleep": {},
        "steps": {},
    }

    visited = set()
    for data_type, folder_ids in folder_map.items():
        for fid in folder_ids:
            key = (fid, data_type)
            if key in visited:
                continue
            visited.add(key)

            is_dedicated = os.getenv(FOLDER_ENV_BY_TYPE.get(data_type, ""), "").strip() == fid
            forced = data_type if is_dedicated else None

            print(f"\n[?] Lettura cartella per '{data_type}' (ID: {fid})...")
            files = _list_drive_files(service, fid, since_date=since_date)
            print(f"Trovati {len(files)} file modificati dal {since_date}.")

            for file_info in files:
                filename = file_info.get("name", "")
                file_id = file_info.get("id", "")
                
                dtype = forced or _detect_data_type(filename)
                if not dtype:
                    continue

                print(f"  [+] Download e parsing di: {filename}...")
                content = _download_file_content(service, file_id)
                if content is None:
                    print(f"  [-] Download fallito per {filename}")
                    continue

                parsed_day_map = {}
                try:
                    # Prima tenta JSON, poi CSV
                    raw = json.loads(content.decode("utf-8", errors="replace"))
                    if dtype == "heart_rate":
                        parsed_day_map = _parse_heart_rate(raw)
                    elif dtype == "sleep":
                        parsed_day_map = _parse_sleep(raw)
                    elif dtype == "steps":
                        parsed_day_map = _parse_steps(raw)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    parsed_day_map = _try_parse_csv(content, dtype)
                except Exception as e:
                    print(f"  [-] Errore parsing {filename}: {e}")
                    continue

                # Unisci i dati
                for day, val in parsed_day_map.items():
                    if dtype == "heart_rate":
                        if day not in aggregated["heart_rate"] or val < aggregated["heart_rate"][day]:
                            aggregated["heart_rate"][day] = val
                    elif dtype == "sleep":
                        if day not in aggregated["sleep"] or val > aggregated["sleep"][day]:
                            aggregated["sleep"][day] = val
                    elif dtype == "steps":
                        if day not in aggregated["steps"] or val > aggregated["steps"][day]:
                            aggregated["steps"][day] = val

    print("\n--- Statistiche dei Dati Letti ---")
    for k, v in aggregated.items():
        print(f"Metric '{k}': {len(v)} giorni estratti.")
        if v:
            sorted_days = sorted(v.keys())
            print(f"  Range date: {sorted_days[0]} -> {sorted_days[-1]}")
            values = list(v.values())
            print(f"  Valori (min/max/media): {min(values)} / {max(values)} / {sum(values)/len(values):.2f}")

    generate_html_report(aggregated)

def generate_html_report(aggregated):
    # Raccogliamo tutte le date uniche e le ordiniamo
    all_dates = sorted(list(set(
        list(aggregated["heart_rate"].keys()) +
        list(aggregated["sleep"].keys()) +
        list(aggregated["steps"].keys())
    )))
    
    # Preparazione liste per il javascript
    labels = [day.strftime("%Y-%m-%d") for day in all_dates]
    hr_values = [aggregated["heart_rate"].get(day, None) for day in all_dates]
    sleep_values = [aggregated["sleep"].get(day, None) for day in all_dates]
    steps_values = [aggregated["steps"].get(day, None) for day in all_dates]

    html_content = f"""<!DOCTYPE html>
<html>
<head>
    <title>Preview Dati Samsung Health</title>
    <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
    <style>
        body {{
            font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
            background-color: #121212;
            color: #e0e0e0;
            margin: 20px;
        }}
        .container {{
            max-width: 1000px;
            margin: auto;
        }}
        h1 {{
            text-align: center;
            color: #ffffff;
        }}
        .card {{
            background: #1e1e1e;
            padding: 20px;
            border-radius: 8px;
            margin-bottom: 20px;
            box-shadow: 0 4px 6px rgba(0,0,0,0.3);
        }}
        .summary {{
            display: flex;
            justify-content: space-around;
            margin-bottom: 20px;
        }}
        .metric-box {{
            text-align: center;
            padding: 15px;
            background: #2d2d2d;
            border-radius: 6px;
            width: 30%;
        }}
        .value {{
            font-size: 24px;
            font-weight: bold;
            color: #00adb5;
            margin-top: 5px;
        }}
    </style>
</head>
<body>
    <div class="container">
        <h1>📊 Samsung Health Data Preview</h1>
        <div class="summary">
            <div class="metric-box">
                <div>Giorni con FC a Riposo</div>
                <div class="value">{len([v for v in hr_values if v is not None])}</div>
            </div>
            <div class="metric-box">
                <div>Giorni con Passi</div>
                <div class="value">{len([v for v in steps_values if v is not None])}</div>
            </div>
            <div class="metric-box">
                <div>Giorni con Sonno</div>
                <div class="value">{len([v for v in sleep_values if v is not None])}</div>
            </div>
        </div>

        <div class="card">
            <canvas id="hrChart"></canvas>
        </div>
        <div class="card">
            <canvas id="stepsChart"></canvas>
        </div>
        <div class="card">
            <canvas id="sleepChart"></canvas>
        </div>
    </div>

    <script>
        const labels = {json.dumps(labels)};
        
        // Config Chart.js dark mode defaults
        Chart.defaults.color = '#e0e0e0';
        Chart.defaults.borderColor = '#333333';

        // FC Chart
        new Chart(document.getElementById('hrChart'), {{
            type: 'line',
            data: {{
                labels: labels,
                datasets: [{{
                    label: 'Frequenza Cardiaca a Riposo (BPM)',
                    data: {json.dumps(hr_values)},
                    borderColor: '#ff5722',
                    backgroundColor: 'rgba(255, 87, 34, 0.1)',
                    tension: 0.1,
                    spanGaps: true
                }}]
            }},
            options: {{ responsive: true }}
        }});

        // Steps Chart
        new Chart(document.getElementById('stepsChart'), {{
            type: 'bar',
            data: {{
                labels: labels,
                datasets: [{{
                    label: 'Passi Giornalieri',
                    data: {json.dumps(steps_values)},
                    backgroundColor: '#00adb5',
                    borderRadius: 4
                }}]
            }},
            options: {{ responsive: true }}
        }});

        // Sleep Chart
        new Chart(document.getElementById('sleepChart'), {{
            type: 'line',
            data: {{
                labels: labels,
                datasets: [{{
                    label: 'Ore di Sonno',
                    data: {json.dumps(sleep_values)},
                    borderColor: '#9c27b0',
                    backgroundColor: 'rgba(156, 39, 176, 0.1)',
                    tension: 0.1,
                    spanGaps: true
                }}]
            }},
            options: {{ responsive: true }}
        }});
    </script>
</body>
</html>
"""
    out_dir = Path("C:/Users/nicol/.gemini/antigravity-ide/brain/e11d9c81-e940-4cb9-aa97-f9fa5edfff83")
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "samsung_health_preview.html"
    
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html_content)
        
    print(f"\n[+] Report interattivo generato in:\n{out_path}")
    webbrowser.open(f"file:///{out_path}")

if __name__ == "__main__":
    test_and_plot()
