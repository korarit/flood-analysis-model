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
from typing import Dict, List, Tuple, Any, Optional, Set
from datetime import datetime

# Add parent directory to sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from scripts.modules.gis_utils import save_json, load_geojson, load_stations_for_basin
from scripts.modules.hydrology_model import (
    parse_timestamp,
    detect_flood_rise_and_plateau_events,
    calculate_observed_travel_time,
    train_estimated_response_model
)


def build_station_alias_map(basin_dir: str) -> Dict[str, Set[str]]:
    """Builds a bidirectional alias mapping between station_id, station_oldcode, and station_code."""
    water_st, rain_st = load_stations_for_basin(basin_dir)
    all_st = (water_st or []) + (rain_st or [])
    alias_map: Dict[str, Set[str]] = {}
    for st in all_st:
        sid = str(st.get('station_id') or '').strip()
        old = str(st.get('station_oldcode') or '').strip()
        code = str(st.get('station_code') or '').strip()
        aliases = {a for a in (sid, old, code) if a}
        if old and '-' in old:
            aliases.add(old.split('-', 1)[1].strip())
        for a in aliases:
            if a not in alias_map:
                alias_map[a] = set()
            alias_map[a].update(aliases)
    return alias_map


def load_hourly_waterlevel_series(
    csv_path: str,
    station_aliases: Optional[Dict[str, Set[str]]] = None
) -> Dict[str, Tuple[List[datetime], List[float]]]:
    """Loads waterlevel time-series grouped by station_id / station_code with alias support."""
    data = {}
    print(f"  [LOAD] Reading hourly waterlevel data from {csv_path}...")
    with open(csv_path, 'r', encoding='utf-8-sig') as f:
        reader = csv.DictReader(f)
        for row in reader:
            # 1. Flexible station identifier column lookup
            st_id = (
                row.get('station_id') or
                row.get('station_code') or
                row.get('code') or
                row.get('id') or
                row.get('stn_code') or
                ''
            ).strip()
            if not st_id:
                continue

            # 2. Flexible datetime column lookup
            dt_str = (
                row.get('datetime') or
                row.get('measure_datetime') or
                row.get('timestamp') or
                row.get('date_time') or
                row.get('time') or
                ''
            ).strip()
            if not dt_str:
                continue

            # 3. Flexible water level value column lookup
            val_str = (
                row.get('waterlevel_msl') or
                row.get('water_level_m') or
                row.get('water_level') or
                row.get('waterlevel') or
                row.get('wl') or
                row.get('value') or
                ''
            )
            if isinstance(val_str, str):
                val_str = val_str.strip()
            if val_str == '' or val_str is None:
                continue

            try:
                ts = parse_timestamp(dt_str)
                val = float(val_str)
                if val <= -90.0:  # nodata
                    continue

                # Register data point under station ID and all mapped aliases
                keys_to_register = {st_id}
                if station_aliases and st_id in station_aliases:
                    keys_to_register.update(station_aliases[st_id])

                for key in keys_to_register:
                    if key not in data:
                        data[key] = ([], [])
                    data[key][0].append(ts)
                    data[key][1].append(val)
            except (ValueError, KeyError):
                continue

    unique_count = len({station_aliases[k].copy().pop() if (station_aliases and k in station_aliases) else k for k in data.keys()}) if data else 0
    print(f"        Loaded time-series for {unique_count} stations ({len(data)} indexed keys).")
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

    alias_map = build_station_alias_map(basin_dir)
    wl_series = load_hourly_waterlevel_series(hourly_wl_path, station_aliases=alias_map)

    # 2. Detect Flood Events for each station
    print("  [1/4] Detecting 4-hour continuous rise events and plateau holding periods...")
    station_events = {}
    total_events = 0
    for st_id, (times, values) in wl_series.items():
        events = detect_flood_rise_and_plateau_events(times, values, min_rise_hours=4)
        if events:
            station_events[st_id] = events
            total_events += len(events)

    print(f"        Detected flood events across active stations.")

    # 3. Calculate Observed Travel Times for connected station pairs
    print("  [2/4] Calculating Observed Travel Times for connected station pairs...")
    observed_pairs = []
    for rel in station_relations:
        st_up = str(rel.get('from_station_id', '')).strip()
        st_down = str(rel.get('to_station_id', '')).strip()

        ev_up = station_events.get(st_up)
        ev_down = station_events.get(st_down)

        if ev_up and ev_down:
            obs_res = calculate_observed_travel_time(ev_up, ev_down)
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
            "station_id": str(rel.get('from_station_id', '')).strip(),
            "target_station_id": str(rel.get('to_station_id', '')).strip(),
            "from_station_name": rel.get('from_station_name', ''),
            "to_station_name": rel.get('to_station_name', ''),
            "distance_km": float(rel.get('distance_km', 15.0)),
            "river_slope": float(rel.get('river_slope', 0.0008)),
            "elevation_diff_m": float(rel.get('elevation_diff_m', 5.0))
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
