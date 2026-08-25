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
    """Calculate length of a LineString coordinates list [[lon, lat], ...]."""
    if not coordinates or len(coordinates) < 2:
        return 0.0
    total = 0.0
    for i in range(len(coordinates) - 1):
        lon1, lat1 = coordinates[i][0], coordinates[i][1]
        lon2, lat2 = coordinates[i + 1][0], coordinates[i + 1][1]
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


def save_geojson(data: Dict[str, Any], filepath: str, indent: int = 2):
    """Save dictionary as a GeoJSON file ensuring directory exists."""
    os.makedirs(os.path.dirname(os.path.abspath(filepath)), exist_ok=True)
    with open(filepath, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=indent)


def save_json(data: Any, filepath: str, indent: int = 2):
    """Save data as JSON file ensuring directory exists."""
    os.makedirs(os.path.dirname(os.path.abspath(filepath)), exist_ok=True)
    with open(filepath, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=indent)


def load_stations_for_basin(basin_dir: str) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Loads water level and rainfall stations from dataset/{basin}/station/."""
    import csv
    import glob
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

