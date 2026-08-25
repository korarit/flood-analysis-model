#!/usr/bin/env python3
"""
Step 4: Hydrological Response & Travel Time Engine
Detects 4-hour continuous rise events, analyzes plateau holding durations,
calculates Observed Travel Times, trains ML model for Unobserved Pairs,
and models Rain-to-Stage response.
"""

import argparse
import csv
import os
import sys
from typing import Dict, List, Tuple, Any, Optional
from datetime import datetime

# Add parent directory to sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from scripts.modules.gis_utils import save_json, load_geojson
from scripts.modules.hydrology_model import (
    parse_timestamp,
    detect_flood_rise_and_plateau_events,
    calculate_observed_travel_time,
    train_estimated_response_model
)


def load_hourly_waterlevel_series(csv_path: str) -> Dict[str, Tuple[List[datetime], List[float]]]:
    """Loads waterlevel time-series grouped by station_id."""
    data = {}
    print(f"  [LOAD] Reading hourly waterlevel data from {csv_path}...")
    with open(csv_path, 'r', encoding='utf-8-sig') as f:
        reader = csv.DictReader(f)
        for row in reader:
            st_id = row['station_id']
            try:
                ts = parse_timestamp(row['datetime'])
                val = float(row['water_level_m'])
                if val <= -90.0:  # nodata
                    continue
                if st_id not in data:
                    data[st_id] = ([], [])
                data[st_id][0].append(ts)
                data[st_id][1].append(val)
            except (ValueError, KeyError):
                continue
    print(f"        Loaded time-series for {len(data)} stations.")
    return data


def run_response_model_pipeline(basin: str, basin_dir: str):
    """Executes the travel time and response modeling workflow."""
    station_dir = os.path.join(basin_dir, "station")
    response_dir = os.path.join(basin_dir, "response")
    processed_dir = os.path.join(basin_dir, "processed")
    os.makedirs(response_dir, exist_ok=True)

    station_relations_path = os.path.join(station_dir, "station-relations.json")
    hourly_wl_path = os.path.join(processed_dir, f"{basin}_hourly_waterlevel.csv")

    if not os.path.exists(station_relations_path):
        print(f"❌ ERROR: station-relations.json not found in {station_dir}. Run 03_build_station_chain.py first!", file=sys.stderr)
        sys.exit(1)
    if not os.path.exists(hourly_wl_path):
        print(f"❌ ERROR: {hourly_wl_path} not found. Run consolidate_basin_data.py first!", file=sys.stderr)
        sys.exit(1)

    print(f"\n📊 [STEP 4] Training Travel Time & Response Model for Basin: {basin.upper()}")

    # 1. Load Station Relations & Time-Series
    with open(station_relations_path, 'r', encoding='utf-8') as f:
        import json
        station_relations = json.load(f)

    wl_series = load_hourly_waterlevel_series(hourly_wl_path)

    # 2. Detect Flood Events for each station
    print("  [1/4] Detecting 4-hour continuous rise events and plateau holding periods...")
    station_events = {}
    total_events = 0
    for st_id, (times, values) in wl_series.items():
        events = detect_flood_rise_and_plateau_events(times, values, min_rise_hours=4)
        if events:
            station_events[st_id] = events
            total_events += len(events)

    print(f"        Detected {total_events} flood events across {len(station_events)} active stations.")

    # 3. Calculate Observed Travel Times for connected station pairs
    print("  [2/4] Calculating Observed Travel Times for connected station pairs...")
    observed_pairs = []
    for rel in station_relations:
        st_up = rel['from_station_id']
        st_down = rel['to_station_id']

        if st_up in station_events and st_down in station_events:
            obs_res = calculate_observed_travel_time(station_events[st_up], station_events[st_down])
            if obs_res:
                obs_data = dict(rel)
                obs_data['station_id'] = st_up
                obs_data['target_station_id'] = st_down
                obs_data.update(obs_res)
                observed_pairs.append(obs_data)

    print(f"        Identified {len(observed_pairs)} Observed Station Pairs with empirical ground truth.")
    observed_path = os.path.join(response_dir, "observed-response.json")
    save_json(observed_pairs, observed_path)

    # 4. Train ML Regression Model & Predict Unobserved Pairs
    print("  [3/4] Training ML Regression Model for Partially Observed and Unobserved Pairs...")
    formatted_all_pairs = []
    for rel in station_relations:
        formatted_all_pairs.append({
            "station_id": rel['from_station_id'],
            "target_station_id": rel['to_station_id'],
            "distance_km": rel.get('distance_km', 15.0),
            "river_slope": rel.get('river_slope', 0.0008),
            "elevation_diff_m": rel.get('elevation_diff_m', 5.0)
        })

    estimated_results = train_estimated_response_model(observed_pairs, formatted_all_pairs)
    estimated_path = os.path.join(response_dir, "estimated-response.json")
    save_json(estimated_results, estimated_path)
    print(f"        Generated Travel Times for {len(estimated_results)} total station pairs.")
    print(f"        Saved Estimated Response: {estimated_path}")

    # 5. Validation Summary
    print("  [4/4] Generating Model Validation Report...")
    val_report = {
        "basin": basin,
        "total_station_pairs": len(estimated_results),
        "observed_pairs_count": len(observed_pairs),
        "estimated_pairs_count": len(estimated_results) - len(observed_pairs),
        "total_flood_events_detected": total_events,
        "detection_rule": "continuous_rise_4h_with_plateau_midpoint",
        "timestamp": datetime.now().isoformat()
    }
    val_path = os.path.join(response_dir, "validation.json")
    save_json(val_report, val_path)
    print(f"        Saved Validation Report: {val_path}")


def main():
    parser = argparse.ArgumentParser(description="Train Travel Time and Hydrological Response Model")
    parser.add_argument("--basin", type=str, default="yom", help="River basin slug (e.g. yom, nan, ping, all)")
    parser.add_argument("--dir", type=str, default="./dataset", help="Dataset directory")
    args = parser.parse_args()

    basin_list = ["yom", "nan", "ping", "wang", "chao-phraya"] if args.basin == "all" else [args.basin]
    for b in basin_list:
        basin_dir = os.path.join(args.dir, b)
        run_response_model_pipeline(b, basin_dir)


if __name__ == "__main__":
    main()
