"""
Generate Complete Waterlevel-to-Waterlevel Gauge Relations & Flow Paths

Uses the comprehensive Hybrid Flow Path Engine (OSM River Backbone + D8 Hydrology + DEM Conditioning)
to accurately trace downstream gauge-to-gauge connectivity, travel times, slopes, and flow path geometry.

Outputs:
1. dataset/{basin}/station/osm-waterlevel-relations.json (Raw ML & Hydrology Relations)
2. dataset/{basin}/processed/relation_waterlevel_frontend.json (Frontend UI Relations)
"""

import os
import sys
import json
import gzip
import argparse
from typing import Dict, List, Any

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from scripts.modules.basin_registry import get_all_slugs, get_basin
from scripts.modules.gis_utils import load_stations_for_basin, save_json
from scripts.generate_flow_paths import generate_basin_flow_paths



def extract_waterlevel_relations_from_flow_paths(
    basin_dir: str,
    flow_paths_data: Dict[str, Any],
    water_stations: List[Dict[str, Any]]
) -> tuple:
    """
    Extracts gauge-to-gauge relations and formats frontend JSON from full flow path features.
    """
    water_st_by_id = {str(st.get("station_id", "")).strip(): st for st in water_stations}
    gauge_features = []
    raw_relations = []
    frontend_map: Dict[str, Dict[str, Any]] = {}

    # Initialize all water stations in frontend map
    for st in water_stations:
        st_id = str(st.get("station_id", "")).strip()
        if st_id:
            frontend_map[st_id] = {
                "stationId": st_id,
                "stationName": st.get("station_name", ""),
                "stationType": "water_level",
                "influencingStations": [],
                "downstreamStations": []
            }

    for feat in flow_paths_data.get("features", []):
        props = feat.get("properties", {})
        ftype = props.get("feature_type", "")
        if ftype != "gauge_to_gauge_flowpath":
            continue

        from_id = str(props.get("from_station_id", props.get("station_id", ""))).strip()
        to_id = str(props.get("to_station_id", props.get("target_station_id", ""))).strip()

        if not from_id or not to_id:
            continue

        from_st = water_st_by_id.get(from_id, {})
        to_st = water_st_by_id.get(to_id, {})

        from_name = props.get("from_station_name") or from_st.get("station_name", "")
        to_name = props.get("to_station_name") or to_st.get("station_name", "")

        dist_km = float(props.get("distance_km", 0.0))
        tt_hours = float(props.get("travel_time_hours", 0.0))
        tt_min_h = float(props.get("travel_time_hours_min", tt_hours * 0.8))
        tt_max_h = float(props.get("travel_time_hours_max", tt_hours * 1.2))

        tt_avg_m = int(props.get("travel_time_minutes", int(round(tt_hours * 60))))
        tt_min_m = int(props.get("travel_time_minutes_min", int(round(tt_min_h * 60))))
        tt_max_m = int(props.get("travel_time_minutes_max", int(round(tt_max_h * 60))))

        slope = float(props.get("river_slope", 0.0001))
        dz = float(props.get("elevation_diff_m", 0.0))
        z_up = float(props.get("upstream_elev_m", 0.0))
        z_down = float(props.get("downstream_elev_m", 0.0))

        raw_rel = {
            "feature_type": "gauge_to_gauge_flowpath",
            "routing": "hybrid_osm_d8",
            "from_station_id": from_id,
            "from_station_name": from_name,
            "to_station_id": to_id,
            "to_station_name": to_name,
            "distance_km": round(dist_km, 2),
            "river_slope": round(slope, 6),
            "elevation_diff_m": round(dz, 2),
            "upstream_elev_m": round(z_up, 2),
            "downstream_elev_m": round(z_down, 2),
            "travel_time_minutes": tt_avg_m,
            "travel_time_minutes_min": tt_min_m,
            "travel_time_minutes_max": tt_max_m,
            "travel_time_hours": round(tt_hours, 2),
            "travel_time_hours_min": round(tt_min_h, 2),
            "travel_time_hours_max": round(tt_max_h, 2),
            "coordinates": feat.get("geometry", {}).get("coordinates", [])
        }
        raw_relations.append(raw_rel)

        # Frontend map
        if from_id not in frontend_map:
            frontend_map[from_id] = {
                "stationId": from_id,
                "stationName": from_name,
                "stationType": "water_level",
                "influencingStations": [],
                "downstreamStations": []
            }

        frontend_map[from_id]["downstreamStations"].append({
            "stationId": to_id,
            "stationName": to_name,
            "stationType": "water_level",
            "distanceKm": round(dist_km, 2),
            "travelTimeMinutes": tt_avg_m,
            "travelTimeMinutesMin": tt_min_m,
            "travelTimeMinutesMax": tt_max_m,
            "travelTimeHours": round(tt_hours, 2),
            "travelTimeHoursMin": round(tt_min_h, 2),
            "travelTimeHoursMax": round(tt_max_h, 2),
            "riverSlope": round(slope, 6),
            "elevationDiffM": round(dz, 2),
            "confidence": props.get("confidence", "HIGH"),
            "responseType": props.get("response_type", "ESTIMATED")
        })

    return raw_relations, list(frontend_map.values())


def generate_osm_relations(basin: str, basin_dir: str, terrain_dir: str, force: bool = False):
    print(f"\n==================================================================")
    print(f"🗺️ Generating Full OSM & Hydrology Water Level Relations: {basin.upper()}")
    print(f"==================================================================")

    station_dir = os.path.join(basin_dir, "station")
    processed_dir = os.path.join(basin_dir, "processed")
    os.makedirs(station_dir, exist_ok=True)
    os.makedirs(processed_dir, exist_ok=True)

    out_raw_path = os.path.join(station_dir, "osm-waterlevel-relations.json")
    out_frontend_path = os.path.join(processed_dir, "relation_waterlevel_frontend.json")
    flow_paths_path = os.path.join(processed_dir, "flow_paths.geojson")
    flow_paths_gz_path = os.path.join(processed_dir, "flow_paths.geojson.gz")

    if not force and os.path.exists(out_raw_path) and os.path.exists(out_frontend_path):
        print(f"  [CACHE] Water level relations already exist.")
        print(f"          Raw ML    : {out_raw_path}")
        print(f"          Frontend  : {out_frontend_path}")
        print(f"          (Use --force to re-generate from full flow paths)")
        return

    # 1. Load water stations
    water_st, _ = load_stations_for_basin(basin_dir)
    if not water_st:
        print(f"  ❌ ERROR: No water level stations found in {basin_dir}")
        return

    # 2. Check or build full flow paths
    flow_paths_data = None
    if not force:
        if os.path.exists(flow_paths_path) and os.path.getsize(flow_paths_path) > 100:
            print(f"  [1/2] Loading existing Full Flow Paths from {flow_paths_path}...")
            with open(flow_paths_path, 'r', encoding='utf-8') as f:
                flow_paths_data = json.load(f)
        elif os.path.exists(flow_paths_gz_path) and os.path.getsize(flow_paths_gz_path) > 100:
            print(f"  [1/2] Loading existing Full Flow Paths from {flow_paths_gz_path}...")
            with gzip.open(flow_paths_gz_path, 'rt', encoding='utf-8') as f:
                flow_paths_data = json.load(f)

    if flow_paths_data is None:
        print(f"  [1/2] Generating Full Flow Paths using generate_basin_flow_paths...")
        generate_basin_flow_paths(
            basin=basin,
            basin_dir=basin_dir,
            terrain_dir=terrain_dir,
            force=force
        )
        if os.path.exists(flow_paths_path):
            with open(flow_paths_path, 'r', encoding='utf-8') as f:
                flow_paths_data = json.load(f)

    if not flow_paths_data:
        print(f"  ❌ ERROR: Could not generate or load flow paths for basin: {basin}")
        return

    # 3. Extract and export full gauge-to-gauge relations
    print(f"  [2/2] Extracting Waterlevel-to-Waterlevel relations from Full Flow Paths...")
    raw_relations, frontend_relations = extract_waterlevel_relations_from_flow_paths(
        basin_dir=basin_dir,
        flow_paths_data=flow_paths_data,
        water_stations=water_st
    )

    save_json(raw_relations, out_raw_path)
    save_json(frontend_relations, out_frontend_path)

    print(f"\n  ✅ Successfully generated full waterlevel flow path relations:")
    print(f"        • Total Gauge-to-Gauge Connections : {len(raw_relations)}")
    print(f"        • Total Stations in Frontend Map   : {len(frontend_relations)}")
    print(f"        • Saved Raw ML Relations           : {out_raw_path}")
    print(f"        • Saved Frontend Relations         : {out_frontend_path}")


def main():
    parser = argparse.ArgumentParser(description="Generate full flow-path based Water Level Gauge-to-Gauge relations")
    parser.add_argument("--basin", type=str, default="nan", help="River basin slug (e.g. yom, nan, ping, wang, chao-phraya, all)")
    parser.add_argument("--dir", type=str, default="./dataset", help="Dataset directory (supports root e.g. ./dataset or basin dir e.g. ./dataset/nan)")
    parser.add_argument("--terrain-dir", type=str, default="./terrain", help="Terrain DEM directory (independent of dataset --dir)")
    parser.add_argument("--force", action="store_true", help="Force re-generation of relations and flow paths")
    args = parser.parse_args()

    basin_list = get_all_slugs() if args.basin == "all" else [args.basin]


    for b in basin_list:
        # Smart path resolution for --dir (supports both './dataset' and './dataset/nan')
        if os.path.basename(os.path.normpath(args.dir)) == b:
            basin_dir = args.dir
        else:
            basin_dir = os.path.join(args.dir, b)

        # Smart path resolution for --terrain-dir
        if os.path.basename(os.path.normpath(args.terrain_dir)) == b:
            terrain_basin_dir = args.terrain_dir
        else:
            terrain_basin_dir = os.path.join(args.terrain_dir, b)
            if not os.path.exists(terrain_basin_dir) and os.path.exists(args.terrain_dir):
                terrain_basin_dir = args.terrain_dir

        if not os.path.exists(basin_dir):
            print(f"❌ ERROR: Basin directory not found: {basin_dir} (Check --dir path)", file=sys.stderr)
            continue

        generate_osm_relations(b, basin_dir, terrain_basin_dir, force=args.force)


if __name__ == "__main__":
    main()
