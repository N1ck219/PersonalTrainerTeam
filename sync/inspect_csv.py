import os
import sys
import json
from pathlib import Path
from dotenv import load_dotenv

_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_ROOT))

from sync.samsung_health_sync import _get_drive_service, _get_folder_ids_by_type, _list_drive_files, _download_file_content

def inspect_csvs():
    load_dotenv(override=True)
    service = _get_drive_service()
    if not service:
        print("Errore Drive connection")
        return
        
    folder_map = _get_folder_ids_by_type()
    
    for dtype, fids in folder_map.items():
        if dtype != "sleep":
            continue
        fid = fids[0]
        print(f"\n--- Ispezione tipo: {dtype} (Folder: {fid}) ---")
        files = _list_drive_files(service, fid)
        if not files:
            print("Nessun file trovato.")
            continue
            
        for target in files:
            if "23:59:00" in target['name']:
                print(f"File: {target['name']}")
                content = _download_file_content(service, target['id'])
                if not content:
                    print("Impossibile scaricare.")
                    continue
                    
                text = content.decode("utf-8", errors="replace")
                lines = text.splitlines()
                for i, line in enumerate(lines[:20]):
                    print(f"Line {i+1}: {line}")

if __name__ == "__main__":
    inspect_csvs()
