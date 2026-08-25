#!/usr/bin/env python3
"""
Master Model Pipeline Runner
Orchestrates the entire Water Flow Chain & Travel Time Response Model
from automated GIS/DEM downloads to Backend & Frontend GeoJSON exports in a single command.
"""

import argparse
import os
import sys
import time

# Add parent directory to sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.modules.gis_utils import load_stations_for_basin
from scripts.fetch_basin_gis import (
    fetch_basin_boundary,
    download_alos_palsar_dem
)
from scripts.build_river_network import process_basin_terrain
from scripts.build_station_chain import build_basin_station_chain
from scripts.train_response_model import run_response_model_pipeline
from scripts.export_backend_dataset import export_basin_model_dataset


def run_pipeline_for_basin(
    basin: str,
    dataset_dir: str,
    username: str = None,
    password: str = None,
    stream_threshold: int = 300
):
    """Runs all 5 pipeline steps for a single river basin."""
    basin_dir = os.path.join(dataset_dir, basin)
    start_total_time = time.time()

    print("\n" + "═" * 70)
    print(f"🚀 [PIPELINE START] River Basin: {basin.upper()}")
    print("═" * 70)

    # -------------------------------------------------------------
    # Step 1: Automated GIS & ALOS PALSAR 12.5m DEM Fetcher
    # -------------------------------------------------------------
    t0 = time.time()
    print(f"\n[Step 1/5] Fetching GIS Boundaries & ALOS PALSAR 12.5m DEM...")
    water_st, rain_st = load_stations_for_basin(basin_dir)
    all_st = water_st + rain_st
    if not all_st:
        print(f"❌ ERROR: No stations found in {basin_dir}/station/", file=sys.stderr)
        return

    boundary_path = os.path.join(basin_dir, "gis", f"{basin}_boundary.geojson")
    fetch_basin_boundary(basin, boundary_path, all_st)
    download_alos_palsar_dem(basin_dir, all_st, username, password)
    t1_elapsed = time.time() - t0
    print(f"  ⏱️ Step 1 completed in {t1_elapsed:.1f}s")

    # -------------------------------------------------------------
    # Step 2: Terrain Engine & River Network Extraction
    # -------------------------------------------------------------
    t0 = time.time()
    print(f"\n[Step 2/5] Conditioning DEM, Computing D8 Flow, and Extracting River Lines...")
    process_basin_terrain(basin, basin_dir, stream_threshold=stream_threshold)
    t2_elapsed = time.time() - t0
    print(f"  ⏱️ Step 2 completed in {t2_elapsed:.1f}s")

    # -------------------------------------------------------------
    # Step 3: Station Snapping & Directed River Chain
    # -------------------------------------------------------------
    t0 = time.time()
    print(f"\n[Step 3/5] Snapping Stations, Tracing Flow Paths, and Catchment Delineation...")
    build_basin_station_chain(basin, basin_dir)
    t3_elapsed = time.time() - t0
    print(f"  ⏱️ Step 3 completed in {t3_elapsed:.1f}s")

    # -------------------------------------------------------------
    # Step 4: Travel Time & Hydrological Response Model
    # -------------------------------------------------------------
    t0 = time.time()
    print(f"\n[Step 4/5] Detecting 4h Rises, Computing Observed Times, and Training ML Model...")
    run_response_model_pipeline(basin, basin_dir)
    t4_elapsed = time.time() - t0
    print(f"  ⏱️ Step 4 completed in {t4_elapsed:.1f}s")

    # -------------------------------------------------------------
    # Step 5: Backend & Frontend Export Engine
    # -------------------------------------------------------------
    t0 = time.time()
    print(f"\n[Step 5/5] Exporting Database Schema and Map GeoJSON Layers...")
    export_basin_model_dataset(basin, basin_dir)
    t5_elapsed = time.time() - t0
    print(f"  ⏱️ Step 5 completed in {t5_elapsed:.1f}s")

    total_elapsed = time.time() - start_total_time
    print("\n" + "═" * 70)
    print(f"✅ [PIPELINE FINISHED] Basin: {basin.upper()} in {total_elapsed:.1f}s")
    print(f"   • Step 1 (GIS/DEM Fetch)    : {t1_elapsed:.1f}s")
    print(f"   • Step 2 (Terrain/Rivers)   : {t2_elapsed:.1f}s")
    print(f"   • Step 3 (Station Chain)    : {t3_elapsed:.1f}s")
    print(f"   • Step 4 (Response Model)   : {t4_elapsed:.1f}s")
    print(f"   • Step 5 (Backend Export)   : {t5_elapsed:.1f}s")
    print("═" * 70 + "\n")


def main():
    parser = argparse.ArgumentParser(description="Automated Water Flow Chain & Travel Time Response Model Pipeline")
    parser.add_argument("--basin", type=str, default="yom", help="Target river basin (yom, nan, ping, wang, chao-phraya, all)")
    parser.add_argument("--dir", type=str, default="./dataset", help="Root dataset directory")
    parser.add_argument("--username", "-u", type=str, default=None, help="NASA Earthdata username for ALOS PALSAR 12.5m DEM")
    parser.add_argument("--password", "-p", type=str, default=None, help="NASA Earthdata password for ALOS PALSAR 12.5m DEM")
    parser.add_argument("--threshold", type=int, default=300, help="Stream delineation cell accumulation threshold")
    args = parser.parse_args()

    basin_list = ["yom", "nan", "ping", "wang", "chao-phraya"] if args.basin == "all" else [args.basin]
    
    for b in basin_list:
        run_pipeline_for_basin(
            basin=b,
            dataset_dir=args.dir,
            username=args.username,
            password=args.password,
            stream_threshold=args.threshold
        )


if __name__ == "__main__":
    main()
