#!/usr/bin/env python3
"""
Step 5: Empirical Rainfall-Runoff Trigger Engine
================================================
Calculates empirical rainfall thresholds (Inception & Warning Stage) across 4 Key Windows
(3h, 24h, 72h, 168h) using Machine Learning (Unsupervised Soil Clustering & Quantile Regression).

Features:
- Zero Hardcoding: Dynamically clusters 7-day antecedent rainfall into Wet / Normal / Dry soil regimes.
- Direct In-Place Update: Can update existing relations and backend exports without re-running DEM/GIS steps.

Usage:
  python scripts/calculate_rainfall_thresholds.py --basin yom --update-existing
  python scripts/calculate_rainfall_thresholds.py --basin all --update-existing
"""

import argparse
import os
import sys
import json
import time
from pathlib import Path
from typing import Dict, List, Set, Any, Optional

# Add parent directory to sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.modules.gis_utils import save_json, load_stations_for_basin
from scripts.train_response_model import build_station_alias_map, load_hourly_waterlevel_series
from scripts.modules.hydrology_model import (
    load_hourly_rainfall_series,
    compute_data_driven_rainfall_thresholds,
    train_estimated_rain_thresholds_model,
)
from scripts.export_backend_dataset import export_basin_model_dataset


def generate_fallback_rainfall_relations(basin_dir: str) -> List[Dict[str, Any]]:
    """Generates initial spatial nearest-gauge rainfall relations if Step 3 has not been run yet."""
    water_st, rain_st = load_stations_for_basin(basin_dir)
    if not water_st or not rain_st:
        return []

    def haversine_km(lat1, lon1, lat2, lon2):
        from math import radians, cos, sin, asin, sqrt
        lon1, lat1, lon2, lat2 = map(radians, [lon1, lat1, lon2, lat2])
        dlon = lon2 - lon1
        dlat = lat2 - lat1
        a = sin(dlat/2)**2 + cos(lat1) * cos(lat2) * sin(dlon/2)**2
        return 6371.0 * 2 * asin(sqrt(a))

    relations = []
    for r in rain_st:
        r_id = str(r.get('station_id', r.get('station_code', ''))).strip()
        r_lat = float(r.get('latitude', 0))
        r_lon = float(r.get('longitude', 0))
        if not r_lat or not r_lon:
            continue

        # Find nearest downstream/neighboring water station within 50 km
        candidates = []
        for w in water_st:
            w_id = str(w.get('station_id', w.get('station_code', ''))).strip()
            w_lat = float(w.get('latitude', 0))
            w_lon = float(w.get('longitude', 0))
            if not w_lat or not w_lon:
                continue
            dist = haversine_km(r_lat, r_lon, w_lat, w_lon)
            if dist <= 50.0:
                candidates.append((dist, w))

        if candidates:
            candidates.sort(key=lambda x: x[0])
            best_dist, best_w = candidates[0]
            w_id = str(best_w.get('station_id', best_w.get('station_code', ''))).strip()
            lag_hours = max(1.5, round(best_dist / 6.5, 1))
            relations.append({
                "from_station_id": r_id,
                "from_station_name": r.get('station_name', r.get('station_name_th', '')),
                "to_station_id": w_id,
                "to_station_name": best_w.get('station_name', best_w.get('station_name_th', '')),
                "total_distance_km": round(best_dist, 2),
                "distance_km": round(best_dist, 2),
                "response_lag_hours": lag_hours,
                "response_lag_minutes": int(round(lag_hours * 60)),
                "influence_weight_percent": 60.0
            })

    return relations


def calculate_basin_rainfall_thresholds(
    basin: str,
    basin_dir: str,
    update_existing: bool = True
) -> List[Dict[str, Any]]:
    """Calculates ML rainfall trigger thresholds for a river basin."""
    t0 = time.time()
    station_dir = os.path.join(basin_dir, "station")
    processed_dir = os.path.join(basin_dir, "processed")
    response_dir = os.path.join(basin_dir, "response")
    os.makedirs(response_dir, exist_ok=True)
    os.makedirs(station_dir, exist_ok=True)

    rainfall_relations_path = os.path.join(station_dir, "rainfall-relations.json")
    hourly_rain_path = os.path.join(processed_dir, f"{basin}_hourly_rainfall.csv")
    hourly_wl_path = os.path.join(processed_dir, f"{basin}_hourly_waterlevel.csv")
    wl_stations_path = os.path.join(station_dir, f"{basin}_waterlevel_stations.json")

    if not os.path.exists(hourly_rain_path) or not os.path.exists(hourly_wl_path):
        print(f"❌ ERROR: Processed time-series files not found in {processed_dir}. Run consolidate_basin_data.py first!", file=sys.stderr)
        return []

    print(f"\n🌧️ [STEP 5] Calculating ML Rainfall Trigger Thresholds for Basin: {basin.upper()}")

    # 1. Load or Generate Rainfall Relations & Station Metadata
    if os.path.exists(rainfall_relations_path) and os.path.getsize(rainfall_relations_path) > 100:
        with open(rainfall_relations_path, 'r', encoding='utf-8') as f:
            rainfall_relations = json.load(f)
    else:
        print("  [INFO] Generating spatial rain-to-gauge relations from station network...")
        rainfall_relations = generate_fallback_rainfall_relations(basin_dir)
        save_json(rainfall_relations, rainfall_relations_path)
        print(f"        Generated {len(rainfall_relations)} spatial rain-to-gauge pairs.")

    station_metadata_map: Dict[str, Dict[str, Any]] = {}
    if os.path.exists(wl_stations_path):
        with open(wl_stations_path, 'r', encoding='utf-8') as f:
            st_list = json.load(f)
            for item in st_list:
                st = item.get("station") or {}
                sid = str(st.get("id") or "").strip()
                oldcode = str(st.get("tele_station_oldcode") or "").strip()
                b_msl = st.get("bank_level_msl") or st.get("min_bank")
                w_msl = st.get("warning_level_msl")
                for k in (sid, oldcode, oldcode.split("-")[-1] if "-" in oldcode else ""):
                    if k:
                        station_metadata_map[k] = {
                            "bank_level_msl": float(b_msl) if b_msl is not None else None,
                            "warning_level_msl": float(w_msl) if w_msl is not None else None
                        }

    alias_map = build_station_alias_map(basin_dir)
    print("  [1/4] Loading Hourly Time-Series (Rainfall & Water Level)...")
    wl_series = load_hourly_waterlevel_series(hourly_wl_path, station_aliases=alias_map)
    rain_series = load_hourly_rainfall_series(hourly_rain_path, station_aliases=alias_map)

    # 2. Process Observed Rain-to-Gauge Pairs
    print("  [2/4] Running ML Antecedent Soil Moisture Clustering & Event Back-Tracing...")
    observed_records = []
    observed_count = 0

    for rel in rainfall_relations:
        r_id = str(rel.get('from_station_id', rel.get('station_id', ''))).strip()
        w_id = str(rel.get('to_station_id', rel.get('target_station_id', ''))).strip()
        lag_h = float(rel.get('response_lag_hours', 3.5))

        r_data = rain_series.get(r_id)
        w_data = wl_series.get(w_id)

        if r_data and w_data and len(r_data[1]) >= 168 and len(w_data[1]) >= 100:
            meta = station_metadata_map.get(w_id, {})
            b_lvl = meta.get("bank_level_msl")
            w_lvl = meta.get("warning_level_msl")

            res = compute_data_driven_rainfall_thresholds(
                rain_times=r_data[0],
                rain_values=r_data[1],
                water_times=w_data[0],
                water_values=w_data[1],
                lag_hours=lag_h,
                bank_level=b_lvl,
                warning_level=w_lvl,
                windows=[3, 24, 72, 168]
            )

            if res:
                rec = dict(rel)
                rec.update(res)
                observed_records.append(rec)
                observed_count += 1

    print(f"        ✓ Identified {observed_count} Empirical Ground-Truth Pairs with ML soil regimes.")

    # 3. Train Multi-Variate ML Regression Model for Unobserved / Sparse Pairs
    print("  [3/4] Training Multi-Variate ML Regression Model for Unobserved Pairs...")
    final_relations = train_estimated_rain_thresholds_model(
        observed_records=observed_records,
        all_rainfall_relations=rainfall_relations,
        windows=[3, 24, 72, 168]
    )

    # Save dedicated rainfall-thresholds.json
    out_thresholds_path = os.path.join(response_dir, "rainfall-thresholds.json")
    save_json(final_relations, out_thresholds_path)
    print(f"        ✓ Saved ML Rainfall Trigger Thresholds: {out_thresholds_path} ({len(final_relations)} relations)")

    # 4. Direct In-Place Updates
    if update_existing:
        print("  [4/4] Direct In-Place Updating station and response exports...")
        save_json(final_relations, rainfall_relations_path)
        # Update backend & frontend exports
        export_basin_model_dataset(basin, basin_dir)
        print(f"        ✓ Updated rainfall-relations.json, station_relations_db.json, and relations_frontend.json in {basin_dir}")

    elapsed = time.time() - t0
    print(f"  ⏱️ Step 5 completed in {elapsed:.2f}s")
    return final_relations


def main():
    parser = argparse.ArgumentParser(description="Calculate Empirical ML Rainfall-Runoff Trigger Thresholds (4 Key Windows)")
    parser.add_argument("--basin", type=str, default="yom", help="Target river basin (yom, nan, ping, wang, chao-phraya, all)")
    parser.add_argument("--dir", type=str, default="./dataset", help="Root dataset directory")
    parser.add_argument("--update-existing", action="store_true", default=True, help="Update existing relation and export files in-place")
    args = parser.parse_args()

    basin_list = ["yom", "nan", "ping", "wang", "chao-phraya"] if args.basin == "all" else [args.basin]

    for b in basin_list:
        basin_dir = os.path.join(args.dir, b)
        if os.path.exists(basin_dir):
            calculate_basin_rainfall_thresholds(b, basin_dir, update_existing=args.update_existing)


if __name__ == "__main__":
    main()
