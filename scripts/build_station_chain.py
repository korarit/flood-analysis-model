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
from scripts.modules.basin_registry import get_all_slugs, get_basin
from scripts.modules.gis_utils import save_geojson, save_json, load_stations_for_basin

from scripts.modules.terrain_engine import read_dem_geotiff
from scripts.modules.graph_topology import (
    snap_stations_to_stream,
    build_flow_paths_and_relations,
    delineate_station_catchments
)


def build_basin_station_chain(basin: str, basin_dir: str, terrain_dir: str):
    """Snaps stations, generates flow paths, and delineates catchments for any basin."""
    station_dir = os.path.join(basin_dir, "station")
    catchment_dir = os.path.join(basin_dir, "catchment")
    processed_dir = os.path.join(basin_dir, "processed")
    os.makedirs(station_dir, exist_ok=True)
    os.makedirs(catchment_dir, exist_ok=True)
    os.makedirs(processed_dir, exist_ok=True)

    station_mapping_path = os.path.join(station_dir, "station-mapping.json")
    flow_paths_path = os.path.join(processed_dir, "flow_paths.geojson")
    gauge_relations_path = os.path.join(station_dir, "station-relations.json")
    rain_relations_path = os.path.join(station_dir, "rainfall-relations.json")
    catchments_path = os.path.join(catchment_dir, "catchments.geojson")
    processed_catchments_path = os.path.join(processed_dir, "catchments.geojson")

    def _is_valid_relation_file(path: str) -> bool:
        if not os.path.exists(path) or os.path.getsize(path) <= 100:
            return False
        try:
            import json
            with open(path, 'r', encoding='utf-8') as f:
                data = json.load(f)
                if isinstance(data, list) and len(data) > 0:
                    first_dist = data[0].get('distance_km', 0.0) or data[0].get('total_distance_km', 0.0)
                    if first_dist > 2000.0:  # Invalidated corrupted distance from old bug
                        return False
                    # Ensure rainfall relations have calculated response_lag_minutes
                    if "rainfall-relations" in path and "response_lag_minutes" not in data[0]:
                        return False
        except Exception:
            return False
        return True

    # -------------------------------------------------------------
    # Top-Level CACHE Check: Skip entire Step 3 if all artifacts exist
    # -------------------------------------------------------------
    all_cached = (
        os.path.exists(station_mapping_path) and os.path.getsize(station_mapping_path) > 100 and
        os.path.exists(flow_paths_path) and os.path.getsize(flow_paths_path) > 100 and
        _is_valid_relation_file(gauge_relations_path) and
        _is_valid_relation_file(rain_relations_path) and
        os.path.exists(catchments_path) and os.path.getsize(catchments_path) > 100
    )

    if all_cached:
        print(f"\n🔗 [STEP 3] [CACHE] Station chain, flow paths, and catchments already exist for basin: {basin.upper()}")
        print(f"        • Station Mapping   : {station_mapping_path}")
        print(f"        • Flow Paths GeoJSON: {flow_paths_path}")
        print(f"        • Station Relations : {gauge_relations_path}")
        print(f"        • Rainfall Relations: {rain_relations_path}")
        print(f"        • Catchments GeoJSON: {catchments_path}")
        print("        Loaded cached Step 3 outputs (skipping re-computation).")
        if not os.path.exists(processed_catchments_path) or os.path.getsize(processed_catchments_path) <= 100:
            import shutil
            shutil.copy2(catchments_path, processed_catchments_path)
        return

    raw_dem_path = os.path.join(terrain_dir, "raw_dem.tif")
    cond_dem_path = os.path.join(terrain_dir, "conditioned_dem.tif")
    fdir_path = os.path.join(terrain_dir, "flow_direction.tif")
    acc_path = os.path.join(terrain_dir, "flow_accumulation.tif")

    # Safely select valid DEM file
    dem_to_use = raw_dem_path
    if os.path.exists(cond_dem_path):
        try:
            if os.path.getsize(cond_dem_path) > 1024:
                import rasterio
                with rasterio.open(cond_dem_path) as test_src:
                    if test_src.width > 0 and test_src.height > 0:
                        dem_to_use = cond_dem_path
        except Exception:
            try:
                os.remove(cond_dem_path)
            except Exception:
                pass

    if not os.path.exists(dem_to_use):
        print(f"❌ ERROR: DEM not found in {terrain_dir}. Please run fetch_basin_gis.py first!", file=sys.stderr)
        sys.exit(1)

    print(f"\n🔗 [STEP 3] Building Station Chain & Flow Paths for Basin: {basin.upper()}")

    # 1. Load Terrain Grids
    print("  [1/4] Loading DEM and computing Flow Direction for station snapping...")
    filled_dem, transform, crs, nodata = read_dem_geotiff(dem_to_use)
    
    if os.path.exists(fdir_path) and os.path.exists(acc_path) and os.path.getsize(fdir_path) > 1024:
        fdir, _, _, _ = read_dem_geotiff(fdir_path)
        acc, _, _, _ = read_dem_geotiff(acc_path)
    else:
        import pyflwdir
        is_latlon = (crs is None) or getattr(crs, 'is_geographic', False) or (str(crs) == "EPSG:4326")
        flw = pyflwdir.from_dem(filled_dem, nodata=nodata, transform=transform, latlon=is_latlon)
        fdir = flw.to_array(ftype='d8')
        acc = flw.upstream_area(unit='cell')
        del flw
        import gc
        gc.collect()

    # 2. Load and Snap Stations
    water_st, rain_st = load_stations_for_basin(basin_dir)
    
    # Load OSM Waterways if available
    osm_waterways_path = os.path.join(basin_dir, "gis", "osm_waterways.geojson")
    osm_waterways = None
    if os.path.exists(osm_waterways_path) and os.path.getsize(osm_waterways_path) > 100:
        import json
        with open(osm_waterways_path, 'r', encoding='utf-8') as f:
            osm_waterways = json.load(f)

    if os.path.exists(station_mapping_path) and os.path.getsize(station_mapping_path) > 100:
        import json
        with open(station_mapping_path, 'r', encoding='utf-8') as f:
            snapped_water_st = json.load(f)
        print(f"  [2/4] [CACHE] Loaded {len(snapped_water_st)} cached snapped stations: {station_mapping_path}")
    else:
        print(f"  [2/4] Loaded {len(water_st)} water stations and {len(rain_st)} rain stations.")
        print("        Snapping water level stations to OSM & stream channels...")
        snapped_water_st = snap_stations_to_stream(
            water_st, fdir, acc, transform, osm_waterways_geojson=osm_waterways, crs=crs
        )
        save_json(snapped_water_st, station_mapping_path)
        print(f"        Saved station mapping: {station_mapping_path}")

    # 3. Build Flow Paths (Gauge-to-Gauge & Rain-to-Gauge)
    flow_paths_cached = (
        os.path.exists(flow_paths_path) and os.path.getsize(flow_paths_path) > 100 and
        _is_valid_relation_file(gauge_relations_path) and
        _is_valid_relation_file(rain_relations_path)
    )
    if flow_paths_cached:
        print(f"  [3/4] [CACHE] Flow paths and relations already exist (skipping tracing).")
    else:
        print("  [3/4] Tracing Hybrid Gauge-to-Gauge & Overland Rain-to-Gauge Flow Paths...")
        flow_paths_geojson, gauge_relations, rain_relations = build_flow_paths_and_relations(
            snapped_water_st, rain_st, fdir, acc, filled_dem, transform,
            osm_waterways_geojson=osm_waterways, crs=crs
        )
        save_geojson(flow_paths_geojson, flow_paths_path)
        save_json(gauge_relations, gauge_relations_path)
        save_json(rain_relations, rain_relations_path)

        print(f"        Generated {len(gauge_relations)} Gauge-to-Gauge relations.")
        print(f"        Generated {len(rain_relations)} Rainfall-to-Gauge relations.")
        print(f"        Saved Flow Paths GeoJSON: {flow_paths_path}")

    # 4. Delineate Sub-catchments
    if os.path.exists(catchments_path) and os.path.getsize(catchments_path) > 100:
        print(f"  [4/4] [CACHE] Catchments GeoJSON already exists: {catchments_path} (skipping delineation).")
        if not os.path.exists(processed_catchments_path) or os.path.getsize(processed_catchments_path) <= 100:
            import shutil
            shutil.copy2(catchments_path, processed_catchments_path)
    else:
        print("  [4/4] Delineating Sub-Catchment Polygons for station outlets...")
        catchments_geojson = delineate_station_catchments(snapped_water_st, fdir, transform, crs=crs)
        save_geojson(catchments_geojson, catchments_path)
        save_geojson(catchments_geojson, processed_catchments_path)
        print(f"        Generated {len(catchments_geojson['features'])} catchment polygons.")
        print(f"        Saved Catchments GeoJSON: {catchments_path}")

    # Free memory buffers immediately
    import gc
    try:
        del filled_dem, fdir, acc
    except Exception:
        pass
    gc.collect()


def main():
    parser = argparse.ArgumentParser(description="Snap stations, generate flow paths, and delineate catchments")
    parser.add_argument("--basin", type=str, default="yom", help="River basin slug (e.g. yom, nan, ping, all)")
    parser.add_argument("--dir", type=str, default="./dataset", help="Dataset directory")
    parser.add_argument("--terrain-dir", type=str, default="./terrain", help="Terrain DEM directory (independent of dataset --dir)")
    args = parser.parse_args()

    basin_list = get_all_slugs() if args.basin == "all" else [args.basin]

    for b in basin_list:
        basin_dir = os.path.join(args.dir, b)
        terrain_basin_dir = os.path.join(args.terrain_dir, b)
        build_basin_station_chain(b, basin_dir, terrain_basin_dir)


if __name__ == "__main__":
    main()
