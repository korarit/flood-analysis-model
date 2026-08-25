#!/usr/bin/env python3
"""
Step 3: Station Snapping & Directed River Chain
Snaps stations to stream channel, traces Gauge-to-Gauge and Rain-to-Gauge
flow paths, and delineates sub-catchment polygons.
"""

import argparse
import os
import sys
from typing import Dict, List, Any

# Add parent directory to sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from scripts.modules.gis_utils import save_geojson, save_json, load_stations_for_basin
from scripts.modules.terrain_engine import read_dem_geotiff
from scripts.modules.graph_topology import (
    snap_stations_to_stream,
    build_flow_paths_and_relations,
    delineate_station_catchments
)


def build_basin_station_chain(basin: str, basin_dir: str):
    """Snaps stations, generates flow paths, and delineates catchments for the basin."""
    terrain_dir = os.path.join(basin_dir, "terrain")
    station_dir = os.path.join(basin_dir, "station")
    catchment_dir = os.path.join(basin_dir, "catchment")
    processed_dir = os.path.join(basin_dir, "processed")
    os.makedirs(catchment_dir, exist_ok=True)
    os.makedirs(processed_dir, exist_ok=True)

    cond_dem_path = os.path.join(terrain_dir, "conditioned_dem.tif")
    fdir_path = os.path.join(terrain_dir, "flow_direction.tif")
    acc_path = os.path.join(terrain_dir, "flow_accumulation.tif")

    for f in (cond_dem_path, fdir_path, acc_path):
        if not os.path.exists(f):
            print(f"❌ ERROR: Required terrain file missing: {f}. Please run 02_build_river_network.py first!", file=sys.stderr)
            sys.exit(1)

    print(f"\n🔗 [STEP 3] Building Station Chain & Flow Paths for Basin: {basin.upper()}")

    # 1. Load Terrain Grids
    print("  [1/4] Loading conditioned DEM, Flow Direction, and Accumulation grids...")
    filled_dem, transform, crs, _ = read_dem_geotiff(cond_dem_path)
    fdir, _, _, _ = read_dem_geotiff(fdir_path)
    acc, _, _, _ = read_dem_geotiff(acc_path)

    # 2. Load Stations
    water_st, rain_st = load_stations_for_basin(basin_dir)
    print(f"  [2/4] Loaded {len(water_st)} water stations and {len(rain_st)} rain stations.")

    # 3. Snap Water Stations to stream channel
    print("        Snapping water level stations to stream channel...")
    snapped_water_st = snap_stations_to_stream(water_st, fdir, acc, transform)
    station_mapping_path = os.path.join(station_dir, "station-mapping.json")
    save_json(snapped_water_st, station_mapping_path)
    print(f"        Saved station mapping: {station_mapping_path}")

    # 4. Build Flow Paths (Gauge-to-Gauge & Rain-to-Gauge)
    print("  [3/4] Tracing Gauge-to-Gauge & Overland Rain-to-Gauge Flow Paths...")
    flow_paths_geojson, gauge_relations, rain_relations = build_flow_paths_and_relations(
        snapped_water_st, rain_st, fdir, acc, filled_dem, transform
    )
    flow_paths_path = os.path.join(processed_dir, "flow_paths.geojson")
    gauge_relations_path = os.path.join(station_dir, "station-relations.json")
    rain_relations_path = os.path.join(station_dir, "rainfall-relations.json")

    save_geojson(flow_paths_geojson, flow_paths_path)
    save_json(gauge_relations, gauge_relations_path)
    save_json(rain_relations, rain_relations_path)

    print(f"        Generated {len(gauge_relations)} Gauge-to-Gauge relations.")
    print(f"        Generated {len(rain_relations)} Rainfall-to-Gauge relations.")
    print(f"        Saved Flow Paths GeoJSON: {flow_paths_path}")

    # 5. Delineate Sub-catchments
    print("  [4/4] Delineating Sub-Catchment Polygons for station outlets...")
    catchments_geojson = delineate_station_catchments(snapped_water_st, fdir, transform)
    catchments_path = os.path.join(catchment_dir, "catchments.geojson")
    save_geojson(catchments_geojson, catchments_path)
    print(f"        Generated {len(catchments_geojson['features'])} catchment polygons.")
    print(f"        Saved Catchments GeoJSON: {catchments_path}")


def main():
    parser = argparse.ArgumentParser(description="Snap stations, generate flow paths, and delineate catchments")
    parser.add_argument("--basin", type=str, default="yom", help="River basin slug (e.g. yom, nan, ping, all)")
    parser.add_argument("--dir", type=str, default="./dataset", help="Dataset directory")
    args = parser.parse_args()

    basin_list = ["yom", "nan", "ping", "wang", "chao-phraya"] if args.basin == "all" else [args.basin]
    for b in basin_list:
        basin_dir = os.path.join(args.dir, b)
        build_basin_station_chain(b, basin_dir)


if __name__ == "__main__":
    main()
