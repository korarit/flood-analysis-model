"""
GIS & Geospatial Helper Utilities
Provides coordinate math, bounding box calculation, Haversine distance,
WGS84 / UTM 47N coordinate conversions, and GeoJSON I/O.
"""

import json
import math
import os
from typing import Dict, List, Tuple, Any, Optional

EARTH_RADIUS_KM = 6371.0088


def haversine_distance(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Calculate the great-circle distance between two points in kilometers."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2.0) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2.0) ** 2
    c = 2.0 * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))
    return EARTH_RADIUS_KM * c


def linestring_length_km(coordinates: List[List[float]]) -> float:
    """Calculate length of a LineString coordinates list [[lon, lat], ...]. Supports both WGS84 and Projected (UTM) coords."""
    if not coordinates or len(coordinates) < 2:
        return 0.0
    total = 0.0
    for i in range(len(coordinates) - 1):
        lon1, lat1 = coordinates[i][0], coordinates[i][1]
        lon2, lat2 = coordinates[i + 1][0], coordinates[i + 1][1]
        # Check if coordinates are in projected meters (UTM: abs > 180 deg)
        if abs(lon1) > 180.0 or abs(lat1) > 90.0 or abs(lon2) > 180.0 or abs(lat2) > 90.0:
            dx = lon2 - lon1
            dy = lat2 - lat1
            total += math.sqrt(dx * dx + dy * dy) / 1000.0
        else:
            total += haversine_distance(lat1, lon1, lat2, lon2)
    return total


def get_station_bbox(stations: List[Dict[str, Any]], buffer_deg: float = 0.25) -> Tuple[float, float, float, float]:
    """
    Returns (min_lat, min_lon, max_lat, max_lon) with buffer.
    Filters out spatial coordinate outliers using robust IQR filtering.
    """
    pairs = []
    for s in stations:
        try:
            lat = float(s['latitude']) if s.get('latitude') is not None else None
            lon = float(s['longitude']) if s.get('longitude') is not None else None
            if lat is not None and lon is not None:
                # Basic sanity filter for Thailand bounding region
                if 5.0 <= lat <= 22.0 and 96.0 <= lon <= 107.0:
                    pairs.append((lat, lon))
        except (ValueError, TypeError):
            continue

    if not pairs:
        raise ValueError("No valid station coordinates found to calculate bounding box.")

    lats = [p[0] for p in pairs]
    lons = [p[1] for p in pairs]

    if len(pairs) >= 5:
        sorted_lats = sorted(lats)
        sorted_lons = sorted(lons)
        n = len(pairs)
        q25_idx = int(0.25 * n)
        q75_idx = int(0.75 * n)

        lat_q25, lat_q75 = sorted_lats[q25_idx], sorted_lats[q75_idx]
        lon_q25, lon_q75 = sorted_lons[q25_idx], sorted_lons[q75_idx]

        lat_iqr = max(lat_q75 - lat_q25, 0.3)
        lon_iqr = max(lon_q75 - lon_q25, 0.3)

        min_valid_lat = lat_q25 - 2.5 * lat_iqr
        max_valid_lat = lat_q75 + 2.5 * lat_iqr
        min_valid_lon = lon_q25 - 2.5 * lon_iqr
        max_valid_lon = lon_q75 + 2.5 * lon_iqr

        clean_lats = [lat for lat in lats if min_valid_lat <= lat <= max_valid_lat]
        clean_lons = [lon for lon in lons if min_valid_lon <= lon <= max_valid_lon]

        if len(clean_lats) < len(lats) or len(clean_lons) < len(lons):
            outlier_count = len(lats) - min(len(clean_lats), len(clean_lons))
            print(f"  [BBOX] Filtered {outlier_count} coordinate outlier(s) when calculating basin bbox.")
            lats = clean_lats if clean_lats else lats
            lons = clean_lons if clean_lons else lons

    return (
        max(-90.0, min(lats) - buffer_deg),
        max(-180.0, min(lons) - buffer_deg),
        min(90.0, max(lats) + buffer_deg),
        min(180.0, max(lons) + buffer_deg),
    )


def bbox_to_wkt(min_lat: float, min_lon: float, max_lat: float, max_lon: float) -> str:
    """Convert bounding box to WKT Polygon."""
    return f"POLYGON(({min_lon} {min_lat}, {max_lon} {min_lat}, {max_lon} {max_lat}, {min_lon} {max_lat}, {min_lon} {min_lat}))"


def _json_serialize_default(obj: Any) -> Any:
    """Universal JSON serializer fallback handling numpy scalars, arrays, and sets."""
    if hasattr(obj, 'item'):  # numpy scalar (int32, int64, float32, float64, bool_)
        return obj.item()
    if hasattr(obj, 'tolist'):  # numpy ndarray
        return obj.tolist()
    if isinstance(obj, set):
        return list(obj)
    return str(obj)


def dumps_compact_json(data: Any) -> str:
    """Serializes to compact JSON (single pass) — reused by raw and gzip writers."""
    return json.dumps(data, ensure_ascii=False, separators=(',', ':'), default=_json_serialize_default)


def write_geojson_pair(data: Dict[str, Any], filepath: str, write_gzip: bool = True) -> Tuple[int, int]:
    """
    G3: writes a compact .geojson and (optionally) its .gz sibling, serializing once.
    Returns (raw_bytes, gz_bytes).
    """
    payload = dumps_compact_json(data)
    os.makedirs(os.path.dirname(os.path.abspath(filepath)), exist_ok=True)
    with open(filepath, 'w', encoding='utf-8') as f:
        f.write(payload)
    gz_path = filepath + ".gz"
    gz_bytes = 0
    if write_gzip:
        import gzip
        with open(gz_path, 'wb') as fh:
            with gzip.GzipFile(fileobj=fh, mode='wb', compresslevel=9, mtime=0) as gz:
                gz.write(payload.encode('utf-8'))
        gz_bytes = os.path.getsize(gz_path)
    return len(payload.encode('utf-8')), gz_bytes


def load_geojson(filepath: str) -> Dict[str, Any]:
    """Load a GeoJSON file."""
    with open(filepath, 'r', encoding='utf-8') as f:
        return json.load(f)


def save_geojson(data: Dict[str, Any], filepath: str, indent: Optional[int] = 2):
    """Save dictionary as a GeoJSON file ensuring directory exists."""
    os.makedirs(os.path.dirname(os.path.abspath(filepath)), exist_ok=True)
    with open(filepath, 'w', encoding='utf-8') as f:
        if indent is None:
            json.dump(data, f, ensure_ascii=False, separators=(',', ':'), default=_json_serialize_default)
        else:
            json.dump(data, f, ensure_ascii=False, indent=indent, default=_json_serialize_default)


def save_json(data: Any, filepath: str, indent: Optional[int] = 2):
    """Save data as JSON file ensuring directory exists."""
    os.makedirs(os.path.dirname(os.path.abspath(filepath)), exist_ok=True)
    with open(filepath, 'w', encoding='utf-8') as f:
        if indent is None:
            json.dump(data, f, ensure_ascii=False, separators=(',', ':'), default=_json_serialize_default)
        else:
            json.dump(data, f, ensure_ascii=False, indent=indent, default=_json_serialize_default)


def load_stations_for_basin(basin_dir: str) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Loads water level and rainfall stations from dataset/{basin}/station/."""
    import csv
    import glob

    def _load_and_dedup(pattern: str) -> List[Dict[str, Any]]:
        st_dir = os.path.join(basin_dir, "station")
        unique_stations: Dict[str, Dict[str, Any]] = {}
        for csv_file in sorted(glob.glob(os.path.join(st_dir, pattern))):
            with open(csv_file, 'r', encoding='utf-8-sig') as f:
                for row in csv.DictReader(f):
                    lat = row.get('latitude')
                    lon = row.get('longitude')
                    if lat and lon:
                        st_id = (
                            row.get('station_id') or
                            row.get('station_code') or
                            row.get('code') or
                            row.get('id') or
                            ''
                        ).strip()
                        if not st_id:
                            st_id = f"{float(lat):.4f}_{float(lon):.4f}"
                        row['station_id'] = st_id

                        st_name = (
                            row.get('station_name_th') or
                            row.get('station_name_en') or
                            row.get('station_name') or
                            row.get('name') or
                            ''
                        ).strip()
                        row['station_name'] = st_name

                        if st_id not in unique_stations:
                            unique_stations[st_id] = row
                        else:
                            for k, v in row.items():
                                if v and not unique_stations[st_id].get(k):
                                    unique_stations[st_id][k] = v

        return list(unique_stations.values())

    water_stations = _load_and_dedup("*waterlevel*.csv")
    rain_stations = _load_and_dedup("*rain*.csv")
    return water_stations, rain_stations

