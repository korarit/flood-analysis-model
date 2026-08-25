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
import json
from shapely.geometry import shape

from scripts.modules.terrain_engine import (
    clip_dem_to_polygon,
    fill_depressions_priority_flood,
    compute_d8_flow_direction,
    compute_flow_accumulation,
    extract_river_network_reaches,
    save_geotiff_raster
)
from scripts.modules.graph_topology import detect_confluences
from scripts.fetch_basin_gis import fetch_subbasins_boundary, load_stations_for_basin


def process_basin_terrain(basin: str, basin_dir: str, terrain_dir: str, stream_threshold: int = 300):
    """
    Processes DEM using Sub-basin Topological Cascade at native 12.5m resolution.
    Runs each sub-basin in order with minimal RAM footprint (~200-400 MB),
    extracting high-resolution river reaches, slope profiles, and confluences.
    """
    gis_dir = os.path.join(basin_dir, "gis")
    river_dir = os.path.join(basin_dir, "river")
    processed_dir = os.path.join(basin_dir, "processed")
    os.makedirs(river_dir, exist_ok=True)
    os.makedirs(processed_dir, exist_ok=True)
    os.makedirs(terrain_dir, exist_ok=True)

    raw_dem_path = os.path.join(terrain_dir, "raw_dem.tif")
    if not os.path.exists(raw_dem_path):
        print(f"❌ ERROR: raw_dem.tif not found in {terrain_dir}. Please run fetch_basin_gis.py first!", file=sys.stderr)
        sys.exit(1)

    # 1. Load Sub-basins (Topological Order 1 -> N)
    subbasins_path = os.path.join(gis_dir, f"{basin}_subbasins.geojson")
    if not os.path.exists(subbasins_path):
        water_st, rain_st = load_stations_for_basin(basin_dir)
        subbasins_geojson = fetch_subbasins_boundary(basin, subbasins_path, water_st + rain_st)
    else:
        with open(subbasins_path, 'r', encoding='utf-8') as f:
            subbasins_geojson = json.load(f)

    subbasin_features = sorted(
        subbasins_geojson['features'],
        key=lambda x: x['properties'].get('order', 1)
    )

    import time
    try:
        from tqdm import tqdm
    except ImportError:
        def tqdm(iterable, **kwargs):
            return iterable

    print(f"\n🏔️ [STEP 2] Sub-basin Cascade Engine (Native 12.5m DEM) for Basin: {basin.upper()}")
    print(f"        Processing {len(subbasin_features)} sub-basins from headwaters to outlet...")

    all_river_features = []
    all_river_segments = []
    all_confluences = []
    reach_counter = 0

    subbasin_pbar = tqdm(subbasin_features, desc="Total Sub-basin Progress", unit="subbasin", ncols=85)

    for idx, sub_feat in enumerate(subbasin_pbar, 1):
        t_sub_start = time.time()
        props = sub_feat['properties']
        sub_id = props['subbasin_id']
        sub_name = props['subbasin_name_th']
        sub_order = props.get('order', idx)
        print(f"\n  ┌─ [{idx}/{len(subbasin_features)}] Sub-basin ({sub_order}): {sub_name}")

        # 2. Clip native 12.5m DEM for this sub-basin
        t0 = time.time()
        print(f"  │  [1/4] Clipping 12.5m DEM for sub-basin...")
        sub_elev, sub_transform, crs, nodata = clip_dem_to_polygon(
            raw_dem_path,
            sub_feat['geometry'],
            buffer_deg=0.015
        )
        print(f"  │        Grid shape: {sub_elev.shape[0]:,} rows x {sub_elev.shape[1]:,} cols ({sub_elev.size:,} cells, {time.time()-t0:.1f}s)")

        # 3. Flow Routing & Accumulation in C
        t0 = time.time()
        print("  │  [2/4] Running C-accelerated Pit-filling & D8 Flow Routing...")
        filled_dem, flw_obj = fill_depressions_priority_flood(sub_elev, transform=sub_transform, crs=crs, nodata=nodata)
        fdir = compute_d8_flow_direction(filled_dem, sub_transform, flw_obj=flw_obj, nodata=nodata)
        acc = compute_flow_accumulation(fdir, flw_obj=flw_obj)
        print(f"  │        Flow tree & accumulation computed in {time.time()-t0:.1f}s")

        # 4. Extract River Reaches at 12.5m resolution
        t0 = time.time()
        print(f"  │  [3/4] Extracting river reaches (Threshold >= {stream_threshold} cells)...")
        sub_river_geojson, sub_segments = extract_river_network_reaches(
            filled_dem, fdir, acc, sub_transform, crs=crs, min_stream_acc_cells=stream_threshold
        )
        
        # Tag reach features with subbasin_id
        for feat in sub_river_geojson['features']:
            reach_counter += 1
            feat['id'] = f"REACH_{reach_counter:05d}"
            feat['properties']['reach_id'] = feat['id']
            feat['properties']['subbasin_id'] = sub_id
            feat['properties']['subbasin_name'] = sub_name
            all_river_features.append(feat)

        for seg in sub_segments:
            seg['subbasin_id'] = sub_id
            all_river_segments.append(seg)
        print(f"  │        Extracted {len(sub_segments)} reaches in {time.time()-t0:.1f}s")

        # 5. Detect Confluences
        t0 = time.time()
        print(f"  │  [4/4] Detecting Confluences...")
        sub_confluences = detect_confluences(fdir, acc, sub_transform, crs=crs, min_acc_cells=stream_threshold)
        for conf in sub_confluences['features']:
            conf['properties']['subbasin_id'] = sub_id
            all_confluences.append(conf)
        print(f"  │        Found {len(sub_confluences['features'])} junctions in {time.time()-t0:.1f}s")

        sub_elapsed = time.time() - t_sub_start
        print(f"  └─ ✅ Sub-basin ({sub_order}) finished in {sub_elapsed:.1f}s")

    # 6. Save Merged Native 12.5m River Network
    merged_river_geojson = {
        "type": "FeatureCollection",
        "features": all_river_features
    }
    merged_confluences_geojson = {
        "type": "FeatureCollection",
        "features": all_confluences
    }

    river_geojson_path = os.path.join(river_dir, "river_network.geojson")
    river_segments_path = os.path.join(river_dir, "river_segments.json")
    confluences_path = os.path.join(river_dir, "confluences.geojson")
    processed_river_path = os.path.join(processed_dir, "river_network.geojson")

    save_geojson(merged_river_geojson, river_geojson_path)
    save_geojson(merged_river_geojson, processed_river_path)
    save_json(all_river_segments, river_segments_path)
    save_geojson(merged_confluences_geojson, confluences_path)

    print("\n" + "═" * 70)
    print(f"  ✅ [SUCCESS] Native 12.5m River Network Extracted:")
    print(f"     • Total River Reaches : {len(all_river_features)} segments")
    print(f"     • Total Confluences   : {len(all_confluences)} junctions")
    print(f"     • Saved: {river_geojson_path}")
    print(f"     • Saved: {processed_river_path}")
    print("═" * 70)


def main():
    parser = argparse.ArgumentParser(description="Process DEM using Sub-basin Cascade to extract 12.5m river lines")
    parser.add_argument("--basin", type=str, default="yom", help="River basin slug (e.g. yom, nan, ping, wang, all)")
    parser.add_argument("--dir", type=str, default="./dataset", help="Dataset directory")
    parser.add_argument("--terrain-dir", type=str, default="./terrain", help="Terrain DEM directory (independent of dataset --dir)")
    parser.add_argument("--threshold", type=int, default=300, help="Stream accumulation threshold in cells")
    args = parser.parse_args()

    basin_list = ["yom", "nan", "ping", "wang", "chao-phraya"] if args.basin == "all" else [args.basin]
    for b in basin_list:
        basin_dir = os.path.join(args.dir, b)
        terrain_basin_dir = os.path.join(args.terrain_dir, b)
        process_basin_terrain(b, basin_dir, terrain_basin_dir, stream_threshold=args.threshold)


if __name__ == "__main__":
    main()
