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
    crs: Any = None,
    search_radius_cells: int = 15,
    min_acc_cells: int = 100
) -> List[Dict[str, Any]]:
    """
    Snaps each water level station to the highest flow accumulation cell
    within `search_radius_cells` to align with the actual raster stream channel.
    Handles projected CRS (UTM) and WGS84 coordinates.
    """
    nrows, ncols = fdir.shape
    snapped = []

    is_geographic = (crs is None) or getattr(crs, 'is_geographic', False) or (str(crs) == "EPSG:4326")
    transformer = None
    inv_transformer = None
    if not is_geographic and crs is not None:
        try:
            from pyproj import Transformer
            transformer = Transformer.from_crs(crs, "EPSG:4326", always_xy=True)
            inv_transformer = Transformer.from_crs("EPSG:4326", crs, always_xy=True)
        except Exception:
            transformer = None
            inv_transformer = None

    for st in stations:
        lat = float(st['latitude'])
        lon = float(st['longitude'])

        if inv_transformer is not None:
            proj_x, proj_y = inv_transformer.transform(lon, lat)
            r, c = rowcol(transform, proj_x, proj_y)
        else:
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
        x, y = transform * (best_c + 0.5, best_r + 0.5)
        if transformer is not None:
            snapped_lon, snapped_lat = transformer.transform(x, y)
        else:
            snapped_lon, snapped_lat = x, y

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
    transformer = None
    if not is_geographic and crs is not None:
        try:
            from pyproj import Transformer
            transformer = Transformer.from_crs(crs, "EPSG:4326", always_xy=True)
        except Exception:
            transformer = None

    junc_rows, junc_cols = np.where((in_degree >= 2) & (acc >= min_acc_cells))
    for r, c in zip(junc_rows, junc_cols):
        junction_count += 1
        x, y = transform * (c + 0.5, r + 0.5)
        if transformer is not None:
            lon, lat = transformer.transform(x, y)
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
    crs: Any = None,
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

    is_geographic = (crs is None) or getattr(crs, 'is_geographic', False) or (str(crs) == "EPSG:4326")
    transformer = None
    if not is_geographic and crs is not None:
        try:
            from pyproj import Transformer
            transformer = Transformer.from_crs(crs, "EPSG:4326", always_xy=True)
        except Exception:
            transformer = None

    for _ in range(max_steps):
        if not (0 <= curr_r < nrows and 0 <= curr_c < ncols):
            break
        if (curr_r, curr_c) in visited:
            break  # cycle protection
        visited.add((curr_r, curr_c))

        x, y = transform * (curr_c + 0.5, curr_r + 0.5)
        if transformer is not None:
            lon, lat = transformer.transform(x, y)
        else:
            lon, lat = x, y
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


def compute_rainfall_lag_bounds(dist_km: float, slope: float, dz_m: float) -> Tuple[float, float, float]:
    """
    Computes hydrological runoff response lag time bounds (avg, min, max in hours)
    from a rainfall telemetry station down the mountain catchment to the destination water station.

    Hydrological Modeling Basis & References:
    1. US Army Corps of Engineers (USACE) HEC-HMS Technical Reference Manual:
       - Hydrograph Transform Methods (Lag Time & Time of Concentration Tc).
       - Hydrological Lag Time T_lag ≈ 0.6 * Tc for peak flood response.
    2. USDA NRCS National Engineering Handbook (Part 630: Hydrology, Chapter 15 / TR-55):
       - Segmented Velocity Method: Overland hill slope runoff + Open channel river routing.
    3. Flash Flood / Saturated Catchment (Min Lag):
       - Rapid rill formation, lower Manning n (0.035), higher flood wave speed.
    4. Baseflow / Initial Abstraction / Dry Soil (Max Lag):
       - Slower initial overland sheet flow, vegetative resistance, lower channel stage.
    """
    s_safe = max(0.0005, slope)
    l_m = max(100.0, dist_km * 1000.0)

    # 1. Min Lag (Saturated Catchment / High Intensity Flash Flood)
    tc_min = 0.000095 * ((l_m / math.sqrt(s_safe)) ** 0.77)
    v_max = max(3.5, min(10.0, 5.5 * (s_safe / 0.005) ** 0.25))
    t_min = round(max(0.3, 0.4 * tc_min + 0.6 * (dist_km / v_max)), 1)

    # 2. Average Lag (Typical Antecedent Soil Moisture)
    tc_avg = 0.00013 * ((l_m / math.sqrt(s_safe)) ** 0.77)
    v_avg = max(2.0, min(8.0, 4.0 * (s_safe / 0.005) ** 0.22))
    t_avg = round(max(0.5, 0.5 * tc_avg + 0.5 * (dist_km / v_avg)), 1)

    # 3. Max Lag (Dry Antecedent Soil / Low Intensity Runoff)
    tc_max = 0.000185 * ((l_m / math.sqrt(s_safe)) ** 0.77)
    v_min = max(1.2, min(5.0, 2.5 * (s_safe / 0.005) ** 0.20))
    t_max = round(max(t_avg + 0.3, 0.6 * tc_max + 0.4 * (dist_km / v_min)), 1)

    return t_avg, t_min, t_max


def build_flow_paths_and_relations(
    water_stations: List[Dict[str, Any]],
    rain_stations: List[Dict[str, Any]],
    fdir: np.ndarray,
    acc: np.ndarray,
    filled_dem: np.ndarray,
    transform: Affine,
    crs: Any = None
) -> Tuple[Dict[str, Any], List[Dict[str, Any]], List[Dict[str, Any]]]:
    """
    Generates:
    1. flow_paths.geojson (LineString vector features for Frontend Map)
    2. station_relations (Gauge -> Downstream Gauge)
    3. rainfall_relations (Rain Gauge -> Receiving Water Gauge)
    """
    nrows, ncols = fdir.shape

    # Set up CRS Transformer for Projected DEMs (e.g. UTM 47N) vs WGS84
    is_geographic = (crs is None) or getattr(crs, 'is_geographic', False) or (str(crs) == "EPSG:4326")
    transformer = None
    inv_transformer = None
    if not is_geographic and crs is not None:
        try:
            from pyproj import Transformer
            transformer = Transformer.from_crs(crs, "EPSG:4326", always_xy=True)
            inv_transformer = Transformer.from_crs("EPSG:4326", crs, always_xy=True)
        except Exception:
            transformer = None
            inv_transformer = None

    # Map grid coordinates to water station IDs
    water_grid_map = {}
    for st in water_stations:
        r, c = st.get('grid_row'), st.get('grid_col')
        st_id = str(st.get('station_id', '')).strip()
        if r is not None and c is not None and st_id:
            # Also register a 3x3 neighborhood around station to ensure intersection
            for dr in (-1, 0, 1):
                for dc in (-1, 0, 1):
                    water_grid_map[(r + dr, c + dc)] = st_id

    features = []
    gauge_relations = []
    rainfall_relations = []

    # 1. Trace Gauge-to-Gauge Flow Paths (Upstream -> Downstream)
    for st in water_stations:
        st_id = str(st.get('station_id', '')).strip()
        start_r = st.get('grid_row')
        start_c = st.get('grid_col')
        if start_r is None or start_c is None or not st_id:
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
                first_r, first_c, fdir, transform, crs=crs,
                stop_condition_fn=make_stop_fn(st_id),
                max_steps=1500
            )
            # Prepend start station coordinate in WGS84
            x, y = transform * (start_c + 0.5, start_r + 0.5)
            if transformer is not None:
                st_lon, st_lat = transformer.transform(x, y)
            else:
                st_lon, st_lat = x, y
            coords = [[round(st_lon, 6), round(st_lat, 6)]] + coords

            if target_station_id and len(coords) >= 2:
                dist_km = linestring_length_km(coords)
                z_up = float(filled_dem[start_r, start_c])
                target_st = next((s for s in water_stations if s['station_id'] == target_station_id), None)
                z_down = float(filled_dem[target_st['grid_row'], target_st['grid_col']]) if target_st else z_up
                dz = max(0.0, z_up - z_down)
                slope = (dz / (dist_km * 1000.0)) if dist_km > 0.001 else 0.0001

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
        r_id = str(r_st.get('station_id', '')).strip()
        if not r_id:
            continue
        lat, lon = float(r_st['latitude']), float(r_st['longitude'])
        if inv_transformer is not None:
            proj_x, proj_y = inv_transformer.transform(lon, lat)
            r, c = rowcol(transform, proj_x, proj_y)
        else:
            r, c = rowcol(transform, lon, lat)

        if not (0 <= r < nrows and 0 <= c < ncols):
            continue

        def stop_at_any_water_station(curr_r, curr_c):
            target_id = water_grid_map.get((curr_r, curr_c))
            if target_id:
                return True, target_id
            return False, None

        coords, target_water_id = trace_downstream_path(
            r, c, fdir, transform, crs=crs,
            stop_condition_fn=stop_at_any_water_station,
            max_steps=2000
        )

        if target_water_id and len(coords) >= 2:
            dist_km = linestring_length_km(coords)
            target_st = next((s for s in water_stations if s['station_id'] == target_water_id), None)
            z_rain = float(filled_dem[r, c])
            z_water = float(filled_dem[target_st['grid_row'], target_st['grid_col']]) if target_st and target_st.get('grid_row') is not None else z_rain
            dz = max(0.0, z_rain - z_water)
            slope = (dz / (dist_km * 1000.0)) if dist_km > 0.001 else 0.0005
            lag_avg, lag_min, lag_max = compute_rainfall_lag_bounds(dist_km, slope, dz)
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
                    "response_lag_hours": lag_avg,
                    "response_lag_hours_min": lag_min,
                    "response_lag_hours_max": lag_max,
                    "elevation_diff_m": round(dz, 2),
                    "slope": round(slope, 6),
                    "upstream_elev_m": round(z_rain, 2),
                    "downstream_elev_m": round(z_water, 2),
                    "influence_weight_percent": 30.0  # Initial default, updated below via IDW
                },
                "geometry": {
                    "type": "LineString",
                    "coordinates": coords
                }
            }
            features.append(feature)
            rainfall_relations.append(feature["properties"])

    # 3. Compute Dynamic Influence Weight % via Inverse Distance Weighting (IDW) per Target Water Station
    target_groups: Dict[str, List[Dict[str, Any]]] = {}
    for r_prop in rainfall_relations:
        target_groups.setdefault(r_prop['to_station_id'], []).append(r_prop)

    for target_id, group in target_groups.items():
        inv_dists = [1.0 / max(0.5, float(r.get('total_distance_km', 5.0))) for r in group]
        sum_inv = sum(inv_dists)
        for r_prop, inv_d in zip(group, inv_dists):
            pct = round((inv_d / sum_inv) * 100.0, 1) if sum_inv > 0 else round(100.0 / len(group), 1)
            r_prop['influence_weight_percent'] = pct

    flow_paths_geojson = {
        "type": "FeatureCollection",
        "features": features
    }
    return flow_paths_geojson, gauge_relations, rainfall_relations


def delineate_station_catchments(
    water_stations: List[Dict[str, Any]],
    fdir: np.ndarray,
    transform: Affine,
    crs: Any = None
) -> Dict[str, Any]:
    """
    Delineates contributing upstream watershed boundary polygons for each water station.
    Returns GeoJSON FeatureCollection of Catchment Polygons.
    """
    nrows, ncols = fdir.shape
    features = []

    is_geographic = (crs is None) or getattr(crs, 'is_geographic', False) or (str(crs) == "EPSG:4326")
    transformer = None
    if not is_geographic and crs is not None:
        try:
            from pyproj import Transformer
            transformer = Transformer.from_crs(crs, "EPSG:4326", always_xy=True)
        except Exception:
            transformer = None

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

    from collections import deque
    try:
        from tqdm import tqdm
    except ImportError:
        def tqdm(iterable, **kwargs):
            return iterable

    pbar = tqdm(
        water_stations,
        desc="        [Progress] Delineating Catchment Polygons",
        unit="station",
        ncols=85,
        leave=True
    )

    for st in pbar:
        st_id = str(st.get('station_id', '')).strip()
        start_r = st.get('grid_row')
        start_c = st.get('grid_col')
        if start_r is None or start_c is None:
            continue

        # Fast O(1) BFS upstream traversal using collections.deque
        visited = set()
        queue = deque([(start_r, start_c)])
        visited.add((start_r, start_c))

        while queue:
            cr, cc = queue.popleft()
            for code, (dr, dc) in reverse_d8.items():
                nr, nc = cr + dr, cc + dc
                if 0 <= nr < nrows and 0 <= nc < ncols and (nr, nc) not in visited:
                    if int(fdir[nr, nc]) == code:
                        visited.add((nr, nc))
                        queue.append((nr, nc))

        # Create bounding polygon for the catchment cells
        if len(visited) > 10:
            xs = []
            ys = []
            for (vr, vc) in list(visited)[::max(1, len(visited) // 100)]:  # sample points for convex hull
                vx, vy = transform * (vc + 0.5, vr + 0.5)
                xs.append(vx)
                ys.append(vy)

            from shapely.geometry import MultiPoint
            mp = MultiPoint(list(zip(xs, ys)))
            hull = mp.convex_hull
            if hull.geom_type == 'Polygon':
                poly_pts = list(hull.exterior.coords)
                if transformer is not None:
                    h_xs = [p[0] for p in poly_pts]
                    h_ys = [p[1] for p in poly_pts]
                    lons, lats = transformer.transform(h_xs, h_ys)
                    poly_coords = [[round(lo, 6), round(la, 6)] for lo, la in zip(lons, lats)]
                else:
                    poly_coords = [[round(p[0], 6), round(p[1], 6)] for p in poly_pts]

                # Approx area in sq.km
                if is_geographic:
                    cell_area_km2 = (abs(transform[0]) * 111.32) * (abs(transform[4]) * 110.54)
                else:
                    cell_area_km2 = (abs(transform[0]) * abs(transform[4])) / 1_000_000.0

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
