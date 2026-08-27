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
        self._kdtree = None
        self._node_id_list: List[int] = []
        self._is_indexed = False

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
        self._is_indexed = False
        return new_id

    def build_spatial_index(self):
        """Builds high-performance scipy cKDTree for O(log N) nearest neighbor search in microseconds."""
        if not self.nodes:
            self._kdtree = None
            self._node_id_list = []
            self._is_indexed = True
            return
        try:
            from scipy.spatial import cKDTree
            self._node_id_list = list(self.nodes.keys())
            coords = np.array([[self.nodes[nid][0], self.nodes[nid][1]] for nid in self._node_id_list], dtype=np.float64)
            self._kdtree = cKDTree(coords)
        except Exception:
            self._kdtree = None
        self._is_indexed = True

    def add_river_segment(
        self,
        coords: List[List[float]],
        sample_elev_fn=None,
        river_name: str = ""
    ):
        """
        Adds an OSM waterway LineString into the directed graph with full-vertex granular noding.
        Every consecutive vertex pair (C_i, C_{i+1}) becomes a discrete directed edge,
        enabling 100% topological connectivity for all tributaries, stations, and overland entries.
        """
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

        # Connect vertex-by-vertex
        prev_node = None
        for i in range(len(segment_coords)):
            p_lon, p_lat = segment_coords[i][0], segment_coords[i][1]
            p_elev = sample_elev_fn(p_lon, p_lat) if sample_elev_fn else 100.0
            curr_node = self._get_or_create_node(p_lon, p_lat, p_elev)

            if prev_node is not None and prev_node != curr_node:
                p_prev_lon, p_prev_lat, z_p = self.nodes[prev_node]
                p_curr_lon, p_curr_lat, z_c = self.nodes[curr_node]
                sub_coords = [[p_prev_lon, p_prev_lat], [p_curr_lon, p_curr_lat]]
                length_km = linestring_length_km(sub_coords)

                edge_data = {
                    "coords": sub_coords,
                    "length_km": max(0.001, length_km),
                    "z_start": z_p,
                    "z_end": z_c,
                    "dz": max(0.0, z_p - z_c),
                    "river_name": river_name
                }

                # Primary downstream edge (enforce downstream flow only)
                self.adj[prev_node].append((curr_node, edge_data))

            prev_node = curr_node

    def find_nearest_node(self, lon: float, lat: float, max_dist_deg: float = 0.05) -> Tuple[Optional[int], float]:
        """Finds the nearest graph node to a given coordinate using O(log N) cKDTree or spatial grid fallback."""
        if not self._is_indexed:
            self.build_spatial_index()

        if self._kdtree is not None and self._node_id_list:
            dist, idx = self._kdtree.query([lon, lat], distance_upper_bound=max_dist_deg)
            if not math.isinf(dist) and idx < len(self._node_id_list) and dist <= max_dist_deg:
                return self._node_id_list[idx], float(dist)
            return None, float(dist) if not math.isinf(dist) else float('inf')

        # Fallback to spatial hash grid search
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

    def dijkstra_single_source(
        self,
        start_node: int,
        target_nodes_set: Optional[Set[int]] = None,
        max_dist_km: float = 60.0
    ) -> Tuple[Dict[int, float], Dict[int, Tuple[int, Dict[str, Any]]]]:
        """
        Runs Dijkstra shortest path from start_node to reachable nodes within max_dist_km.
        If target_nodes_set is provided, stops early once all reachable target nodes have been settled.
        Returns:
            dist: Dict[node_id, shortest_distance_km]
            prev: Dict[node_id, (parent_node_id, edge_data)]
        """
        dist: Dict[int, float] = {start_node: 0.0}
        prev: Dict[int, Tuple[int, Dict[str, Any]]] = {}
        pq: List[Tuple[float, int]] = [(0.0, start_node)]

        remaining_targets = set(target_nodes_set) if target_nodes_set is not None else None
        if remaining_targets is not None:
            remaining_targets.discard(start_node)

        while pq:
            cost, u = heapq.heappop(pq)
            if cost > max_dist_km:
                break
            if cost > dist.get(u, float('inf')):
                continue

            if remaining_targets is not None and u in remaining_targets:
                remaining_targets.discard(u)
                if not remaining_targets:
                    break

            for v, edge_d in self.adj.get(u, []):
                new_cost = cost + edge_d["length_km"]
                if new_cost <= max_dist_km and new_cost < dist.get(v, float('inf')):
                    dist[v] = new_cost
                    prev[v] = (u, edge_d)
                    heapq.heappush(pq, (new_cost, v))

        return dist, prev

    def reconstruct_path_from_prev(
        self,
        prev: Dict[int, Tuple[int, Dict[str, Any]]],
        start_node: int,
        end_node: int
    ) -> Optional[List[List[float]]]:
        """Backtracks parent pointers from end_node to start_node and stitches coordinates seamlessly."""
        if start_node == end_node:
            p = self.nodes.get(start_node)
            return [[p[0], p[1]]] if p else None

        if end_node not in prev:
            return None

        curr = end_node
        path_edges = []
        visited_nodes = set()
        while curr in prev:
            if curr in visited_nodes or curr == start_node:
                break
            visited_nodes.add(curr)
            parent, edge_d = prev[curr]
            path_edges.append(edge_d)
            curr = parent
            if curr == start_node:
                break

        if curr != start_node and end_node != start_node:
            return None

        path_edges.reverse()
        combined_coords = []
        for edge_d in path_edges:
            c = edge_d["coords"]
            if not combined_coords:
                combined_coords.extend(c)
            else:
                combined_coords.extend(c[1:])
        return combined_coords

    def shortest_path(self, start_node: int, end_node: int, max_dist_km: float = 250.0) -> Tuple[Optional[List[List[float]]], float]:
        """Memory-efficient Dijkstra shortest-path routing between two nodes via parent backtracking."""
        if start_node == end_node:
            p = self.nodes.get(start_node)
            return ([[p[0], p[1]]] if p else None), 0.0

        dist, prev = self.dijkstra_single_source(start_node, target_nodes_set={end_node}, max_dist_km=max_dist_km)
        if end_node not in dist:
            return None, float('inf')

        coords = self.reconstruct_path_from_prev(prev, start_node, end_node)
        return coords, dist[end_node]


def merge_coordinates(*coord_lists: Optional[List[List[float]]]) -> List[List[float]]:
    """Helper to merge multiple coordinate lists while removing duplicate adjacent points."""
    merged: List[List[float]] = []
    for cl in coord_lists:
        if not cl:
            continue
        for pt in cl:
            pt_clean = [round(float(pt[0]), 5), round(float(pt[1]), 5)]
            if not merged:
                merged.append(pt_clean)
            else:
                prev = merged[-1]
                if math.hypot(pt_clean[0] - prev[0], pt_clean[1] - prev[1]) > 1e-6:
                    merged.append(pt_clean)
    return merged


def simplify_linestring_coords(
    coords: List[List[float]],
    tolerance_deg: float = 0.00035,
    max_step_km: float = 0.5
) -> List[List[float]]:
    """
    Simplifies LineString coordinates using Douglas-Peucker algorithm (tolerance ~35m).
    Eliminates redundant collinear raster stair steps while preserving 100% of curves, bends,
    and exact station endpoints.
    Strictly sanitizes against any straight-line jumps > max_step_km (500m).
    """
    if not coords or len(coords) < 2:
        return [[round(p[0], 5), round(p[1], 5)] for p in coords]

    # 1. Topological Continuity Sanitization: Stop at any artificial leap > max_step_km
    clean_coords = [coords[0]]
    for i in range(len(coords) - 1):
        p1, p2 = coords[i], coords[i + 1]
        d_km = math.hypot((p2[0] - p1[0]) * 111.32 * 0.95, (p2[1] - p1[1]) * 110.54)
        if d_km > max_step_km:
            break
        clean_coords.append(p2)

    if len(clean_coords) < 2:
        return [[round(p[0], 5), round(p[1], 5)] for p in coords[:2]]

    # 2. Douglas-Peucker Simplification
    try:
        line = LineString(clean_coords)
        simplified = line.simplify(tolerance_deg, preserve_topology=True)
        if simplified.geom_type == 'LineString':
            simp_pts = [[round(p[0], 5), round(p[1], 5)] for p in simplified.coords]
            if len(simp_pts) >= 2:
                # Strictly preserve exact origin and destination coordinates
                simp_pts[0] = [round(clean_coords[0][0], 5), round(clean_coords[0][1], 5)]
                simp_pts[-1] = [round(clean_coords[-1][0], 5), round(clean_coords[-1][1], 5)]
                return simp_pts
    except Exception:
        pass
    return [[round(p[0], 5), round(p[1], 5)] for p in clean_coords]


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
        best_acc = int(acc[r, c]) if 0 <= r < nrows and 0 <= c < ncols else -1

        # 3. Always refine via Flow Accumulation raster to sit directly on the flowing D8 channel
        # Use tighter radius if already on OSM (~5 cells = 60m), wider if raw coordinate (~15 cells = 180m)
        cur_search_radius = 5 if snapped_via_osm else search_radius_cells
        if 0 <= r < nrows and 0 <= c < ncols:
            r_min = max(0, r - cur_search_radius)
            r_max = min(nrows, r + cur_search_radius + 1)
            c_min = max(0, c - cur_search_radius)
            c_max = min(ncols, c + cur_search_radius + 1)

            for cr in range(r_min, r_max):
                for cc in range(c_min, c_max):
                    if acc[cr, cc] > best_acc and acc[cr, cc] >= min_acc_cells:
                        best_acc = int(acc[cr, cc])
                        best_r, best_c = cr, cc

            if not snapped_via_osm:
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
    min_lat: Optional[float] = None,
    max_steps: int = 5000
) -> Tuple[List[List[float]], Optional[Any]]:
    """
    Traces D8 flow path downstream cell by cell with high performance vectorized coordinate conversion.
    Stops immediately if path reaches min_lat (southernmost basin boundary).
    Returns (coordinates_list [[lon, lat], ...], stop_data).
    """
    nrows, ncols = fdir.shape
    curr_r, curr_c = start_r, start_c
    visited: Set[Tuple[int, int]] = set()
    stop_data = None
    path_rc = []

    for _ in range(max_steps):
        if not (0 <= curr_r < nrows and 0 <= curr_c < ncols):
            break
        if (curr_r, curr_c) in visited:
            break  # cycle protection
        visited.add((curr_r, curr_c))
        path_rc.append((curr_r, curr_c))

        if stop_condition_fn:
            should_stop, data = stop_condition_fn(curr_r, curr_c)
            if should_stop:
                stop_data = data
                break

        if min_lat is not None:
            # Check cell latitude to never extend past southern limit
            cell_lat = transform[5] + (curr_r + 0.5) * transform[4]
            if cell_lat < min_lat:
                break

        code = int(fdir[curr_r, curr_c])
        if code not in D8_DELTAS:
            break
        dr, dc = D8_DELTAS[code]
        curr_r, curr_c = curr_r + dr, curr_c + dc

    if not path_rc:
        return [], stop_data

    # Vectorized conversion from (row, col) to (lon, lat)
    is_geographic = (crs is None) or getattr(crs, 'is_geographic', False) or (str(crs) == "EPSG:4326")
    transformer = None
    if not is_geographic and crs is not None:
        try:
            from pyproj import Transformer
            transformer = Transformer.from_crs(crs, "EPSG:4326", always_xy=True)
        except Exception:
            transformer = None

    r_arr = np.array([p[0] for p in path_rc], dtype=np.float64)
    c_arr = np.array([p[1] for p in path_rc], dtype=np.float64)
    xs = transform[2] + (c_arr + 0.5) * transform[0] + (r_arr + 0.5) * transform[1]
    ys = transform[5] + (c_arr + 0.5) * transform[3] + (r_arr + 0.5) * transform[4]

    if transformer is not None:
        lons, lats = transformer.transform(xs, ys)
    else:
        lons, lats = xs, ys

    coords = [[round(float(lo), 6), round(float(la), 6)] for lo, la in zip(lons, lats)]

    # Strictly filter out any coordinates below min_lat
    if min_lat is not None:
        filtered_coords = []
        for pt in coords:
            if pt[1] >= min_lat:
                filtered_coords.append(pt)
            else:
                filtered_coords.append([pt[0], round(min_lat, 6)])
                break
        coords = filtered_coords

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
    s_overland = max(0.0005, overland_slope)
    s_channel = max(0.0001, channel_slope)
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
    - Layer 1: Gauge-to-Gauge River Backbone Flowpaths (Continuous 12.5m Hydro-D8 Routing, 0 straight lines)
    - Layer 2: Rainfall-to-Gauge Overland Flowpaths (Hillslope D8 -> River Network)
    - Southern Limit: Strictly bounded to southernmost water station + 5 km.

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

    # Calculate Southernmost boundary: Southernmost water level station - 5km (~0.045 deg)
    water_lats = [float(st['latitude']) for st in water_stations if st.get('latitude') is not None]
    min_water_lat = min(water_lats) if water_lats else None
    southern_limit_lat = round(min_water_lat - (5.0 / 111.0), 5) if min_water_lat is not None else None

    # Optimized elevation sampling with memoization cache
    inv_trans = ~transform
    elev_cache: Dict[Tuple[float, float], float] = {}

    def sample_elevation(lon: float, lat: float) -> float:
        r_key = (round(lon, 4), round(lat, 4))
        if r_key in elev_cache:
            return elev_cache[r_key]
        if inv_transformer is not None:
            px, py = inv_transformer.transform(lon, lat)
            gc, gr = inv_trans * (px, py)
        else:
            gc, gr = inv_trans * (lon, lat)
        gr = max(0, min(nrows - 1, int(gr)))
        gc = max(0, min(ncols - 1, int(gc)))
        val = float(filled_dem[gr, gc])
        res = val if not np.isnan(val) and val != -9999.0 else 100.0
        elev_cache[r_key] = res
        return res

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
    river_graph.build_spatial_index()

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
            # Generous 11x11 capture footprint (~135m) around each station so flowing stream D8 channel always hits it
            for dr in (-5, -4, -3, -2, -1, 0, 1, 2, 3, 4, 5):
                for dc in (-5, -4, -3, -2, -1, 0, 1, 2, 3, 4, 5):
                    nr, nc = r + dr, c + dc
                    if 0 <= nr < nrows and 0 <= nc < ncols:
                        water_grid_map[(nr, nc)] = st_id

        # Find closest node on river graph within tight tolerance (~1.5 km)
        nid, d_nid = river_graph.find_nearest_node(st_lon, st_lat, max_dist_deg=0.015)
        if nid:
            water_node_map[st_id] = nid

    features = []
    gauge_relations = []
    rainfall_relations = []

    try:
        from tqdm import tqdm
    except ImportError:
        def tqdm(iterable, **kwargs):
            return iterable

    # =========================================================================
    # LAYER 1: Gauge-to-Gauge Backbone Flow Paths (Upstream -> Downstream)
    # =========================================================================
    pbar_water = tqdm(
        water_stations,
        desc="        [Progress] Layer 1: Gauge-to-Gauge Flow Paths",
        unit="station",
        ncols=85,
        leave=False
    )
    for st in pbar_water:
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
            min_lat=southern_limit_lat,
            max_steps=5000
        )

        if target_station_id:
            target_st = next((s for s in water_stations if s['station_id'] == target_station_id), None)
            if not target_st:
                continue

            tgt_lon = float(target_st.get('longitude', 0.0))
            tgt_lat = float(target_st.get('latitude', 0.0))

            # Continuous 12.5m DEM D8 raster coordinates: zero straight lines
            coords = merge_coordinates([[st_lon, st_lat]], raster_coords)
            
            # Snap the final vertex to the target station smoothly only if within 500m
            last_dist = math.hypot(coords[-1][0] - tgt_lon, coords[-1][1] - tgt_lat)
            if last_dist <= 0.005:
                coords[-1] = [round(tgt_lon, 6), round(tgt_lat, 6)]

            dist_km = linestring_length_km(coords)
            z_up = sample_elevation(st_lon, st_lat)
            z_down = sample_elevation(tgt_lon, tgt_lat)
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
                    "to_station_name": target_st.get('station_name', ''),
                    "distance_km": round(dist_km, 2),
                    "river_slope": round(slope, 6),
                    "elevation_diff_m": round(dz, 2),
                    "upstream_elev_m": round(z_up, 2),
                    "downstream_elev_m": round(z_down, 2),
                },
                "geometry": {
                    "type": "LineString",
                    "coordinates": simplify_linestring_coords(coords, tolerance_deg=0.00035)
                }
            }
            features.append(feature)
            gauge_relations.append(feature["properties"])
        elif raster_coords and len(raster_coords) >= 2:
            coords = merge_coordinates([[st_lon, st_lat]], raster_coords)
            dist_km = linestring_length_km(coords)
            if dist_km >= 1.0:
                z_up = sample_elevation(st_lon, st_lat)
                z_down = sample_elevation(coords[-1][0], coords[-1][1])
                dz = max(0.0, z_up - z_down)
                slope = (dz / (dist_km * 1000.0)) if dist_km > 0.001 else 0.0001

                feature_id = f"flow_gauge_{st_id}_downstream"
                feature = {
                    "type": "Feature",
                    "id": feature_id,
                    "properties": {
                        "feature_type": "gauge_to_gauge_flowpath",
                        "from_station_id": st_id,
                        "from_station_name": st.get('station_name', ''),
                        "to_station_id": "",
                        "to_station_name": "Basin Outlet / Main River Flow",
                        "distance_km": round(dist_km, 2),
                        "river_slope": round(slope, 6),
                        "elevation_diff_m": round(dz, 2),
                        "upstream_elev_m": round(z_up, 2),
                        "downstream_elev_m": round(z_down, 2),
                    },
                    "geometry": {
                        "type": "LineString",
                        "coordinates": simplify_linestring_coords(coords, tolerance_deg=0.00035)
                    }
                }
                features.append(feature)

    # =========================================================================
    # LAYER 2: Rain-to-Gauge Overland Connectors (Overland -> River Backbone)
    # =========================================================================
    # Target water nodes set for early termination and Dijkstra SSSP caching
    target_water_nodes = set(water_node_map.values())
    dijkstra_cache: Dict[int, Tuple[Dict[int, float], Dict[int, Tuple[int, Dict[str, Any]]]]] = {}

    # Stop condition: stops when encountering any water level station on the 12.5m grid
    def stop_at_water_station(curr_r, curr_c):
        t_id = water_grid_map.get((curr_r, curr_c))
        if t_id:
            return True, t_id
        return False, None

    pbar_rain = tqdm(
        rain_stations,
        desc="        [Progress] Layer 2: Rain-to-Gauge Overland Flow Paths",
        unit="station",
        ncols=85,
        leave=False
    )
    for r_st in pbar_rain:
        r_id = str(r_st.get('station_id', '')).strip()
        if not r_id:
            continue
        lat, lon = float(r_st['latitude']), float(r_st['longitude'])

        # Skip rain stations located south of the basin limit
        if southern_limit_lat is not None and lat < southern_limit_lat:
            continue

        if inv_transformer is not None:
            proj_x, proj_y = inv_transformer.transform(lon, lat)
            r, c = rowcol(transform, proj_x, proj_y)
        else:
            r, c = rowcol(transform, lon, lat)

        if not (0 <= r < nrows and 0 <= c < ncols):
            continue

        # 1. Trace Continuous 12.5m DEM D8 flow path downstream from rain station
        overland_coords, direct_target_water_id = trace_downstream_path(
            r, c, fdir, transform, crs=crs,
            stop_condition_fn=stop_at_water_station,
            min_lat=southern_limit_lat,
            max_steps=5000
        )

        z_rain = sample_elevation(lon, lat)

        # Case 1: D8 flow path directly encountered a water level gauge
        if direct_target_water_id:
            target_st = next((s for s in water_stations if s['station_id'] == direct_target_water_id), None)
            if target_st:
                tgt_lon = float(target_st.get('longitude', 0.0))
                tgt_lat = float(target_st.get('latitude', 0.0))
                z_water = sample_elevation(tgt_lon, tgt_lat)

                coords = merge_coordinates([[lon, lat]], overland_coords)
                last_dist = math.hypot(coords[-1][0] - tgt_lon, coords[-1][1] - tgt_lat)
                if last_dist <= 0.005:
                    coords[-1] = [round(tgt_lon, 6), round(tgt_lat, 6)]

                dist_km = linestring_length_km(coords)
                dz = max(0.0, z_rain - z_water)
                slope = (dz / (dist_km * 1000.0)) if dist_km > 0.001 else 0.005

                lag_min_m, lag_avg_m, lag_max_m, lag_min_h, lag_avg_h, lag_max_h = compute_rainfall_lag_bounds(
                    overland_dist_km=dist_km,
                    overland_slope=slope,
                    channel_dist_km=0.0,
                    channel_slope=slope,
                    total_dz_m=dz
                )

                feature_id = f"flow_rain_{r_id}_to_{direct_target_water_id}"
                feature = {
                    "type": "Feature",
                    "id": feature_id,
                    "properties": {
                        "feature_type": "rainfall_to_gauge_flowpath",
                        "from_station_id": r_id,
                        "from_station_name": r_st.get('station_name', ''),
                        "to_station_id": direct_target_water_id,
                        "to_station_name": target_st.get('station_name', ''),
                        "total_distance_km": round(dist_km, 2),
                        "distance_km": round(dist_km, 2),
                        "response_lag_minutes": lag_avg_m,
                        "response_lag_minutes_min": lag_min_m,
                        "response_lag_minutes_max": lag_max_m,
                        "response_lag_hours": lag_avg_h,
                        "response_lag_hours_min": lag_min_h,
                        "response_lag_hours_max": lag_max_h,
                        "elevation_diff_m": round(dz, 2),
                        "slope": round(slope, 6),
                        "upstream_elev_m": round(z_rain, 2),
                        "downstream_elev_m": round(z_water, 2),
                        "influence_weight_percent": 100.0
                    },
                    "geometry": {
                        "type": "LineString",
                        "coordinates": simplify_linestring_coords(coords, tolerance_deg=0.00035)
                    }
                }
                features.append(feature)
                rainfall_relations.append(feature["properties"])
                continue

        # Case 2: Scan for tight intersection with OSM river backbone (< 300m)
        entry_idx = None
        entry_node = None
        if len(overland_coords) >= 2:
            STRIDE = 8
            for p_idx in range(0, len(overland_coords), STRIDE):
                pt = overland_coords[p_idx]
                nid, d_nid = river_graph.find_nearest_node(pt[0], pt[1], max_dist_deg=0.003)  # tight 300m
                if nid and d_nid <= 0.003:
                    entry_idx = p_idx
                    entry_node = nid
                    break

        downstream_targets = []
        if entry_node is not None:
            if entry_node not in dijkstra_cache:
                dijkstra_cache[entry_node] = river_graph.dijkstra_single_source(entry_node, target_water_nodes, max_dist_km=60.0)
            dist_map, prev_map = dijkstra_cache[entry_node]

            for wst in water_stations:
                w_id = str(wst.get('station_id', '')).strip()
                v_node = water_node_map.get(w_id)
                if v_node and v_node != entry_node and v_node in dist_map:
                    b_dist = dist_map[v_node]
                    w_lon, w_lat = float(wst['longitude']), float(wst['latitude'])
                    if southern_limit_lat is not None and w_lat < southern_limit_lat:
                        continue
                    w_elev = sample_elevation(w_lon, w_lat)
                    if w_elev <= z_rain + 2.0:
                        backbone_coords = river_graph.reconstruct_path_from_prev(prev_map, entry_node, v_node)
                        if backbone_coords and len(backbone_coords) >= 2:
                            downstream_targets.append((b_dist, w_id, wst, backbone_coords))

            downstream_targets.sort(key=lambda x: x[0])
            # Select ONLY the single best downstream receiving station (no artificial branching)
            if downstream_targets:
                downstream_targets = [downstream_targets[0]]

        if entry_idx is not None:
            # Case 2: Hit OSM River. We MUST stop overland runoff at entry_idx.
            coords = merge_coordinates(
                [[lon, lat]],
                overland_coords[:entry_idx + 1]
            )
            overland_dist_km = linestring_length_km(coords)
            entry_pt = overland_coords[entry_idx]

            if downstream_targets:
                # Has valid downstream water gauge(s)
                for b_dist, target_water_id, target_st, backbone_coords in downstream_targets:
                    tgt_lon = float(target_st.get('longitude', 0.0))
                    tgt_lat = float(target_st.get('latitude', 0.0))
                    z_water = sample_elevation(tgt_lon, tgt_lat)
                    
                    channel_dist_km = b_dist
                    total_dist_km = overland_dist_km + channel_dist_km
                    dz = max(0.0, z_rain - z_water)

                    overland_dz = max(0.0, z_rain - sample_elevation(entry_pt[0], entry_pt[1]))
                    overland_slope = (overland_dz / (overland_dist_km * 1000.0)) if overland_dist_km > 0.001 else 0.01
                    channel_slope = (dz / (total_dist_km * 1000.0)) if total_dist_km > 0.001 else 0.0005

                    lag_min_m, lag_avg_m, lag_max_m, lag_min_h, lag_avg_h, lag_max_h = compute_rainfall_lag_bounds(
                        overland_dist_km=overland_dist_km,
                        overland_slope=overland_slope,
                        channel_dist_km=channel_dist_km,
                        channel_slope=channel_slope,
                        total_dz_m=dz
                    )
                    
                    # Append river coordinates so the flow path follows the river to the gauge
                    full_coords = merge_coordinates(coords, backbone_coords)
                    
                    feature_id = f"flow_rain_{r_id}_to_{target_water_id}"
                    feature = {
                        "type": "Feature",
                        "id": feature_id,
                        "properties": {
                            "feature_type": "rainfall_to_gauge_flowpath",
                            "from_station_id": r_id,
                            "from_station_name": r_st.get('station_name', ''),
                            "to_station_id": target_water_id,
                            "to_station_name": target_st.get('station_name', ''),
                            "total_distance_km": round(total_dist_km, 2),
                            "distance_km": round(overland_dist_km, 2),
                            "channel_distance_km": round(channel_dist_km, 2),
                            "response_lag_minutes": lag_avg_m,
                            "response_lag_minutes_min": lag_min_m,
                            "response_lag_minutes_max": lag_max_m,
                            "response_lag_hours": lag_avg_h,
                            "response_lag_hours_min": lag_min_h,
                            "response_lag_hours_max": lag_max_h,
                            "elevation_diff_m": round(dz, 2),
                            "slope": round(overland_slope, 6),
                            "upstream_elev_m": round(z_rain, 2),
                            "downstream_elev_m": round(z_water, 2),
                            "influence_weight_percent": 100.0
                        },
                        "geometry": {
                            "type": "LineString",
                            "coordinates": simplify_linestring_coords(full_coords, tolerance_deg=0.00035)
                        }
                    }
                    features.append(feature)
                    rainfall_relations.append(feature["properties"])
            else:
                # Hit the river, but NO water station within 60km.
                # Stop at the river, don't wander off.
                z_water = sample_elevation(entry_pt[0], entry_pt[1])
                dz = max(0.0, z_rain - z_water)
                overland_slope = (dz / (overland_dist_km * 1000.0)) if overland_dist_km > 0.001 else 0.01

                lag_min_m, lag_avg_m, lag_max_m, lag_min_h, lag_avg_h, lag_max_h = compute_rainfall_lag_bounds(
                    overland_dist_km=overland_dist_km,
                    overland_slope=overland_slope,
                    channel_dist_km=0.0,
                    channel_slope=overland_slope,
                    total_dz_m=dz
                )

                feature_id = f"flow_rain_{r_id}_to_river"
                feature = {
                    "type": "Feature",
                    "id": feature_id,
                    "properties": {
                        "feature_type": "rainfall_to_gauge_flowpath",
                        "from_station_id": r_id,
                        "from_station_name": r_st.get('station_name', ''),
                        "to_station_id": "",
                        "to_station_name": "Stream Entry Point (No Gauge)",
                        "total_distance_km": round(overland_dist_km, 2),
                        "distance_km": round(overland_dist_km, 2),
                        "channel_distance_km": 0.0,
                        "response_lag_minutes": lag_avg_m,
                        "response_lag_minutes_min": lag_min_m,
                        "response_lag_minutes_max": lag_max_m,
                        "response_lag_hours": lag_avg_h,
                        "response_lag_hours_min": lag_min_h,
                        "response_lag_hours_max": lag_max_h,
                        "elevation_diff_m": round(dz, 2),
                        "slope": round(overland_slope, 6),
                        "upstream_elev_m": round(z_rain, 2),
                        "downstream_elev_m": round(z_water, 2),
                        "influence_weight_percent": 100.0
                    },
                    "geometry": {
                        "type": "LineString",
                        "coordinates": simplify_linestring_coords(coords, tolerance_deg=0.00035)
                    }
                }
                features.append(feature)
        else:
            # Case 3: Standalone overland drainage along terrain D8 (never hit OSM river)
            if len(overland_coords) >= 2:
                coords = merge_coordinates([[lon, lat]], overland_coords)
                dist_km = linestring_length_km(coords)
                if dist_km >= 0.5:  # Changed to 500m per user request
                    z_end = sample_elevation(coords[-1][0], coords[-1][1])
                    dz = max(0.0, z_rain - z_end)
                    slope = (dz / (dist_km * 1000.0)) if dist_km > 0.001 else 0.01

                    lag_min_m, lag_avg_m, lag_max_m, lag_min_h, lag_avg_h, lag_max_h = compute_rainfall_lag_bounds(
                        overland_dist_km=dist_km,
                        overland_slope=slope,
                        channel_dist_km=0.0,
                        channel_slope=slope,
                        total_dz_m=dz
                    )
                    feature_id = f"flow_rain_{r_id}_overland"
                    feature = {
                        "type": "Feature",
                        "id": feature_id,
                        "properties": {
                            "feature_type": "rainfall_to_gauge_flowpath",
                            "from_station_id": r_id,
                            "from_station_name": r_st.get('station_name', ''),
                            "to_station_id": "",
                            "to_station_name": "Local Overland Drainage",
                            "total_distance_km": round(dist_km, 2),
                            "distance_km": round(dist_km, 2),
                            "response_lag_minutes": lag_avg_m,
                            "response_lag_minutes_min": lag_min_m,
                            "response_lag_minutes_max": lag_max_m,
                            "response_lag_hours": lag_avg_h,
                            "response_lag_hours_min": lag_min_h,
                            "response_lag_hours_max": lag_max_h,
                            "elevation_diff_m": round(dz, 2),
                            "slope": round(slope, 6),
                            "upstream_elev_m": round(z_rain, 2),
                            "downstream_elev_m": round(z_end, 2),
                            "influence_weight_percent": 100.0
                        },
                        "geometry": {
                            "type": "LineString",
                            "coordinates": simplify_linestring_coords(coords, tolerance_deg=0.00035)
                        }
                    }
                    features.append(feature)

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

    # Reusable bitmask / boolean array for ultra-fast zero-allocation BFS
    visited_mask = np.zeros((nrows, ncols), dtype=bool)

    for st in pbar:
        st_id = str(st.get('station_id', '')).strip()
        start_r = st.get('grid_row')
        start_c = st.get('grid_col')
        if start_r is None or start_c is None:
            continue
        if not (0 <= start_r < nrows and 0 <= start_c < ncols):
            continue

        # Fast O(1) BFS upstream traversal
        visited_coords = []
        queue = deque([(start_r, start_c)])
        visited_mask[start_r, start_c] = True
        visited_coords.append((start_r, start_c))

        while queue:
            cr, cc = queue.popleft()
            for code, (dr, dc) in reverse_d8.items():
                nr, nc = cr + dr, cc + dc
                if 0 <= nr < nrows and 0 <= nc < ncols and not visited_mask[nr, nc]:
                    if int(fdir[nr, nc]) == code:
                        visited_mask[nr, nc] = True
                        queue.append((nr, nc))
                        visited_coords.append((nr, nc))

        # Reset mask for visited cells only (blazing fast O(K) instead of O(N*M))
        for vr, vc in visited_coords:
            visited_mask[vr, vc] = False

        # Create bounding polygon for the catchment cells
        if len(visited_coords) > 10:
            sample_step = max(1, len(visited_coords) // 100)
            xs = []
            ys = []
            for vr, vc in visited_coords[::sample_step]:
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

                area_km2 = len(visited_coords) * cell_area_km2

                features.append({
                    "type": "Feature",
                    "id": f"catchment_{st_id}",
                    "properties": {
                        "station_id": st_id,
                        "station_name": st.get('station_name', ''),
                        "catchment_area_km2": round(area_km2, 2),
                        "contributing_cells": len(visited_coords)
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
