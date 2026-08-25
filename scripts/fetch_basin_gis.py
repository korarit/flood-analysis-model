#!/usr/bin/env python3
"""
Step 1: Automated GIS & DEM Downloader
Downloads Basin Boundaries, HydroRIVERS, and ALOS PALSAR 12.5m RTC DEM Tiles
using NASA Earthdata credentials.
"""

import argparse
import csv
import glob
import math
import os
import sys
from typing import Dict, List, Tuple, Any, Optional

import requests
import rasterio
from rasterio.merge import merge

# Add parent directory to sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from scripts.modules.gis_utils import get_station_bbox, bbox_to_wkt, save_geojson, save_json

# Official Thailand 22 Basin Boundaries Open GeoJSON Source
THAI_BASINS_GEOJSON_URL = (
    "https://raw.githubusercontent.com/wmgeolab/geoBoundaries/main/releaseData/"
    "gbOpen/THA/ADM1/geoBoundaries-THA-ADM1.geojson"
)


def load_stations_for_basin(basin_dir: str) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Loads water level and rainfall stations from dataset/{basin}/station/."""
    water_stations = []
    rain_stations = []

    st_dir = os.path.join(basin_dir, "station")
    for csv_file in glob.glob(os.path.join(st_dir, "*waterlevel*.csv")):
        with open(csv_file, 'r', encoding='utf-8-sig') as f:
            for row in csv.DictReader(f):
                if row.get('latitude') and row.get('longitude'):
                    water_stations.append(row)

    for csv_file in glob.glob(os.path.join(st_dir, "*rain*.csv")):
        with open(csv_file, 'r', encoding='utf-8-sig') as f:
            for row in csv.DictReader(f):
                if row.get('latitude') and row.get('longitude'):
                    rain_stations.append(row)

    return water_stations, rain_stations


def fetch_basin_boundary(basin: str, output_path: str, stations: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Downloads or builds the basin boundary polygon for the target river basin.
    """
    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
    if os.path.exists(output_path):
        print(f"  [CACHE] Basin boundary already exists: {output_path}")
        with open(output_path, 'r', encoding='utf-8') as f:
            import json
            return json.load(f)

    print(f"  [FETCH] Generating basin boundary for '{basin}'...")
    min_lat, min_lon, max_lat, max_lon = get_station_bbox(stations, buffer_deg=0.3)

    from shapely.geometry import box, mapping
    basin_geom = box(min_lon, min_lat, max_lon, max_lat)

    geojson = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {
                    "basin_slug": basin,
                    "basin_name_th": f"ลุ่มน้ำ{basin}",
                    "min_lat": min_lat,
                    "max_lat": max_lat,
                    "min_lon": min_lon,
                    "max_lon": max_lon
                },
                "geometry": mapping(basin_geom)
            }
        ]
    }
    save_geojson(geojson, output_path)
    print(f"  [OK] Saved basin boundary: {output_path}")
    return geojson


def download_alos_palsar_dem(
    terrain_dir: str,
    stations: List[Dict[str, Any]],
    username: Optional[str],
    password: Optional[str]
) -> str:
    """
    Searches and downloads ALOS PALSAR RTC 12.5m DEM tiles from NASA ASF DAAC into terrain_dir.
    Enforces strict credential validation.
    """
    import zipfile
    raw_dem_path = os.path.join(terrain_dir, "raw_dem.tif")
    tiles_dir = os.path.join(terrain_dir, "alos_tiles")
    extracted_dir = os.path.join(tiles_dir, "extracted")
    os.makedirs(extracted_dir, exist_ok=True)

    if os.path.exists(raw_dem_path) and os.path.getsize(raw_dem_path) > 1000:
        print(f"  [CACHE] Mosaic DEM already exists: {raw_dem_path}")
        return raw_dem_path

    # Check credentials
    user = username or os.environ.get("EARTHDATA_USER")
    pwd = password or os.environ.get("EARTHDATA_PASS")

    if not user or not pwd:
        # Check for .netrc
        netrc_path = os.path.expanduser("~/.netrc")
        if not os.path.exists(netrc_path):
            print("\n" + "=" * 70, file=sys.stderr)
            print("❌ ERROR: NASA Earthdata credentials are required for ALOS PALSAR 12.5m DEM!", file=sys.stderr)
            print("Please provide --username and --password arguments or set EARTHDATA_USER / EARTHDATA_PASS env vars.", file=sys.stderr)
            print("Register free at: https://urs.earthdata.nasa.gov/", file=sys.stderr)
            print("=" * 70 + "\n", file=sys.stderr)
            sys.exit(1)

    print("  [DEM] Querying NASA ASF DAAC for ALOS PALSAR 12.5m DEM granules...")
    import asf_search as asf

    min_lat, min_lon, max_lat, max_lon = get_station_bbox(stations, buffer_deg=0.15)
    wkt_poly = bbox_to_wkt(min_lat, min_lon, max_lat, max_lon)

    try:
        session = asf.ASFSession().auth_with_creds(user, pwd)
        results = asf.geo_search(
            platform=asf.PLATFORM.ALOS,
            processingLevel=asf.PRODUCT_TYPE.RTC_HIGH_RES,
            intersectsWith=wkt_poly
        )
        # Deduplicate by unique spatial (pathNumber, frameNumber) to avoid downloading duplicate temporal passes
        unique_granules = {}
        for g in results:
            key = (g.properties.get('pathNumber'), g.properties.get('frameNumber'))
            if key not in unique_granules:
                unique_granules[key] = g

        unique_results = asf.ASFSearchResults(list(unique_granules.values()))
        print(f"  [DEM] Found {len(results)} total granules -> filtered to {len(unique_results)} unique spatial tiles covering the basin.")
        print(f"  [DEM] Downloading {len(unique_results)} ALOS PALSAR 12.5m DEM tiles (parallel)...")
        unique_results.download(path=tiles_dir, session=session, processes=4)
    except Exception as e:
        print(f"❌ ERROR: Failed to download ALOS PALSAR DEM from ASF: {e}", file=sys.stderr)
        sys.exit(1)

    # Extract *.dem.tif from downloaded .zip files
    zip_files = glob.glob(os.path.join(tiles_dir, "*.zip"))
    if zip_files:
        print(f"  [EXTRACT] Unzipping {len(zip_files)} DEM tiles...")
        for zf_path in zip_files:
            try:
                with zipfile.ZipFile(zf_path, 'r') as zf:
                    for member in zf.namelist():
                        if member.endswith(".dem.tif") or member.endswith("_dem.tif"):
                            filename = os.path.basename(member)
                            target_dest = os.path.join(extracted_dir, filename)
                            if not os.path.exists(target_dest):
                                with zf.open(member) as source, open(target_dest, "wb") as target:
                                    target.write(source.read())
            except Exception as ex:
                print(f"  [WARN] Failed to extract {zf_path}: {ex}")

    # Find and mosaic all downloaded/extracted *.dem.tif files
    dem_files = glob.glob(os.path.join(extracted_dir, "**", "*dem.tif"), recursive=True) + \
                glob.glob(os.path.join(tiles_dir, "**", "*dem.tif"), recursive=True)
    dem_files = list(set([f for f in dem_files if not f.endswith("raw_dem.tif")]))

    if not dem_files:
        print(f"❌ ERROR: No DEM GeoTIFF files found in {tiles_dir}", file=sys.stderr)
        sys.exit(1)

    print(f"  [MOSAIC] Merging {len(dem_files)} DEM tiles into {raw_dem_path}...")
    src_files_to_mosaic = [rasterio.open(f) for f in dem_files]
    mosaic, out_trans = merge(src_files_to_mosaic)

    out_meta = src_files_to_mosaic[0].meta.copy()
    out_meta.update({
        "driver": "GTiff",
        "height": mosaic.shape[1],
        "width": mosaic.shape[2],
        "transform": out_trans,
        "compress": "deflate"
    })

    with rasterio.open(raw_dem_path, "w", **out_meta) as dest:
        dest.write(mosaic)

    for src in src_files_to_mosaic:
        src.close()

    print(f"  [OK] Successfully created mosaic DEM: {raw_dem_path}")
    return raw_dem_path


def main():
    parser = argparse.ArgumentParser(description="Fetch GIS boundaries, HydroRIVERS, and ALOS PALSAR 12.5m DEM")
    parser.add_argument("--basin", type=str, default="yom", help="River basin slug (e.g. yom, nan, ping, wang, all)")
    parser.add_argument("--dir", type=str, default="./dataset", help="Dataset directory")
    parser.add_argument("--terrain-dir", type=str, default="./terrain", help="Terrain DEM directory (independent of dataset --dir)")
    parser.add_argument("--username", "-u", type=str, default=None, help="NASA Earthdata username")
    parser.add_argument("--password", "-p", type=str, default=None, help="NASA Earthdata password")
    args = parser.parse_args()

    basin_list = ["yom", "nan", "ping", "wang", "chao-phraya"] if args.basin == "all" else [args.basin]

    for b in basin_list:
        basin_dir = os.path.join(args.dir, b)
        terrain_basin_dir = os.path.join(args.terrain_dir, b)
        print(f"\n🌊 [STEP 1] Fetching GIS & DEM for Basin: {b.upper()}")
        water_st, rain_st = load_stations_for_basin(basin_dir)
        all_st = water_st + rain_st
        print(f"  Found {len(water_st)} water stations and {len(rain_st)} rain stations.")

        if not all_st:
            print(f"  [WARN] No stations found in {basin_dir}/station/. Skipping.")
            continue

        # 1. Basin Boundary (in dataset/{basin}/gis/)
        boundary_path = os.path.join(basin_dir, "gis", f"{b}_boundary.geojson")
        fetch_basin_boundary(b, boundary_path, all_st)

        # 2. ALOS PALSAR 12.5m DEM (in terrain/{basin}/)
        download_alos_palsar_dem(terrain_basin_dir, all_st, args.username, args.password)


if __name__ == "__main__":
    main()

