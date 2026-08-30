import os
import sys
import json
import argparse
import numpy as np

# Adjust sys.path to allow importing modules when running from anywhere
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from scripts.modules.gis_fetcher import load_stations_for_basin, save_geojson
from scripts.modules.terrain_engine import read_dem_geotiff
from scripts.modules.graph_topology import snap_stations_to_stream, delineate_station_catchments


def generate_basin_catchments(
    basin: str,
    basin_dir: str,
    terrain_dir: str,
    force: bool = False
):
    print(f"\n==================================================================")
    print(f"🌊 Generating Catchment Polygons for Basin: {basin.upper()}")
    print(f"==================================================================")

    fdir_path = os.path.join(terrain_dir, "fdir.tif")
    acc_path = os.path.join(terrain_dir, "acc.tif")
    cond_dem_path = os.path.join(terrain_dir, "cond_dem.tif")
    
    catchments_path = os.path.join(basin_dir, "gis", "catchments.geojson")
    processed_catchments_path = os.path.join(basin_dir, "processed", "catchments.geojson")
    
    if not force and os.path.exists(catchments_path) and os.path.getsize(catchments_path) > 100:
        print(f"  [CACHE] Catchments GeoJSON already exists: {catchments_path}")
        print("          Use --force to regenerate.")
        return

    # 1. Load Stations & OSM Waterways
    print(f"  [1/3] Loading water stations and OSM waterways...")
    water_st, rain_st = load_stations_for_basin(basin_dir)
    
    if not water_st:
        print("  ❌ ERROR: No water stations found in dataset.", file=sys.stderr)
        return

    osm_waterways_path = os.path.join(basin_dir, "gis", "osm_waterways.geojson")
    osm_waterways = None
    if os.path.exists(osm_waterways_path) and os.path.getsize(osm_waterways_path) > 100:
        with open(osm_waterways_path, 'r', encoding='utf-8') as f:
            osm_waterways = json.load(f)

    # 2. Load Cached Flow Rasters (fdir, acc)
    print(f"  [2/3] Loading cached flow direction and accumulation matrices...")
    if not (os.path.exists(fdir_path) and os.path.exists(acc_path)):
        print(f"  ❌ ERROR: Cached flow rasters not found in {terrain_dir}.", file=sys.stderr)
        print("          Please run `python scripts/generate_flow_paths.py` first to build them!", file=sys.stderr)
        return

    fdir, transform, crs, nodata = read_dem_geotiff(fdir_path)
    acc, _, _, _ = read_dem_geotiff(acc_path)

    # 3. Snap Stations & Delineate Catchments
    print(f"  [3/3] Snapping stations and delineating sub-catchments...")
    
    # Snap stations to the deepest flow accumulation channel
    snapped_water_st = snap_stations_to_stream(
        water_st, fdir, acc, transform, osm_waterways_geojson=osm_waterways, crs=crs
    )
    
    # Generate the catchment polygons
    catchments_geojson = delineate_station_catchments(snapped_water_st, fdir, transform, crs=crs)
    
    # Save the output
    os.makedirs(os.path.dirname(catchments_path), exist_ok=True)
    save_geojson(catchments_geojson, catchments_path)
    
    os.makedirs(os.path.dirname(processed_catchments_path), exist_ok=True)
    save_geojson(catchments_geojson, processed_catchments_path)
    
    print(f"        Generated {len(catchments_geojson.get('features', []))} catchment polygons.")
    print(f"        ✅ Saved successfully to: {catchments_path}")


def main():
    parser = argparse.ArgumentParser(description="Generate Catchment Polygons from flow direction grids")
    parser.add_argument("--basin", type=str, default="nan", help="River basin slug (e.g. yom, nan, ping, all)")
    parser.add_argument("--dir", type=str, default="./dataset", help="Dataset directory")
    parser.add_argument("--terrain-dir", type=str, default="./terrain", help="Terrain DEM directory")
    parser.add_argument("--force", action="store_true", help="Force re-generation of catchments")
    args = parser.parse_args()

    basin_list = ["yom", "nan", "ping", "wang", "chao-phraya"] if args.basin == "all" else [args.basin]

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
            if not os.path.exists(terrain_basin_dir) and os.path.exists(args.terrain_dir):
                terrain_basin_dir = args.terrain_dir

        if not os.path.exists(basin_dir):
            print(f"❌ ERROR: Basin directory not found: {basin_dir} (Check --dir path)", file=sys.stderr)
            continue

        generate_basin_catchments(
            basin=b,
            basin_dir=basin_dir,
            terrain_dir=terrain_basin_dir,
            force=args.force
        )


if __name__ == "__main__":
    main()
