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
from scripts.modules.gis_utils import (
    get_station_bbox,
    bbox_to_wkt,
    save_geojson,
    save_json,
    linestring_length_km
)

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


def fetch_subbasins_boundary(basin: str, output_path: str, stations: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Builds or loads topological Sub-basin polygons for native 12.5m DEM Cascade processing.
    Partitions the river basin into ordered upstream-to-downstream sub-basins.
    """
    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
    if os.path.exists(output_path):
        print(f"  [CACHE] Sub-basins boundary already exists: {output_path}")
        with open(output_path, 'r', encoding='utf-8') as f:
            import json
            return json.load(f)

    print(f"  [SUBBASINS] Generating Topological Sub-basins for '{basin}' (Cascade 12.5m)...")
    min_lat, min_lon, max_lat, max_lon = get_station_bbox(stations, buffer_deg=0.15)
    from shapely.geometry import box, mapping

    # Comprehensive Sub-basin Registry for all Thai River Basins
    SUBBASIN_REGISTRY = {
        "yom": [
            ("yom_01_upper", "ลุ่มน้ำยมตอนบน (พะเยา/ปง/เชียงม่วน)", 1),
            ("yom_02_mid_north", "ลุ่มน้ำยมตอนกลางเหนือ (สอง/ร้องกวาง/แพร่)", 2),
            ("yom_03_central", "ลุ่มน้ำยมตอนกลาง (ศรีสัชนาลัย/สวรรคโลก)", 3),
            ("yom_04_mid_south", "ลุ่มน้ำยมตอนกลางใต้ (สุโขทัย/กงไกรลาศ)", 4),
            ("yom_05_lower", "ลุ่มน้ำยมตอนล่าง (บางระกำ/พิจิตร/ชุมแสง)", 5),
        ],
        "nan": [
            ("nan_01_upper", "ลุ่มน้ำน่านตอนบน (เฉลิมพระเกียรติ/ปัว/ท่าวังผา/เมืองน่าน)", 1),
            ("nan_02_mid_north", "ลุ่มน้ำน่านตอนกลางเหนือ (เวียงสา/นาน้อย/เขื่อนสิริกิติ์)", 2),
            ("nan_03_central", "ลุ่มน้ำน่านตอนกลาง (อุตรดิตถ์/ตรอน/พิชัย)", 3),
            ("nan_04_mid_south", "ลุ่มน้ำน่านตอนกลางใต้ (พรหมพิราม/พิษณุโลก/บางกระทุ่ม)", 4),
            ("nan_05_lower", "ลุ่มน้ำน่านตอนล่าง (พิจิตร/บางมูลนาก/ชุมแสง/ปากน้ำโพ)", 5),
        ],
        "ping": [
            ("ping_01_upper", "ลุ่มน้ำปิงตอนบน (เชียงดาว/แม่แตง/แม่ริม/เมืองเชียงใหม่)", 1),
            ("ping_02_mid_north", "ลุ่มน้ำปิงตอนกลางเหนือ (หางดง/ลำพูน/จอมทอง/ฮอด)", 2),
            ("ping_03_central", "ลุ่มน้ำปิงตอนกลาง (ดอยเต่า/เขื่อนภูมิพล/สามเงา)", 3),
            ("ping_04_mid_south", "ลุ่มน้ำปิงตอนกลางใต้ (บ้านตาก/เมืองตาก/วังเจ้า)", 4),
            ("ping_05_lower", "ลุ่มน้ำปิงตอนล่าง (โกสัมพีนคร/กำแพงเพชร/ขาณุวรลักษบุรี/บรรพตพิสัย/นครสวรรค์)", 5),
        ],
        "wang": [
            ("wang_01_upper", "ลุ่มน้ำวังตอนบน (พาน/วังเหนือ/แจ้ห่ม)", 1),
            ("wang_02_central", "ลุ่มน้ำวังตอนกลาง (เมืองลำปาง/เกาะคา/สบปราบ)", 2),
            ("wang_03_lower", "ลุ่มน้ำวังตอนล่าง (เถิน/แม่พริก/สามเงา/บรรจบแม่น้ำปิง)", 3),
        ],
        "chao-phraya": [
            ("chao_01_upper", "ลุ่มน้ำเจ้าพระยาตอนบน (ปากน้ำโพ/นครสวรรค์/โกรกพระ/พยุหะคีรี)", 1),
            ("chao_02_mid_north", "ลุ่มน้ำเจ้าพระยาตอนกลางเหนือ (อุทัยธานี/มโนรมย์/เขื่อนเจ้าพระยา/ชัยนาท)", 2),
            ("chao_03_central", "ลุ่มน้ำเจ้าพระยาตอนกลาง (อินทร์บุรี/สิงห์บุรี/พรหมบุรี/อ่างทอง/ป่าโมก)", 3),
            ("chao_04_mid_south", "ลุ่มน้ำเจ้าพระยาตอนกลางใต้ (บางปะอิน/พระนครศรีอยุธยา/ปทุมธานี/นนทบุรี)", 4),
            ("chao_05_lower", "ลุ่มน้ำเจ้าพระยาตอนล่าง (กรุงเทพฯ/สมุทรปราการ/อ่าวไทย)", 5),
        ],
        "pa-sak": [
            ("pasak_01_upper", "ลุ่มน้ำป่าสักตอนบน (ด่านซ้าย/หล่มเก่า/หล่มสัก/เมืองเพชรบูรณ์)", 1),
            ("pasak_02_central", "ลุ่มน้ำป่าสักตอนกลาง (หนองไผ่/วิเชียรบุรี/เขื่อนป่าสักชลสิทธิ์)", 2),
            ("pasak_03_lower", "ลุ่มน้ำป่าสักตอนล่าง (พัฒนานิคม/แก่งคอย/สระบุรี/พระนครศรีอยุธยา)", 3),
        ],
        "sakaekrang": [
            ("sakae_01_upper", "ลุ่มน้ำสะแกกรังตอนบน (ลาดยาว/สว่างอารมณ์)", 1),
            ("sakae_02_lower", "ลุ่มน้ำสะแกกรังตอนล่าง (ทัพทัน/เมืองอุทัยธานี/บรรจบเจ้าพระยา)", 2),
        ],
        "tha-chin": [
            ("thachin_01_upper", "ลุ่มน้ำท่าจีนตอนบน (วัดสิงห์/เดิมบางนางบวช/สามชุก)", 1),
            ("thachin_02_central", "ลุ่มน้ำท่าจีนตอนกลาง (เมืองสุพรรณบุรี/สองพี่น้อง/บางเลน)", 2),
            ("thachin_03_lower", "ลุ่มน้ำท่าจีนตอนล่าง (นครชัยศรี/สามพราน/กระทุ่มแบน/สมุทรสาคร/อ่าวไทย)", 3),
        ],
        "chi": [
            ("chi_01_upper", "ลุ่มน้ำชีตอนบน (หนองบัวแดง/เกษตรสมบูรณ์/ชัยภูมิ)", 1),
            ("chi_02_mid_north", "ลุ่มน้ำชีตอนกลางเหนือ (โคกโพธิ์ไชย/มัญจาคีรี/ขอนแก่น)", 2),
            ("chi_03_central", "ลุ่มน้ำชีตอนกลาง (โกสุมพิสัย/มหาสารคาม/ร้อยเอ็ด)", 3),
            ("chi_04_lower", "ลุ่มน้ำชีตอนล่าง (ยโสธร/มหาชนะชัย/กันทรารมย์/บรรจบแม่น้ำมูล)", 4),
        ],
        "mun": [
            ("mun_01_upper", "ลุ่มน้ำมูลตอนบน (ปักธงชัย/โชคชัย/นครราชสีมา)", 1),
            ("mun_02_mid_north", "ลุ่มน้ำมูลตอนกลางเหนือ (พิมาย/ชุมพวง/สตึก)", 2),
            ("mun_03_central", "ลุ่มน้ำมูลตอนกลาง (ท่าตูม/รัตนบุรี/ราษีไศล/ศรีสะเกษ)", 3),
            ("mun_04_lower", "ลุ่มน้ำมูลตอนล่าง (วารินชำราบ/อุบลราชธานี/โขงเจียม/บรรจบแม่น้ำโขง)", 4),
        ]
    }

    subbasin_defs = SUBBASIN_REGISTRY.get(basin.lower())

    if subbasin_defs:
        n_splits = len(subbasin_defs)
    else:
        # Dynamic generic generator for ANY other basin slug
        n_splits = max(3, min(6, int(round((max_lat - min_lat) / 0.7))))
        subbasin_defs = []
        for i in range(n_splits):
            if i == 0:
                pos_name = "ตอนบน (ต้นน้ำ)"
            elif i == n_splits - 1:
                pos_name = "ตอนล่าง (ท้ายน้ำ)"
            elif i == 1 and n_splits > 3:
                pos_name = "ตอนกลางเหนือ"
            elif i == n_splits - 2 and n_splits > 3:
                pos_name = "ตอนกลางใต้"
            else:
                pos_name = "ตอนกลาง"
            
            sub_id = f"{basin}_{i+1:02d}"
            sub_name = f"ลุ่มน้ำ{basin} {pos_name}"
            subbasin_defs.append((sub_id, sub_name, i + 1))

    lat_step = (max_lat - min_lat) / float(n_splits)
    features = []

    for i, (sub_id, sub_name, order) in enumerate(subbasin_defs):
        sub_min_lat = min_lat + (n_splits - 1 - i) * lat_step
        sub_max_lat = sub_min_lat + lat_step
        # Add slight overlap (0.02 deg ~ 2.2km) for seamless boundary hydrological connection
        sub_geom = box(min_lon - 0.05, sub_min_lat - 0.02, max_lon + 0.05, sub_max_lat + 0.02)

        downstream_id = subbasin_defs[i + 1][0] if (i + 1 < len(subbasin_defs)) else None

        features.append({
            "type": "Feature",
            "properties": {
                "subbasin_id": sub_id,
                "subbasin_name_th": sub_name,
                "basin_slug": basin,
                "order": order,
                "downstream_subbasin": downstream_id,
                "min_lat": sub_min_lat,
                "max_lat": sub_max_lat,
                "min_lon": min_lon,
                "max_lon": max_lon
            },
            "geometry": mapping(sub_geom)
        })

    geojson = {
        "type": "FeatureCollection",
        "features": features
    }
    save_geojson(geojson, output_path)
    print(f"  [OK] Saved {len(features)} sub-basins for '{basin}': {output_path}")
    return geojson


def download_alos_palsar_dem(
    terrain_dir: str,
    stations: List[Dict[str, Any]],
    username: Optional[str],
    password: Optional[str],
    chunk_size: int = 10
) -> str:
    """
    Searches and downloads ALOS PALSAR RTC 12.5m DEM tiles from NASA ASF DAAC into terrain_dir.
    Downloads in manageable chunks (default: 10 tiles/batch), unzips immediately, and deletes
    .zip archives after extraction to prevent filling up disk storage.
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

        granules_list = list(unique_granules.values())
        total_granules = len(granules_list)
        print(f"  [DEM] Found {len(results)} total granules -> filtered to {total_granules} unique spatial tiles.")

        # Process in chunks of chunk_size to optimize disk space
        num_chunks = math.ceil(total_granules / float(chunk_size)) if chunk_size > 0 else 1
        print(f"  [DEM] Chunked Download & Extraction: {num_chunks} batches (Batch size: {chunk_size} tiles/batch)...")

        for chunk_idx in range(num_chunks):
            start_i = chunk_idx * chunk_size
            end_i = min(total_granules, (chunk_idx + 1) * chunk_size)
            chunk_granules = granules_list[start_i:end_i]
            chunk_results = asf.ASFSearchResults(chunk_granules)

            print(f"\n  ┌─ [Chunk {chunk_idx + 1}/{num_chunks}] Downloading {len(chunk_granules)} tiles ({start_i + 1}-{end_i} of {total_granules})...")
            chunk_results.download(path=tiles_dir, session=session, processes=4)

            # Unzip each downloaded .zip immediately and delete .zip to reclaim disk space
            zip_files = glob.glob(os.path.join(tiles_dir, "*.zip"))
            extracted_count = 0
            freed_bytes = 0

            for zf_path in zip_files:
                try:
                    file_size = os.path.getsize(zf_path)
                    with zipfile.ZipFile(zf_path, 'r') as zf:
                        for member in zf.namelist():
                            if member.endswith(".dem.tif") or member.endswith("_dem.tif"):
                                filename = os.path.basename(member)
                                target_dest = os.path.join(extracted_dir, filename)
                                if not os.path.exists(target_dest):
                                    with zf.open(member) as source, open(target_dest, "wb") as target:
                                        target.write(source.read())
                                extracted_count += 1
                    # Remove .zip archive to save disk space
                    os.remove(zf_path)
                    freed_bytes += file_size
                except Exception as ex:
                    print(f"  │  [WARN] Failed to extract/cleanup {zf_path}: {ex}")

            freed_mb = freed_bytes / (1024 * 1024)
            print(f"  └─ ✅ [Chunk {chunk_idx + 1}/{num_chunks}] Extracted {extracted_count} DEM files, deleted .zip archives (Freed {freed_mb:.1f} MB disk space)")

    except Exception as e:
        print(f"❌ ERROR: Failed to download ALOS PALSAR DEM from ASF: {e}", file=sys.stderr)
        sys.exit(1)

    # Find and mosaic all downloaded/extracted *.dem.tif files
    dem_files = glob.glob(os.path.join(extracted_dir, "**", "*dem.tif"), recursive=True) + \
                glob.glob(os.path.join(tiles_dir, "**", "*dem.tif"), recursive=True)
    dem_files = list(set([f for f in dem_files if not f.endswith("raw_dem.tif")]))

    if not dem_files:
        print(f"❌ ERROR: No DEM GeoTIFF files found in {tiles_dir}", file=sys.stderr)
        sys.exit(1)

    print(f"\n  [MOSAIC] Merging {len(dem_files)} DEM tiles into {raw_dem_path}...")
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


def fetch_osm_waterways(
    basin: str,
    output_path: str,
    stations: List[Dict[str, Any]],
    force: bool = False
) -> Dict[str, Any]:
    """
    Downloads and caches high-resolution River & Stream Waterway Network from OpenStreetMap (OSM)
    via Overpass API for the target river basin.
    Tags: waterway=river, stream, canal.
    Returns standard GeoJSON FeatureCollection.
    """
    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
    if os.path.exists(output_path) and os.path.getsize(output_path) > 1024 and not force:
        print(f"  [CACHE] OSM Waterways already exist: {output_path}")
        with open(output_path, 'r', encoding='utf-8') as f:
            import json
            return json.load(f)

    print(f"  [OSM] Fetching OpenStreetMap Waterway Network for '{basin}'...")
    min_lat, min_lon, max_lat, max_lon = get_station_bbox(stations, buffer_deg=0.25)

    overpass_query = f"""
    [out:json][timeout:90];
    (
      way["waterway"~"river|stream|canal"]({min_lat},{min_lon},{max_lat},{max_lon});
    );
    out body geom;
    """

    headers = {
        "User-Agent": "FloodAnalysisModel/1.0 (Hydrological Research; https://github.com/flood-analysis-project)"
    }

    mirrors = [
        "https://overpass-api.de/api/interpreter",
        "https://overpass.kumi.systems/api/interpreter",
        "https://lz4.overpass-api.de/api/interpreter",
        "https://z.overpass-api.de/api/interpreter",
        "https://overpass.private.coffee/api/interpreter",
        "https://overpass.nchc.org.tw/api/interpreter"
    ]

    osm_data = None
    last_err = None
    for mirror_url in mirrors:
        try:
            print(f"        Querying Overpass mirror: {mirror_url} ...")
            resp = requests.post(mirror_url, data={"data": overpass_query}, headers=headers, timeout=60)
            if resp.status_code == 200:
                data_json = resp.json()
                if "elements" in data_json:
                    osm_data = data_json
                    break
                else:
                    last_err = "No elements in JSON response"
            else:
                last_err = f"HTTP {resp.status_code}: {resp.text[:100]}"
        except Exception as ex:
            last_err = str(ex)

    if not osm_data or "elements" not in osm_data:
        print(f"  [WARN] Failed to fetch OSM waterways ({last_err}). Creating placeholder GeoJSON.")
        empty_geojson = {"type": "FeatureCollection", "features": []}
        save_geojson(empty_geojson, output_path)
        return empty_geojson

    from scripts.modules.gis_utils import linestring_length_km

    features = []
    for elem in osm_data.get("elements", []):
        if elem.get("type") != "way" or "geometry" not in elem:
            continue
        coords = [[round(pt["lon"], 6), round(pt["lat"], 6)] for pt in elem["geometry"] if "lon" in pt and "lat" in pt]
        if len(coords) < 2:
            continue

        tags = elem.get("tags", {})
        length_km = linestring_length_km(coords)
        feat_id = f"osm_way_{elem['id']}"

        features.append({
            "type": "Feature",
            "id": feat_id,
            "properties": {
                "osm_id": elem["id"],
                "name": tags.get("name", ""),
                "name_th": tags.get("name:th", tags.get("name", "")),
                "name_en": tags.get("name:en", ""),
                "waterway": tags.get("waterway", "stream"),
                "length_km": round(length_km, 3)
            },
            "geometry": {
                "type": "LineString",
                "coordinates": coords
            }
        })

    geojson = {
        "type": "FeatureCollection",
        "features": features
    }
    save_geojson(geojson, output_path)
    print(f"  [OK] Saved {len(features)} OSM waterway features to: {output_path}")
    return geojson


def main():
    parser = argparse.ArgumentParser(description="Fetch GIS boundaries, HydroRIVERS, OSM Waterways, and ALOS PALSAR 12.5m DEM")
    parser.add_argument("--basin", type=str, default="yom", help="River basin slug (e.g. yom, nan, ping, wang, all)")
    parser.add_argument("--dir", type=str, default="./dataset", help="Dataset directory")
    parser.add_argument("--terrain-dir", type=str, default="./terrain", help="Terrain DEM directory (independent of dataset --dir)")
    parser.add_argument("--username", "-u", type=str, default=None, help="NASA Earthdata username")
    parser.add_argument("--password", "-p", type=str, default=None, help="NASA Earthdata password")
    parser.add_argument("--chunk-size", type=int, default=10, help="Number of DEM tiles per download chunk to optimize disk space (default: 10)")
    parser.add_argument("--force-osm", action="store_true", help="Force re-download OSM waterways")
    args = parser.parse_args()

    basin_list = ["yom", "nan", "ping", "wang", "chao-phraya"] if args.basin == "all" else [args.basin]

    for b in basin_list:
        # Smart path resolution for --dir (supports both './dataset' and './dataset/nan')
        if os.path.basename(os.path.normpath(args.dir)) == b:
            basin_dir = args.dir
        else:
            basin_dir = os.path.join(args.dir, b)

        # Smart path resolution for --terrain-dir
        if os.path.basename(os.path.normpath(args.terrain_dir)) == b:
            terrain_basin_dir = args.terrain_dir
        else:
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

        # 2. Sub-basins Boundary for 12.5m Cascade Processing
        subbasins_path = os.path.join(basin_dir, "gis", f"{b}_subbasins.geojson")
        fetch_subbasins_boundary(b, subbasins_path, all_st)

        # 3. OpenStreetMap Waterway Network (in dataset/{basin}/gis/osm_waterways.geojson)
        osm_path = os.path.join(basin_dir, "gis", "osm_waterways.geojson")
        fetch_osm_waterways(b, osm_path, all_st, force=args.force_osm)

        # 4. ALOS PALSAR 12.5m DEM (in terrain/{basin}/) with Chunked Download & Auto-Cleanup
        download_alos_palsar_dem(terrain_basin_dir, all_st, args.username, args.password, chunk_size=args.chunk_size)


if __name__ == "__main__":
    main()

