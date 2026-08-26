#!/usr/bin/env python3
"""
Step 5: Backend & Frontend Export Engine
Formats and exports model results to:
1. station_relations_db.json (PostgreSQL station_relations table format in backend-req.md)
2. relations_frontend.json (Cloudflare R2 format for StationRelations.tsx)
3. Map GeoJSON Layers (flow_paths.geojson & river_network.geojson for LeafletWaterMap.tsx)
"""

import argparse
import os
import sys
import json
from typing import Dict, List, Any

# Add parent directory to sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from scripts.modules.gis_utils import save_json, load_geojson, save_geojson
from scripts.modules.backend_exporter import export_backend_station_relations


def export_basin_model_dataset(basin: str, basin_dir: str):
    """Exports all final artifacts for backend and frontend consumption."""
    response_dir = os.path.join(basin_dir, "response")
    station_dir = os.path.join(basin_dir, "station")
    river_dir = os.path.join(basin_dir, "river")
    processed_dir = os.path.join(basin_dir, "processed")
    os.makedirs(processed_dir, exist_ok=True)

    estimated_response_path = os.path.join(response_dir, "estimated-response.json")
    station_relations_path = os.path.join(station_dir, "station-relations.json")
    rainfall_relations_path = os.path.join(station_dir, "rainfall-relations.json")
    flow_paths_src = os.path.join(processed_dir, "flow_paths.geojson")
    river_network_src = os.path.join(river_dir, "river_network.geojson")

    print(f"\n📦 [STEP 6] Exporting Backend & Frontend Artifacts for Basin: {basin.upper()}")

    # 1. Load Response Model Results & Rainfall Relations
    gauge_relations = []
    if os.path.exists(estimated_response_path):
        with open(estimated_response_path, 'r', encoding='utf-8') as f:
            gauge_relations = json.load(f)
    elif os.path.exists(station_relations_path):
        with open(station_relations_path, 'r', encoding='utf-8') as f:
            gauge_relations = json.load(f)

    rain_thresholds_path = os.path.join(response_dir, "rainfall-thresholds.json")
    rain_relations = []
    if os.path.exists(rain_thresholds_path):
        with open(rain_thresholds_path, 'r', encoding='utf-8') as f:
            rain_relations = json.load(f)
    elif os.path.exists(rainfall_relations_path):
        with open(rainfall_relations_path, 'r', encoding='utf-8') as f:
            rain_relations = json.load(f)

    # 2. Export Database & Frontend JSON Files
    out_db_path = os.path.join(processed_dir, "station_relations_db.json")
    out_frontend_path = os.path.join(processed_dir, "relations_frontend.json")
    export_backend_station_relations(gauge_relations, rain_relations, out_db_path, out_frontend_path)

    print(f"  [OK] Exported PostgreSQL Table Payload: {out_db_path}")
    print(f"  [OK] Exported Frontend StationRelations: {out_frontend_path}")

    # 3. Synchronize Map GeoJSON Layers
    if os.path.exists(river_network_src):
        river_dest = os.path.join(processed_dir, "river_network.geojson")
        river_data = load_geojson(river_network_src)
        save_geojson(river_data, river_dest)
        print(f"  [OK] Synchronized Map River Layer: {river_dest}")

    if os.path.exists(flow_paths_src):
        print(f"  [OK] Verified Map Flow Paths Layer: {flow_paths_src}")

    # 4. Generate Final Summary
    summary = {
        "basin": basin,
        "total_database_relations": len(gauge_relations) + len(rain_relations),
        "gauge_to_gauge_relations": len(gauge_relations),
        "rainfall_to_gauge_relations": len(rain_relations),
        "files_exported": [
            out_db_path,
            out_frontend_path,
            os.path.join(processed_dir, "flow_paths.geojson"),
            os.path.join(processed_dir, "river_network.geojson")
        ]
    }
    summary_path = os.path.join(processed_dir, "model_export_summary.json")
    save_json(summary, summary_path)
    print(f"  [OK] Export Summary: {summary_path}")


def main():
    parser = argparse.ArgumentParser(description="Export Model Artifacts for Backend and Frontend")
    parser.add_argument("--basin", type=str, default="yom", help="River basin slug (e.g. yom, nan, ping, all)")
    parser.add_argument("--dir", type=str, default="./dataset", help="Dataset directory")
    args = parser.parse_args()

    basin_list = ["yom", "nan", "ping", "wang", "chao-phraya"] if args.basin == "all" else [args.basin]
    for b in basin_list:
        basin_dir = os.path.join(args.dir, b)
        export_basin_model_dataset(b, basin_dir)


if __name__ == "__main__":
    main()
