"""
Hydrological Response & Travel Time Engine
Implements Continuous 4h Rise Detection, Plateau Holding Duration Analysis,
Tri-Feature Travel Time Matching (Wave Front, Plateau Midpoint, Centroid),
Rain-to-Stage Response, and ML Regression for Unobserved Stations.
"""

import csv
from datetime import datetime, timedelta
import math
from typing import Dict, List, Tuple, Any, Optional
import numpy as np
from sklearn.linear_model import Ridge
from sklearn.ensemble import RandomForestRegressor


def parse_timestamp(ts_str: str) -> datetime:
    """Parses standard ISO or SQL datetime string."""
    ts_clean = ts_str.replace('Z', '').split('+')[0].strip()
    for fmt in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%dT%H:%M:%S', '%Y-%m-%d %H:%M', '%Y-%m-%d'):
        try:
            return datetime.strptime(ts_clean, fmt)
        except ValueError:
            pass
    raise ValueError(f"Unable to parse timestamp: {ts_str}")


def detect_flood_rise_and_plateau_events(
    times: List[datetime],
    values: List[float],
    min_rise_hours: int = 4,
    plateau_diff_threshold: float = 0.02, # 2 cm
    min_plateau_hours: int = 3
) -> List[Dict[str, Any]]:
    """
    Detects flood events using:
    1. Continuous Rise >= min_rise_hours (without arbitrary height threshold).
    2. Plateau Holding Period and Plateau Midpoint calculation.
    """
    if not times or not values or len(times) != len(values):
        return []

    # Ensure time-series is sorted chronologically
    paired = sorted(zip(times, values), key=lambda x: x[0])
    times = [p[0] for p in paired]
    values = [p[1] for p in paired]

    n = len(values)
    if n < min_rise_hours:
        return []

    events = []
    i = 0

    while i < n - min_rise_hours:
        # Check if water is non-decreasing over min_rise_hours
        is_rising = True
        for k in range(min_rise_hours):
            if values[i + k + 1] < values[i + k] - 0.01: # allow 1cm sensor noise
                is_rising = False
                break
        
        # Net gain must be positive
        if is_rising and (values[i + min_rise_hours] > values[i]):
            rise_start_idx = i
            # Trace until the peak / plateau is reached
            curr = i + min_rise_hours
            while curr < n - 1 and (values[curr + 1] >= values[curr] - 0.01):
                curr += 1
            
            peak_val = max(values[rise_start_idx:curr + 1])
            
            # Step backward from curr to find start of plateau at peak level
            plateau_start_idx = curr
            while plateau_start_idx > rise_start_idx and abs(values[plateau_start_idx - 1] - peak_val) <= plateau_diff_threshold:
                plateau_start_idx -= 1

            # Step forward from curr to find end of plateau at peak level
            plateau_end_idx = curr
            while plateau_end_idx < n - 1 and abs(values[plateau_end_idx + 1] - peak_val) <= plateau_diff_threshold:
                plateau_end_idx += 1

            hold_hours = max(1.0, (times[plateau_end_idx] - times[plateau_start_idx]).total_seconds() / 3600.0)
            
            # Plateau Midpoint time
            mid_sec = (times[plateau_start_idx].timestamp() + times[plateau_end_idx].timestamp()) / 2.0
            mid_time = datetime.fromtimestamp(mid_sec)

            # Centroid calculation over the event window
            t_event = times[rise_start_idx:plateau_end_idx + 1]
            v_event = values[rise_start_idx:plateau_end_idx + 1]
            base_v = min(v_event)
            excess = [max(0.0, v - base_v) for v in v_event]
            sum_excess = sum(excess)

            if sum_excess > 0:
                t_offsets = [(t - times[rise_start_idx]).total_seconds() / 3600.0 for t in t_event]
                centroid_offset = sum(w * t for w, t in zip(excess, t_offsets)) / sum_excess
                centroid_time = times[rise_start_idx] + timedelta(hours=centroid_offset)
            else:
                centroid_time = mid_time

            events.append({
                "rise_start_time": times[rise_start_idx],
                "plateau_start_time": times[plateau_start_idx],
                "plateau_end_time": times[plateau_end_idx],
                "plateau_mid_time": mid_time,
                "centroid_time": centroid_time,
                "holding_duration_hours": round(hold_hours, 1),
                "peak_value": round(peak_val, 2),
                "rise_height_m": round(peak_val - values[rise_start_idx], 2)
            })

            i = max(curr + 1, plateau_end_idx + 1)
        else:
            i += 1

    return events


def calculate_observed_travel_time(
    events_up: List[Dict[str, Any]],
    events_down: List[Dict[str, Any]],
    max_lag_window_hours: float = 72.0
) -> Optional[Dict[str, Any]]:
    """
    Matches flood events between Upstream and Downstream gauges using Tri-Feature Matching:
    1. Wave Front Lag (Rise Start) -> travel_time_hours_min
    2. Plateau Midpoint Lag -> travel_time_hours (typical)
    3. Volume Centroid Lag -> cross-validation
    4. 90th percentile -> travel_time_hours_max
    """
    if not events_up or not events_down:
        return None

    matched_mid_lags = []
    matched_front_lags = []
    matched_holding_durations = []

    for ev_u in events_up:
        t_mid_u = ev_u['plateau_mid_time']
        t_rise_u = ev_u['rise_start_time']

        # Find best matching event downstream within lag window
        best_ev_d = None
        min_diff = 999999.0

        for ev_d in events_down:
            t_mid_d = ev_d['plateau_mid_time']
            dt = (t_mid_d - t_mid_u).total_seconds() / 3600.0
            if 0.5 <= dt <= max_lag_window_hours: # water must travel forward
                if dt < min_diff:
                    min_diff = dt
                    best_ev_d = ev_d

        if best_ev_d:
            dt_mid = (best_ev_d['plateau_mid_time'] - t_mid_u).total_seconds() / 3600.0
            dt_front = (best_ev_d['rise_start_time'] - t_rise_u).total_seconds() / 3600.0
            matched_mid_lags.append(dt_mid)
            matched_front_lags.append(max(0.5, dt_front))
            matched_holding_durations.append(best_ev_d['holding_duration_hours'])

    if not matched_mid_lags:
        return None

    typical_h = float(np.median(matched_mid_lags))
    min_h = float(np.percentile(matched_front_lags, 10))
    max_h = float(np.percentile(matched_mid_lags, 90))
    avg_hold = float(np.mean(matched_holding_durations))

    # Ensure min <= typical <= max
    min_h = min(min_h, typical_h * 0.9)
    max_h = max(max_h, typical_h * 1.1)

    typical_m = int(round(typical_h * 60.0))
    min_m = int(round(max(10.0, min_h * 60.0 * 0.70)))  # Apply -30% Early Warning SF
    max_m = int(round(max_h * 60.0))

    return {
        "travel_time_minutes": typical_m,
        "travel_time_minutes_min": min_m,
        "travel_time_minutes_max": max_m,
        "travel_time_hours": round(typical_m / 60.0, 2),
        "travel_time_hours_min": round(min_m / 60.0, 2),
        "travel_time_hours_max": round(max_m / 60.0, 2),
        "avg_holding_duration_hours": round(avg_hold, 1),
        "matched_event_count": len(matched_mid_lags),
        "detection_rule": "continuous_rise_4h_with_plateau_midpoint"
    }


def train_estimated_response_model(
    observed_pairs: List[Dict[str, Any]],
    all_pairs: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    """
    Trains ML regression model on Observed Pairs (distance, slope, elevation_diff, catchment_area)
    and predicts Travel Time for Partially Observed & Unobserved Pairs with Confidence scoring.
    """
    valid_train = [p for p in observed_pairs if p.get('travel_time_hours') and p.get('distance_km')]
    results = []

    # If training samples exist, train Ridge model
    model = None
    if len(valid_train) >= 3:
        X = []
        y = []
        for p in valid_train:
            dist = float(p.get('distance_km', 10.0))
            slope = float(p.get('river_slope', 0.001))
            dz = float(p.get('elevation_diff_m', 5.0))
            X.append([dist, math.sqrt(max(0.00001, slope)), dz])
            y.append(float(p['travel_time_hours']))
        
        model = Ridge(alpha=1.0)
        model.fit(X, y)

    for p in all_pairs:
        pair_data = dict(p)
        st_up = pair_data['station_id']
        st_down = pair_data['target_station_id']
        
        # Check if already observed
        obs = next((o for o in valid_train if o['station_id'] == st_up and o['target_station_id'] == st_down), None)
        if obs:
            pair_data['travel_time_minutes'] = obs.get('travel_time_minutes', int(round(obs['travel_time_hours'] * 60.0)))
            pair_data['travel_time_minutes_min'] = obs.get('travel_time_minutes_min', int(round(obs['travel_time_hours_min'] * 60.0)))
            pair_data['travel_time_minutes_max'] = obs.get('travel_time_minutes_max', int(round(obs['travel_time_hours_max'] * 60.0)))
            pair_data['travel_time_hours'] = obs['travel_time_hours']
            pair_data['travel_time_hours_min'] = obs['travel_time_hours_min']
            pair_data['travel_time_hours_max'] = obs['travel_time_hours_max']
            pair_data['avg_holding_duration_hours'] = obs.get('avg_holding_duration_hours', 6.0)
            pair_data['response_type'] = 'OBSERVED'
            pair_data['confidence'] = 'HIGH' if obs['matched_event_count'] >= 5 else 'MEDIUM'
            pair_data['event_count'] = obs['matched_event_count']
        else:
            # Predict using model or hydraulic formula (wave speed ~ 5-8 km/h typical)
            dist = float(pair_data.get('distance_km', 15.0))
            slope = float(pair_data.get('river_slope', 0.0008))
            dz = float(pair_data.get('elevation_diff_m', 5.0))
            # Hydraulic Manning wave speed approximation across flow stages
            s_safe = max(0.0001, slope)
            v_bankfull = max(3.8, min(11.5, 7.2 * (s_safe / 0.001) ** 0.22))
            v_lowflow = max(1.8, min(5.5, 3.6 * (s_safe / 0.001) ** 0.18))
            v_mean = max(2.5, min(9.0, 5.2 * (s_safe / 0.001) ** 0.20))

            if model:
                pred_y_h = float(model.predict([[dist, math.sqrt(s_safe), dz]])[0])
                pred_y_m = int(round(max(0.5, pred_y_h) * 60.0))
            else:
                pred_y_m = int(round((dist / v_mean) * 60.0))

            t_min_m = int(round(max(15, min((dist / v_bankfull) * 60.0, pred_y_m * 0.70))))
            t_max_m = int(round(max(pred_y_m + 30, (dist / v_lowflow) * 60.0)))

            pair_data['travel_time_minutes'] = pred_y_m
            pair_data['travel_time_minutes_min'] = t_min_m
            pair_data['travel_time_minutes_max'] = t_max_m
            pair_data['travel_time_hours'] = round(pred_y_m / 60.0, 2)
            pair_data['travel_time_hours_min'] = round(t_min_m / 60.0, 2)
            pair_data['travel_time_hours_max'] = round(t_max_m / 60.0, 2)
            pair_data['avg_holding_duration_hours'] = 6.0
            pair_data['response_type'] = 'ESTIMATED'
            pair_data['confidence'] = 'MEDIUM' if dist <= 50.0 else 'LOW'
            pair_data['event_count'] = 0

        pair_data['detection_rule'] = "continuous_rise_4h_with_plateau_midpoint"
        results.append(pair_data)

    return results
