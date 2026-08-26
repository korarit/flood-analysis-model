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
    """
    lats = [float(s['latitude']) for s in stations if s.get('latitude')]
    lons = [float(s['longitude']) for s in stations if s.get('longitude')]
    if not lats or not lons:
        raise ValueError("No valid station coordinates found to calculate bounding box.")
    return (
        max(-90.0, min(lats) - buffer_deg),
        max(-180.0, min(lons) - buffer_deg),
        min(90.0, max(lats) + buffer_deg),
        min(180.0, max(lons) + buffer_deg),
    )


def bbox_to_wkt(min_lat: float, min_lon: float, max_lat: float, max_lon: float) -> str:
    """Convert bounding box to WKT Polygon."""
    return f"POLYGON(({min_lon} {min_lat}, {max_lon} {min_lat}, {max_lon} {max_lat}, {min_lon} {max_lat}, {min_lon} {min_lat}))"


def load_geojson(filepath: str) -> Dict[str, Any]:
    """Load a GeoJSON file."""
    with open(filepath, 'r', encoding='utf-8') as f:
        return json.load(f)


def save_geojson(data: Dict[str, Any], filepath: str, indent: Optional[int] = 2):
    """Save dictionary as a GeoJSON file ensuring directory exists."""
    os.makedirs(os.path.dirname(os.path.abspath(filepath)), exist_ok=True)
    with open(filepath, 'w', encoding='utf-8') as f:
        if indent is None:
            json.dump(data, f, ensure_ascii=False, separators=(',', ':'))
        else:
            json.dump(data, f, ensure_ascii=False, indent=indent)


def save_json(data: Any, filepath: str, indent: Optional[int] = 2):
    """Save data as JSON file ensuring directory exists."""
    os.makedirs(os.path.dirname(os.path.abspath(filepath)), exist_ok=True)
    with open(filepath, 'w', encoding='utf-8') as f:
        if indent is None:
            json.dump(data, f, ensure_ascii=False, separators=(',', ':'))
        else:
            json.dump(data, f, ensure_ascii=False, indent=indent)


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

