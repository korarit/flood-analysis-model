#!/usr/bin/env python3
"""
Standalone Flow Path & River Topology Generator
Generates and updates flow_paths.geojson, station-relations.json, and rainfall-relations.json
using Hybrid OpenStreetMap (OSM) Waterway Vector Topology + Hydro-Enforced D8 Hydrology.

Usage:
  python scripts/generate_flow_paths.py --basin yom --force
"""

import argparse
import json
import os
import sys
import time
from typing import Dict, List, Any

# Add parent directory to sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from scripts.modules.gis_utils import save_geojson, save_json, load_stations_for_basin
from scripts.modules.terrain_engine import read_dem_geotiff, burn_stream_network_into_dem
from scripts.modules.graph_topology import (
    snap_stations_to_stream,
    build_flow_paths_and_relations,
    delineate_station_catchments
)
from scripts.fetch_basin_gis import fetch_osm_waterways


def generate_basin_flow_paths(
    basin: str,
    basin_dir: str,
    terrain_dir: str,
    force: bool = False,
    burn_depth: float = 15.0
):
    """
    Generates high-precision hybrid flow paths and station relations for a river basin.
    """
    t_start = time.time()
    station_dir = os.path.join(basin_dir, "station")
    gis_dir = os.path.join(basin_dir, "gis")
    processed_dir = os.path.join(basin_dir, "processed")
    os.makedirs(station_dir, exist_ok=True)
    os.makedirs(gis_dir, exist_ok=True)
    os.makedirs(processed_dir, exist_ok=True)

    station_mapping_path = os.path.join(station_dir, "station-mapping.json")
    flow_paths_path = os.path.join(processed_dir, "flow_paths.geojson")
    gauge_relations_path = os.path.join(station_dir, "station-relations.json")
    rain_relations_path = os.path.join(station_dir, "rainfall-relations.json")
    osm_waterways_path = os.path.join(gis_dir, "osm_waterways.geojson")
    relations_frontend_path = os.path.join(processed_dir, "relations_frontend.json")
    final_station_data_path = os.path.join(basin_dir, "final_station_data.json")

    print(f"\n🌊 [FLOW PATHS] Generating Hybrid Flow Paths for Basin: {basin.upper()}")

    # 1. Load Stations
    water_st, rain_st = load_stations_for_basin(basin_dir)
    print(f"  [1/5] Loaded {len(water_st)} water stations and {len(rain_st)} rain stations.")
    if not water_st:
        print(f"  ❌ ERROR: No water level stations found in {station_dir}!", file=sys.stderr)
        return

    # 2. Fetch or Load OpenStreetMap Waterways
    print("  [2/5] Loading OpenStreetMap River Network...")
    osm_waterways = fetch_osm_waterways(basin, osm_waterways_path, water_st + rain_st, force=force)
    n_osm = len(osm_waterways.get("features", []))
    print(f"        Loaded {n_osm:,} OSM river/stream features.")

    # 3. Load or Condition DEM Raster
    raw_dem_path = os.path.join(terrain_dir, "raw_dem.tif")
    cond_dem_path = os.path.join(terrain_dir, "conditioned_dem.tif")
    fdir_path = os.path.join(terrain_dir, "flow_direction.tif")
    acc_path = os.path.join(terrain_dir, "flow_accumulation.tif")

    dem_to_use = cond_dem_path if (os.path.exists(cond_dem_path) and os.path.getsize(cond_dem_path) > 1024) else raw_dem_path

    if not os.path.exists(dem_to_use):
        print(f"  ❌ ERROR: DEM not found in {terrain_dir}. Please run fetch_basin_gis.py first!", file=sys.stderr)
        return

    print("  [3/5] Loading DEM & Hydro-Enforcing OSM River Channels...")
    filled_dem, transform, crs, nodata = read_dem_geotiff(dem_to_use)

    # Apply Stream Burning to DEM if OSM is available and not already cached
    if n_osm > 0:
        filled_dem = burn_stream_network_into_dem(
            filled_dem, transform, osm_waterways, crs=crs, burn_depth_m=burn_depth, nodata=nodata
        )

    # Compute Flow Direction & Accumulation
    import pyflwdir
    is_latlon = (crs is None) or getattr(crs, 'is_geographic', False) or (str(crs) == "EPSG:4326")
    flw = pyflwdir.from_dem(filled_dem, nodata=nodata, transform=transform, latlon=is_latlon)
    fdir = flw.to_array(ftype='d8')
    acc = flw.upstream_area(unit='cell')

    # 4. Snap Stations to OSM Rivers and Stream Channel
    print("  [4/5] Snapping stations to OSM River Channels...")
    snapped_water_st = snap_stations_to_stream(
        water_st, fdir, acc, transform, osm_waterways_geojson=osm_waterways, crs=crs
    )
    save_json(snapped_water_st, station_mapping_path)

    # 5. Build Hybrid Flow Paths (Gauge-to-Gauge & Rain-to-Gauge)
    print("  [5/5] Tracing Hybrid Flow Paths (OSM Rivers + D8 Hydrology)...")
    flow_paths_geojson, gauge_relations, rain_relations = build_flow_paths_and_relations(
        snapped_water_st, rain_st, fdir, acc, filled_dem, transform,
        osm_waterways_geojson=osm_waterways, crs=crs
    )

    save_geojson(flow_paths_geojson, flow_paths_path)
    save_json(gauge_relations, gauge_relations_path)
    save_json(rain_relations, rain_relations_path)

    # Export frontend relations format
    frontend_relations = {
        "basin": basin,
        "gauge_to_gauge": gauge_relations,
        "rainfall_to_gauge": rain_relations,
        "total_gauge_relations": len(gauge_relations),
        "total_rainfall_relations": len(rain_relations),
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    }
    save_json(frontend_relations, relations_frontend_path)

    # Sync into final_station_data.json if exists
    if os.path.exists(final_station_data_path):
        try:
            with open(final_station_data_path, 'r', encoding='utf-8') as f:
                final_data = json.load(f)

            # Build lookup of rainfall influences per target water station
            rain_influences: Dict[str, List[Dict[str, Any]]] = {}
            for rr in rain_relations:
                to_id = str(rr.get('to_station_id', '')).strip()
                if to_id:
                    rain_influences.setdefault(to_id, []).append({
                        "stationId": rr.get("from_station_id"),
                        "stationName": rr.get("from_station_name"),
                        "stationType": "rainfall",
                        "distanceKm": rr.get("total_distance_km"),
                        "travelTimeMinutes": rr.get("response_lag_minutes"),
                        "travelTimeMinutesMin": rr.get("response_lag_minutes_min"),
                        "travelTimeMinutesMax": rr.get("response_lag_minutes_max"),
                        "travelTimeHours": rr.get("response_lag_hours"),
                        "travelTimeHoursMin": rr.get("response_lag_hours_min"),
                        "travelTimeHoursMax": rr.get("response_lag_hours_max"),
                        "elevationDiffM": rr.get("elevation_diff_m"),
                        "slope": rr.get("slope"),
                        "influenceWeightPercent": rr.get("influence_weight_percent")
                    })

            # Update final_station_data entries
            for st_id, st_obj in final_data.items():
                if st_id in rain_influences:
                    st_obj["influencingStations"] = rain_influences[st_id]

            save_json(final_data, final_station_data_path)
            print(f"        Synced relations into: {final_station_data_path}")
        except Exception as ex:
            print(f"  [WARN] Could not sync final_station_data.json: {ex}")

    # Free memory buffers immediately
    import gc
    del filled_dem, flw, fdir, acc
    gc.collect()

    elapsed = time.time() - t_start
    print(f"\n✅ [DONE] Generated {len(flow_paths_geojson['features'])} Flow Paths in {elapsed:.1f}s:")
    print(f"        • Flow Paths GeoJSON : {flow_paths_path}")
    print(f"        • Gauge Relations    : {len(gauge_relations)} relations")
    print(f"        • Rainfall Relations : {len(rain_relations)} relations")
    print(f"        • Relations Frontend : {relations_frontend_path}")


def main():
    parser = argparse.ArgumentParser(
        description="Standalone Generator for Flow Paths & River Relations (Hybrid OSM + D8 Hydrology)"
    )
    parser.add_argument("--basin", type=str, default="yom", help="River basin slug (e.g. yom, nan, ping, wang, all)")
    parser.add_argument("--dir", type=str, default="./dataset", help="Dataset root directory (e.g. ./dataset or ./dataset/yom)")
    parser.add_argument("--terrain-dir", type=str, default="./terrain", help="Terrain DEM directory (independent of --dir)")
    parser.add_argument("--force", action="store_true", help="Force re-generation of flow paths")
    parser.add_argument("--burn-depth", type=float, default=15.0, help="Stream burn depth in meters (default: 15.0)")
    args = parser.parse_args()

    basin_list = ["yom", "nan", "ping", "wang", "chao-phraya"] if args.basin == "all" else [args.basin]

    for b in basin_list:
        # Smart path resolution for --dir (supports both './dataset' and './dataset/yom')
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

        generate_basin_flow_paths(
            basin=b,
            basin_dir=basin_dir,
            terrain_dir=terrain_basin_dir,
            force=args.force,
            burn_depth=args.burn_depth
        )


if __name__ == "__main__":
    main()
