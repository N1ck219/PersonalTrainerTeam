import os
import datetime
from typing import Dict, Any, List, Optional
import pandas as pd
import numpy as np
from scipy.signal import find_peaks

# Optional imports handled gracefully
try:
    import fitparse
except ImportError:
    fitparse = None

try:
    import gpxpy
except ImportError:
    gpxpy = None


def parse_fit_to_df(file_path: str) -> pd.DataFrame:
    """
    Parses a Garmin .fit file and returns a pandas DataFrame with raw metrics.
    """
    if not fitparse:
        raise ImportError("The 'fitparse' library is required to parse .fit files.")

    fit_file = fitparse.FitFile(file_path)
    records = []

    for record in fit_file.get_messages("record"):
        data = {}
        for record_data in record:
            data[record_data.name] = record_data.value
        records.append(data)

    if not records:
        return pd.DataFrame()

    df = pd.DataFrame(records)
    
    # Standardize column names
    rename_dict = {
        'timestamp': 'timestamp',
        'distance': 'distance_m',
        'enhanced_speed': 'speed_m_s',
        'speed': 'speed_m_s',
        'enhanced_altitude': 'altitude_m',
        'altitude': 'altitude_m',
        'heart_rate': 'heart_rate',
        'cadence': 'cadence'
    }
    
    # Rename matching columns
    columns_to_rename = {k: v for k, v in rename_dict.items() if k in df.columns}
    df = df.rename(columns=columns_to_rename)
    
    # Ensure critical columns exist
    if 'timestamp' not in df.columns:
        df['timestamp'] = pd.date_range(start=datetime.datetime.now(), periods=len(df), freq='s')
    
    df = df.sort_values('timestamp').reset_index(drop=True)
    return df


def parse_gpx_to_df(file_path: str) -> pd.DataFrame:
    """
    Parses a standard .gpx file and returns a pandas DataFrame.
    """
    if not gpxpy:
        raise ImportError("The 'gpxpy' library is required to parse .gpx files.")

    with open(file_path, 'r') as gpx_file:
        gpx = gpxpy.parse(gpx_file)

    records = []
    total_dist = 0.0
    prev_point = None

    for track in gpx.tracks:
        for segment in track.segments:
            for point in segment.points:
                if prev_point is not None:
                    # Calculate distance in meters
                    dist = point.distance_2d(prev_point)
                    total_dist += dist if dist else 0.0
                
                # Check for extensions (cadence, heart rate)
                hr = None
                cadence = None
                if point.extensions:
                    for ext in point.extensions:
                        # Simple extraction of common GPX extension tags
                        # E.g. Garmin TrackpointExtension
                        for child in ext.iter():
                            if 'hr' in child.tag.lower():
                                try:
                                    hr = int(child.text)
                                except (ValueError, TypeError):
                                    pass
                            elif 'cad' in child.tag.lower():
                                try:
                                    cadence = int(child.text)
                                except (ValueError, TypeError):
                                    pass

                records.append({
                    'timestamp': point.time if point.time else datetime.datetime.now(),
                    'latitude': point.latitude,
                    'longitude': point.longitude,
                    'altitude_m': point.elevation,
                    'distance_m': total_dist,
                    'heart_rate': hr,
                    'cadence': cadence
                })
                prev_point = point

    if not records:
        return pd.DataFrame()

    df = pd.DataFrame(records)
    
    # Calculate speed if not present
    if len(df) > 1:
        # Time diff in seconds
        time_diff = pd.Series([r['timestamp'] for r in records]).diff().dt.total_seconds()
        # Distance diff in meters
        dist_diff = df['distance_m'].diff()
        
        df['speed_m_s'] = dist_diff / time_diff
        df['speed_m_s'] = df['speed_m_s'].fillna(0.0).replace([np.inf, -np.inf], 0.0)
    else:
        df['speed_m_s'] = 0.0

    df = df.sort_values('timestamp').reset_index(drop=True)
    return df


def clean_and_smooth_data(df: pd.DataFrame) -> pd.DataFrame:
    """
    Cleans the time-series data and applies rolling averages to smooth out GPS noise.
    """
    if df.empty:
        return df

    # Fill missing cadence/heart rate with forward fill then backward fill
    if 'cadence' in df.columns:
        df['cadence'] = df['cadence'].ffill().bfill().fillna(0)
    else:
        df['cadence'] = 0

    if 'heart_rate' in df.columns:
        df['heart_rate'] = df['heart_rate'].ffill().bfill().fillna(0)
    else:
        df['heart_rate'] = 0

    # Ensure speed_m_s exists and is valid
    if 'speed_m_s' not in df.columns:
        df['speed_m_s'] = 0.0
    df['speed_m_s'] = df['speed_m_s'].clip(lower=0.0)

    # Convert speed (m/s) to pace (seconds per km)
    # Avoid division by zero
    df['pace_sec_km'] = df['speed_m_s'].apply(
        lambda s: 1000.0 / s if s > 0.1 else 3600.0  # Max pace capped at 1h/km for non-movement
    )
    
    # Apply rolling averages for smoothing GPS pace noise
    # Standard GPS polling is 1Hz, 15-second window is typical to filter pace spikes
    df['pace_smooth'] = df['pace_sec_km'].rolling(window=15, min_periods=1).mean()
    df['cadence_smooth'] = df['cadence'].rolling(window=10, min_periods=1).mean()
    
    if 'heart_rate' in df.columns:
        df['heart_rate_smooth'] = df['heart_rate'].rolling(window=10, min_periods=1).mean()

    return df


def detect_laps(df: pd.DataFrame, template: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """
    Segment the GPS run trace automatically into laps.
    Uses scipy.signal.find_peaks on the derivatives of smoothed pace/speed or heart rate
    to find interval transitions, matching them against the DB template if provided.
    """
    if df.empty or len(df) < 30:
        # Fallback for simple short/empty file
        total_dist_km = (df['distance_m'].max() - df['distance_m'].min()) / 1000.0 if not df.empty else 0.0
        avg_pace = df['pace_smooth'].mean() if not df.empty else 0.0
        return [{
            "lap_index": 1,
            "distance_km": total_dist_km,
            "duration_sec": len(df),
            "avg_pace_sec": avg_pace,
            "avg_cadence": df['cadence_smooth'].mean() if not df.empty else 0.0,
            "avg_hr": df['heart_rate_smooth'].mean() if 'heart_rate_smooth' in df.columns else 0.0
        }]

    # Peak detection approach:
    # During interval training, there are large transitions in speed (going fast, resting).
    # The gradient (derivative) of the smoothed speed will show sharp peaks when speed increases/decreases.
    speed = df['speed_m_s'].values
    speed_gradient = np.abs(np.gradient(speed))

    # Use find_peaks to identify transitions
    # Height & distance thresholds prevent detecting tiny micro-adjustments
    # A transition should happen at least 60 seconds apart in intervals
    peaks, _ = find_peaks(speed_gradient, prominence=0.2, distance=60)
    
    # We add start and end points of the file to partition it
    split_indices = [0] + sorted(peaks.tolist()) + [len(df) - 1]
    
    laps = []
    for i in range(len(split_indices) - 1):
        start_idx = split_indices[i]
        end_idx = split_indices[i + 1]
        
        # Calculate statistics for this segment
        lap_df = df.iloc[start_idx:end_idx]
        if len(lap_df) < 5:
            continue
            
        lap_dist = (lap_df['distance_m'].max() - lap_df['distance_m'].min()) / 1000.0
        lap_duration = (lap_df['timestamp'].max() - lap_df['timestamp'].min()).total_seconds()
        
        if lap_duration <= 0:
            lap_duration = len(lap_df)
            
        avg_speed = (lap_dist * 1000) / lap_duration if lap_duration > 0 else 0
        avg_pace_sec = 1000 / avg_speed if avg_speed > 0.1 else 3600.0
        
        laps.append({
            "lap_index": len(laps) + 1,
            "start_time": lap_df['timestamp'].min().isoformat(),
            "end_time": lap_df['timestamp'].max().isoformat(),
            "distance_km": round(lap_dist, 3),
            "duration_sec": int(lap_duration),
            "avg_pace_sec": round(avg_pace_sec, 1),
            "avg_cadence": round(lap_df['cadence_smooth'].mean(), 1),
            "avg_hr": round(lap_df['heart_rate_smooth'].mean(), 1) if 'heart_rate_smooth' in df.columns else 0.0
        })

    # If template is provided, we can map/adjust laps to the template targets (e.g. if target was 5x 1km)
    if template and "expected_intervals" in template:
        # Match detected laps with template requirements
        pass

    return laps


def parse_workout_file(file_path: str, template: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    Orchestrates the parsing of a .fit or .gpx file, cleaning, smoothing,
    and returns a clean, compiled workout summary dict.
    """
    ext = os.path.splitext(file_path)[1].lower()
    
    if ext == '.fit':
        df = parse_fit_to_df(file_path)
    elif ext in ['.gpx', '.xml']:
        df = parse_gpx_to_df(file_path)
    else:
        raise ValueError(f"Unsupported file format: {ext}")

    if df.empty:
        raise ValueError(f"File {file_path} is empty or could not be parsed.")

    df_cleaned = clean_and_smooth_data(df)
    laps = detect_laps(df_cleaned, template)

    # Compute overall metrics
    total_distance = (df_cleaned['distance_m'].max() - df_cleaned['distance_m'].min()) / 1000.0
    
    # Calculate total duration in seconds
    start_time = df_cleaned['timestamp'].min()
    end_time = df_cleaned['timestamp'].max()
    duration_sec = (end_time - start_time).total_seconds()
    
    if duration_sec <= 0:
        duration_sec = len(df_cleaned)

    # Pace in seconds per km
    avg_speed = (total_distance * 1000.0) / duration_sec if duration_sec > 0 else 0
    avg_pace = 1000.0 / avg_speed if avg_speed > 0.1 else 3600.0
    
    avg_cadence = df_cleaned['cadence_smooth'].mean()
    avg_hr = df_cleaned['heart_rate_smooth'].mean() if 'heart_rate_smooth' in df_cleaned.columns else 0.0

    return {
        "date": start_time.date().isoformat(),
        "distance_km": round(total_distance, 2),
        "duration_sec": int(duration_sec),
        "avg_pace": round(avg_pace, 1),  # in seconds per km
        "avg_cadence": round(avg_cadence, 1),
        "avg_hr": round(avg_hr, 1),
        "laps_summary": laps
    }


def generate_mock_workout_data(workout_type: str = "easy") -> Dict[str, Any]:
    """
    Utility helper to generate realistic parsed workout dictionary for mock runs
    to allow local execution without requiring a physical .fit or .gpx file.
    """
    now = datetime.datetime.now()
    
    if workout_type == "easy":
        # 10km run at 5:15 pace (315 sec/km), 168 cadence, Nimbus shoes
        dist = 10.0
        pace = 315.0
        duration = int(dist * pace)
        laps = [
            {
                "lap_index": i + 1,
                "distance_km": 1.0,
                "duration_sec": int(pace),
                "avg_pace_sec": pace,
                "avg_cadence": 168.0,
                "avg_hr": 142.0
            } for i in range(10)
        ]
        avg_hr = 142.0
        avg_cad = 168.0
        shoe = "Asics Gel-Nimbus"
    else:  # interval/quality
        # 8km total, 2k warmup, 4x 1k hard @ 4:20 pace (260s), 1k cooldown, Adizero shoes
        dist = 8.0
        duration = 2400
        laps = [
            {"lap_index": 1, "distance_km": 2.0, "duration_sec": 660, "avg_pace_sec": 330.0, "avg_cadence": 170.0, "avg_hr": 135.0}, # warmup
            {"lap_index": 2, "distance_km": 1.0, "duration_sec": 260, "avg_pace_sec": 260.0, "avg_cadence": 182.0, "avg_hr": 172.0}, # int 1
            {"lap_index": 3, "distance_km": 0.5, "duration_sec": 180, "avg_pace_sec": 360.0, "avg_cadence": 162.0, "avg_hr": 140.0}, # rest
            {"lap_index": 4, "distance_km": 1.0, "duration_sec": 260, "avg_pace_sec": 260.0, "avg_cadence": 181.0, "avg_hr": 175.0}, # int 2
            {"lap_index": 5, "distance_km": 0.5, "duration_sec": 180, "avg_pace_sec": 360.0, "avg_cadence": 160.0, "avg_hr": 141.0}, # rest
            {"lap_index": 6, "distance_km": 1.0, "duration_sec": 260, "avg_pace_sec": 260.0, "avg_cadence": 182.0, "avg_hr": 176.0}, # int 3
            {"lap_index": 7, "distance_km": 1.0, "duration_sec": 340, "avg_pace_sec": 340.0, "avg_cadence": 168.0, "avg_hr": 145.0}, # cooldown
        ]
        avg_hr = 155.0
        avg_cad = 174.0
        pace = sum(l['duration_sec'] for l in laps) / dist
        shoe = "Adidas Adizero"

    return {
        "date": now.date().isoformat(),
        "distance_km": dist,
        "duration_sec": duration,
        "avg_pace": round(pace, 1),
        "avg_cadence": avg_cad,
        "avg_hr": avg_hr,
        "shoe_used": shoe,
        "laps_summary": laps
    }


def parse_gpx_workout(file_path: str, params: dict) -> dict:
    """
    Parses a GPX file and extracts overall workout metrics and detailed segment splits
    based on the provided workout classification and parameter structure.
    """
    if not gpxpy:
        raise ImportError("The 'gpxpy' library is required to parse .gpx files.")

    with open(file_path, 'r', encoding='utf-8') as gpx_file:
        gpx = gpxpy.parse(gpx_file)

    # 1. Extract all track points chronologically
    points = []
    prev_point = None
    total_dist = 0.0

    for track in gpx.tracks:
        for segment in track.segments:
            for point in segment.points:
                # Calculate incremental distance
                if prev_point is not None:
                    dist = point.distance_2d(prev_point)
                    if dist:
                        total_dist += dist

                # Extract extensions (HR, Cadence)
                hr = None
                cadence = None
                if point.extensions:
                    for ext in point.extensions:
                        for child in ext.iter():
                            tag_lower = child.tag.lower()
                            if 'hr' in tag_lower:
                                try:
                                    hr = int(child.text)
                                except (ValueError, TypeError):
                                    pass
                            elif 'cad' in tag_lower:
                                try:
                                    cadence = int(child.text)
                                except (ValueError, TypeError):
                                    pass

                points.append({
                    "time": point.time,
                    "lat": point.latitude,
                    "lon": point.longitude,
                    "ele": point.elevation,
                    "hr": hr,
                    "cadence": cadence,
                    "cum_dist_km": total_dist / 1000.0
                })
                prev_point = point

    if not points:
        raise ValueError("No trackpoints found in the GPX file.")

    start_time = points[0]["time"]

    # Calculate scale factor if device summary distance is available
    total_dist_km = points[-1]["cum_dist_km"]
    device_total_dist_km = None
    try:
        import xml.etree.ElementTree as ET
        tree = ET.parse(file_path)
        root = tree.getroot()
        for elem in root.iter():
            if elem.tag.endswith('distance') and elem.text:
                try:
                    val = float(elem.text)
                    if val > 100.0:
                        device_total_dist_km = val / 1000.0
                        break
                    elif val > 0.0:
                        device_total_dist_km = val
                        break
                except ValueError:
                    pass
    except Exception:
        pass

    scale_factor = 1.0
    if device_total_dist_km and total_dist_km > 0.05:
        raw_scale = device_total_dist_km / total_dist_km
        if 0.9 <= raw_scale <= 1.1:
            scale_factor = raw_scale

    if scale_factor != 1.0:
        for p in points:
            p["cum_dist_km"] = p["cum_dist_km"] * scale_factor
        total_dist_km = points[-1]["cum_dist_km"]

    # 2. Extract active (moving) metrics by filtering out pauses/stops
    moving_time_sec = 0.0
    moving_dist_km = 0.0
    total_hr_weighted = 0.0
    total_hr_time = 0.0
    total_cad_weighted = 0.0
    total_cad_time = 0.0
    
    for i in range(1, len(points)):
        p_prev = points[i-1]
        p_curr = points[i]
        
        dt = (p_curr["time"] - p_prev["time"]).total_seconds()
        if dt <= 0:
            continue
            
        dx = p_curr["cum_dist_km"] - p_prev["cum_dist_km"]
        speed_m_s = (dx * 1000.0) / dt
        
        # Pause criteria
        is_pause = (dt > 10.0) or (speed_m_s < 0.5)
        if is_pause:
            continue
            
        moving_time_sec += dt
        moving_dist_km += dx
        
        if p_curr["hr"] is not None and p_curr["hr"] > 0:
            total_hr_weighted += p_curr["hr"] * dt
            total_hr_time += dt
            
        if p_curr["cadence"] is not None and p_curr["cadence"] > 0:
            total_cad_weighted += p_curr["cadence"] * dt
            total_cad_time += dt
            
    # Fallback if moving data is corrupt
    total_elapsed_sec = (points[-1]["time"] - points[0]["time"]).total_seconds()
    if moving_time_sec < 10.0:
        moving_time_sec = total_elapsed_sec
        moving_dist_km = total_dist_km
        
        hr_values = [p["hr"] for p in points if p["hr"] is not None and p["hr"] > 0]
        avg_hr = sum(hr_values) / len(hr_values) if hr_values else None
        
        cad_values = [p["cadence"] for p in points if p["cadence"] is not None and p["cadence"] > 0]
        avg_cad = sum(cad_values) / len(cad_values) if cad_values else None
    else:
        avg_hr = total_hr_weighted / total_hr_time if total_hr_time > 0 else None
        avg_cad = total_cad_weighted / total_cad_time if total_cad_time > 0 else None

    # Max HR is computed across the whole trace
    hr_values_all = [p["hr"] for p in points if p["hr"] is not None and p["hr"] > 0]
    max_hr = max(hr_values_all) if hr_values_all else None

    # Calculate average pace (seconds per km) based on moving metrics
    if moving_dist_km > 0.05:
        avg_pace = moving_time_sec / moving_dist_km
    else:
        avg_pace = 0.0
        
    # Override globals with the moving/active metrics
    total_dist_km = moving_dist_km
    duration_sec = moving_time_sec

    # Elevation gain/loss
    elevation_gain = 0.0
    elevation_loss = 0.0
    prev_ele = None
    for p in points:
        if p["ele"] is not None:
            if prev_ele is not None:
                diff = p["ele"] - prev_ele
                if diff > 0.5:
                    elevation_gain += diff
                    prev_ele = p["ele"]
                elif diff < -0.5:
                    elevation_loss += abs(diff)
                    prev_ele = p["ele"]
            else:
                prev_ele = p["ele"]

    # 3. Sequential Segmentation
    segments = []
    current_idx = 0
    total_points = len(points)

    def slice_segment(name, start_idx, end_idx):
        if start_idx >= end_idx:
            return None
        seg_points = points[start_idx:end_idx]
        
        # Calculate active moving metrics for this segment
        s_moving_time = 0.0
        s_moving_dist = 0.0
        s_hr_weighted = 0.0
        s_hr_time = 0.0
        s_cad_weighted = 0.0
        s_cad_time = 0.0
        
        for i in range(1, len(seg_points)):
            p_prev = seg_points[i-1]
            p_curr = seg_points[i]
            
            dt = (p_curr["time"] - p_prev["time"]).total_seconds()
            if dt <= 0:
                continue
                
            dx = p_curr["cum_dist_km"] - p_prev["cum_dist_km"]
            speed_m_s = (dx * 1000.0) / dt
            
            is_pause = (dt > 10.0) or (speed_m_s < 0.5)
            if is_pause:
                continue
                
            s_moving_time += dt
            s_moving_dist += dx
            
            if p_curr["hr"] is not None and p_curr["hr"] > 0:
                s_hr_weighted += p_curr["hr"] * dt
                s_hr_time += dt
                
            if p_curr["cadence"] is not None and p_curr["cadence"] > 0:
                s_cad_weighted += p_curr["cadence"] * dt
                s_cad_time += dt
                
        # Fallback if no moving data
        p_start = seg_points[0]
        p_end = seg_points[-1]
        elapsed_dur = (p_end["time"] - p_start["time"]).total_seconds()
        elapsed_dist = p_end["cum_dist_km"] - p_start["cum_dist_km"]
        
        if s_moving_time < 2.0:
            s_moving_time = elapsed_dur
            s_moving_dist = elapsed_dist
            s_avg_hr = sum([pt["hr"] for pt in seg_points if pt["hr"]]) / len([pt["hr"] for pt in seg_points if pt["hr"]]) if [pt["hr"] for pt in seg_points if pt["hr"]] else None
            s_avg_cad = sum([pt["cadence"] for pt in seg_points if pt["cadence"]]) / len([pt["cadence"] for pt in seg_points if pt["cadence"]]) if [pt["cadence"] for pt in seg_points if pt["cadence"]] else None
        else:
            s_avg_hr = s_hr_weighted / s_hr_time if s_hr_time > 0 else None
            s_avg_cad = s_cad_weighted / s_cad_time if s_cad_time > 0 else None
            
        if s_moving_dist > 0.01:
            pace = s_moving_time / s_moving_dist
        else:
            pace = 0.0
            
        return {
            "name": name,
            "distance_km": round(s_moving_dist, 2),
            "duration_sec": int(s_moving_time),
            "avg_pace_sec": round(pace, 1),
            "avg_hr": round(s_avg_hr, 1) if s_avg_hr else None,
            "avg_cadence": round(s_avg_cad, 1) if s_avg_cad else None
        }

    def find_end_idx(start_idx, target_type, target_value):
        if start_idx >= total_points:
            return total_points
        p_start = points[start_idx]
        for idx in range(start_idx, total_points):
            p = points[idx]
            if target_type == "distance":
                dist_delta = p["cum_dist_km"] - p_start["cum_dist_km"]
                if dist_delta >= target_value:
                    return idx + 1
            else:  # time in seconds
                time_delta = (p["time"] - p_start["time"]).total_seconds()
                if time_delta >= target_value:
                    return idx + 1
        return total_points

    # 3.1. Warmup
    if params.get("warmup_enabled"):
        w_val = float(params.get("warmup_value") or 0.0)
        w_type = params.get("warmup_type", "distance")
        if w_type == "time":
            # value in minutes, convert to seconds
            w_val = w_val * 60.0
        warmup_end = find_end_idx(current_idx, w_type, w_val)
        seg = slice_segment("Riscaldamento", current_idx, warmup_end)
        if seg:
            segments.append(seg)
        current_idx = warmup_end

    # 3.2. Central Phase
    workout_type = params.get("workout_type", "lento_corto")
    if workout_type == "ripetute":
        reps = int(params.get("repetitions") or 1)
        int_type = params.get("interval_type", "distance")
        int_val = float(params.get("interval_value") or 0.0)
        if int_type == "distance":
            # interval value on UI is in meters, convert to km
            int_val = int_val / 1000.0

        rec_type = params.get("recovery_type", "time")
        rec_val = float(params.get("recovery_value") or 0.0)
        if rec_type == "distance":
            # recovery value on UI is in meters, convert to km
            rec_val = rec_val / 1000.0

        for i in range(reps):
            if current_idx >= total_points:
                break
            # Interval
            int_end = find_end_idx(current_idx, int_type, int_val)
            seg_int = slice_segment(f"Ripetuta {i+1}", current_idx, int_end)
            if seg_int:
                segments.append(seg_int)
            current_idx = int_end

            if current_idx >= total_points:
                break

            # Recovery
            is_last_rep = (i == reps - 1)
            if is_last_rep and not params.get("cooldown_enabled"):
                # Cooldown is disabled: the last recovery takes all remaining points
                rec_end = total_points
            else:
                # Normal recovery (including the last one if cooldown is enabled)
                rec_end = find_end_idx(current_idx, rec_type, rec_val)
                
            seg_rec = slice_segment(f"Recupero {i+1}", current_idx, rec_end)
            if seg_rec:
                segments.append(seg_rec)
            current_idx = rec_end

    elif workout_type == "medio":
        cooldown_start = total_points
        if params.get("cooldown_enabled"):
            c_val = float(params.get("cooldown_value") or 0.0)
            c_type = params.get("cooldown_type", "distance")
            
            if c_type == "distance":
                cooldown_dist_target = total_dist_km - c_val
                for idx in range(current_idx, total_points):
                    if points[idx]["cum_dist_km"] >= cooldown_dist_target:
                        cooldown_start = idx
                        break
            else:  # time in minutes
                c_val_sec = c_val * 60.0
                total_time_sec = (points[-1]["time"] - points[0]["time"]).total_seconds()
                cooldown_time_target = total_time_sec - c_val_sec
                for idx in range(current_idx, total_points):
                    elapsed = (points[idx]["time"] - points[0]["time"]).total_seconds()
                    if elapsed >= cooldown_time_target:
                        cooldown_start = idx
                        break

        cooldown_start = max(current_idx, cooldown_start)
        seg_medio = slice_segment("Medio", current_idx, cooldown_start)
        if seg_medio:
            segments.append(seg_medio)
        current_idx = cooldown_start

    else:  # lento_corto, lento_lungo, gara
        if workout_type == "lento_corto":
            seg_name = "Corsa Lenta"
        elif workout_type == "lento_lungo":
            seg_name = "Lungo"
        elif workout_type == "gara":
            seg_name = "Gara"
        else:
            seg_name = "Corsa"
        seg_lento = slice_segment(seg_name, current_idx, total_points)
        if seg_lento:
            segments.append(seg_lento)
        current_idx = total_points

    # 3.3. Cooldown (only if enabled and there are remaining points)
    if current_idx < total_points and params.get("cooldown_enabled"):
        seg_cool = slice_segment("Defaticamento", current_idx, total_points)
        if seg_cool:
            segments.append(seg_cool)

    # 4. Compile response dict
    date_str = start_time.date().isoformat() if start_time else datetime.date.today().isoformat()
    return {
        "date": date_str,
        "distance_km": round(total_dist_km, 2),
        "duration_sec": int(duration_sec),
        "avg_pace": round(avg_pace, 1),
        "avg_hr": round(avg_hr, 1) if avg_hr else 0.0,
        "max_hr": max_hr,
        "elevation_gain": round(elevation_gain, 1),
        "elevation_loss": round(elevation_loss, 1),
        "avg_cadence": round(avg_cad, 1) if avg_cad else 0.0,
        "laps_summary": segments
    }
