#!/usr/bin/env python3
"""
Standalone Flow Path & River Topology Generator
Generates and updates flow_paths.geojson, station-relations.json, and rainfall-relations.json
using Hybrid OpenStreetMap (OSM) Waterway Vector Topology + Hydro-Enforced D8 Hydrology.

Usage:
  python scripts/generate_flow_paths.py --basin yom --force
"""

import argparse
import gc
import json
import os
import sys
import time
from typing import Dict, List, Any

import numpy as np
import rasterio

# Add parent directory to sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from scripts.modules.gis_utils import (
    save_geojson,
    save_json,
    load_stations_for_basin,
    write_geojson_pair
)
from scripts.modules.terrain_engine import (
    read_dem_geotiff,
    burn_stream_network_into_dem,
    build_river_mask,
    save_geotiff_raster
)
from scripts.modules.graph_topology import (
    snap_stations_to_stream,
    build_flow_paths_and_relations
)
from scripts.modules.backend_exporter import export_backend_station_relations
from scripts.fetch_basin_gis import (
    fetch_osm_waterways,
    fetch_osm_water_polygons,
    ensure_osm_cropped,
    load_valid_boundary
)


def resolve_basin_boundary_or_fail(basin: str, boundary_path: str) -> Dict[str, Any]:
    """
    Phase 1 (G5) + Step 1 (round 5): the basin boundary polygon is MANDATORY and must
    be a REAL basin outline. When it is missing, unreadable, or a coarse rectangular
    cache (the round-4 root cause: < 50 vertices / "Station Bounding Box Fallback"
    source), the pipeline exits with fix instructions instead of silently running on
    a rectangle. Generation NEVER auto-fetches here — boundary retrieval belongs to
    fetch_basin_gis.py.
    """
    data = load_valid_boundary(basin, boundary_path)
    if data is not None:
        return data
    if os.path.exists(boundary_path):
        raise SystemExit(
            f"❌ ERROR: Basin boundary file is invalid or a coarse rectangle: {boundary_path}\n"
            f"   A real basin polygon is mandatory (rectangular fallback is disabled).\n"
            f"   Fix: DELETE this file, then run "
            f"`python scripts/fetch_basin_gis.py --basin {basin}` to refetch, "
            f"then re-run this script."
        )
    raise SystemExit(
        f"❌ ERROR: Basin boundary polygon not found: {boundary_path}\n"
        f"   A real basin polygon is mandatory (rectangular station-bbox fallback is disabled).\n"
        f"   Fix: run `python scripts/fetch_basin_gis.py --basin {basin}` first, then re-run this script."
    )


def generate_basin_flow_paths(
    basin: str,
    basin_dir: str,
    terrain_dir: str,
    force: bool = False,
    burn_depth: float = 15.0,
    polygon_burn_depth: float = None,
    min_flow_km: float = 1.0,
    cascade_max_km: float = 60.0,
    branch_min_acc: int = 500,
    include_branches: bool = True,
    include_osm_layer: bool = True,
    branch_max_cells: int = 400_000,
    branch_max_count: int = 30,
    branch_min_km: float = 1.0,
    overland_max_km: float = 5.0,
    clip_to_basin: bool = True,
    write_gzip: bool = True,
    crop_buffer_m: float = 2000.0
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
    water_polygons_path = os.path.join(gis_dir, "osm_water_polygons.geojson")
    boundary_path = os.path.join(gis_dir, f"{basin}_boundary.geojson")
    relations_frontend_path = os.path.join(processed_dir, "relations_frontend.json")
    final_station_data_path = os.path.join(basin_dir, "final_station_data.json")

    print(f"\n🌊 [FLOW PATHS] Generating Hybrid Flow Paths for Basin: {basin.upper()}")

    # 1. Load Stations
    water_st, rain_st = load_stations_for_basin(basin_dir)
    print(f"  [1/5] Loaded {len(water_st)} water stations and {len(rain_st)} rain stations.")
    if not water_st:
        print(f"  ❌ ERROR: No water level stations found in {station_dir}!", file=sys.stderr)
        return

    # 2. Fetch or Load OpenStreetMap Waterways (scoped to the official basin polygon)
    # Phase 1 (G5): boundary is mandatory — fail fast when missing, never fall back
    # to a rectangular station bbox.
    print("  [2/5] Loading OpenStreetMap River Network...")
    boundary_geojson = resolve_basin_boundary_or_fail(basin, boundary_path)

    osm_waterways = fetch_osm_waterways(
        basin, osm_waterways_path, water_st + rain_st, force=force,
        basin_boundary_geojson=boundary_geojson, crop_buffer_m=crop_buffer_m
    )
    # Phase 2.8: recrop legacy caches (no crop fingerprint) with the current boundary,
    # idempotently, so old rectangular-era caches can never pass through silently.
    osm_waterways = ensure_osm_cropped(
        osm_waterways, boundary_geojson, osm_waterways_path,
        buffer_m=crop_buffer_m, label="osm_waterways"
    )
    n_osm = len(osm_waterways.get("features", []))
    print(f"        Loaded {n_osm:,} OSM river/stream features.")

    # 2b. OpenStreetMap Water Polygons (reservoirs / wide rivers) for stream burning
    water_polygons = fetch_osm_water_polygons(
        basin, water_polygons_path, water_st + rain_st, force=force,
        basin_boundary_geojson=boundary_geojson, crop_buffer_m=crop_buffer_m
    )
    water_polygons = ensure_osm_cropped(
        water_polygons, boundary_geojson, water_polygons_path,
        buffer_m=crop_buffer_m, label="osm_water_polygons"
    )
    n_poly = len(water_polygons.get("features", []))
    print(f"        Loaded {n_poly:,} OSM water polygon features.")

    # 3. Load or Condition DEM Raster with Smart Caching
    raw_dem_path = os.path.join(terrain_dir, "raw_dem.tif")
    cond_dem_path = os.path.join(terrain_dir, "conditioned_dem.tif")
    fdir_path = os.path.join(terrain_dir, "flow_direction.tif")
    acc_path = os.path.join(terrain_dir, "flow_accumulation.tif")

    has_cached_rasters = (
        not force
        and os.path.exists(fdir_path) and os.path.getsize(fdir_path) > 1024
        and os.path.exists(acc_path) and os.path.getsize(acc_path) > 1024
        and os.path.exists(cond_dem_path) and os.path.getsize(cond_dem_path) > 1024
    )

    if has_cached_rasters:
        print("  [3/5] [CACHE] Loading cached Flow Direction, Accumulation, and Conditioned DEM...")
        if n_poly > 0 and force is False:
            print("        [NOTE] Cached conditioned DEM reused — water-polygon burn changes require --force to re-burn.")
        with rasterio.open(fdir_path) as src_fdir:
            fdir = src_fdir.read(1).astype(np.uint8)
            transform = src_fdir.transform
            crs = src_fdir.crs
        with rasterio.open(acc_path) as src_acc:
            acc = src_acc.read(1).astype(np.int32)
        with rasterio.open(cond_dem_path) as src_dem:
            filled_dem = src_dem.read(1).astype(np.float32)
            nodata = src_dem.nodata if src_dem.nodata is not None else -9999.0
    else:
        dem_to_use = cond_dem_path if (os.path.exists(cond_dem_path) and os.path.getsize(cond_dem_path) > 1024) else raw_dem_path

        if not os.path.exists(dem_to_use):
            print(f"  ❌ ERROR: DEM not found in {terrain_dir}. Please run fetch_basin_gis.py first!", file=sys.stderr)
            return

        print("  [3/5] Loading DEM & Hydro-Enforcing OSM River Channels...")
        filled_dem, transform, crs, nodata = read_dem_geotiff(dem_to_use)

        # Apply Stream Burning to DEM if OSM is available
        if n_osm > 0:
            filled_dem = burn_stream_network_into_dem(
                filled_dem, transform, osm_waterways, crs=crs, burn_depth_m=burn_depth, nodata=nodata,
                water_polygons_geojson=water_polygons if n_poly > 0 else None,
                polygon_burn_depth_m=(polygon_burn_depth if polygon_burn_depth is not None else max(1.0, burn_depth - 5.0))
            )

        # Compute Flow Direction & Accumulation
        import pyflwdir
        is_latlon = (crs is None) or getattr(crs, 'is_geographic', False) or (str(crs) == "EPSG:4326")
        flw = pyflwdir.from_dem(filled_dem, nodata=nodata, transform=transform, latlon=is_latlon)
        fdir = flw.to_array(ftype='d8')
        acc = flw.upstream_area(unit='cell')
        del flw
        gc.collect()

        # Save cached rasters to disk for fast subsequent runs
        print("        Saving flow rasters to cache...")
        save_geotiff_raster(fdir, transform, crs, fdir_path, nodata=0)
        save_geotiff_raster(acc, transform, crs, acc_path, nodata=-1)
        save_geotiff_raster(filled_dem, transform, crs, cond_dem_path, nodata=nodata)

    # 3b. River mask (OSM waterway footprint) for river-aware D8 stopping — cached.
    # Overland traces stop at the first river cell so runoff merges into the adjacent
    # river instead of crossing it, and rain stations next to a river enter it at once.
    river_mask_path = os.path.join(terrain_dir, "river_mask.tif")
    river_mask = None
    if (not force and os.path.exists(river_mask_path) and os.path.getsize(river_mask_path) > 1024):
        try:
            with rasterio.open(river_mask_path) as src_m:
                m = src_m.read(1).astype(np.uint8)
            if m.shape == fdir.shape:
                river_mask = (m == 1)
                print("  [3b/5] [CACHE] Loaded river mask for river-aware D8 stopping.")
            else:
                print("  [3b/5] Cached river mask shape mismatch — rebuilding.")
        except Exception:
            river_mask = None
    if river_mask is None:
        print("  [3b/5] Building river mask (OSM waterway footprint)...")
        river_mask = build_river_mask(osm_waterways, transform, out_shape=fdir.shape, crs=crs)
        if river_mask is not None:
            try:
                save_geotiff_raster(river_mask.astype(np.uint8), transform, crs, river_mask_path, nodata=255)
            except Exception as ex:
                print(f"  [WARN] Could not cache river_mask.tif: {ex}")
        else:
            print("  [WARN] No river mask available — river-aware D8 stopping disabled.")

    # 4. Snap Stations to OSM Rivers and Stream Channel
    print("  [4/5] Snapping stations to OSM River Channels...")
    snapped_water_st = snap_stations_to_stream(
        water_st, fdir, acc, transform, osm_waterways_geojson=osm_waterways, crs=crs,
        water_polygons_geojson=water_polygons if n_poly > 0 else None
    )
    save_json(snapped_water_st, station_mapping_path)

    # 5. Build Hybrid Flow Paths (Gauge-to-Gauge & Rain-to-Gauge with downstream cascade)
    print("  [5/5] Tracing Hybrid Flow Paths (OSM Rivers + D8 Hydrology + Drainage Branches)...")
    flow_paths_geojson, gauge_relations, rain_relations = build_flow_paths_and_relations(
        snapped_water_st, rain_st, fdir, acc, filled_dem, transform,
        osm_waterways_geojson=osm_waterways, crs=crs,
        min_flow_km=min_flow_km,
        cascade_max_km=cascade_max_km,
        branch_min_acc=branch_min_acc,
        include_branches=include_branches,
        include_osm_layer=include_osm_layer,
        branch_max_cells=branch_max_cells,
        branch_max_count=branch_max_count,
        branch_min_km=branch_min_km,
        river_mask=river_mask,
        overland_max_km=overland_max_km,
        basin_boundary_geojson=boundary_geojson,
        clip_to_basin=clip_to_basin
    )

    raw_bytes, gz_bytes = write_geojson_pair(flow_paths_geojson, flow_paths_path, write_gzip=write_gzip)
    save_json(gauge_relations, gauge_relations_path)
    save_json(rain_relations, rain_relations_path)

    # 6. Preserve existing calculated rainfallThresholds if present
    existing_thresholds = {}
    if os.path.exists(final_station_data_path):
        try:
            with open(final_station_data_path, 'r', encoding='utf-8') as f:
                fdata = json.load(f)
                for st_id, st_obj in fdata.items():
                    for inf in st_obj.get("influencingStations", []):
                        inf_id = str(inf.get("stationId", "")).strip()
                        if inf.get("rainfallThresholds") and inf_id:
                            existing_thresholds[(str(st_id), inf_id)] = inf["rainfallThresholds"]
        except Exception:
            pass

    for rr in rain_relations:
        to_id = str(rr.get("to_station_id", rr.get("target_station_id", ""))).strip()
        from_id = str(rr.get("from_station_id", rr.get("station_id", ""))).strip()
        key = (to_id, from_id)
        if key in existing_thresholds and not rr.get("rainfallThresholds"):
            rr["rainfallThresholds"] = existing_thresholds[key]

    # 7. Export Frontend relations_frontend.json & Database station_relations_db.json
    db_relations_path = os.path.join(processed_dir, "station_relations_db.json")
    export_backend_station_relations(
        gauge_relations=gauge_relations,
        rainfall_relations=rain_relations,
        output_db_path=db_relations_path,
        output_frontend_path=relations_frontend_path
    )
    print(f"        Saved Frontend relations : {relations_frontend_path}")
    print(f"        Saved Database relations : {db_relations_path}")

    # 8. Sync relations into final_station_data.json if exists
    if os.path.exists(final_station_data_path) and os.path.exists(relations_frontend_path):
        try:
            with open(final_station_data_path, 'r', encoding='utf-8') as f:
                final_data = json.load(f)
            with open(relations_frontend_path, 'r', encoding='utf-8') as f:
                frontend_list = json.load(f)

            frontend_by_id = {str(item.get("stationId")): item for item in frontend_list}

            for st_id, st_obj in final_data.items():
                if str(st_id) in frontend_by_id:
                    f_item = frontend_by_id[str(st_id)]
                    st_obj["influencingStations"] = f_item.get("influencingStations", [])
                    st_obj["downstreamStations"] = f_item.get("downstreamStations", [])

            save_json(final_data, final_station_data_path)
            print(f"        Synced frontend relations into: {final_station_data_path}")
        except Exception as ex:
            print(f"  [WARN] Could not sync final_station_data.json: {ex}")

    # Free memory buffers immediately
    del filled_dem, fdir, acc
    if 'flw' in locals() and flw is not None:
        del flw
    gc.collect()

    elapsed = time.time() - t_start

    # G4: size report per feature_type (points drive the file size)
    type_stats: Dict[str, List[int]] = {}
    for feat in flow_paths_geojson.get("features", []):
        t = feat.get("properties", {}).get("feature_type", "unknown")
        stats = type_stats.setdefault(t, [0, 0])
        stats[0] += 1
        stats[1] += len(feat.get("geometry", {}).get("coordinates", []))

    print(f"\n✅ [DONE] Generated {len(flow_paths_geojson['features'])} Flow Paths in {elapsed:.1f}s:")
    print(f"        • Gauge Relations    : {len(gauge_relations)} relations")
    print(f"        • Rainfall Relations : {len(rain_relations)} relations")
    print(f"        • Relations Frontend : {relations_frontend_path}")
    print(f"        • Size breakdown (feature_type: features / points):")
    for t, (n, npts) in sorted(type_stats.items(), key=lambda x: -x[1][1]):
        print(f"            {t:<35s} {n:>7,} / {npts:>9,}")
    print(f"        • Flow Paths GeoJSON : {flow_paths_path} ({raw_bytes / 1e6:.1f} MB)")
    if write_gzip:
        print(f"        • Flow Paths GeoJSON (gzip for frontend): {flow_paths_path}.gz ({gz_bytes / 1e6:.1f} MB)")


def main():
    parser = argparse.ArgumentParser(
        description="Standalone Generator for Flow Paths & River Relations (Hybrid OSM + D8 Hydrology)"
    )
    parser.add_argument("--basin", type=str, default="yom", help="River basin slug (e.g. yom, nan, ping, wang, all)")
    parser.add_argument("--dir", type=str, default="./dataset", help="Dataset root directory (e.g. ./dataset or ./dataset/yom)")
    parser.add_argument("--terrain-dir", type=str, default="./terrain", help="Terrain DEM directory (independent of --dir)")
    parser.add_argument("--force", action="store_true", help="Force re-generation of flow paths")
    parser.add_argument("--burn-depth", type=float, default=15.0, help="Stream burn depth in meters (default: 15.0)")
    parser.add_argument("--polygon-burn-depth", type=float, default=None,
                        help="Water polygon burn depth in meters (default: burn-depth - 5)")
    parser.add_argument("--min-flow-km", type=float, default=1.0,
                        help="Minimum flow path length in km (default: 1.0)")
    parser.add_argument("--rain-cascade-km", type=float, default=60.0,
                        help="Maximum downstream cascade reach along the river backbone in km (default: 60)")
    parser.add_argument("--branch-min-acc", type=int, default=500,
                        help="Minimum flow accumulation (cells) for drainage branches (default: 500)")
    parser.add_argument("--no-branches", action="store_true",
                        help="Disable per-rain-station drainage branch extraction (faster on low-RAM machines)")
    parser.add_argument("--no-osm-layer", action="store_true",
                        help="Disable the OSM river display layer (feature_type=osm_river) in flow_paths.geojson")
    parser.add_argument("--branch-max-cells", type=int, default=400_000,
                        help="Max upstream cells collected per rain station for branches (default: 400000)")
    parser.add_argument("--branch-max-count", type=int, default=30,
                        help="Max drainage branches kept per rain station, longest first (default: 30)")
    parser.add_argument("--branch-min-km", type=float, default=1.0,
                        help="Minimum drainage branch length in km, branches only (default: 1.0; flow paths use --min-flow-km)")
    parser.add_argument("--overland-max-km", type=float, default=5.0,
                        help="Cap length of pure-overland (river-less) rain flow paths in km (default: 5.0; 0 disables)")
    parser.add_argument("--no-basin-clip", action="store_true",
                        help="Do NOT clip output lines to the ThaiWater basin boundary polygon")
    parser.add_argument("--no-gzip", action="store_true",
                        help="Skip writing flow_paths.geojson.gz (raw .geojson is always written)")
    parser.add_argument("--crop-buffer-m", type=float, default=2000.0,
                        help="Buffer in meters applied to the basin polygon when cropping OSM data "
                             "(default: 2000; must match the fetch_basin_gis.py crop buffer)")
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
            burn_depth=args.burn_depth,
            polygon_burn_depth=args.polygon_burn_depth,
            min_flow_km=args.min_flow_km,
            cascade_max_km=args.rain_cascade_km,
            branch_min_acc=args.branch_min_acc,
            include_branches=not args.no_branches,
            include_osm_layer=not args.no_osm_layer,
            branch_max_cells=args.branch_max_cells,
            branch_max_count=args.branch_max_count,
            branch_min_km=args.branch_min_km,
            overland_max_km=args.overland_max_km,
            clip_to_basin=not args.no_basin_clip,
            write_gzip=not args.no_gzip,
            crop_buffer_m=args.crop_buffer_m
        )


if __name__ == "__main__":
    main()
