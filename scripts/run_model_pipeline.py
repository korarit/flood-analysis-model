#!/usr/bin/env python3
"""
Master Model Pipeline Runner (22 Basins)
========================================
Orchestrates the entire end-to-end Physics-Informed Hydrological & Machine Learning Pipeline
from automated GIS & FABDEM 30m downloads to Backend & Frontend GeoJSON/JSON exports in a single command.

Steps:
  [Step 1] Automated GIS Boundaries, OSM Waterways/Polygons & FABDEM 30m Ingestion
  [Step 2] 2-Layer Hybrid Flow Paths & Hydro-Enforced Stream Routing (Stream Burning 15m + D8)
  [Step 3] Sub-Catchment Polygon Delineation (from Flow Accumulation Grids)
  [Step 3.5] OSM Vector-Based Waterlevel Topology (Gauge-to-Gauge Relations)
  [Step 4] Travel Time & Hydrological Response Model (4h Rise Detection + ML Ridge / Kinematic Fallback)
  [Step 5] Empirical ML Rainfall Trigger Thresholds (K-Means AMC Soil Regimes + 4 Windows: 3h, 24h, 72h, 168h)
  [Step 6] Backend & Frontend Export Engine & Final Station Map (final_station_data.json, relations_frontend.json, DB Payloads)

Usage:
  # Run full pipeline for Yom basin:
  python scripts/run_model_pipeline.py --basin yom

  # Run for another basin (e.g. nan, ping, chi, mae-klong):
  python scripts/run_model_pipeline.py --basin nan

  # Run full pipeline for all 22 Thai river basins:
  python scripts/run_model_pipeline.py --basin all

  # Force re-generation / re-fetch:
  python scripts/run_model_pipeline.py --basin yom --force
"""

import argparse
import os
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional, Any

# Add parent directory to sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.modules.basin_registry import get_all_slugs, get_basin
from scripts.modules.gis_utils import load_stations_for_basin

from scripts.fetch_basin_gis import (
    fetch_basin_boundary,
    fetch_subbasins_boundary,
    fetch_osm_waterways,
    fetch_osm_water_polygons,
    download_fabdem
)
from scripts.generate_flow_paths import generate_basin_flow_paths
from scripts.generate_catchments import generate_basin_catchments
from scripts.generate_osm_waterlevel_relations import generate_osm_relations
from scripts.train_response_model import run_response_model_pipeline
from scripts.calculate_rainfall_thresholds import calculate_basin_rainfall_thresholds
from scripts.export_backend_dataset import export_basin_model_dataset
from scripts.generate_final_station_data import build_final_station_dataset


def run_pipeline_for_basin(
    basin: str,
    dataset_dir: str = "./dataset",
    terrain_dir: str = "./terrain",
    force: bool = False,
    force_osm: bool = False,
    force_dem: bool = False,
    crop_buffer_m: float = 2000.0,
    burn_depth: float = 15.0,
    skip_fetch: bool = False
) -> bool:
    """Executes the full 6-step pipeline for a single river basin."""
    start_total_time = time.time()

    # Smart path resolution for dataset directory (supports both './dataset' and './dataset/nan')
    if os.path.basename(os.path.normpath(dataset_dir)) == basin:
        basin_dir = dataset_dir
        root_dataset_dir = os.path.dirname(os.path.normpath(dataset_dir))
    else:
        basin_dir = os.path.join(dataset_dir, basin)
        root_dataset_dir = dataset_dir

    # Smart path resolution for terrain directory
    if os.path.basename(os.path.normpath(terrain_dir)) == basin:
        terrain_basin_dir = terrain_dir
    else:
        terrain_basin_dir = os.path.join(terrain_dir, basin)

    os.makedirs(basin_dir, exist_ok=True)
    os.makedirs(terrain_basin_dir, exist_ok=True)

    basin_info = get_basin(basin)
    th_name = basin_info.get("name_th", basin) if basin_info else basin
    en_name = basin_info.get("name_en", basin) if basin_info else basin

    print("\n" + "═" * 78)
    print(f"🚀 [PIPELINE START] Basin: {basin.upper()} ({th_name} / {en_name})")
    print(f"   • Dataset Dir : {basin_dir}")
    print(f"   • Terrain Dir : {terrain_basin_dir}")
    print(f"   • Parameters  : force={force}, burn_depth={burn_depth}m, crop_buffer={crop_buffer_m}m")
    print("═" * 78)

    # Check station metadata
    water_st, rain_st = load_stations_for_basin(basin_dir)
    all_st = water_st + rain_st
    if not all_st:
        print(f"\n❌ ERROR: No stations found in {basin_dir}/station/!", file=sys.stderr)
        print(f"   Please make sure station metadata exists, or run:", file=sys.stderr)
        print(f"     python generate_station_dataset.py --basin {basin}", file=sys.stderr)
        print(f"     python scrape_rid_station_metadata.py --basin {basin}", file=sys.stderr)
        return False

    print(f"  • Found {len(water_st)} water level stations and {len(rain_st)} rainfall stations.")

    # -------------------------------------------------------------
    # Step 1: Automated GIS & FABDEM 30m Bare-Earth DEM Fetcher
    # -------------------------------------------------------------
    t1_elapsed = 0.0
    if not skip_fetch:
        t0 = time.time()
        print(f"\n🌊 [Step 1/6] Fetching GIS Boundaries, OSM Waterways & FABDEM 30m DEM...")
        boundary_path = os.path.join(basin_dir, "gis", f"{basin}_boundary.geojson")
        subbasins_path = os.path.join(basin_dir, "gis", f"{basin}_subbasins.geojson")
        osm_waterways_path = os.path.join(basin_dir, "gis", "osm_waterways.geojson")
        water_polygons_path = os.path.join(basin_dir, "gis", "osm_water_polygons.geojson")

        # 1.1 Basin Boundary (Mandatory)
        fetch_basin_boundary(basin, boundary_path, all_st, force=force)

        # 1.2 Sub-basins Boundary
        fetch_subbasins_boundary(basin, subbasins_path, all_st)

        # 1.3 Load boundary for spatial cropping
        boundary_geojson = None
        if os.path.exists(boundary_path):
            import json
            try:
                with open(boundary_path, 'r', encoding='utf-8') as f:
                    boundary_geojson = json.load(f)
            except Exception:
                boundary_geojson = None

        # 1.4 OSM Waterways & Water Polygons
        fetch_osm_waterways(
            basin, osm_waterways_path, all_st,
            force=(force or force_osm),
            basin_boundary_geojson=boundary_geojson,
            crop_buffer_m=crop_buffer_m
        )
        fetch_osm_water_polygons(
            basin, water_polygons_path, all_st,
            force=(force or force_osm),
            basin_boundary_geojson=boundary_geojson,
            crop_buffer_m=crop_buffer_m
        )

        # 1.5 FABDEM 30m Bare-Earth DEM (AWS Open Data)
        download_fabdem(terrain_basin_dir, all_st, force=(force or force_dem))

        t1_elapsed = time.time() - t0
        print(f"  ⏱️ Step 1 completed in {t1_elapsed:.1f}s")
    else:
        print(f"\n🌊 [Step 1/6] Skipping GIS/DEM fetch (--skip-fetch specified)...")

    # -------------------------------------------------------------
    # Step 2: Station Snapping & 2-Layer Hybrid Flow Paths
    # -------------------------------------------------------------
    t0 = time.time()
    print(f"\n🔗 [Step 2/6] Hydro-Enforcing DEM, Snapping Stations & Tracing 2-Layer Flow Paths...")
    generate_basin_flow_paths(
        basin=basin,
        basin_dir=basin_dir,
        terrain_dir=terrain_basin_dir,
        force=force,
        burn_depth=burn_depth,
        crop_buffer_m=crop_buffer_m
    )
    t2_elapsed = time.time() - t0
    print(f"  ⏱️ Step 2 completed in {t2_elapsed:.1f}s")

    # -------------------------------------------------------------
    # Step 3: Catchment Polygon Delineation
    # -------------------------------------------------------------
    t0 = time.time()
    print(f"\n🏔️ [Step 3/6] Delineating Sub-Catchment Polygons for Stations...")
    generate_basin_catchments(
        basin=basin,
        basin_dir=basin_dir,
        terrain_dir=terrain_basin_dir,
        force=force
    )
    t3_elapsed = time.time() - t0
    print(f"  ⏱️ Step 3 completed in {t3_elapsed:.1f}s")

    # -------------------------------------------------------------
    # Step 3.5: OSM Vector-based Gauge-to-Gauge Topology
    # -------------------------------------------------------------
    t0 = time.time()
    print(f"\n🗺️ [Step 3.5/6] Building pure OSM topological water level relations...")
    generate_osm_relations(
        basin=basin,
        basin_dir=basin_dir,
        terrain_dir=terrain_basin_dir,
        force=force
    )
    t35_elapsed = time.time() - t0
    print(f"  ⏱️ Step 3.5 completed in {t35_elapsed:.1f}s")

    # -------------------------------------------------------------
    # Step 4: Travel Time & Hydrological Response Model
    # -------------------------------------------------------------
    t0 = time.time()
    print(f"\n🤖 [Step 4/6] Detecting 4h Rises, Computing Observed Times, and Training ML Travel Time Model...")
    run_response_model_pipeline(basin=basin, basin_dir=basin_dir)
    t4_elapsed = time.time() - t0
    print(f"  ⏱️ Step 4 completed in {t4_elapsed:.1f}s")

    # -------------------------------------------------------------
    # Step 5: Empirical ML Rainfall Trigger Thresholds (4 Windows)
    # -------------------------------------------------------------
    t0 = time.time()
    print(f"\n🌧️ [Step 5/6] Learning Soil Regimes (AMC) & Calculating Rainfall Trigger Thresholds...")
    calculate_basin_rainfall_thresholds(basin=basin, basin_dir=basin_dir, update_existing=True)
    t5_elapsed = time.time() - t0
    print(f"  ⏱️ Step 5 completed in {t5_elapsed:.1f}s")

    # -------------------------------------------------------------
    # Step 6: Backend & Frontend Export Engine & Final Station Map
    # -------------------------------------------------------------
    t0 = time.time()
    print(f"\n📦 [Step 6/6] Exporting Database Payloads, Frontend Map Layers & Final Station Dataset...")
    export_basin_model_dataset(basin=basin, basin_dir=basin_dir)
    build_final_station_dataset(basin=basin, dataset_dir=Path(root_dataset_dir))
    t6_elapsed = time.time() - t0
    print(f"  ⏱️ Step 6 completed in {t6_elapsed:.1f}s")

    total_elapsed = time.time() - start_total_time
    print("\n" + "═" * 78)
    print(f"✅ [PIPELINE FINISHED] Basin: {basin.upper()} ({th_name}) in {total_elapsed:.1f}s")
    print(f"   • Step 1 (GIS & DEM Fetch)        : {t1_elapsed:.1f}s")
    print(f"   • Step 2 (Flow Paths Engine)      : {t2_elapsed:.1f}s")
    print(f"   • Step 3 (Catchment Delineation)  : {t3_elapsed:.1f}s")
    print(f"   • Step 3.5 (OSM Gauge Topology)   : {t35_elapsed:.1f}s")
    print(f"   • Step 4 (Response Travel Time)   : {t4_elapsed:.1f}s")
    print(f"   • Step 5 (Rain Trigger Engine)    : {t5_elapsed:.1f}s")
    print(f"   • Step 6 (Backend & Final Export) : {t6_elapsed:.1f}s")
    print("═" * 78 + "\n")
    return True


def main():
    parser = argparse.ArgumentParser(
        description="Master Model Pipeline Runner: Automated Water Flow Chain & Travel Time Response Model (22 Basins)"
    )
    parser.add_argument(
        "--basin", type=str, default="yom",
        help="Target river basin slug (e.g. yom, nan, ping, wang, chi, mun, mae-klong, chao-phraya, or all)"
    )
    parser.add_argument("--dir", type=str, default="./dataset", help="Root dataset directory")
    parser.add_argument("--terrain-dir", type=str, default="./terrain", help="Terrain DEM directory (independent of --dir)")
    parser.add_argument("--burn-depth", type=float, default=15.0, help="Stream channel burn depth in meters (default: 15.0)")
    parser.add_argument("--crop-buffer-m", type=float, default=2000.0, help="Buffer in meters for OSM data boundary cropping (default: 2000.0)")
    parser.add_argument("--force", action="store_true", help="Force re-generation of all steps and caches")
    parser.add_argument("--force-osm", action="store_true", help="Force re-download of OSM waterways/polygons only")
    parser.add_argument("--force-dem", action="store_true", help="Force re-download of FABDEM 30m DEM")
    parser.add_argument("--skip-fetch", action="store_true", help="Skip Step 1 (GIS/DEM download) if data is already fetched")
    args = parser.parse_args()

    basin_list = get_all_slugs() if args.basin.lower() == "all" else [args.basin.lower()]

    success_count = 0
    total_basins = len(basin_list)

    for idx, b in enumerate(basin_list, 1):
        print(f"\n[{idx}/{total_basins}] Running Pipeline for Basin: {b.upper()}")
        ok = run_pipeline_for_basin(
            basin=b,
            dataset_dir=args.dir,
            terrain_dir=args.terrain_dir,
            force=args.force,
            force_osm=args.force_osm,
            force_dem=args.force_dem,
            crop_buffer_m=args.crop_buffer_m,
            burn_depth=args.burn_depth,
            skip_fetch=args.skip_fetch
        )
        if ok:
            success_count += 1

    print("═" * 78)
    print(f"🎉 [ALL BASINS COMPLETE] Successfully processed {success_count}/{total_basins} basin(s).")
    print("═" * 78)


if __name__ == "__main__":
    main()
