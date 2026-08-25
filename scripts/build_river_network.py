#!/usr/bin/env python3
"""
Step 2: Terrain Engine & River Network Extraction
Conditions DEM, computes D8 Flow Direction, Flow Accumulation,
extracts River Network Reaches with slopes, and detects Confluences.
"""

import argparse
import os
import sys
from typing import Dict, Any

# Add parent directory to sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from scripts.modules.gis_utils import save_geojson, save_json
from scripts.modules.terrain_engine import (
    read_dem_geotiff,
    fill_depressions_priority_flood,
    compute_d8_flow_direction,
    compute_flow_accumulation,
    extract_river_network_reaches,
    save_geotiff_raster
)
from scripts.modules.graph_topology import detect_confluences


def process_basin_terrain(basin: str, basin_dir: str, stream_threshold: int = 300):
    """Processes DEM to extract river reaches, slope profiles, and confluences."""
    terrain_dir = os.path.join(basin_dir, "terrain")
    river_dir = os.path.join(basin_dir, "river")
    os.makedirs(river_dir, exist_ok=True)

    raw_dem_path = os.path.join(terrain_dir, "raw_dem.tif")
    if not os.path.exists(raw_dem_path):
        print(f"❌ ERROR: raw_dem.tif not found in {terrain_dir}. Please run 01_fetch_basin_gis.py first!", file=sys.stderr)
        sys.exit(1)

    print(f"\n🏔️ [STEP 2] Processing Terrain & River Network for Basin: {basin.upper()}")
    
    # 1. Read DEM
    print("  [1/5] Reading raw DEM GeoTIFF...")
    elev, transform, crs, nodata = read_dem_geotiff(raw_dem_path)
    print(f"        Grid shape: {elev.shape[0]} rows x {elev.shape[1]} cols (Total {elev.size:,} cells)")

    # 2. Pit Filling / Conditioning
    cond_dem_path = os.path.join(terrain_dir, "conditioned_dem.tif")
    if os.path.exists(cond_dem_path):
        print("  [2/5] [CACHE] Loading existing conditioned_dem.tif...")
        filled_dem, _, _, _ = read_dem_geotiff(cond_dem_path)
    else:
        print("  [2/5] Running Priority-Flood hydrological pit filling...")
        filled_dem = fill_depressions_priority_flood(elev, nodata=nodata)
        save_geotiff_raster(filled_dem, transform, crs, cond_dem_path, nodata=nodata)
        print(f"        Saved conditioned DEM: {cond_dem_path}")

    # 3. D8 Flow Direction
    fdir_path = os.path.join(terrain_dir, "flow_direction.tif")
    if os.path.exists(fdir_path):
        print("  [3/5] [CACHE] Loading existing flow_direction.tif...")
        fdir, _, _, _ = read_dem_geotiff(fdir_path)
    else:
        print("  [3/5] Computing D8 Flow Direction grid...")
        fdir = compute_d8_flow_direction(filled_dem, transform, nodata=nodata)
        save_geotiff_raster(fdir, transform, crs, fdir_path, nodata=0)
        print(f"        Saved D8 flow direction: {fdir_path}")

    # 4. Flow Accumulation
    acc_path = os.path.join(terrain_dir, "flow_accumulation.tif")
    if os.path.exists(acc_path):
        print("  [4/5] [CACHE] Loading existing flow_accumulation.tif...")
        acc, _, _, _ = read_dem_geotiff(acc_path)
    else:
        print("  [4/5] Computing Flow Accumulation grid (upstream cell counts)...")
        acc = compute_flow_accumulation(fdir)
        save_geotiff_raster(acc, transform, crs, acc_path, nodata=0)
        print(f"        Saved flow accumulation: {acc_path}")

    # 5. Extract River Network & Confluences
    print(f"  [5/5] Extracting vectorized river reaches (Stream threshold >= {stream_threshold} cells)...")
    river_geojson, river_segments = extract_river_network_reaches(
        filled_dem, fdir, acc, transform, min_stream_acc_cells=stream_threshold
    )
    river_geojson_path = os.path.join(river_dir, "river_network.geojson")
    river_segments_path = os.path.join(river_dir, "river_segments.json")
    save_geojson(river_geojson, river_geojson_path)
    save_json(river_segments, river_segments_path)
    print(f"        Extracted {len(river_segments)} river reach segments.")
    print(f"        Saved: {river_geojson_path}")

    # 6. Confluence Junctions
    print("  [+] Detecting River Confluences (In-degree >= 2)...")
    confluences_geojson = detect_confluences(fdir, acc, transform, min_acc_cells=stream_threshold)
    confluences_path = os.path.join(river_dir, "confluences.geojson")
    save_geojson(confluences_geojson, confluences_path)
    print(f"        Found {len(confluences_geojson['features'])} river junctions/confluences.")
    print(f"        Saved: {confluences_path}")


def main():
    parser = argparse.ArgumentParser(description="Process DEM to extract flow network and river lines")
    parser.add_argument("--basin", type=str, default="yom", help="River basin slug (e.g. yom, nan, ping, all)")
    parser.add_argument("--dir", type=str, default="./dataset", help="Dataset directory")
    parser.add_argument("--threshold", type=int, default=300, help="Stream accumulation threshold in cells")
    args = parser.parse_args()

    basin_list = ["yom", "nan", "ping", "wang", "chao-phraya"] if args.basin == "all" else [args.basin]
    for b in basin_list:
        basin_dir = os.path.join(args.dir, b)
        process_basin_terrain(b, basin_dir, stream_threshold=args.threshold)


if __name__ == "__main__":
    main()
