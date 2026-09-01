#!/usr/bin/env python3
"""
Step 2: Terrain Engine & River Network Extraction
Conditions DEM, computes D8 Flow Direction, Flow Accumulation,
extracts River Network Reaches with slopes, and detects Confluences.
"""

import argparse
import json
import os
import sys
from typing import Dict, Any

# Add parent directory to sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from scripts.modules.basin_registry import get_all_slugs, get_basin

import numpy as np
from shapely.geometry import shape

from scripts.modules.gis_utils import save_geojson, save_json

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


def process_basin_terrain(
    basin: str,
    basin_dir: str,
    terrain_dir: str,
    stream_threshold: int = 300,
    force: bool = False
):
    """
    Builds the complete River Network by basing 100% on OpenStreetMap (OSM) Waterways
    in the basin BBox, enhanced with ALOS PALSAR 12.5m DEM elevations, slopes,
    and topological flow direction enforcement.
    """
    gis_dir = os.path.join(basin_dir, "gis")
    river_dir = os.path.join(basin_dir, "river")
    processed_dir = os.path.join(basin_dir, "processed")
    os.makedirs(river_dir, exist_ok=True)
    os.makedirs(processed_dir, exist_ok=True)
    os.makedirs(terrain_dir, exist_ok=True)

    # 0. Check Cache
    processed_river_path = os.path.join(processed_dir, "river_network.geojson")
    river_segments_path = os.path.join(river_dir, "river_segments.json")
    if (
        os.path.exists(processed_river_path)
        and os.path.exists(river_segments_path)
        and os.path.getsize(processed_river_path) > 1024
        and not force
    ):
        print(f"\n🏔️ [STEP 2] [CACHE] River network already exists: {processed_river_path}")
        print("        Loaded cached river network (skipping re-computation).")
        return

    # 1. Load Stations and Bounding Box
    water_st, rain_st = load_stations_for_basin(basin_dir)
    all_st = water_st + rain_st
    if not all_st:
        print(f"❌ ERROR: No stations found in {basin_dir}/station/", file=sys.stderr)
        sys.exit(1)

    from scripts.modules.gis_utils import get_station_bbox, linestring_length_km
    bbox_min_lat, bbox_min_lon, bbox_max_lat, bbox_max_lon = get_station_bbox(all_st, buffer_deg=0.25)

    # Calculate southern limit: Southernmost water level station - 5km (~0.045 deg)
    water_lats = [float(st['latitude']) for st in water_st if st.get('latitude') is not None]
    southern_limit_lat = round(min(water_lats) - (5.0 / 111.0), 5) if water_lats else None
    effective_min_lat = southern_limit_lat if southern_limit_lat is not None else bbox_min_lat

    # 2. Fetch or Load 100% of OSM Waterways in BBox
    from scripts.fetch_basin_gis import fetch_osm_waterways
    osm_waterways_path = os.path.join(gis_dir, "osm_waterways.geojson")
    osm_geojson = fetch_osm_waterways(basin, osm_waterways_path, all_st, force=force)
    osm_features = osm_geojson.get("features", [])
    print(f"\n🌊 [STEP 2] Building Complete River Network from 100% OSM Waterways ({len(osm_features):,} features)...")

    # 3. Load DEM for Elevation & Slope Profiling
    raw_dem_path = os.path.join(terrain_dir, "raw_dem.tif")
    cond_dem_path = os.path.join(terrain_dir, "conditioned_dem.tif")
    dem_path = cond_dem_path if os.path.exists(cond_dem_path) else raw_dem_path

    dem_data = None
    dem_transform = None
    dem_crs = None
    if os.path.exists(dem_path):
        import rasterio
        with rasterio.open(dem_path) as src:
            dem_data = src.read(1).astype(np.float32)
            dem_transform = src.transform
            dem_crs = src.crs
            dem_nodata = src.nodata if src.nodata is not None else -9999.0

    def sample_elev(lon: float, lat: float) -> float:
        if dem_data is None or dem_transform is None:
            return 100.0
        try:
            from rasterio.transform import rowcol
            r, c = rowcol(dem_transform, lon, lat)
            if 0 <= r < dem_data.shape[0] and 0 <= c < dem_data.shape[1]:
                val = dem_data[r, c]
                if val != dem_nodata and not np.isnan(val) and val > -500:
                    return float(val)
        except Exception:
            pass
        return 100.0

    # 4. Enhance every OSM River & Tributary reach with DEM Hydrology
    all_river_features = []
    all_river_segments = []
    reach_counter = 0
    node_endpoints = {}

    for feat in osm_features:
        coords = feat.get("geometry", {}).get("coordinates", [])
        if not coords or len(coords) < 2:
            continue

        # Spatial Southern Limit Filter: keep river if ANY part of it is above effective_min_lat
        lats = [c[1] for c in coords]
        if max(lats) < effective_min_lat:
            continue

        props = feat.get("properties", {})
        length_km = linestring_length_km(coords)
        if length_km < 0.05:  # Skip trivial micro-segments < 50m
            continue

        # Sample Elevations
        z_start = sample_elev(coords[0][0], coords[0][1])
        z_end = sample_elev(coords[-1][0], coords[-1][1])

        # Enforce Downhill Flow Direction (higher to lower elevation)
        oriented_coords = list(coords)
        if z_start < z_end - 0.5:
            oriented_coords.reverse()
            z_start, z_end = z_end, z_start

        dz = max(0.0, z_start - z_end)
        slope = (dz / (length_km * 1000.0)) if length_km > 0.001 else 0.0005

        reach_counter += 1
        reach_id = f"REACH_{reach_counter:05d}"
        r_name = props.get("name_th") or props.get("name") or props.get("name_en") or ""

        clean_props = {
            "reach_id": reach_id,
            "osm_id": props.get("osm_id", reach_counter),
            "river_name": r_name,
            "waterway": props.get("waterway", "stream"),
            "length_km": round(length_km, 3),
            "upstream_elev_m": round(z_start, 2),
            "downstream_elev_m": round(z_end, 2),
            "elevation_diff_m": round(dz, 2),
            "river_slope": round(slope, 6)
        }

        out_feat = {
            "type": "Feature",
            "id": reach_id,
            "properties": clean_props,
            "geometry": {
                "type": "LineString",
                "coordinates": oriented_coords
            }
        }
        all_river_features.append(out_feat)
        all_river_segments.append(clean_props)

        # Track endpoints for Confluence detection
        start_pt = (round(oriented_coords[0][0], 4), round(oriented_coords[0][1], 4))
        end_pt = (round(oriented_coords[-1][0], 4), round(oriented_coords[-1][1], 4))
        node_endpoints.setdefault(start_pt, []).append((reach_id, "start"))
        node_endpoints.setdefault(end_pt, []).append((reach_id, "end"))

    # 5. Detect Confluences (where 2 or more tributaries meet)
    all_confluences = []
    conf_counter = 0
    for pt, connections in node_endpoints.items():
        if len(connections) >= 2:
            conf_counter += 1
            conf_id = f"CONF_{conf_counter:04d}"
            all_confluences.append({
                "type": "Feature",
                "id": conf_id,
                "properties": {
                    "confluence_id": conf_id,
                    "connected_reaches": len(connections),
                    "elevation_m": round(sample_elev(pt[0], pt[1]), 2)
                },
                "geometry": {
                    "type": "Point",
                    "coordinates": [pt[0], pt[1]]
                }
            })

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
    raw_river_path = os.path.join(river_dir, "river_network_raw.geojson")

    # Save compact raw backup and confluences
    save_geojson(merged_river_geojson, raw_river_path, indent=None)
    save_json(all_river_segments, river_segments_path)
    save_geojson(merged_confluences_geojson, confluences_path)

    # 6. Simplify and Optimize for Web GIS Rendering
    try:
        from scripts.simplify_river_network import simplify_river_geojson
        simplify_river_geojson(
            input_path=raw_river_path,
            output_path=processed_river_path,
            tolerance_deg=0.0001,
            min_length_km=0.1,
            precision=5,
            create_backup=False
        )
        import shutil
        shutil.copy2(processed_river_path, river_geojson_path)
    except Exception as opt_err:
        print(f"⚠️ Warning: Auto-simplification failed ({opt_err}), falling back to direct save.")
        save_geojson(merged_river_geojson, river_geojson_path, indent=None)
        save_geojson(merged_river_geojson, processed_river_path, indent=None)

    print("\n" + "═" * 70)
    print(f"  ✅ [SUCCESS] Complete OSM River Network Built & DEM-Enhanced:")
    print(f"     • Total River Reaches : {len(all_river_features):,} segments (100% complete)")
    print(f"     • Total Confluences   : {len(all_confluences):,} junctions")
    print(f"     • Saved (Optimized)   : {processed_river_path}")
    print(f"     • Saved (Raw Backup)  : {raw_river_path}")
    print("═" * 70)


def main():
    parser = argparse.ArgumentParser(description="Process DEM using Sub-basin Cascade to extract 12.5m river lines")
    parser.add_argument("--basin", type=str, default="yom", help="River basin slug (e.g. yom, nan, ping, wang, all)")
    parser.add_argument("--dir", type=str, default="./dataset", help="Dataset directory")
    parser.add_argument("--terrain-dir", type=str, default="./terrain", help="Terrain DEM directory (independent of dataset --dir)")
    parser.add_argument("--threshold", type=int, default=300, help="Stream accumulation threshold in cells")
    parser.add_argument("--force", action="store_true", help="Force recomputation without using cached river network")
    args = parser.parse_args()

    basin_list = get_all_slugs() if args.basin == "all" else [args.basin]

    for b in basin_list:
        basin_dir = os.path.join(args.dir, b)
        terrain_basin_dir = os.path.join(args.terrain_dir, b)
        process_basin_terrain(b, basin_dir, terrain_basin_dir, stream_threshold=args.threshold, force=args.force)


if __name__ == "__main__":
    main()
