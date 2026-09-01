#!/usr/bin/env python3
"""
Step 3: Station Snapping & Directed River Chain (2-Layer Hybrid Flow Paths)
==========================================================================
Snaps stations to stream channel, traces Gauge-to-Gauge (OSM Backbone) and
Rain-to-Gauge (Overland + Drainage Branches) flow paths, and delineates sub-catchment polygons.
"""

import argparse
import os
import sys
from typing import Dict, List, Any, Optional

# Add parent directory to sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from scripts.modules.basin_registry import get_all_slugs, get_basin
from scripts.generate_flow_paths import generate_basin_flow_paths
from scripts.generate_catchments import generate_basin_catchments


def build_basin_station_chain(
    basin: str,
    basin_dir: str,
    terrain_dir: str,
    force: bool = False,
    burn_depth: float = 15.0,
    crop_buffer_m: float = 2000.0
):
    """
    Snaps stations, generates 2-Layer hybrid flow paths, and delineates catchments.
    Delegates to the modern stream-burning and geodesic flat-breaking flow path engine.
    """
    print(f"\n🔗 [STEP 3] Executing Station Chain, Hybrid Flow Paths & Catchments: {basin.upper()}")
    
    # 1. Generate 2-Layer Hybrid Flow Paths & Station Relations
    generate_basin_flow_paths(
        basin=basin,
        basin_dir=basin_dir,
        terrain_dir=terrain_dir,
        force=force,
        burn_depth=burn_depth,
        crop_buffer_m=crop_buffer_m
    )

    # 2. Delineate Sub-Catchment Polygons
    generate_basin_catchments(
        basin=basin,
        basin_dir=basin_dir,
        terrain_dir=terrain_dir,
        force=force
    )


def main():
    parser = argparse.ArgumentParser(description="Snap stations, generate flow paths, and delineate catchments")
    parser.add_argument("--basin", type=str, default="yom", help="River basin slug (e.g. yom, nan, ping, wang, all)")
    parser.add_argument("--dir", type=str, default="./dataset", help="Dataset directory")
    parser.add_argument("--terrain-dir", type=str, default="./terrain", help="Terrain DEM directory (independent of dataset --dir)")
    parser.add_argument("--force", action="store_true", help="Force re-generation of flow paths and catchments")
    parser.add_argument("--burn-depth", type=float, default=15.0, help="Stream channel burn depth in meters (default: 15.0)")
    parser.add_argument("--crop-buffer-m", type=float, default=2000.0, help="OSM cropping buffer in meters (default: 2000.0)")
    args = parser.parse_args()

    basin_list = get_all_slugs() if args.basin.lower() == "all" else [args.basin.lower()]

    for b in basin_list:
        # Smart path resolution for --dir
        if os.path.basename(os.path.normpath(args.dir)) == b:
            basin_dir = args.dir
        else:
            basin_dir = os.path.join(args.dir, b)

        # Smart path resolution for --terrain-dir
        if os.path.basename(os.path.normpath(args.terrain_dir)) == b:
            terrain_basin_dir = args.terrain_dir
        else:
            terrain_basin_dir = os.path.join(args.terrain_dir, b)

        build_basin_station_chain(
            basin=b,
            basin_dir=basin_dir,
            terrain_dir=terrain_basin_dir,
            force=args.force,
            burn_depth=args.burn_depth,
            crop_buffer_m=args.crop_buffer_m
        )


if __name__ == "__main__":
    main()
