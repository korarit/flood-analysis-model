#!/usr/bin/env python3
"""
Step 1: Automated GIS & DEM Downloader
Downloads Basin Boundaries, HydroRIVERS, and ALOS PALSAR 12.5m RTC DEM Tiles
using NASA Earthdata credentials.
"""

import argparse
import csv
import glob
import hashlib
import json
import math
import os
import sys
import time
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
    linestring_length_km,
    haversine_distance
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


# Mapping of Basin Slug to ThaiWater BASIN_T Name and Code
THAIWATER_BASIN_URL = "https://www.thaiwater.net/json/boundary/basin.json"
THAIWATER_BASIN_MAPPING = {
    "salawin": ("สาละวิน", "1"),
    "khong-north": ("โขงเหนือ", "2"),
    "khong-ne": ("โขงตะวันออกเฉียงเหนือ", "3"),
    "chi": ("ชี", "4"),
    "mun": ("มูล", "5"),
    "ping": ("ปิง", "6"),
    "wang": ("วัง", "7"),
    "yom": ("ยม", "8"),
    "nan": ("น่าน", "9"),
    "chao-phraya": ("เจ้าพระยา", "10"),
    "sakaekrang": ("สะแกกรัง", "11"),
    "pa-sak": ("ป่าสัก", "12"),
    "tha-chin": ("ท่าจีน", "13"),
    "mae-klong": ("แม่กลอง", "14"),
    "bang-pakong": ("บางปะกง", "15"),
    "tonle-sap": ("โตนเลสาป", "16"),
    "east-coast": ("ชายฝั่งทะเลตะวันออก", "17"),
    "phetchaburi": ("เพชรบุรี-ประจวบคีรีขันธ์", "18"),
    "south-east-upper": ("ภาคใต้ฝั่งตะวันออกตอนบน", "19"),
    "songkhla-lake": ("ทะเลสาบสงขลา", "20"),
    "south-east-lower": ("ภาคใต้ฝั่งตะวันออกตอนล่าง", "21"),
    "south-west": ("ภาคใต้ฝั่งตะวันตก", "22"),
}


def _boundary_vertex_count(geom_obj) -> int:
    """Counts total exterior/interior vertices of a (Multi)Polygon shapely geometry."""
    try:
        polys = list(geom_obj.geoms) if geom_obj.geom_type == "MultiPolygon" else [geom_obj]
        n = 0
        for p in polys:
            if p.exterior is not None:
                n += len(p.exterior.coords)
            for ring in p.interiors:
                n += len(ring.coords)
        return n
    except Exception:
        return 0


def _validate_boundary_geometry(geom_obj, basin: str, source: str):
    """
    Ensures the boundary is a real (Multi)Polygon and warns when it is too coarse
    to be a concave basin outline (< 50 vertices is likely a rough frame).
    """
    if geom_obj.is_empty or geom_obj.geom_type not in ("Polygon", "MultiPolygon"):
        raise RuntimeError(
            f"Basin boundary for '{basin}' from {source} is not a (Multi)Polygon "
            f"(got {geom_obj.geom_type}). Cannot continue without a real basin polygon."
        )
    n_verts = _boundary_vertex_count(geom_obj)
    if n_verts < 50:
        print(f"  [WARN] Basin boundary for '{basin}' has only {n_verts} vertices (< 50) — "
              f"it may be a coarse frame rather than a real basin outline.")


def _boundary_fingerprint(basin_boundary_geojson: Optional[Dict[str, Any]]) -> str:
    """Stable fingerprint of the boundary geometry (coordinates rounded to 5 dp)."""
    import hashlib as _hashlib
    try:
        from shapely.geometry import shape as _shape
        feats = (basin_boundary_geojson or {}).get("features") or []
        geom = (feats[0] or {}).get("geometry") if feats else None
        if not geom:
            return ""
        g = _shape(geom)

        def _round_coords(obj):
            if isinstance(obj, (list, tuple)):
                if obj and isinstance(obj[0], (int, float)):
                    return [round(float(c), 5) for c in obj]
                return [_round_coords(o) for o in obj]
            return obj

        g2 = _shape({"type": geom.get("type"), "coordinates": _round_coords(geom.get("coordinates"))})
        return _hashlib.sha256(g2.wkt.encode("utf-8")).hexdigest()[:16]
    except Exception:
        return ""


def _boundary_from_subbasins(basin: str, boundary_path: str):
    """Fallback 3: dissolve already-downloaded subbasin polygons into one basin polygon."""
    from shapely.geometry import shape as _shape
    from shapely.ops import unary_union as _union

    sub_path = os.path.join(os.path.dirname(os.path.abspath(boundary_path)), f"{basin}_subbasins.geojson")
    if not os.path.exists(sub_path) or os.path.getsize(sub_path) <= 500:
        return None
    try:
        with open(sub_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        polys = []
        for feat in data.get('features', []):
            geom = feat.get('geometry') or {}
            if geom.get('type') not in ('Polygon', 'MultiPolygon'):
                continue
            g = _shape(geom)
            if not g.is_empty:
                polys.append(g)
        if not polys:
            return None
        merged = _union(polys)
        if merged.geom_type not in ('Polygon', 'MultiPolygon') or merged.is_empty:
            return None
        print(f"  [FALLBACK] Dissolved {len(polys)} sub-basin polygon(s) into a basin boundary.")
        return merged, "Sub-basin dissolve (local)"
    except Exception as ex:
        print(f"  [WARN] Sub-basin dissolve fallback failed: {ex}")
        return None


def _boundary_from_osm_admin(basin: str):
    """Fallback 4: fetch the administrative boundary polygon from OSM (Nominatim) and use it."""
    import requests as _requests
    from shapely.geometry import shape as _shape

    b_slug = basin.lower().strip()
    target_tuple = THAIWATER_BASIN_MAPPING.get(b_slug)
    query = target_tuple[0] if target_tuple else basin
    headers = {"User-Agent": "FloodAnalysisModel/1.0 (Hydrological Research)"}
    try:
        resp = _requests.get(
            "https://nominatim.openstreetmap.org/search",
            params={"q": query, "polygon_geojson": 1, "format": "json", "limit": 5},
            headers=headers, timeout=30
        )
        if resp.status_code != 200:
            return None
        for item in resp.json():
            geojson = item.get('geojson') or {}
            if geojson.get('type') not in ('Polygon', 'MultiPolygon'):
                continue
            try:
                g = _shape(geojson)
                if not g.is_empty and g.geom_type in ('Polygon', 'MultiPolygon'):
                    print(f"  [FALLBACK] Using OSM administrative boundary for '{query}'.")
                    return g, f"OSM boundary relation ({item.get('display_name', query)[:60]})"
            except Exception:
                continue
    except Exception as ex:
        print(f"  [WARN] OSM boundary fallback failed: {ex}")
    return None


BOUNDARY_MIN_VERTICES = 50  # fewer = a coarse frame / rectangular fallback, not a basin outline
_BOUNDARY_REJECT_SOURCE_MARKERS = ("bounding box", "bbox fallback", "station bbox")


def load_valid_boundary(basin: str, boundary_path: str, strict: bool = True) -> Optional[Dict[str, Any]]:
    """
    Shared strict validator for the LOCAL boundary cache (Step 1 of round 5).
    Returns the FeatureCollection only when the cache is a REAL basin polygon:
      - (Multi)Polygon geometry
      - >= BOUNDARY_MIN_VERTICES vertices (a 4-5 corner box is the old rectangular
        fallback — round 4 trusted it and the whole pipeline ran on a rectangle)
      - source label is not the old "Station Bounding Box Fallback"
    Returns None (with the rejection reason printed) when missing/invalid —
    callers either refetch (fetch_basin_gis) or fail fast (generate_flow_paths).
    """
    if not os.path.exists(boundary_path) or os.path.getsize(boundary_path) <= 500:
        return None
    try:
        with open(boundary_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except Exception as ex:
        print(f"  [BOUNDARY] Cache unreadable ({ex}): {boundary_path}")
        return None
    feats = data.get("features") or []
    if not feats:
        print(f"  [BOUNDARY] Cache has no features: {boundary_path}")
        return None
    geom = (feats[0] or {}).get("geometry") or {}
    props = feats[0].get("properties") or {}
    if geom.get("type") not in ("Polygon", "MultiPolygon"):
        print(f"  [BOUNDARY] Cache rejected: geometry is {geom.get('type')}, not a polygon "
              f"({boundary_path})")
        return None
    source = str(props.get("source", ""))
    if strict and any(marker in source.lower() for marker in _BOUNDARY_REJECT_SOURCE_MARKERS):
        print(f"  [BOUNDARY] Cache rejected: source='{source}' is the rectangular fallback "
              f"({boundary_path}) — a real basin polygon is mandatory")
        return None
    try:
        from shapely.geometry import shape as _shape
        n_verts = _boundary_vertex_count(_shape(geom))
    except Exception:
        n_verts = -1
    if strict and n_verts < BOUNDARY_MIN_VERTICES:
        print(f"  [BOUNDARY] Cache rejected: only {n_verts} vertices (< {BOUNDARY_MIN_VERTICES}) "
              f"— coarse frame, likely the old bbox rectangle ({boundary_path})")
        return None
    bounds = _shape(geom).bounds if n_verts >= 0 else None
    if bounds:
        print(f"  [CACHE] Basin boundary OK: {n_verts:,} vertices, "
              f"lon[{bounds[0]:.4f}, {bounds[2]:.4f}] lat[{bounds[1]:.4f}, {bounds[3]:.4f}] "
              f"(source={source or '-'})")
    return data


def fetch_basin_boundary(
    basin: str,
    output_path: str,
    stations: List[Dict[str, Any]],
    force: bool = False
) -> Dict[str, Any]:
    """
    Resolves the official ThaiWater River Basin Boundary Polygon with a mandatory
    fallback chain (NO rectangular station-bbox fallback — G5/F3):
      1) local cache file `{basin}_boundary.geojson` (skipped with force=True)
      2) thaiwater.net basin.json
      3) dissolve of already-downloaded `{basin}_subbasins.geojson`
      4) OSM administrative boundary relation (Nominatim)
    All fallbacks failing -> raises RuntimeError (fail fast; never run with a rectangle).
    """
    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)

    if force:
        print("  [FORCE] Re-fetching basin boundary from sources (cache will be overwritten)...")
    else:
        # Step 1 (round 5): the local cache must be a REAL basin polygon — coarse or
        # rectangular caches (the round-4 root cause) are rejected and refetched.
        cached = load_valid_boundary(basin, output_path)
        if cached is not None:
            return cached
        if os.path.exists(output_path):
            print("  [BOUNDARY] Existing boundary file rejected as coarse/rectangular — "
                  "refetching from sources (file will be overwritten)...")

    print(f"  [FETCH] Downloading Official Basin Boundary from ThaiWater for '{basin}'...")
    import requests
    from shapely.geometry import shape, mapping

    b_slug = basin.lower().strip()
    target_tuple = THAIWATER_BASIN_MAPPING.get(b_slug)
    matched_feature = None

    try:
        resp = requests.get(THAIWATER_BASIN_URL, timeout=30)
        if resp.status_code == 200:
            tw_data = resp.json()
            features = tw_data.get('features', [])
            for feat in features:
                props = feat.get('properties', {})
                b_name_t = props.get('BASIN_T', '').strip()
                b_code = str(props.get('BASIN_CODE', '')).strip()

                if target_tuple:
                    target_name, target_code = target_tuple
                    if b_name_t == target_name or b_code == target_code or target_name in b_name_t:
                        matched_feature = feat
                        break
                else:
                    if b_slug in b_name_t.lower():
                        matched_feature = feat
                        break
    except Exception as ex:
        print(f"  [WARN] Could not fetch ThaiWater basin boundary: {ex}")

    if matched_feature and matched_feature.get('geometry'):
        geom_obj = shape(matched_feature['geometry'])
        _validate_boundary_geometry(geom_obj, basin, "ThaiWater")
        bounds = geom_obj.bounds  # (minx, miny, maxx, maxy)
        basin_name_th = matched_feature.get('properties', {}).get('BASIN_T', f"ลุ่มน้ำ{basin}")
        print(f"  [OK] Successfully matched ThaiWater Official Boundary Polygon for '{basin_name_th}'!")

        geojson = {
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "properties": {
                        "basin_slug": basin,
                        "basin_name_th": f"ลุ่มน้ำ{basin_name_th.replace('ลุ่มน้ำ', '')}",
                        "source": "ThaiWater (HII Official)",
                        "min_lat": round(bounds[1], 5),
                        "max_lat": round(bounds[3], 5),
                        "min_lon": round(bounds[0], 5),
                        "max_lon": round(bounds[2], 5),
                    },
                    "geometry": mapping(geom_obj)
                }
            ]
        }
        save_geojson(geojson, output_path)
        print(f"  [OK] Saved basin boundary: {output_path}")
        return geojson

    # Fallback 3: dissolve sub-basins
    sub_result = _boundary_from_subbasins(basin, output_path)
    # Fallback 4: OSM administrative boundary
    if sub_result is None:
        sub_result = _boundary_from_osm_admin(basin)

    if sub_result is None:
        raise RuntimeError(
            f"❌ Cannot obtain basin boundary for '{basin}' (ThaiWater, sub-basin dissolve and "
            f"OSM fallbacks all failed). A real basin polygon is MANDATORY — rectangular "
            f"station-bbox fallbacks are no longer allowed.\n"
            f"   Fix: run `python scripts/fetch_basin_gis.py --basin {basin}` with network access, "
            f"or place a valid {basin}_boundary.geojson in the basin's gis/ directory."
        )

    geom_obj, source_label = sub_result
    _validate_boundary_geometry(geom_obj, basin, source_label)
    bounds = geom_obj.bounds
    geojson = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {
                    "basin_slug": basin,
                    "basin_name_th": f"ลุ่มน้ำ{basin}",
                    "source": source_label,
                    "min_lat": round(bounds[1], 5),
                    "max_lat": round(bounds[3], 5),
                    "min_lon": round(bounds[0], 5),
                    "max_lon": round(bounds[2], 5),
                },
                "geometry": mapping(geom_obj)
            }
        ]
    }
    save_geojson(geojson, output_path)
    print(f"  [OK] Saved basin boundary (source={source_label}): {output_path}")
    return geojson


def fetch_subbasins_boundary(basin: str, output_path: str, stations: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Builds or loads topological Sub-basin polygons for native 12.5m DEM Cascade processing.
    Partitions the river basin into ordered upstream-to-downstream sub-basins with wide overlap
    to ensure 100% continuous main river extraction without boundary gaps.
    """
    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
    min_lat, min_lon, max_lat, max_lon = get_station_bbox(stations, buffer_deg=0.25)

    if os.path.exists(output_path) and os.path.getsize(output_path) > 500:
        try:
            with open(output_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
                features = data.get('features', [])
                if features and len(features) >= 2:
                    print(f"  [CACHE] Sub-basins boundary already exists: {output_path}")
                    return data
        except Exception:
            pass

    print(f"  [SUBBASINS] Generating Topological Sub-basins for '{basin}' (Cascade 12.5m)...")
    from shapely.geometry import box, mapping, shape

    # Check if real basin boundary polygon is available
    boundary_path = os.path.join(os.path.dirname(output_path), f"{basin}_boundary.geojson")
    basin_poly = None
    if os.path.exists(boundary_path):
        try:
            with open(boundary_path, 'r', encoding='utf-8') as f:
                b_data = json.load(f)
                b_feat = b_data.get('features', [{}])[0]
                if b_feat.get('geometry'):
                    basin_poly = shape(b_feat['geometry'])
                    b_bounds = basin_poly.bounds
                    min_lon, min_lat, max_lon, max_lat = b_bounds
        except Exception:
            pass

    # Collect station coordinate pairs
    st_pairs = []
    for s in stations:
        try:
            lat = float(s['latitude']) if s.get('latitude') is not None else None
            lon = float(s['longitude']) if s.get('longitude') is not None else None
            if lat is not None and lon is not None:
                st_pairs.append((lat, lon))
        except (ValueError, TypeError):
            continue

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

        # Longitude range: use full basin longitude extent + buffer so curved main river channels are NEVER cut off
        sub_min_lon = min_lon - 0.05
        sub_max_lon = max_lon + 0.05

        # Add generous overlap (0.03 deg ~ 3.3km) for seamless boundary hydrological connection
        sub_geom = box(sub_min_lon, sub_min_lat - 0.03, sub_max_lon, sub_max_lat + 0.03)

        downstream_id = subbasin_defs[i + 1][0] if (i + 1 < len(subbasin_defs)) else None

        features.append({
            "type": "Feature",
            "properties": {
                "subbasin_id": sub_id,
                "subbasin_name_th": sub_name,
                "basin_slug": basin,
                "order": order,
                "downstream_subbasin": downstream_id,
                "min_lat": round(sub_min_lat, 5),
                "max_lat": round(sub_max_lat, 5),
                "min_lon": round(sub_min_lon, 5),
                "max_lon": round(sub_max_lon, 5)
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
    chunk_size: int = 10,
    force: bool = False
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

    if not force and os.path.exists(raw_dem_path) and os.path.getsize(raw_dem_path) > 1000:
        print(f"  [CACHE] Mosaic DEM already exists: {raw_dem_path}")
        return raw_dem_path
    if force and os.path.exists(raw_dem_path):
        print(f"  [FORCE] Re-downloading & re-mosaicking the ALOS DEM (raw_dem.tif will be overwritten)...")

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

    print(f"\n  [MOSAIC] Merging {len(dem_files)} DEM tiles into {raw_dem_path} (Low-RAM Streaming Engine)...")
    stream_mosaic_geotiffs(dem_files, raw_dem_path)
    return raw_dem_path


def stream_mosaic_geotiffs(dem_files: List[str], output_path: str, nodata: float = -9999.0):
    """
    Memory-efficient streaming mosaic that writes GeoTIFF tiles directly into the
    destination raster on disk window by window, without loading multi-gigabyte arrays into RAM.
    Requires only ~30-50 MB RAM regardless of how large the river basin is.
    """
    from rasterio.transform import from_bounds
    from rasterio.windows import from_bounds as window_from_bounds

    srcs = [rasterio.open(f) for f in dem_files]
    try:
        min_xs = [s.bounds.left for s in srcs]
        min_ys = [s.bounds.bottom for s in srcs]
        max_xs = [s.bounds.right for s in srcs]
        max_ys = [s.bounds.top for s in srcs]

        left = min(min_xs)
        bottom = min(min_ys)
        right = max(max_xs)
        top = max(max_ys)

        res_x = srcs[0].res[0]
        res_y = srcs[0].res[1]
        crs = srcs[0].crs

        width = int(round((right - left) / res_x))
        height = int(round((top - bottom) / res_y))
        transform = from_bounds(left, bottom, right, top, width, height)

        print(f"  [MOSAIC] Streaming {len(dem_files)} tiles into disk ({height:,} x {width:,} cells, Low-RAM mode)...")

        out_meta = {
            "driver": "GTiff",
            "height": height,
            "width": width,
            "count": 1,
            "dtype": "float32",
            "crs": crs,
            "transform": transform,
            "nodata": nodata,
            "compress": "deflate",
            "tiled": True,
            "blockxsize": 512,
            "blockysize": 512
        }

        with rasterio.open(output_path, "w", **out_meta) as dst:
            for idx, s in enumerate(srcs, 1):
                data = s.read(1)
                win = window_from_bounds(s.bounds.left, s.bounds.bottom, s.bounds.right, s.bounds.top, transform=transform)
                win = win.round_offsets().round_shape()
                dst.write(data, 1, window=win)
                del data
                if idx % 10 == 0 or idx == len(srcs):
                    print(f"        Streamed {idx}/{len(dem_files)} tiles to disk...")

        print(f"  [OK] Successfully created mosaic DEM: {output_path}")
    finally:
        for s in srcs:
            s.close()


# Overpass mirrors are tried in order until one returns a complete response
OVERPASS_MIRRORS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://lz4.overpass-api.de/api/interpreter",
    "https://z.overpass-api.de/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
    "https://overpass.nchc.org.tw/api/interpreter"
]

# Remarks indicating an incomplete Overpass response (must refetch / try next mirror)
_OVERPASS_BAD_REMARKS = ("timed out", "incomplete", "out of memory", "abort")


def _basin_query_geometry(basin_boundary_geojson: Optional[Dict[str, Any]]):
    """
    Extracts a shapely (Multi)Polygon from a basin boundary GeoJSON FeatureCollection.
    Returns (geometry_or_None, source_label).
    """
    if not basin_boundary_geojson:
        return None, "station_bbox"
    feats = basin_boundary_geojson.get("features") or []
    if not feats:
        return None, "station_bbox"
    geom = feats[0].get("geometry")
    if not geom:
        return None, "station_bbox"
    try:
        from shapely.geometry import shape
        g = shape(geom)
        if g.is_empty or g.geom_type not in ("Polygon", "MultiPolygon"):
            return None, "station_bbox"
        return g, "basin_polygon"
    except Exception:
        return None, "station_bbox"


def _overpass_poly_statements(geom: Any, tag_filter: str, max_polygons: int = 12) -> List[str]:
    """
    Converts a (Multi)Polygon into Overpass `poly:` filter statements.
    Simplifies each polygon to ~0.005 deg (~550m) to keep the query compact;
    irrelevant for waterway fetching since we only need basin-level coverage.
    """
    polys = list(geom.geoms) if geom.geom_type == "MultiPolygon" else [geom]
    polys = sorted(polys, key=lambda p: p.area, reverse=True)

    stmts = []
    for p in polys[:max_polygons]:
        poly = p
        if poly.geom_type == "MultiPolygon":
            poly = max(poly.geoms, key=lambda g: g.area)
        simp = poly.simplify(0.005, preserve_topology=True)
        if simp.geom_type == "MultiPolygon":
            simp = max(simp.geoms, key=lambda g: g.area)
        coords = list(simp.exterior.coords)
        if len(coords) < 4:
            continue
        # Overpass poly format: "lat1 lon1 lat2 lon2 ..."
        pairs = " ".join(f"{lat:.5f} {lon:.5f}" for lon, lat in coords)
        stmts.append(f'way[{tag_filter}](poly:"{pairs}");')
    return stmts


def _build_overpass_query(
    tag_filters: List[str],
    geom: Any,
    stations: List[Dict[str, Any]],
    station_bbox_buffer_deg: float = 0.35
) -> Tuple[str, str, str]:
    """
    Builds an Overpass QL query + fingerprint from basin polygon (preferred) or station bbox.
    When both basin polygon and stations are available, expands the query geometry to include
    the buffered convex hull of all stations (0.05 deg ~ 5.5 km buffer) so perimeter and ridge
    stations never suffer from boundary-clip dead-zones.
    Returns (overpass_query, fingerprint, source_label).
    """
    stmts: List[str] = []
    source_label = "basin_polygon"
    query_geom = geom
    if geom is not None and stations:
        try:
            from shapely.geometry import MultiPoint
            st_coords = [[float(s['longitude']), float(s['latitude'])]
                         for s in stations if s.get('latitude') is not None and s.get('longitude') is not None]
            if st_coords:
                st_hull = MultiPoint(st_coords).convex_hull.buffer(0.05)
                query_geom = geom.union(st_hull).buffer(0.02)
        except Exception:
            query_geom = geom

    if query_geom is not None:
        for tf in tag_filters:
            stmts.extend(_overpass_poly_statements(query_geom, tf, max_polygons=12))
    if not stmts:
        source_label = "station_bbox"
        min_lat, min_lon, max_lat, max_lon = get_station_bbox(stations, buffer_deg=station_bbox_buffer_deg)
        bbox = f"({min_lat},{min_lon},{max_lat},{max_lon})"
        stmts = [f'way[{tf}]{bbox};' for tf in tag_filters]

    query_body = "\n".join(stmts)
    overpass_query = f"[out:json][timeout:180];\n(\n{query_body}\n);\nout body geom;"
    fingerprint = hashlib.sha256(overpass_query.encode("utf-8")).hexdigest()[:16]
    return overpass_query, fingerprint, source_label


def _overpass_fetch(overpass_query: str) -> Tuple[Optional[Dict[str, Any]], str]:
    """
    Queries Overpass mirrors with long timeout and strict completeness validation.
    Returns (osm_data_or_None, last_error).
    """
    headers = {
        "User-Agent": "FloodAnalysisModel/1.0 (Hydrological Research; https://github.com/flood-analysis-project)"
    }
    osm_data = None
    last_err = ""
    for mirror_url in OVERPASS_MIRRORS:
        try:
            print(f"        Querying Overpass mirror: {mirror_url} ...")
            resp = requests.post(mirror_url, data={"data": overpass_query}, headers=headers, timeout=180)
            if resp.status_code != 200:
                last_err = f"HTTP {resp.status_code}: {resp.text[:100]}"
                continue
            data_json = resp.json()
            remark = str(data_json.get("remark") or "").lower()
            if "elements" not in data_json:
                last_err = "No elements in JSON response"
            elif any(k in remark for k in _OVERPASS_BAD_REMARKS):
                last_err = f"Incomplete response remark: {remark[:120]}"
            else:
                osm_data = data_json
                break
        except Exception as ex:
            last_err = str(ex)
    return osm_data, last_err


def _load_valid_cache(output_path: str, fingerprint: str, force: bool, require_crop: bool = False):
    """
    Loads cached geojson only when its fingerprint matches and status is not 'failed'.
    When require_crop is set, caches written from a rectangular station-bbox query
    (source=station_bbox) or missing the crop fingerprint are rejected (G1/F3: the
    rectangular fallback must never silently come back through an old cache).
    """
    if force or not os.path.exists(output_path) or os.path.getsize(output_path) <= 1024:
        return None
    try:
        import json
        with open(output_path, 'r', encoding='utf-8') as f:
            cached = json.load(f)
    except Exception:
        return None
    meta = cached.get("_meta") or {}
    if meta.get("status") == "failed":
        print(f"  [CACHE] Previous fetch failed for this fingerprint; refetching...")
        return None
    if meta.get("fingerprint") != fingerprint:
        print(f"  [CACHE] Fingerprint mismatch (cached={meta.get('fingerprint')}, want={fingerprint}); refetching...")
        return None
    if require_crop:
        if meta.get("source") == "station_bbox":
            print(f"  [CACHE] Cached OSM was fetched with a rectangular station bbox (source=station_bbox); refetching...")
            return None
        if not meta.get("crop_polygon"):
            print(f"  [CACHE] Cached OSM was never cropped to the basin polygon; refetching...")
            return None
    print(f"  [CACHE] Valid cached data (fingerprint={fingerprint}, fetched_at={meta.get('fetched_at')}): {output_path}")
    return cached


def _crop_buffer_deg(buffer_m: float) -> float:
    """Converts a meter buffer to degrees (latitude-scaled; Thailand ~15 deg N)."""
    return (buffer_m or 0.0) / 111_320.0


def crop_geojson_to_basin(
    geojson: Dict[str, Any],
    basin_boundary_geojson: Optional[Dict[str, Any]],
    buffer_m: float = 5000.0,
    label: str = "osm",
    stations: Optional[List[Dict[str, Any]]] = None
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """
    Phase 2 (G1/RC3): crops every OSM feature to the real basin polygon (+ station envelope buffer)
    using shapely, BEFORE the data enters processing or the cache:
      - features entirely outside the basin envelope are dropped
      - lines crossing the boundary keep all parts inside the basin (preserving multi-part branches)
    Returns (cropped_geojson, stats) where stats carries the filter-report counters
    required by the Flow Layer Filter Matrix (F1).
    """
    from shapely.geometry import shape as _shape, mapping as _mapping, MultiPoint
    from shapely.prepared import prep as _prep
    from shapely.strtree import STRtree  # noqa: F401  (kept for parity with pipeline index usage)
    from scripts.modules.gis_utils import linestring_length_km

    feats = geojson.get("features", []) if geojson else []
    stats = {"n_in": len(feats), "n_out": 0, "dropped_outside": 0, "clipped": 0, "crop_applied": False}

    basin_poly = None
    feats_b = (basin_boundary_geojson or {}).get("features") or []
    if feats_b:
        g = (feats_b[0] or {}).get("geometry") or {}
        if g.get("type") in ("Polygon", "MultiPolygon"):
            try:
                basin_poly = _shape(g)
            except Exception:
                basin_poly = None
    if basin_poly is None:
        stats["n_out"] = len(feats)
        return geojson, stats

    try:
        crop_poly = basin_poly.buffer(_crop_buffer_deg(buffer_m))
        if stations:
            st_coords = [[float(s['longitude']), float(s['latitude'])]
                         for s in stations if s.get('latitude') is not None and s.get('longitude') is not None]
            if st_coords:
                st_hull = MultiPoint(st_coords).convex_hull.buffer(_crop_buffer_deg(buffer_m))
                crop_poly = crop_poly.union(st_hull)
        crop_poly = crop_poly.simplify(0.0005, preserve_topology=True) or crop_poly
        prepared = _prep(crop_poly)
    except Exception as ex:
        print(f"  [WARN] {label}: could not prepare crop polygon ({ex}); crop skipped")
        stats["n_out"] = len(feats)
        return geojson, stats

    out_features: List[Dict[str, Any]] = []
    for feat in feats:
        geom = feat.get("geometry") or {}
        gtype = geom.get("type")
        try:
            g = _shape(geom) if gtype else None
        except Exception:
            g = None
        if g is None or g.is_empty:
            stats["dropped_outside"] += 1
            continue
        # cheap bbox pre-filter (O(N)) before the expensive intersection (O(K))
        if not prepared.intersects(g):
            stats["dropped_outside"] += 1
            continue
        if crop_poly.covers(g):
            out_features.append(feat)
            continue
        inter = g.intersection(crop_poly)
        if inter.is_empty:
            stats["dropped_outside"] += 1
            continue
        if gtype == "LineString":
            parts = [inter] if inter.geom_type == "LineString" else \
                [x for x in getattr(inter, "geoms", []) if x.geom_type == "LineString"]
            # Keep all parts that have length >= 50m (0.05 km)
            valid_parts = [p for p in parts if len(p.coords) >= 2 and linestring_length_km(list(p.coords)) >= 0.05]
            if not valid_parts:
                if parts and len(parts[0].coords) >= 2:
                    valid_parts = [max(parts, key=lambda p: p.length)]
                else:
                    stats["dropped_outside"] += 1
                    continue
            if len(valid_parts) == 1:
                coords = [[round(x, 6), round(y, 6)] for x, y in valid_parts[0].coords]
                nf = dict(feat)
                nf["geometry"] = {"type": "LineString", "coordinates": coords}
                props = dict(feat.get("properties", {}))
                try:
                    props["length_km"] = round(linestring_length_km(coords), 3)
                except Exception:
                    pass
                nf["properties"] = props
                out_features.append(nf)
                stats["clipped"] += 1
            else:
                coords_multi = [[[round(x, 6), round(y, 6)] for x, y in p.coords] for p in valid_parts]
                nf = dict(feat)
                nf["geometry"] = {"type": "MultiLineString", "coordinates": coords_multi}
                props = dict(feat.get("properties", {}))
                try:
                    total_len = sum(linestring_length_km(list(p.coords)) for p in valid_parts)
                    props["length_km"] = round(total_len, 3)
                except Exception:
                    pass
                nf["properties"] = props
                out_features.append(nf)
                stats["clipped"] += 1
        elif gtype == "Polygon":
            if inter.geom_type not in ("Polygon", "MultiPolygon"):
                stats["dropped_outside"] += 1
                continue
            nf = dict(feat)
            nf["geometry"] = _mapping(inter)
            out_features.append(nf)
            stats["clipped"] += 1
        elif gtype == "MultiLineString":
            parts = [x for x in getattr(inter, "geoms", []) if x.geom_type == "LineString"]
            if not parts:
                stats["dropped_outside"] += 1
                continue
            nf = dict(feat)
            nf["geometry"] = {"type": "MultiLineString",
                              "coordinates": [[[round(x, 6), round(y, 6)] for x, y in p.coords] for p in parts]}
            out_features.append(nf)
            stats["clipped"] += 1
        else:
            out_features.append(feat)
            continue

    stats["n_out"] = len(out_features)
    stats["crop_applied"] = True
    cropped = dict(geojson) if isinstance(geojson, dict) else {"type": "FeatureCollection"}
    cropped["features"] = out_features
    meta = dict(cropped.get("_meta") or {})
    meta["crop_polygon"] = _boundary_fingerprint(basin_boundary_geojson)
    meta["crop_buffer_m"] = buffer_m
    meta["crop_stats"] = {k: v for k, v in stats.items() if k != "crop_applied"}
    cropped["_meta"] = meta
    print(f"  [CROP] {label}: {stats['n_in']:,} -> {stats['n_out']:,} features "
          f"(dropped outside basin: {stats['dropped_outside']:,}, clipped: {stats['clipped']:,}, "
          f"buffer {buffer_m:.0f} m)")
    return cropped, stats


def fetch_osm_waterways(
    basin: str,
    output_path: str,
    stations: List[Dict[str, Any]],
    force: bool = False,
    basin_boundary_geojson: Optional[Dict[str, Any]] = None,
    crop_buffer_m: float = 2000.0
) -> Dict[str, Any]:
    """
    Downloads and caches high-resolution River & Stream Waterway Network from OpenStreetMap (OSM)
    via Overpass API, scoped to the official ThaiWater basin polygon when available
    (falls back to station bounding box + buffer).
    Tags: waterway=river, stream, canal, drain, ditch.
    Returns standard GeoJSON FeatureCollection with `_meta` fingerprint for cache validation.
    """
    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
    geom, source_label = _basin_query_geometry(basin_boundary_geojson)
    overpass_query, fingerprint, source_label = _build_overpass_query(
        ['"waterway"~"river|stream|canal|drain|ditch"'], geom, stations
    )

    cached = _load_valid_cache(output_path, fingerprint, force, require_crop=(source_label == "basin_polygon"))
    if cached is not None:
        return cached

    print(f"  [OSM] Fetching OpenStreetMap Waterway Network for '{basin}' "
          f"(source={source_label}, fingerprint={fingerprint})...")

    osm_data, last_err = _overpass_fetch(overpass_query)

    if not osm_data or "elements" not in osm_data:
        print(f"  [WARN] Failed to fetch OSM waterways ({last_err}).", file=sys.stderr)
        if os.path.exists(output_path) and os.path.getsize(output_path) > 1024:
            # Never destroy an existing good cache with an empty placeholder
            print(f"  [WARN] Keeping existing cache file: {output_path}", file=sys.stderr)
            try:
                with open(output_path, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except Exception:
                pass
        empty_geojson = {
            "type": "FeatureCollection",
            "_meta": {"fingerprint": fingerprint, "status": "failed", "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "n_features": 0},
            "features": []
        }
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
        "_meta": {
            "fingerprint": fingerprint,
            "source": source_label,
            "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "n_features": len(features)
        },
        "features": features
    }

    # Step 2b (round 5): split ways at implausible vertex jumps BEFORE caching —
    # a 2-node 10km gap becomes a straight edge in the backbone graph otherwise.
    geojson = sanitize_osm_way_jumps(geojson, label="osm_waterways")

    # Phase 2 (G1): crop to the real basin polygon BEFORE the cache is written —
    # never persist (or process) data scoped by a rectangular fallback.
    if source_label == "basin_polygon":
        geojson, _crop_stats = crop_geojson_to_basin(
            geojson, basin_boundary_geojson, buffer_m=crop_buffer_m, label="osm_waterways", stations=stations
        )

    save_geojson(geojson, output_path)
    print(f"  [OK] Saved {len(geojson.get('features', []))} OSM waterway features to: {output_path}")
    return geojson


def fetch_osm_water_polygons(
    basin: str,
    output_path: str,
    stations: List[Dict[str, Any]],
    force: bool = False,
    basin_boundary_geojson: Optional[Dict[str, Any]] = None,
    crop_buffer_m: float = 2000.0
) -> Dict[str, Any]:
    """
    Downloads and caches open water surfaces (natural=water, landuse=reservoir) from OSM
    as Polygon features. Used to hydro-enforce wide rivers / reservoirs into the DEM
    (stream burning) where no waterway LineString exists.
    """
    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
    geom, source_label = _basin_query_geometry(basin_boundary_geojson)
    overpass_query, fingerprint, source_label = _build_overpass_query(
        ['"natural"="water"', '"landuse"="reservoir"'], geom, stations
    )

    cached = _load_valid_cache(output_path, fingerprint, force, require_crop=(source_label == "basin_polygon"))
    if cached is not None:
        return cached

    print(f"  [OSM] Fetching OpenStreetMap Water Polygons for '{basin}' "
          f"(source={source_label}, fingerprint={fingerprint})...")

    osm_data, last_err = _overpass_fetch(overpass_query)

    if not osm_data or "elements" not in osm_data:
        print(f"  [WARN] Failed to fetch OSM water polygons ({last_err}). Continuing without water polygons.", file=sys.stderr)
        empty_geojson = {
            "type": "FeatureCollection",
            "_meta": {"fingerprint": fingerprint, "status": "failed", "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "n_features": 0},
            "features": []
        }
        # Only write placeholder when nothing usable exists (same policy as waterways)
        if not (os.path.exists(output_path) and os.path.getsize(output_path) > 1024):
            save_geojson(empty_geojson, output_path)
        return empty_geojson

    features = []
    for elem in osm_data.get("elements", []):
        if elem.get("type") != "way" or "geometry" not in elem:
            continue
        coords = [[round(pt["lon"], 6), round(pt["lat"], 6)] for pt in elem["geometry"] if "lon" in pt and "lat" in pt]
        # Closed ways only -> Polygon
        if len(coords) < 4 or coords[0] != coords[-1]:
            continue

        tags = elem.get("tags", {})
        feat_id = f"osm_water_poly_{elem['id']}"
        features.append({
            "type": "Feature",
            "id": feat_id,
            "properties": {
                "osm_id": elem["id"],
                "name": tags.get("name", ""),
                "name_th": tags.get("name:th", tags.get("name", "")),
                "water": tags.get("water", ""),
                "natural": tags.get("natural", ""),
                "landuse": tags.get("landuse", "")
            },
            "geometry": {
                "type": "Polygon",
                "coordinates": [coords]
            }
        })

    geojson = {
        "type": "FeatureCollection",
        "_meta": {
            "fingerprint": fingerprint,
            "source": source_label,
            "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "n_features": len(features)
        },
        "features": features
    }

    # Phase 2 (G1): crop water polygons to the real basin polygon before caching
    if source_label == "basin_polygon":
        geojson, _crop_stats = crop_geojson_to_basin(
            geojson, basin_boundary_geojson, buffer_m=crop_buffer_m, label="osm_water_polygons", stations=stations
        )

    save_geojson(geojson, output_path)
    print(f"  [OK] Saved {len(geojson.get('features', []))} OSM water polygon features to: {output_path}")
    return geojson


def sanitize_osm_way_jumps(
    geojson: Dict[str, Any],
    max_jump_km: float = 2.0,
    min_part_km: float = 1.0,
    label: str = "osm_waterways"
) -> Dict[str, Any]:
    """
    Step 2b (round 5): splits OSM ways at implausible internal vertex jumps
    (verified root cause of ~10km straight teleports: e.g. way 400328476 has a
    10.7km two-node gap that lands exactly on a flow-file hub). Each way becomes
    contiguous chunks; chunks shorter than min_part_km are dropped.

    IMPORTANT: the length filter applies ONLY to chunks created by a jump split —
    ways without jumps pass through untouched regardless of their length (the
    project rule "no length filter on OSM ways" is preserved; a first version of
    this filter accidentally dropped 867 short streams from the nan basin).

    Idempotent: sanitized data passes through unchanged (no jumps remain).
    LineString with a single surviving chunk stays a LineString; multiple chunks
    become a MultiLineString (supported downstream by the graph, burn, mask,
    snapping and the osm_river display layer). `_meta` carries the counters (F1).
    """
    if not geojson or not (geojson.get("features") or []):
        return geojson
    out_features: List[Dict[str, Any]] = []
    n_in = n_out = n_split = n_parts_dropped = n_ways_dropped = 0
    dropped_osm_ids: List[str] = []
    for feat in geojson.get("features", []):
        geom = feat.get("geometry") or {}
        if geom.get("type") != "LineString":
            out_features.append(feat)
            continue
        n_in += 1
        coords = geom.get("coordinates") or []
        if len(coords) < 2:
            out_features.append(feat)
            continue
        # split at jumps
        parts: List[List[List[float]]] = [[coords[0]]]
        has_jump = False
        for i in range(1, len(coords)):
            a, b = coords[i - 1], coords[i]
            jump_km = haversine_distance(a[1], a[0], b[1], b[0])
            if jump_km > max_jump_km:
                has_jump = True
                parts.append([coords[i]])
            else:
                parts[-1].append(coords[i])
        if not has_jump:
            # no jump -> keep the way exactly as-is (NEVER length-filter intact ways)
            out_features.append(feat)
            n_out += 1
            continue
        n_parts_before = len(parts)
        parts = [p for p in parts if len(p) >= 2 and linestring_length_km(p) >= min_part_km]
        n_parts_dropped += n_parts_before - len(parts)
        if not parts:
            n_ways_dropped += 1
            dropped_osm_ids.append(str(feat.get("properties", {}).get("osm_id", "") or feat.get("id", "")))
            continue
        n_split += 1
        if len(parts) == 1:
            nf = dict(feat)
            nf["geometry"] = {"type": "LineString", "coordinates": parts[0]}
            props = dict(feat.get("properties", {}))
            props["length_km"] = round(linestring_length_km(parts[0]), 3)
            nf["properties"] = props
            out_features.append(nf)
            n_out += 1
        else:
            nf = dict(feat)
            nf["geometry"] = {"type": "MultiLineString", "coordinates": parts}
            props = dict(feat.get("properties", {}))
            props["length_km"] = round(sum(linestring_length_km(p) for p in parts), 3)
            props["jump_split"] = True
            nf["properties"] = props
            out_features.append(nf)
            n_out += 1

    meta = dict(geojson.get("_meta") or {})
    meta["way_jump_stats"] = {
        "n_ways_in": n_in, "n_ways_out": n_out, "n_split": n_split,
        "n_ways_dropped": n_ways_dropped, "n_parts_dropped": n_parts_dropped,
        # Round 6 (Phase C): audit trail of every way removed entirely, so the
        # validator (and humans) can verify nothing was silently deleted.
        "dropped_osm_ids": dropped_osm_ids[:200],
        "max_jump_km": max_jump_km, "min_part_km": min_part_km,
    }
    geojson = dict(geojson)
    geojson["_meta"] = meta
    geojson["features"] = out_features
    if n_split or n_ways_dropped:
        print(f"  [JUMP-SPLIT] {label}: {n_in:,} ways -> {n_out:,} "
              f"(split at jumps > {max_jump_km} km: {n_split:,}, tiny ways dropped: {n_ways_dropped:,})")
    return geojson


def ensure_osm_cropped(
    geojson: Dict[str, Any],
    basin_boundary_geojson: Optional[Dict[str, Any]],
    output_path: str,
    buffer_m: float = 2000.0,
    label: str = "osm"
) -> Dict[str, Any]:
    """
    Phase 2.8 (idempotent recrop at load time): when an OSM cache loaded from disk has
    no `crop_polygon` fingerprint in its `_meta` (cache written before basin-crop was
    introduced) or the fingerprint no longer matches the current boundary, the cached
    features are cropped in memory and the cache is rewritten. Never requires a refetch.
    """
    if not geojson or not basin_boundary_geojson:
        return geojson
    feats = geojson.get("features") or []
    if not feats:
        return geojson
    meta = geojson.get("_meta") or {}
    if meta.get("source") == "station_bbox":
        # Rectangular-era cache must not be silently reused — force a refetch upstream
        raise RuntimeError(
            f"❌ OSM cache {output_path} was created with a rectangular station-bbox query "
            f"(source=station_bbox). A basin polygon is now mandatory.\n"
            f"   Fix: run `python scripts/fetch_basin_gis.py --basin <slug> --force-osm` to refetch."
        )
    want_fp = _boundary_fingerprint(basin_boundary_geojson)
    if meta.get("crop_polygon") == want_fp:
        return geojson

    cropped, _stats = crop_geojson_to_basin(geojson, basin_boundary_geojson, buffer_m=buffer_m, label=label)
    m = dict(cropped.get("_meta") or {})
    m["fingerprint"] = meta.get("fingerprint")
    m["source"] = meta.get("source", "basin_polygon")
    m["recropped_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    cropped["_meta"] = m
    try:
        save_geojson(cropped, output_path)
        print(f"  [CROP] Rewrote cache with basin-cropped features: {output_path}")
    except Exception as ex:
        print(f"  [WARN] Could not rewrite cropped cache {output_path}: {ex}")
    return cropped


def main():
    parser = argparse.ArgumentParser(description="Fetch GIS boundaries, HydroRIVERS, OSM Waterways, and ALOS PALSAR 12.5m DEM")
    parser.add_argument("--basin", type=str, default="yom", help="River basin slug (e.g. yom, nan, ping, wang, all)")
    parser.add_argument("--dir", type=str, default="./dataset", help="Dataset directory")
    parser.add_argument("--terrain-dir", type=str, default="./terrain", help="Terrain DEM directory (independent of dataset --dir)")
    parser.add_argument("--username", "-u", type=str, default=None, help="NASA Earthdata username")
    parser.add_argument("--password", "-p", type=str, default=None, help="NASA Earthdata password")
    parser.add_argument("--chunk-size", type=int, default=10, help="Number of DEM tiles per download chunk to optimize disk space (default: 10)")
    parser.add_argument("--force", action="store_true",
                        help="Force re-fetch of EVERYTHING: basin boundary, OSM waterways/polygons, "
                             "and the ALOS DEM re-download & re-mosaic (heavy)")
    parser.add_argument("--force-osm", action="store_true",
                        help="Force re-download of OSM waterways/polygons only (boundary cache kept)")
    parser.add_argument("--force-dem", action="store_true",
                        help="Force re-download & re-mosaic of the ALOS DEM from NASA ASF (heavy)")
    parser.add_argument("--crop-buffer-m", type=float, default=2000.0,
                        help="Buffer in meters applied to the basin polygon when cropping OSM data "
                             "(default: 2000; keeps hydrology connected at the basin edge)")
    args = parser.parse_args()

    force_osm = args.force or args.force_osm
    force_dem = args.force or args.force_dem

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

        # 1. Basin Boundary (in dataset/{basin}/gis/) — MANDATORY (G5): no polygon, no pipeline
        boundary_path = os.path.join(basin_dir, "gis", f"{b}_boundary.geojson")
        try:
            fetch_basin_boundary(b, boundary_path, all_st, force=args.force)
        except RuntimeError as ex:
            print(f"❌ ERROR: {ex}", file=sys.stderr)
            sys.exit(1)

        # 2. Sub-basins Boundary for 12.5m Cascade Processing
        subbasins_path = os.path.join(basin_dir, "gis", f"{b}_subbasins.geojson")
        fetch_subbasins_boundary(b, subbasins_path, all_st)

        # 3. OpenStreetMap Waterway Network (in dataset/{basin}/gis/osm_waterways.geojson)
        boundary_geojson = None
        try:
            with open(boundary_path, 'r', encoding='utf-8') as f:
                boundary_geojson = json.load(f)
        except Exception:
            boundary_geojson = None
        osm_path = os.path.join(basin_dir, "gis", "osm_waterways.geojson")
        fetch_osm_waterways(b, osm_path, all_st, force=force_osm,
                            basin_boundary_geojson=boundary_geojson, crop_buffer_m=args.crop_buffer_m)

        # 3b. OpenStreetMap Water Polygons for reservoirs / wide rivers (stream burning support)
        water_polygons_path = os.path.join(basin_dir, "gis", "osm_water_polygons.geojson")
        fetch_osm_water_polygons(b, water_polygons_path, all_st, force=force_osm,
                                 basin_boundary_geojson=boundary_geojson, crop_buffer_m=args.crop_buffer_m)

        # 4. ALOS PALSAR 12.5m DEM (in terrain/{basin}/) with Chunked Download & Auto-Cleanup
        download_alos_palsar_dem(terrain_basin_dir, all_st, args.username, args.password,
                                 chunk_size=args.chunk_size, force=force_dem)


if __name__ == "__main__":
    main()

