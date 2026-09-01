import os
import sys
import json
import argparse

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from scripts.modules.basin_registry import get_all_slugs, get_basin
from scripts.modules.gis_utils import save_json
from scripts.modules.graph_topology import compute_rainfall_lag_bounds


def patch_travel_times(basin: str, basin_dir: str):
    print(f"\n==================================================================")
    print(f"🔧 Patching Travel Times for Basin: {basin.upper()}")
    print(f"==================================================================")

    station_dir = os.path.join(basin_dir, "station")
    relations_path = os.path.join(station_dir, "station-relations.json")

    if not os.path.exists(relations_path):
        print(f"  ❌ ERROR: Missing required file {relations_path}")
        print("          Please run `python scripts/generate_flow_paths.py` first.")
        return

    print(f"  [1/2] Loading {relations_path}...")
    with open(relations_path, 'r', encoding='utf-8') as f:
        relations = json.load(f)

    updated_count = 0
    for rel in relations:
        if rel.get("feature_type") == "gauge_to_gauge_flowpath":
            dist_km = float(rel.get("distance_km", 0.0))
            slope = float(rel.get("river_slope", 0.0001))
            dz = float(rel.get("elevation_diff_m", 0.0))

            # Recalculate kinematic wave travel time based on distance and slope
            lag_min_m, lag_avg_m, lag_max_m, lag_min_h, lag_avg_h, lag_max_h = compute_rainfall_lag_bounds(
                overland_dist_km=0.0,
                overland_slope=0.0,
                channel_dist_km=dist_km,
                channel_slope=slope,
                total_dz_m=dz
            )

            # Inject the calculated times
            rel["travel_time_minutes"] = lag_avg_m
            rel["travel_time_minutes_min"] = lag_min_m
            rel["travel_time_minutes_max"] = lag_max_m
            rel["travel_time_hours"] = lag_avg_h
            rel["travel_time_hours_min"] = lag_min_h
            rel["travel_time_hours_max"] = lag_max_h
            updated_count += 1

    if updated_count > 0:
        print(f"  [2/2] Injected travel times into {updated_count} gauge-to-gauge relations.")
        save_json(relations, relations_path)
        print(f"        ✅ Successfully updated {relations_path}")
        print(f"\n  💡 Next Step: Run `python scripts/export_backend_dataset.py --basin {basin}` to update the frontend JSON.")
    else:
        print("  [2/2] No gauge-to-gauge relations needed updating.")


def main():
    parser = argparse.ArgumentParser(description="Recalculate and patch missing travel times in station-relations.json")
    parser.add_argument("--basin", type=str, default="nan", help="River basin slug (e.g. yom, nan, ping, all)")
    parser.add_argument("--dir", type=str, default="./dataset", help="Dataset directory")
    args = parser.parse_args()

    basin_list = get_all_slugs() if args.basin == "all" else [args.basin]

    for b in basin_list:
        basin_dir = os.path.join(args.dir, b)
        patch_travel_times(b, basin_dir)

if __name__ == "__main__":
    main()
