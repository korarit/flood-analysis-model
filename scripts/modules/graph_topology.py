"""
Graph Topology & River Network Routing Module
Handles Station Snapping, In-degree >= 2 Confluence Detection,
Overland Flow Path Tracing (Rain -> Water Level), Gauge-to-Gauge Flow Paths,
and Sub-catchment Delineation.
"""

import math
from typing import Dict, List, Tuple, Any, Optional, Set
import numpy as np
from rasterio.transform import Affine, rowcol
from shapely.geometry import Point, LineString, Polygon, mapping
from shapely.ops import unary_union
from .gis_utils import haversine_distance, linestring_length_km
from .terrain_engine import D8_DELTAS


def snap_stations_to_stream(
    stations: List[Dict[str, Any]],
    fdir: np.ndarray,
    acc: np.ndarray,
    transform: Affine,
    search_radius_cells: int = 15,
    min_acc_cells: int = 100
) -> List[Dict[str, Any]]:
    """
    Snaps each water level station to the highest flow accumulation cell
    within `search_radius_cells` to align with the actual raster stream channel.
    """
    nrows, ncols = fdir.shape
    snapped = []

    for st in stations:
        lat = float(st['latitude'])
        lon = float(st['longitude'])
        r, c = rowcol(transform, lon, lat)

        best_r, best_c = r, c
        best_acc = -1

        if 0 <= r < nrows and 0 <= c < ncols:
            r_min = max(0, r - search_radius_cells)
            r_max = min(nrows, r + search_radius_cells + 1)
            c_min = max(0, c - search_radius_cells)
            c_max = min(ncols, c + search_radius_cells + 1)

            for cr in range(r_min, r_max):
                for cc in range(c_min, c_max):
                    if acc[cr, cc] > best_acc and acc[cr, cc] >= min_acc_cells:
                        best_acc = acc[cr, cc]
                        best_r, best_c = cr, cc

        # Convert back to lon, lat
        snapped_lon, snapped_lat = transform * (best_c + 0.5, best_r + 0.5)
        offset_m = haversine_distance(lat, lon, snapped_lat, snapped_lon) * 1000.0

        st_copy = dict(st)
        st_copy['orig_latitude'] = lat
        st_copy['orig_longitude'] = lon
        st_copy['latitude'] = round(snapped_lat, 6)
        st_copy['longitude'] = round(snapped_lon, 6)
        st_copy['grid_row'] = best_r
        st_copy['grid_col'] = best_c
        st_copy['flow_acc_cells'] = int(acc[best_r, best_c]) if 0 <= best_r < nrows and 0 <= best_c < ncols else 0
        st_copy['snap_offset_meters'] = round(offset_m, 1)
        snapped.append(st_copy)

    return snapped


def detect_confluences(
    fdir: np.ndarray,
    acc: np.ndarray,
    transform: Affine,
    crs: Any = None,
    min_acc_cells: int = 500
) -> Dict[str, Any]:
    """
    Detects Confluence Points (nodes where in-degree >= 2 in the river network).
    Returns a GeoJSON FeatureCollection of confluence Point features.
    """
    from rasterio.warp import transform as warp_coords

    nrows, ncols = fdir.shape
    in_degree = np.zeros((nrows, ncols), dtype=np.int32)

    # Compute in-degree only along stream channels (1000x faster)
    stream_rows, stream_cols = np.where(acc >= min_acc_cells)
    for r, c in zip(stream_rows, stream_cols):
        code = int(fdir[r, c])
        if code in D8_DELTAS:
            dr, dc = D8_DELTAS[code]
            nr, nc = r + dr, c + dc
            if 0 <= nr < nrows and 0 <= nc < ncols and acc[nr, nc] >= min_acc_cells:
                in_degree[nr, nc] += 1

    features = []
    junction_count = 0
    is_geographic = (crs is None) or getattr(crs, 'is_geographic', False) or (str(crs) == "EPSG:4326")

    junc_rows, junc_cols = np.where((in_degree >= 2) & (acc >= min_acc_cells))
    for r, c in zip(junc_rows, junc_cols):
        junction_count += 1
        x, y = transform * (c + 0.5, r + 0.5)
        if not is_geographic and crs is not None:
            try:
                xs, ys = warp_coords(crs, "EPSG:4326", [x], [y])
                lon, lat = xs[0], ys[0]
            except Exception:
                lon, lat = x, y
        else:
            lon, lat = x, y

        junction_id = f"JUNC_{junction_count:04d}"
        features.append({
            "type": "Feature",
            "id": junction_id,
            "properties": {
                "junction_id": junction_id,
                "in_degree": int(in_degree[r, c]),
                "flow_acc_cells": int(acc[r, c]),
                "grid_row": int(r),
                "grid_col": int(c)
            },
            "geometry": {
                "type": "Point",
                "coordinates": [round(lon, 6), round(lat, 6)]
            }
        })

    return {
        "type": "FeatureCollection",
        "features": features
    }


def trace_downstream_path(
    start_r: int,
    start_c: int,
    fdir: np.ndarray,
    transform: Affine,
    stop_condition_fn=None,
    max_steps: int = 2000
) -> Tuple[List[List[float]], Optional[Any]]:
    """
    Traces D8 flow path downstream cell by cell.
    Returns (coordinates_list [[lon, lat], ...], stop_data).
    """
    nrows, ncols = fdir.shape
    coords = []
    curr_r, curr_c = start_r, start_c
    visited: Set[Tuple[int, int]] = set()
    stop_data = None

    for _ in range(max_steps):
        if not (0 <= curr_r < nrows and 0 <= curr_c < ncols):
            break
        if (curr_r, curr_c) in visited:
            break  # cycle protection
        visited.add((curr_r, curr_c))

        lon, lat = transform * (curr_c + 0.5, curr_r + 0.5)
        coords.append([round(lon, 6), round(lat, 6)])

        if stop_condition_fn:
            should_stop, data = stop_condition_fn(curr_r, curr_c)
            if should_stop:
                stop_data = data
                break

        code = int(fdir[curr_r, curr_c])
        if code not in D8_DELTAS:
            break
        dr, dc = D8_DELTAS[code]
        curr_r, curr_c = curr_r + dr, curr_c + dc

    return coords, stop_data


def build_flow_paths_and_relations(
    water_stations: List[Dict[str, Any]],
    rain_stations: List[Dict[str, Any]],
    fdir: np.ndarray,
    acc: np.ndarray,
    filled_dem: np.ndarray,
    transform: Affine
) -> Tuple[Dict[str, Any], List[Dict[str, Any]], List[Dict[str, Any]]]:
    """
    Generates:
    1. flow_paths.geojson (LineString vector features for Frontend Map)
    2. station_relations (Gauge -> Downstream Gauge)
    3. rainfall_relations (Rain Gauge -> Receiving Water Gauge)
    """
    nrows, ncols = fdir.shape

    # Map grid coordinates to water station IDs
    water_grid_map = {}
    for st in water_stations:
        r, c = st.get('grid_row'), st.get('grid_col')
        if r is not None and c is not None:
            # Also register a 3x3 neighborhood around station to ensure intersection
            for dr in (-1, 0, 1):
                for dc in (-1, 0, 1):
                    water_grid_map[(r + dr, c + dc)] = st['station_id']

    features = []
    gauge_relations = []
    rainfall_relations = []

    # 1. Trace Gauge-to-Gauge Flow Paths (Upstream -> Downstream)
    for st in water_stations:
        st_id = st['station_id']
        start_r = st.get('grid_row')
        start_c = st.get('grid_col')
        if start_r is None or start_c is None:
            continue

        # Trace downstream until hitting the next water station
        def make_stop_fn(origin_id):
            def stop_fn(r, c):
                target_id = water_grid_map.get((r, c))
                if target_id and target_id != origin_id:
                    return True, target_id
                return False, None
            return stop_fn

        # Step 1 step forward first to avoid immediate self-collision
        code = int(fdir[start_r, start_c])
        if code in D8_DELTAS:
            dr, dc = D8_DELTAS[code]
            first_r, first_c = start_r + dr, start_c + dc
            coords, target_station_id = trace_downstream_path(
                first_r, first_c, fdir, transform,
                stop_condition_fn=make_stop_fn(st_id),
                max_steps=1500
            )
            # Prepend start station coordinate
            st_lon, st_lat = transform * (start_c + 0.5, start_r + 0.5)
            coords = [[round(st_lon, 6), round(st_lat, 6)]] + coords

            if target_station_id and len(coords) >= 2:
                dist_km = linestring_length_km(coords)
                z_up = float(filled_dem[start_r, start_c])
                target_st = next((s for s in water_stations if s['station_id'] == target_station_id), None)
                z_down = float(filled_dem[target_st['grid_row'], target_st['grid_col']]) if target_st else z_up
                dz = max(0.0, z_up - z_down)
                slope = (dz / (dist_km * 1000.0)) if dist_km > 0 else 0.0001

                feature_id = f"flow_gauge_{st_id}_to_{target_station_id}"
                feature = {
                    "type": "Feature",
                    "id": feature_id,
                    "properties": {
                        "feature_type": "gauge_to_gauge_flowpath",
                        "from_station_id": st_id,
                        "from_station_name": st.get('station_name', ''),
                        "to_station_id": target_station_id,
                        "to_station_name": target_st.get('station_name', '') if target_st else '',
                        "distance_km": round(dist_km, 2),
                        "river_slope": round(slope, 6),
                        "elevation_diff_m": round(dz, 2),
                        "upstream_elev_m": round(z_up, 2),
                        "downstream_elev_m": round(z_down, 2),
                    },
                    "geometry": {
                        "type": "LineString",
                        "coordinates": coords
                    }
                }
                features.append(feature)
                gauge_relations.append(feature["properties"])

    # 2. Trace Overland Flow from Rainfall Stations to Primary Water Level Station
    for r_st in rain_stations:
        r_id = r_st['station_id']
        lat, lon = float(r_st['latitude']), float(r_st['longitude'])
        r, c = rowcol(transform, lon, lat)
        if not (0 <= r < nrows and 0 <= c < ncols):
            continue

        def stop_at_any_water_station(curr_r, curr_c):
            target_id = water_grid_map.get((curr_r, curr_c))
            if target_id:
                return True, target_id
            return False, None

        coords, target_water_id = trace_downstream_path(
            r, c, fdir, transform,
            stop_condition_fn=stop_at_any_water_station,
            max_steps=2000
        )

        if target_water_id and len(coords) >= 2:
            dist_km = linestring_length_km(coords)
            target_st = next((s for s in water_stations if s['station_id'] == target_water_id), None)
            feature_id = f"flow_rain_{r_id}_to_{target_water_id}"

            feature = {
                "type": "Feature",
                "id": feature_id,
                "properties": {
                    "feature_type": "rainfall_to_gauge_flowpath",
                    "from_station_id": r_id,
                    "from_station_name": r_st.get('station_name', ''),
                    "to_station_id": target_water_id,
                    "to_station_name": target_st.get('station_name', '') if target_st else '',
                    "total_distance_km": round(dist_km, 2),
                },
                "geometry": {
                    "type": "LineString",
                    "coordinates": coords
                }
            }
            features.append(feature)
            rainfall_relations.append(feature["properties"])

    flow_paths_geojson = {
        "type": "FeatureCollection",
        "features": features
    }
    return flow_paths_geojson, gauge_relations, rainfall_relations


def delineate_station_catchments(
    water_stations: List[Dict[str, Any]],
    fdir: np.ndarray,
    transform: Affine
) -> Dict[str, Any]:
    """
    Delineates contributing upstream watershed boundary polygons for each water station.
    Returns GeoJSON FeatureCollection of Catchment Polygons.
    """
    nrows, ncols = fdir.shape
    features = []

    # Map D8 reverse lookups
    reverse_d8 = {
        1: (0, -1),
        2: (-1, -1),
        4: (-1, 0),
        8: (-1, 1),
        16: (0, 1),
        32: (1, 1),
        64: (1, 0),
        128: (1, -1),
    }

    for st in water_stations:
        st_id = st['station_id']
        start_r = st.get('grid_row')
        start_c = st.get('grid_col')
        if start_r is None or start_c is None:
            continue

        # BFS upstream traversal to collect all contributing cells
        visited = set()
        queue = [(start_r, start_c)]
        visited.add((start_r, start_c))

        while queue:
            cr, cc = queue.pop(0)
            for code, (dr, dc) in reverse_d8.items():
                nr, nc = cr + dr, cc + dc
                if 0 <= nr < nrows and 0 <= nc < ncols and (nr, nc) not in visited:
                    if int(fdir[nr, nc]) == code:
                        visited.add((nr, nc))
                        queue.append((nr, nc))

        # Create bounding polygon for the catchment cells
        if len(visited) > 10:
            lons = []
            lats = []
            for (vr, vc) in list(visited)[::max(1, len(visited) // 100)]:  # sample points for convex hull
                vlon, vlat = transform * (vc + 0.5, vr + 0.5)
                lons.append(vlon)
                lats.append(vlat)

            from shapely.geometry import MultiPoint
            mp = MultiPoint(list(zip(lons, lats)))
            hull = mp.convex_hull
            if hull.geom_type == 'Polygon':
                poly_coords = [[round(x, 6), round(y, 6)] for x, y in hull.exterior.coords]
                # Approx area in sq.km
                cell_area_km2 = (abs(transform[0]) * 111.32) * (abs(transform[4]) * 110.54)
                area_km2 = len(visited) * cell_area_km2

                features.append({
                    "type": "Feature",
                    "id": f"catchment_{st_id}",
                    "properties": {
                        "station_id": st_id,
                        "station_name": st.get('station_name', ''),
                        "catchment_area_km2": round(area_km2, 2),
                        "contributing_cells": len(visited)
                    },
                    "geometry": {
                        "type": "Polygon",
                        "coordinates": [poly_coords]
                    }
                })

    return {
        "type": "FeatureCollection",
        "features": features
    }
