"""
Graph Topology & River Network Routing Module (2-Layer Hybrid Architecture)
Handles:
1. Directed River Backbone Graph Construction (OSM Welded, Spatial Grid Indexing, Elevation-Directed)
2. Station Snapping to Stream Network
3. 2-Layer Hybrid Flow Path Tracing:
   - Layer 1: Gauge-to-Gauge Backbone Flow Paths (Smooth, Non-overlapping OSM River Graph)
   - Layer 2: Rain-to-Gauge Overland Connectors (Continuous Terrain-Blended D8 -> Backbone)
4. Hydrological Lag Time & Dynamic Weight Estimation
5. In-degree >= 2 Confluence Detection & Sub-catchment Delineation
"""

import heapq
import math
from typing import Dict, List, Tuple, Any, Optional, Set
import numpy as np
from rasterio.transform import Affine, rowcol
from shapely.geometry import Point, LineString, Polygon, MultiPoint, mapping
from shapely.ops import unary_union
from .gis_utils import haversine_distance, linestring_length_km
from .terrain_engine import D8_DELTAS


class DirectedRiverGraph:
    """
    Topological Directed River Graph built from OpenStreetMap waterways and DEM elevations.
    Features:
    - O(1) Spatial Hash Grid Indexing for ultra-fast node clustering and endpoint snapping.
    - Elevation-enforced directional routing (upstream to downstream).
    - O(E log V) Memory-efficient Dijkstra shortest-path routing via parent backtracking.
    """

    def __init__(self, snap_tolerance_deg: float = 0.00035):
        self.snap_tolerance_deg = snap_tolerance_deg
        self.nodes: Dict[int, Tuple[float, float, float]] = {}  # node_id -> (lon, lat, elev)
        self.adj: Dict[int, List[Tuple[int, Dict[str, Any]]]] = {}  # node_id -> [(neighbor_id, edge_data)]
        self.grid: Dict[Tuple[int, int], List[int]] = {}  # (grid_x, grid_y) -> [node_id, ...]
        self._next_node_id = 1

    def _grid_coord(self, lon: float, lat: float) -> Tuple[int, int]:
        return int(math.floor(lon / self.snap_tolerance_deg)), int(math.floor(lat / self.snap_tolerance_deg))

    def _get_or_create_node(self, lon: float, lat: float, elev: float) -> int:
        """Finds closest existing node within snap tolerance in O(1) time or creates a new node."""
        r_lon = round(lon, 6)
        r_lat = round(lat, 6)
        gx, gy = self._grid_coord(r_lon, r_lat)

        # Search in 3x3 neighboring spatial grid buckets
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                bucket = self.grid.get((gx + dx, gy + dy))
                if bucket:
                    for nid in bucket:
                        s_lon, s_lat, _ = self.nodes[nid]
                        if math.hypot(r_lon - s_lon, r_lat - s_lat) <= self.snap_tolerance_deg:
                            return nid

        new_id = self._next_node_id
        self._next_node_id += 1
        self.nodes[new_id] = (r_lon, r_lat, elev)
        self.adj[new_id] = []
        self.grid.setdefault((gx, gy), []).append(new_id)
        return new_id

    def add_river_segment(
        self,
        coords: List[List[float]],
        sample_elev_fn=None,
        river_name: str = ""
    ):
        """Adds a river LineString segment into the directed graph with downstream elevation enforcement."""
        if len(coords) < 2:
            return

        lon_start, lat_start = coords[0][0], coords[0][1]
        lon_end, lat_end = coords[-1][0], coords[-1][1]

        z_start = sample_elev_fn(lon_start, lat_start) if sample_elev_fn else 100.0
        z_end = sample_elev_fn(lon_end, lat_end) if sample_elev_fn else 90.0

        # Topological Direction Enforcement: flow from higher elevation to lower elevation
        segment_coords = list(coords)
        if z_start < z_end - 0.5:
            segment_coords.reverse()
            z_start, z_end = z_end, z_start
            lon_start, lat_start = segment_coords[0][0], segment_coords[0][1]
            lon_end, lat_end = segment_coords[-1][0], segment_coords[-1][1]

        u = self._get_or_create_node(lon_start, lat_start, z_start)
        v = self._get_or_create_node(lon_end, lat_end, z_end)

        if u == v:
            return

        length_km = linestring_length_km(segment_coords)
        edge_data = {
            "coords": segment_coords,
            "length_km": max(0.01, length_km),
            "z_start": z_start,
            "z_end": z_end,
            "dz": max(0.0, z_start - z_end),
            "river_name": river_name
        }

        # Primary downstream edge
        self.adj[u].append((v, edge_data))

        # Secondary reverse edge with higher penalty for topology bridging fallback
        rev_edge = dict(edge_data)
        rev_coords = list(segment_coords)
        rev_coords.reverse()
        rev_edge["coords"] = rev_coords
        rev_edge["length_km"] = length_km * 2.5
        self.adj[v].append((u, rev_edge))

    def find_nearest_node(self, lon: float, lat: float, max_dist_deg: float = 0.05) -> Tuple[Optional[int], float]:
        """Finds the nearest graph node to a given coordinate using expanding grid search."""
        gx, gy = self._grid_coord(lon, lat)
        search_radius = max(1, int(math.ceil(max_dist_deg / self.snap_tolerance_deg)))

        best_nid = None
        best_d = float('inf')

        for dx in range(-search_radius, search_radius + 1):
            for dy in range(-search_radius, search_radius + 1):
                bucket = self.grid.get((gx + dx, gy + dy))
                if bucket:
                    for nid in bucket:
                        s_lon, s_lat, _ = self.nodes[nid]
                        d = math.hypot(lon - s_lon, lat - s_lat)
                        if d < best_d:
                            best_d = d
                            best_nid = nid

        if best_d <= max_dist_deg:
            return best_nid, best_d
        return None, best_d

    def shortest_path(self, start_node: int, end_node: int) -> Tuple[Optional[List[List[float]]], float]:
        """Memory-efficient Dijkstra shortest-path routing between two nodes via parent backtracking."""
        if start_node == end_node:
            p = self.nodes.get(start_node)
            return ([[p[0], p[1]]] if p else None), 0.0

        dist: Dict[int, float] = {start_node: 0.0}
        prev: Dict[int, Tuple[int, Dict[str, Any]]] = {}  # node -> (parent_node, edge_data)
        pq: List[Tuple[float, int]] = [(0.0, start_node)]

        while pq:
            cost, u = heapq.heappop(pq)
            if cost > dist.get(u, float('inf')):
                continue

            if u == end_node:
                break

            for v, edge_d in self.adj.get(u, []):
                new_cost = cost + edge_d["length_km"]
                if new_cost < dist.get(v, float('inf')):
                    dist[v] = new_cost
                    prev[v] = (u, edge_d)
                    heapq.heappush(pq, (new_cost, v))

        if end_node not in prev:
            return None, float('inf')

        # Backtrack path edges from end_node to start_node
        curr = end_node
        path_edges = []
        while curr in prev:
            parent, edge_d = prev[curr]
            path_edges.append(edge_d)
            curr = parent
        path_edges.reverse()

        # Stitch coordinates without duplicate adjacent points
        combined_coords = []
        for edge_d in path_edges:
            c = edge_d["coords"]
            if not combined_coords:
                combined_coords.extend(c)
            else:
                combined_coords.extend(c[1:])

        return combined_coords, dist[end_node]


def merge_coordinates(*coord_lists: Optional[List[List[float]]]) -> List[List[float]]:
    """Helper to merge multiple coordinate lists while removing duplicate adjacent points."""
    merged: List[List[float]] = []
    for cl in coord_lists:
        if not cl:
            continue
        for pt in cl:
            pt_clean = [round(float(pt[0]), 6), round(float(pt[1]), 6)]
            if not merged:
                merged.append(pt_clean)
            else:
                prev = merged[-1]
                if math.hypot(pt_clean[0] - prev[0], pt_clean[1] - prev[1]) > 1e-6:
                    merged.append(pt_clean)
    return merged


def compute_terrain_slope_and_weights(
    filled_dem: np.ndarray,
    transform: Affine,
    is_geographic: bool = True
) -> np.ndarray:
    """
    Computes local slope grid (m/m) across the basin for continuous terrain-adaptive routing.
    """
    nrows, ncols = filled_dem.shape
    res_x = abs(transform[0])
    res_y = abs(transform[4])

    if is_geographic:
        # Approximate meters per degree in Thailand (~15 deg N)
        dx_m = res_x * 111320.0 * 0.966
        dy_m = res_y * 110540.0
    else:
        dx_m = res_x
        dy_m = res_y

    dz_dy, dz_dx = np.gradient(filled_dem, dy_m, dx_m)
    slope_grid = np.sqrt(dz_dx ** 2 + dz_dy ** 2)
    slope_grid = np.nan_to_num(slope_grid, nan=0.001, posinf=0.5, neginf=0.0001)
    return slope_grid


def snap_stations_to_stream(
    stations: List[Dict[str, Any]],
    fdir: np.ndarray,
    acc: np.ndarray,
    transform: Affine,
    osm_waterways_geojson: Optional[Dict[str, Any]] = None,
    crs: Any = None,
    search_radius_cells: int = 15,
    min_acc_cells: int = 100
) -> List[Dict[str, Any]]:
    """
    Snaps each water level station to the closest OpenStreetMap river vector channel (if available)
    or the highest flow accumulation cell within `search_radius_cells`.
    Ensures stations align perfectly with the actual river geometry.
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

    # Parse OSM River Lines if available
    osm_lines = []
    if osm_waterways_geojson and osm_waterways_geojson.get("features"):
        for feat in osm_waterways_geojson.get("features", []):
            geom = feat.get("geometry")
            if geom and geom.get("type") == "LineString":
                coords = geom.get("coordinates", [])
                if len(coords) >= 2:
                    try:
                        line_geom = LineString(coords)
                        props = feat.get("properties", {})
                        osm_lines.append((line_geom, props))
                    except Exception:
                        pass

    for st in stations:
        lat = float(st['latitude'])
        lon = float(st['longitude'])
        river_name = str(st.get('riverName') or st.get('river_name') or '').strip()

        snapped_lon, snapped_lat = lon, lat
        snapped_via_osm = False

        # 1. Attempt High-Precision OSM Snapping (within ~1.5 km buffer)
        if osm_lines:
            pt = Point(lon, lat)
            best_line_geom = None
            min_dist_deg = 0.015  # ~1.6 km

            # Priority 1: Matching River Name in OSM
            if river_name and len(river_name) >= 2:
                for line_geom, props in osm_lines:
                    p_name = str(props.get('name') or props.get('name_th') or '')
                    if river_name in p_name or p_name in river_name:
                        d = pt.distance(line_geom)
                        if d < min_dist_deg:
                            min_dist_deg = d
                            best_line_geom = line_geom

            # Priority 2: Closest Major River / Stream
            if best_line_geom is None:
                for line_geom, props in osm_lines:
                    d = pt.distance(line_geom)
                    if d < min_dist_deg:
                        min_dist_deg = d
                        best_line_geom = line_geom

            if best_line_geom is not None:
                from shapely.ops import nearest_points
                near_pt = nearest_points(best_line_geom, pt)[0]
                snapped_lon, snapped_lat = round(near_pt.x, 6), round(near_pt.y, 6)
                snapped_via_osm = True

        # 2. Convert to Raster Coordinates (r, c)
        if inv_transformer is not None:
            proj_x, proj_y = inv_transformer.transform(snapped_lon, snapped_lat)
            r, c = rowcol(transform, proj_x, proj_y)
        else:
            r, c = rowcol(transform, snapped_lon, snapped_lat)

        best_r, best_c = r, c
        best_acc = -1

        # 3. Fallback / Refinement via Flow Accumulation raster if not snapped via OSM
        if not snapped_via_osm and 0 <= r < nrows and 0 <= c < ncols:
            r_min = max(0, r - search_radius_cells)
            r_max = min(nrows, r + search_radius_cells + 1)
            c_min = max(0, c - search_radius_cells)
            c_max = min(ncols, c + search_radius_cells + 1)

            for cr in range(r_min, r_max):
                for cc in range(c_min, c_max):
                    if acc[cr, cc] > best_acc and acc[cr, cc] >= min_acc_cells:
                        best_acc = acc[cr, cc]
                        best_r, best_c = cr, cc

            x, y = transform * (best_c + 0.5, best_r + 0.5)
            if transformer is not None:
                snapped_lon, snapped_lat = transformer.transform(x, y)
            else:
                snapped_lon, snapped_lat = x, y

        # Ensure grid_row / grid_col are clamped within DEM bounds
        best_r = max(0, min(nrows - 1, best_r))
        best_c = max(0, min(ncols - 1, best_c))

        offset_m = haversine_distance(lat, lon, snapped_lat, snapped_lon) * 1000.0

        st_copy = dict(st)
        st_copy['orig_latitude'] = lat
        st_copy['orig_longitude'] = lon
        st_copy['latitude'] = round(snapped_lat, 6)
        st_copy['longitude'] = round(snapped_lon, 6)
        st_copy['grid_row'] = int(best_r)
        st_copy['grid_col'] = int(best_c)
        st_copy['flow_acc_cells'] = int(acc[best_r, best_c]) if 0 <= best_r < nrows and 0 <= best_c < ncols else 0
        st_copy['snap_offset_meters'] = round(float(offset_m), 1)
        st_copy['snapped_via_osm'] = bool(snapped_via_osm)
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
    nrows, ncols = fdir.shape
    in_degree = np.zeros((nrows, ncols), dtype=np.int32)

    # Compute in-degree only along stream channels
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
            inv_transformer = Transformer.from_crs("EPSG:4326", crs, always_xy=True)
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


def compute_rainfall_lag_bounds(
    overland_dist_km: float,
    overland_slope: float,
    channel_dist_km: float,
    channel_slope: float,
    total_dz_m: float
) -> Tuple[int, int, int, float, float, float]:
    """
    Computes physically sound hydrological response lag times by decomposing into:
    1. Overland Hillslope Travel Time (Kirpich/SCS Tc on steep mountain slopes)
    2. Channel Kinematic Wave Travel Time (on river network)
    """
    s_overland = max(0.001, overland_slope)
    s_channel = max(0.0002, channel_slope)
    l_overland_m = max(50.0, overland_dist_km * 1000.0)

    # 1. Overland Time of Concentration Tc (minutes)
    tc_avg_min = 0.0078 * ((l_overland_m / math.sqrt(s_overland)) ** 0.77)
    tc_min_min = 0.0057 * ((l_overland_m / math.sqrt(s_overland)) ** 0.77)
    tc_max_min = 0.0111 * ((l_overland_m / math.sqrt(s_overland)) ** 0.77)

    # 2. Channel Kinematic Wave Velocity (km/h) -> travel time in minutes
    v_max = max(3.5, min(10.0, 5.5 * (s_channel / 0.005) ** 0.25))
    v_avg = max(2.0, min(8.0, 4.0 * (s_channel / 0.005) ** 0.22))
    v_min = max(1.2, min(5.0, 2.5 * (s_channel / 0.005) ** 0.20))

    t_kin_min_min = (channel_dist_km / v_max) * 60.0 if channel_dist_km > 0 else 0.0
    t_kin_avg_min = (channel_dist_km / v_avg) * 60.0 if channel_dist_km > 0 else 0.0
    t_kin_max_min = (channel_dist_km / v_min) * 60.0 if channel_dist_km > 0 else 0.0

    # 3. Total Lag Time directly in Minutes
    lag_avg_m = int(round(max(15, tc_avg_min + t_kin_avg_min)))
    lag_min_m = int(round(max(10, min(tc_min_min + t_kin_min_min, lag_avg_m * 0.75))))
    lag_max_m = int(round(max(lag_avg_m + 15, tc_max_min + t_kin_max_min)))

    lag_avg_h = round(lag_avg_m / 60.0, 1)
    lag_min_h = round(lag_min_m / 60.0, 1)
    lag_max_h = round(lag_max_m / 60.0, 1)

    return lag_min_m, lag_avg_m, lag_max_m, lag_min_h, lag_avg_h, lag_max_h


def build_flow_paths_and_relations(
    water_stations: List[Dict[str, Any]],
    rain_stations: List[Dict[str, Any]],
    fdir: np.ndarray,
    acc: np.ndarray,
    filled_dem: np.ndarray,
    transform: Affine,
    osm_waterways_geojson: Optional[Dict[str, Any]] = None,
    crs: Any = None
) -> Tuple[Dict[str, Any], List[Dict[str, Any]], List[Dict[str, Any]]]:
    """
    2-Layer Hybrid Flow Path & River Topology Generator:
    - Layer 1: Gauge-to-Gauge River Backbone Flowpaths (Smooth, Directed OSM Graph Routing)
    - Layer 2: Rainfall-to-Gauge Overland Flowpaths (Continuous Terrain-Blended D8 -> River Backbone)

    Returns:
    1. flow_paths_geojson (LineString vector features for Frontend Map)
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

    # Helper to sample elevation from DEM safely
    def sample_elevation(lon: float, lat: float) -> float:
        if inv_transformer is not None:
            px, py = inv_transformer.transform(lon, lat)
            gr, gc = rowcol(transform, px, py)
        else:
            gr, gc = rowcol(transform, lon, lat)
        gr = max(0, min(nrows - 1, gr))
        gc = max(0, min(ncols - 1, gc))
        val = float(filled_dem[gr, gc])
        return val if not np.isnan(val) and val != -9999.0 else 100.0

    # 1. Construct Directed River Backbone Graph from OSM with Spatial Grid Indexing
    river_graph = DirectedRiverGraph(snap_tolerance_deg=0.00035)
    if osm_waterways_geojson and osm_waterways_geojson.get("features"):
        for feat in osm_waterways_geojson.get("features", []):
            geom = feat.get("geometry")
            if geom and geom.get("type") == "LineString":
                coords = geom.get("coordinates", [])
                if len(coords) >= 2:
                    p_name = feat.get("properties", {}).get("name", "")
                    river_graph.add_river_segment(coords, sample_elev_fn=sample_elevation, river_name=p_name)

    # 2. Map Water Stations onto Grid and Graph Nodes
    water_grid_map = {}
    water_node_map = {}
    for st in water_stations:
        st_id = str(st.get('station_id', '')).strip()
        if not st_id:
            continue
        r, c = st.get('grid_row'), st.get('grid_col')
        st_lat, st_lon = float(st.get('latitude', 0.0)), float(st.get('longitude', 0.0))

        if r is not None and c is not None:
            for dr in (-2, -1, 0, 1, 2):
                for dc in (-2, -1, 0, 1, 2):
                    nr, nc = r + dr, c + dc
                    if 0 <= nr < nrows and 0 <= nc < ncols:
                        water_grid_map[(nr, nc)] = st_id

        # Find closest node on river graph
        nid, _ = river_graph.find_nearest_node(st_lon, st_lat, max_dist_deg=0.02)
        if nid:
            water_node_map[st_id] = nid

    features = []
    gauge_relations = []
    rainfall_relations = []

    # =========================================================================
    # LAYER 1: Gauge-to-Gauge Backbone Flow Paths (Upstream -> Downstream)
    # =========================================================================
    for st in water_stations:
        st_id = str(st.get('station_id', '')).strip()
        start_r = st.get('grid_row')
        start_c = st.get('grid_col')
        st_lat = float(st.get('latitude', 0.0))
        st_lon = float(st.get('longitude', 0.0))
        if start_r is None or start_c is None or not st_id:
            continue

        def make_stop_fn(origin_id):
            def stop_fn(r, c):
                target_id = water_grid_map.get((r, c))
                if target_id and target_id != origin_id:
                    return True, target_id
                return False, None
            return stop_fn

        # Identify downstream candidate via D8 downhill step
        code = int(fdir[start_r, start_c])
        first_r, first_c = (start_r + D8_DELTAS[code][0], start_c + D8_DELTAS[code][1]) if code in D8_DELTAS else (start_r, start_c)
        raster_coords, target_station_id = trace_downstream_path(
            first_r, first_c, fdir, transform, crs=crs,
            stop_condition_fn=make_stop_fn(st_id),
            max_steps=2000
        )

        if target_station_id:
            target_st = next((s for s in water_stations if s['station_id'] == target_station_id), None)
            coords = None

            # Attempt clean OSM Directed Graph Route first
            u_node = water_node_map.get(st_id)
            v_node = water_node_map.get(target_station_id)
            if u_node and v_node and u_node != v_node:
                graph_coords, _ = river_graph.shortest_path(u_node, v_node)
                if graph_coords and len(graph_coords) >= 2:
                    coords = merge_coordinates([[st_lon, st_lat]], graph_coords, [[target_st['longitude'], target_st['latitude']]])

            # Fallback to raster D8 coordinates if graph path is disconnected
            if not coords or len(coords) < 2:
                coords = merge_coordinates([[st_lon, st_lat]], raster_coords)

            dist_km = linestring_length_km(coords)
            z_up = sample_elevation(st_lon, st_lat)
            z_down = sample_elevation(target_st['longitude'], target_st['latitude']) if target_st else z_up
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

    # =========================================================================
    # LAYER 2: Rain-to-Gauge Overland Connectors (Overland -> River Backbone)
    # =========================================================================
    # Define stop condition once outside the loop for maximum efficiency
    def stop_at_water_station(curr_r, curr_c):
        t_id = water_grid_map.get((curr_r, curr_c))
        if t_id:
            return True, t_id
        return False, None

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

        # Trace Overland flow from mountain station
        overland_coords, direct_target_water_id = trace_downstream_path(
            r, c, fdir, transform, crs=crs,
            stop_condition_fn=stop_at_water_station,
            max_steps=2200
        )

        z_rain = sample_elevation(lon, lat)
        target_water_id = direct_target_water_id

        # Downstream-aware Fallback: If D8 trace did not directly hit a station grid
        if not target_water_id:
            last_pt = overland_coords[-1] if overland_coords else [lon, lat]
            downstream_candidates = []
            for wst in water_stations:
                w_lon, w_lat = float(wst['longitude']), float(wst['latitude'])
                w_elev = sample_elevation(w_lon, w_lat)
                # Elevation-aware: candidate must be downstream (lower or near elevation)
                if w_elev <= z_rain + 2.0:
                    d = math.hypot(last_pt[0] - w_lon, last_pt[1] - w_lat)
                    downstream_candidates.append((d, str(wst.get('station_id', '')).strip()))

            if downstream_candidates:
                downstream_candidates.sort(key=lambda x: x[0])
                target_water_id = downstream_candidates[0][1]

        if target_water_id and (len(overland_coords) >= 1):
            target_st = next((s for s in water_stations if s['station_id'] == target_water_id), None)
            tgt_lon = float(target_st.get('longitude', 0.0)) if target_st else lon
            tgt_lat = float(target_st.get('latitude', 0.0)) if target_st else lat
            z_water = sample_elevation(tgt_lon, tgt_lat) if target_st else z_rain

            coords = overland_coords
            overland_dist_km = linestring_length_km(overland_coords)
            channel_dist_km = 0.0

            # Stitch to OSM River Backbone if possible
            if target_st:
                last_pt = overland_coords[-1]
                mid_node, _ = river_graph.find_nearest_node(last_pt[0], last_pt[1], max_dist_deg=0.03)
                tgt_node = water_node_map.get(target_water_id)

                if mid_node and tgt_node and mid_node != tgt_node:
                    backbone_coords, b_dist = river_graph.shortest_path(mid_node, tgt_node)
                    if backbone_coords and len(backbone_coords) >= 2:
                        coords = merge_coordinates(overland_coords, backbone_coords, [[tgt_lon, tgt_lat]])
                        channel_dist_km = b_dist
                    else:
                        coords = merge_coordinates(overland_coords, [[tgt_lon, tgt_lat]])
                else:
                    coords = merge_coordinates(overland_coords, [[tgt_lon, tgt_lat]])

            dist_km = linestring_length_km(coords)
            dz = max(0.0, z_rain - z_water)

            # Decomposed hillslope vs channel slopes for accurate lag times
            overland_dz = max(0.0, z_rain - sample_elevation(overland_coords[-1][0], overland_coords[-1][1])) if len(overland_coords) > 1 else dz
            overland_slope = (overland_dz / (overland_dist_km * 1000.0)) if overland_dist_km > 0.001 else 0.01
            channel_slope = (dz / (dist_km * 1000.0)) if dist_km > 0.001 else 0.0005

            lag_min_m, lag_avg_m, lag_max_m, lag_min_h, lag_avg_h, lag_max_h = compute_rainfall_lag_bounds(
                overland_dist_km=overland_dist_km,
                overland_slope=overland_slope,
                channel_dist_km=channel_dist_km,
                channel_slope=channel_slope,
                total_dz_m=dz
            )
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
                    "distance_km": round(dist_km, 2),
                    "response_lag_minutes": lag_avg_m,
                    "response_lag_minutes_min": lag_min_m,
                    "response_lag_minutes_max": lag_max_m,
                    "response_lag_hours": lag_avg_h,
                    "response_lag_hours_min": lag_min_h,
                    "response_lag_hours_max": lag_max_h,
                    "elevation_diff_m": round(dz, 2),
                    "slope": round(channel_slope, 6),
                    "upstream_elev_m": round(z_rain, 2),
                    "downstream_elev_m": round(z_water, 2),
                    "influence_weight_percent": 30.0
                },
                "geometry": {
                    "type": "LineString",
                    "coordinates": coords
                }
            }
            features.append(feature)
            rainfall_relations.append(feature["properties"])

    # 3. Compute Dynamic Influence Weight % via Inverse Distance Weighting (IDW)
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
