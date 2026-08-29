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
import time
from typing import Dict, List, Tuple, Any, Optional, Set
import numpy as np
from rasterio.transform import Affine, rowcol
from shapely.geometry import Point, LineString, Polygon, MultiPoint, mapping, shape
from shapely.ops import unary_union
from .gis_utils import haversine_distance, linestring_length_km
from .terrain_engine import D8_DELTAS

# Sentinel returned by trace_downstream_path when the trace stops on a river-mask cell
# (OSM waterway footprint) before reaching any gauge footprint.
RIVER_STOP = "__river_stop__"

# Sentinel returned when the trace stops on an OSM water POLYGON cell (reservoir /
# lake / wide river surface). Open water must not be traced cell-by-cell: the flow
# enters the backbone / reservoir transit instead (round 6, Phase A2).
WATER_POLY_STOP = "__water_poly_stop__"


def _point_seg_project(px: float, py: float, ax: float, ay: float, bx: float, by: float):
    """
    Projects point (px, py) onto segment (a -> b) in lon/lat space.
    Returns (t, dist, cx, cy) with t in [0, 1], dist = point-to-projection distance,
    (cx, cy) = projection point. Degenerate segments project to the endpoint.
    """
    dx, dy = bx - ax, by - ay
    l2 = dx * dx + dy * dy
    if l2 <= 0.0:
        return 0.0, math.hypot(px - ax, py - ay), ax, ay
    t = ((px - ax) * dx + (py - ay) * dy) / l2
    t = max(0.0, min(1.0, t))
    cx, cy = ax + t * dx, ay + t * dy
    return t, math.hypot(px - cx, py - cy), cx, cy


def _extract_intersection_points(geom) -> List[Tuple[float, float]]:
    """Recursively extracts representative (lon, lat) points from an intersection geometry."""
    out: List[Tuple[float, float]] = []
    try:
        gt = geom.geom_type
    except Exception:
        return out
    if gt == "Point":
        out.append((geom.x, geom.y))
    elif gt == "LineString":
        cs = list(geom.coords)
        if cs:
            out.append((cs[0][0], cs[0][1]))
            if len(cs) > 1:
                out.append((cs[-1][0], cs[-1][1]))
    elif gt in ("MultiPoint", "MultiLineString", "GeometryCollection"):
        for sub in geom.geoms:
            out.extend(_extract_intersection_points(sub))
    return out


def _collect_line_intersections(lines: List["LineString"]) -> List[Tuple[float, float]]:
    """
    Returns unique (lon, lat) intersection points between distinct input lines using
    an STRtree candidate search. Ways that merely share welded vertices yield points
    that already coincide with graph nodes and are skipped later by the caller.
    """
    from shapely.strtree import STRtree

    out: Dict[Tuple[float, float], Tuple[float, float]] = {}
    if len(lines) < 2:
        return []
    tree = STRtree(lines)
    for i, li in enumerate(lines):
        try:
            cand = tree.query(li)
        except Exception:
            continue
        for c in cand:
            j = int(c)
            if j <= i or j >= len(lines):
                continue
            lj = lines[j]
            try:
                if not li.intersects(lj):
                    continue
                inter = li.intersection(lj)
            except Exception:
                continue
            for (px, py) in _extract_intersection_points(inter):
                key = (round(px, 6), round(py, 6))
                out[key] = (px, py)
    return list(out.values())


# Reverse D8 lookup: code -> (dr, dc) offset of the UPSTREAM neighbor cell that
# flows into the current cell (fdir[nr, nc] == code means (nr, nc) drains into (r, c)).
REVERSE_D8 = {
    1: (0, -1),
    2: (-1, -1),
    4: (-1, 0),
    8: (-1, 1),
    16: (0, 1),
    32: (1, 1),
    64: (1, 0),
    128: (1, -1),
}


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
        # Connectivity finalization (noding of crossing ways + endpoint welding):
        self._source_coords: List[List[List[float]]] = []   # raw way geometries for intersection noding
        self.way_end_nodes: List[Tuple[int, int]] = []      # (end_node_id, way_id) per added way
        self._next_way_id = 1
        self._edge_grid: Optional[Dict[Tuple[int, int], List[int]]] = None  # cell -> [edge_uid]
        self._edges: Dict[int, Tuple[int, int]] = {}        # edge_uid -> (a, b)
        self._edge_cells: Dict[int, List[Tuple[int, int]]] = {}
        self._edge_cell_size = 0.01
        self._next_edge_uid = 1
        # Phase 4 (RC2/G4): per-way metadata for attach candidate ranking
        self.way_meta: Dict[int, Dict[str, Any]] = {}   # way_id -> {osm_id, class, length_km}
        self._nodes_reach_gauge: Optional[Set[int]] = None

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
        river_name: str = "",
        elevs: Optional[List[Optional[float]]] = None,
        waterway_class: str = "stream",
        osm_id: str = ""
    ):
        """
        Adds an OSM waterway LineString into the directed graph with full-vertex granular noding.
        Every consecutive vertex pair (C_i, C_{i+1}) becomes a discrete directed edge,
        enabling 100% topological connectivity for all tributaries, stations, and overland entries.

        elevs: optional precomputed elevation list aligned with `coords` (None entry = unknown,
        e.g. vertex outside the DEM). Batch-supplied by the caller to avoid per-vertex
        reprojection overhead. Vertices with unknown elevation never trigger a direction flip;
        the original OSM digitization direction is kept instead (tagged direction_source="osm").
        """
        if len(coords) < 2:
            return

        way_id = self._next_way_id
        self._next_way_id += 1
        self._source_coords.append(list(coords))
        self.way_meta[way_id] = {
            "osm_id": str(osm_id),
            "class": str(waterway_class or "stream"),
            "length_km": linestring_length_km(coords)
        }

        seg_elevs: Optional[List[Optional[float]]] = None
        if elevs is not None and len(elevs) == len(coords):
            seg_elevs = list(elevs)

        def _elev(i: int) -> Optional[float]:
            if seg_elevs is not None:
                return seg_elevs[i]
            if sample_elev_fn is not None:
                v = sample_elev_fn(coords[i][0], coords[i][1])
                return float(v) if v is not None else None
            return 100.0

        lon_start, lat_start = coords[0][0], coords[0][1]
        lon_end, lat_end = coords[-1][0], coords[-1][1]

        z_start = _elev(0)
        z_end = _elev(len(coords) - 1)

        # Topological Direction Enforcement: flow from higher elevation to lower elevation.
        # Unknown elevation (outside DEM / nodata) -> trust the OSM digitization direction.
        segment_coords = list(coords)
        if z_start is not None and z_end is not None and z_start < z_end - 0.5:
            segment_coords.reverse()
            if seg_elevs is not None:
                seg_elevs.reverse()
            direction_source = "elevation"
        elif z_start is None or z_end is None:
            direction_source = "osm"
        else:
            direction_source = "elevation"

        # Connect vertex-by-vertex
        prev_node = None
        for i in range(len(segment_coords)):
            p_lon, p_lat = segment_coords[i][0], segment_coords[i][1]
            if seg_elevs is not None:
                p_elev = seg_elevs[i]
            elif sample_elev_fn is not None:
                v = sample_elev_fn(p_lon, p_lat)
                p_elev = float(v) if v is not None else None
            else:
                p_elev = 100.0
            curr_node = self._get_or_create_node(p_lon, p_lat, p_elev)

            if prev_node is not None and prev_node != curr_node:
                p_prev_lon, p_prev_lat, z_p = self.nodes[prev_node]
                p_curr_lon, p_curr_lat, z_c = self.nodes[curr_node]
                sub_coords = [[p_prev_lon, p_prev_lat], [p_curr_lon, p_curr_lat]]
                length_km = linestring_length_km(sub_coords)

                dz = max(0.0, z_p - z_c) if (z_p is not None and z_c is not None) else 0.0
                edge_data = {
                    "coords": sub_coords,
                    "length_km": max(0.001, length_km),
                    "z_start": z_p,
                    "z_end": z_c,
                    "dz": dz,
                    "river_name": river_name,
                    "direction_source": direction_source,
                    "way": way_id
                }

                # Primary downstream edge (enforce downstream flow only)
                self.adj[prev_node].append((curr_node, edge_data))

            prev_node = curr_node

        # Record the way's downstream end node (after any direction flip) for
        # endpoint-gap welding in finalize_connectivity()
        self.way_end_nodes.append((prev_node, way_id))

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

    # ---------------------------------------------------------------------
    # Connectivity finalization: crossing noding + endpoint-gap welding
    # ---------------------------------------------------------------------
    def _build_edge_grid(self):
        """Spatial-hash index over all graph edges for nearest-edge queries."""
        self._edge_grid = {}
        self._edges = {}
        self._edge_cells = {}
        cell = self._edge_cell_size
        uid = 0
        for a, outs in self.adj.items():
            pa = self.nodes.get(a)
            if pa is None:
                continue
            for (b, _data) in outs:
                pb = self.nodes.get(b)
                if pb is None or a == b:
                    continue
                uid += 1
                self._edges[uid] = (a, b)
                x0, x1 = min(pa[0], pb[0]), max(pa[0], pb[0])
                y0, y1 = min(pa[1], pb[1]), max(pa[1], pb[1])
                cells = []
                cx0, cx1 = int(math.floor(x0 / cell)), int(math.floor(x1 / cell))
                cy0, cy1 = int(math.floor(y0 / cell)), int(math.floor(y1 / cell))
                for cx in range(cx0, cx1 + 1):
                    for cy in range(cy0, cy1 + 1):
                        self._edge_grid.setdefault((cx, cy), []).append(uid)
                        cells.append((cx, cy))
                self._edge_cells[uid] = cells
        self._next_edge_uid = uid + 1

    def _index_edge(self, a: int, b: int) -> int:
        cell = self._edge_cell_size
        pa, pb = self.nodes[a], self.nodes[b]
        uid = self._next_edge_uid
        self._next_edge_uid += 1
        self._edges[uid] = (a, b)
        cells = []
        cx0, cx1 = int(math.floor(min(pa[0], pb[0]) / cell)), int(math.floor(max(pa[0], pb[0]) / cell))
        cy0, cy1 = int(math.floor(min(pa[1], pb[1]) / cell)), int(math.floor(max(pa[1], pb[1]) / cell))
        for cx in range(cx0, cx1 + 1):
            for cy in range(cy0, cy1 + 1):
                self._edge_grid.setdefault((cx, cy), []).append(uid)
                cells.append((cx, cy))
        self._edge_cells[uid] = cells
        return uid

    def _remove_edge_from_index(self, uid: int):
        for cell in self._edge_cells.pop(uid, []):
            bucket = self._edge_grid.get(cell)
            if bucket and uid in bucket:
                bucket.remove(uid)
        self._edges.pop(uid, None)

    def _edge_way_id(self, a: int, b: int) -> Optional[int]:
        for (nb, data) in self.adj.get(a, []):
            if nb == b:
                return data.get("way")
        return None

    def _nearest_edges(
        self,
        lon: float,
        lat: float,
        max_dist_deg: float,
        exclude_nodes: Optional[Set[int]] = None
    ) -> List[Tuple[float, int, float, Tuple[float, float], int, int]]:
        """
        Finds ALL graph edges within max_dist_deg of (lon, lat), sorted by distance.
        Returns list of (dist_deg, edge_uid, t, (proj_lon, proj_lat), a, b).
        """
        if self._edge_grid is None:
            self._build_edge_grid()
        cell = self._edge_cell_size
        gx, gy = int(math.floor(lon / cell)), int(math.floor(lat / cell))
        r = max(1, int(math.ceil(max_dist_deg / cell)))
        cands: List[Tuple[float, int, float, Tuple[float, float], int, int]] = []
        seen: Set[int] = set()
        for dx in range(-r, r + 1):
            for dy in range(-r, r + 1):
                for uid in self._edge_grid.get((gx + dx, gy + dy), ()):
                    if uid in seen:
                        continue
                    seen.add(uid)
                    a, b = self._edges[uid]
                    if exclude_nodes and (a in exclude_nodes or b in exclude_nodes):
                        continue
                    pa, pb = self.nodes[a], self.nodes[b]
                    t, d, cx, cy = _point_seg_project(lon, lat, pa[0], pa[1], pb[0], pb[1])
                    if d <= max_dist_deg:
                        cands.append((d, uid, t, (cx, cy), a, b))
        cands.sort(key=lambda c: c[0])
        return cands

    def _nearest_edge(
        self,
        lon: float,
        lat: float,
        max_dist_deg: float,
        exclude_nodes: Optional[Set[int]] = None
    ):
        """
        Finds the graph edge closest to (lon, lat) within max_dist_deg.
        Returns (dist_deg, edge_uid, t, (proj_lon, proj_lat), a, b) or None.
        """
        cands = self._nearest_edges(lon, lat, max_dist_deg, exclude_nodes)
        return cands[0] if cands else None

    def _split_edge(self, edge_uid: int, px: float, py: float, t: float) -> Optional[int]:
        """
        Splits edge (a -> b) at projection point (px, py) creating/inserting node X.
        The new node welds with any existing node within snap tolerance (so repeated
        splits at the same location are idempotent). Returns the new node id.
        """
        a, b = self._edges.get(edge_uid, (None, None))
        if a is None:
            return None
        data = None
        for i, (nb, d) in enumerate(self.adj.get(a, [])):
            if nb == b:
                data = d
                idx = i
                break
        if data is None:
            return None
        self.adj[a].pop(idx)
        self._remove_edge_from_index(edge_uid)

        pa, pb = self.nodes[a], self.nodes[b]
        za, zb = pa[2], pb[2]
        z_new = None if (za is None or zb is None) else za + t * (zb - za)
        x_node = self._get_or_create_node(px, py, z_new)

        if x_node in (a, b):
            # Degenerate: the split point welds to an endpoint -> restore the edge
            self.adj[a].append((b, data))
            self._index_edge(a, b)
            return x_node

        way_id = data.get("way")
        river_name = data.get("river_name", "")
        dir_src = data.get("direction_source", "")

        seg1 = [[pa[0], pa[1]], [self.nodes[x_node][0], self.nodes[x_node][1]]]
        seg2 = [[self.nodes[x_node][0], self.nodes[x_node][1]], [pb[0], pb[1]]]
        z_x = self.nodes[x_node][2]

        data1 = dict(data)
        data1["coords"] = seg1
        data1["length_km"] = max(0.001, linestring_length_km(seg1))
        data1["z_start"], data1["z_end"] = za, z_x
        data1["dz"] = max(0.0, za - z_x) if (za is not None and z_x is not None) else 0.0

        data2 = dict(data)
        data2["coords"] = seg2
        data2["length_km"] = max(0.001, linestring_length_km(seg2))
        data2["z_start"], data2["z_end"] = z_x, zb
        data2["dz"] = max(0.0, z_x - zb) if (z_x is not None and zb is not None) else 0.0

        self.adj[a].append((x_node, data1))
        self.adj.setdefault(x_node, []).append((b, data2))
        self._index_edge(a, x_node)
        self._index_edge(x_node, b)
        return x_node

    def _insert_noding_point(self, px: float, py: float) -> bool:
        """
        Inserts a junction node at a geometric crossing of two ways: EVERY edge that
        passes through the crossing point gets split at it (all splits weld into the
        same node via the snap tolerance), connecting all crossing ways at one junction.
        """
        tol = max(self.snap_tolerance_deg * 2.0, 1e-4)
        cands = self._nearest_edges(px, py, tol)
        if not cands:
            return False
        changed = False
        for (d, uid, t, (cx, cy), a, b) in cands:
            if uid not in self._edges:
                continue  # already consumed by a previous split in this loop
            pa, pb = self.nodes[a], self.nodes[b]
            # Already welded to an edge endpoint -> nothing to split for this edge
            if math.hypot(px - pa[0], py - pa[1]) <= self.snap_tolerance_deg:
                continue
            if math.hypot(px - pb[0], py - pb[1]) <= self.snap_tolerance_deg:
                continue
            x_node = self._split_edge(uid, cx, cy, t)
            if x_node is not None:
                changed = True
        return changed

    def _weld_end_node(self, end_node: int, way_id: int, endpoint_snap_deg: float) -> bool:
        """
        Welds a dangling way END (no out-edges) onto the nearest edge of ANOTHER way
        within endpoint_snap_deg by splitting that edge at the projection and adding a
        short downstream connector. Restores connectivity where consecutive OSM ways of
        the same river (or tributary mouths) end a few tens of meters short of each other.
        """
        if self.adj.get(end_node):
            return False  # already continues downstream
        pos = self.nodes.get(end_node)
        if pos is None:
            return False
        cand = self._nearest_edge(pos[0], pos[1], endpoint_snap_deg, exclude_nodes={end_node})
        if cand is None:
            return False
        d, uid, t, (cx, cy), a, b = cand
        # Never weld a way onto itself
        if self._edge_way_id(a, b) == way_id:
            return False

        pa, pb = self.nodes[a], self.nodes[b]
        # Projection lands on an existing edge endpoint -> connect directly to it
        if math.hypot(cx - pa[0], cy - pa[1]) <= self.snap_tolerance_deg:
            target = a
        elif math.hypot(cx - pb[0], cy - pb[1]) <= self.snap_tolerance_deg:
            target = b
        else:
            target = self._split_edge(uid, cx, cy, t)
        if target is None or target == end_node:
            return False

        z_n, z_t = self.nodes[end_node][2], self.nodes[target][2]
        seg = [[self.nodes[end_node][0], self.nodes[end_node][1]],
               [self.nodes[target][0], self.nodes[target][1]]]
        length_km = max(0.001, linestring_length_km(seg))
        # Direction by elevation: water flows from the higher end to the lower end;
        # unknown elevations trust the way-end continuation (n -> target).
        if z_n is not None and z_t is not None and z_n < z_t - 0.5:
            src, dst = target, end_node
        else:
            src, dst = end_node, target
        zs, zd = self.nodes[src][2], self.nodes[dst][2]
        connector = {
            "coords": [[self.nodes[src][0], self.nodes[src][1]], [self.nodes[dst][0], self.nodes[dst][1]]],
            "length_km": length_km,
            "z_start": zs,
            "z_end": zd,
            "dz": max(0.0, zs - zd) if (zs is not None and zd is not None) else 0.0,
            "river_name": "",
            "direction_source": "weld",
            "way": -1
        }
        self.adj.setdefault(src, []).append((dst, connector))
        self._index_edge(src, dst)
        return True

    def count_components(self) -> int:
        """Counts connected components of the (undirected view of the) graph."""
        parent: Dict[int, int] = {}

        def find(x: int) -> int:
            r = x
            while parent[r] != r:
                r = parent[r]
            while parent[x] != r:
                parent[x], x = r, parent[x]
            return r

        for nid in self.nodes:
            parent[nid] = nid
        for a, outs in self.adj.items():
            for (b, _data) in outs:
                ra, rb = find(a), find(b)
                if ra != rb:
                    parent[ra] = rb
        return len({find(n) for n in parent})

    def finalize_connectivity(
        self,
        endpoint_snap_deg: float = 0.001,
        max_noding_points: int = 200_000
    ) -> Dict[str, int]:
        """
        Must be called once after all add_river_segment() calls. Performs:
          1. Crossing noding — ways that geometrically intersect without sharing a vertex
             get a junction node at every intersection point (edge split).
          2. Endpoint welding — dangling way ends are projected onto the nearest edge of
             another way (within endpoint_snap_deg, default ~110m) and connected with a
             downstream connector, healing gaps left by way splitting / simplification.
        Returns diagnostics {components_before, components_after, crossings_split, ends_welded}.
        """
        comp_before = self.count_components()
        n_ways = len(self._source_coords)

        points: List[Tuple[float, float]] = []
        if n_ways >= 2:
            try:
                lines = [LineString(c) for c in self._source_coords if len(c) >= 2]
                points = _collect_line_intersections(lines)[:max_noding_points]
            except Exception as ex:
                print(f"  [WARN] finalize_connectivity: crossing noding skipped ({ex})")

        self._build_edge_grid()
        n_split = 0
        for (px, py) in points:
            try:
                if self._insert_noding_point(px, py):
                    n_split += 1
            except Exception:
                continue

        n_weld = 0
        for (end_node, way_id) in self.way_end_nodes:
            try:
                if self._weld_end_node(end_node, way_id, endpoint_snap_deg):
                    n_weld += 1
            except Exception:
                continue

        comp_after = self.count_components()
        print(f"  [GRAPH] OSM backbone connectivity: ways={n_ways:,}, "
              f"components {comp_before:,} -> {comp_after:,} "
              f"(crossing splits: {n_split:,}, end welds: {n_weld:,})")
        return {
            "components_before": comp_before,
            "components_after": comp_after,
            "crossings_split": n_split,
            "ends_welded": n_weld,
        }

    def snap_point_to_graph(
        self,
        lon: float,
        lat: float,
        max_dist_deg: float = 0.003
    ) -> Tuple[Optional[int], float]:
        """
        Snaps a coordinate onto the graph by projecting it onto the nearest EDGE
        (not just the nearest vertex). Splits the edge at the projection when the
        projection is mid-segment, so the returned node lies exactly on the river.
        Returns (node_id or None, distance_deg).
        """
        cand = self._nearest_edge(lon, lat, max_dist_deg)
        if cand is None:
            nid, d = self.find_nearest_node(lon, lat, max_dist_deg=max_dist_deg)
            return nid, d
        d, uid, t, (cx, cy), a, b = cand
        pa, pb = self.nodes[a], self.nodes[b]
        if math.hypot(cx - pa[0], cy - pa[1]) <= self.snap_tolerance_deg:
            return a, d
        if math.hypot(cx - pb[0], cy - pb[1]) <= self.snap_tolerance_deg:
            return b, d
        x_node = self._split_edge(uid, cx, cy, t)
        if x_node is None:
            return None, d
        return x_node, d

    # ---------------------------------------------------------------------
    # Phase 4 (RC2/G4): quality-aware endpoint attachment
    # ---------------------------------------------------------------------
    def compute_gauge_reachability(self, gauge_nodes: Set[int]):
        """
        Precomputes, ONCE per graph, the set of nodes from which a gauge node is
        reachable DOWNSTREAM on the backbone (reverse BFS upstream from the gauge
        nodes). O(E) time, O(V) memory — amortized over every attach query.
        An edge (a -> b) is 'gauge-connected' iff b is in this set.
        """
        if self._nodes_reach_gauge is not None:
            return
        reverse_adj: Dict[int, List[int]] = {}
        for a, outs in self.adj.items():
            for (b, _data) in outs:
                reverse_adj.setdefault(b, []).append(a)
        seen: Set[int] = set(gauge_nodes)
        stack = list(gauge_nodes)
        while stack:
            u = stack.pop()
            for v in reverse_adj.get(u, ()):
                if v not in seen:
                    seen.add(v)
                    stack.append(v)
        self._nodes_reach_gauge = seen

    _ATTACH_CLASS_SCORE = {"river": 3.0, "canal": 2.5, "wadi": 1.0, "stream": 1.0, "drain": 0.5, "ditch": 0.5}

    def _score_attach_candidate(
        self,
        cand: Tuple[float, int, float, Tuple[float, float], int, int]
    ) -> Tuple[float, bool]:
        """
        Scores a candidate edge for endpoint attachment (higher = better):
          +class        river/canal much better than stream/ditch
          +connectivity way with a gauge downstream-reachable = the primary signal;
                        an island with no gauge downstream scores NEGATIVE
          +length       longer ways (main stems) beat short island fragments
          -distance     proximity still matters, but never decides alone
        Returns (score, gauge_connected).
        """
        d_deg, _uid, _t, _proj, a, b = cand
        way_id = self._edge_way_id(a, b)
        meta = self.way_meta.get(way_id, {}) if way_id else {}
        wclass = str(meta.get("class", "stream"))
        class_score = self._ATTACH_CLASS_SCORE.get(wclass, 1.0)
        gauge_connected = bool(self._nodes_reach_gauge and b in self._nodes_reach_gauge)
        conn_score = 10.0 if gauge_connected else -10.0
        way_len_km = float(meta.get("length_km", 0.0) or 0.0)
        length_score = min(2.0, way_len_km / 25.0)
        dist_m = d_deg * 111_320.0 * 0.95
        dist_penalty = dist_m / 100.0
        score = class_score + conn_score + length_score - dist_penalty
        return score, gauge_connected

    def snap_point_to_graph_ranked(
        self,
        lon: float,
        lat: float,
        max_dist_deg: float = 0.003,
        radii: Optional[List[float]] = None
    ) -> Tuple[Optional[int], float, Optional[Dict[str, Any]]]:
        """
        Quality-aware variant of snap_point_to_graph (Phase 4.12-4.15):
        instead of "nearest edge wins", all candidate edges within the search
        radius are scored (class, gauge-connectivity, way length, distance) and the
        best-scoring one is chosen; the endpoint is then projected onto that edge
        (splitting it at the projection), so the attach point lies exactly on the
        selected river line.

        Radii escalation (Phase 4.13): the first radius only accepts candidates
        passing the quality gate (gauge-connected OR major class). If none pass,
        the remaining radii admit secondary candidates; when the chosen attachment
        has no gauge downstream and is not a major class it is tagged
        attach_quality="degraded" for the validator.

        Returns (node_id or None, distance_deg, attach_meta or None).
        attach_meta = {attach_way_id, attach_osm_id, attach_class, attach_length_km,
                       attach_distance_m, attach_quality}
        """
        cand = self._nearest_edge(lon, lat, max_dist_deg)
        if cand is None:
            nid, d = self.find_nearest_node(lon, lat, max_dist_deg=max_dist_deg)
            if nid is None:
                return None, d, None
            meta = {
                "attach_way_id": None, "attach_osm_id": "", "attach_class": "node",
                "attach_length_km": None, "attach_distance_m": round(d * 111_320.0 * 0.95, 1),
                "attach_quality": "degraded"
            }
            return nid, d, meta

        # Radii escalation: primary radius (quality-gated) -> fallback radius (ungated)
        stages = [(max_dist_deg, True)]
        if radii:
            stages = [(radii[0], True)] + [(r, False) for r in radii[1:]]
        elif max_dist_deg > 0:
            stages = [(max_dist_deg, True), (max_dist_deg * 2.0, False)]

        chosen: Optional[Tuple[float, int, float, Tuple[float, float], int, int]] = None
        chosen_score = -float('inf')
        chosen_connected = False
        for (radius, gated) in stages:
            cands = self._nearest_edges(lon, lat, radius)
            if not cands:
                continue
            scored = []
            for c in cands:
                s, conn = self._score_attach_candidate(c)
                scored.append((s, conn, c))
            scored.sort(key=lambda x: -x[0])
            if gated:
                # quality gate: gauge-connected OR major class (river/canal) only
                def _passes(s, conn, c) -> bool:
                    if conn or s > 0:
                        return True
                    _d, _u, _t, _p, ca, cb = c
                    wcls = str((self.way_meta.get(self._edge_way_id(ca, cb)) or {}).get("class", "stream"))
                    return self._ATTACH_CLASS_SCORE.get(wcls, 1.0) >= 2.0
                best = next(((s, conn, c) for (s, conn, c) in scored if _passes(s, conn, c)), None)
            else:
                best = scored[0] if scored else None
            if best is not None:
                s, conn, c = best
                if chosen is None or s > chosen_score:
                    chosen, chosen_score, chosen_connected = c, s, conn
                if chosen_connected or s > 0:
                    break
        if chosen is None:
            return None, float('inf'), None

        d_deg, uid, t, (cx, cy), a, b = chosen
        way_id = self._edge_way_id(a, b)
        meta_way = self.way_meta.get(way_id, {}) if way_id else {}
        wclass = str(meta_way.get("class", "stream"))

        # Project the endpoint onto the chosen edge (split at the projection) —
        # the returned node sits exactly on the selected river line.
        pa, pb = self.nodes[a], self.nodes[b]
        if math.hypot(cx - pa[0], cy - pa[1]) <= self.snap_tolerance_deg:
            x_node = a
        elif math.hypot(cx - pb[0], cy - pb[1]) <= self.snap_tolerance_deg:
            x_node = b
        else:
            x_node = self._split_edge(uid, cx, cy, t)
        if x_node is None:
            return None, d_deg, None

        attach_meta = {
            "attach_way_id": way_id,
            "attach_osm_id": str(meta_way.get("osm_id", "")),
            "attach_class": wclass,
            "attach_length_km": round(float(meta_way.get("length_km", 0.0) or 0.0), 3),
            "attach_distance_m": round(d_deg * 111_320.0 * 0.95, 1),
            "attach_quality": "ok" if (chosen_connected or self._ATTACH_CLASS_SCORE.get(wclass, 1.0) >= 2.0) else "degraded"
        }
        return x_node, d_deg, attach_meta

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

    def reconstruct_node_path(
        self,
        prev: Dict[int, Tuple[int, Dict[str, Any]]],
        start_node: int,
        end_node: int
    ) -> Optional[List[int]]:
        """
        Backtracks parent pointers from end_node to start_node and returns the NODE sequence.
        Returns None when the chain is broken or cyclic.
        O(path length) time, O(path length) memory.
        """
        if start_node == end_node:
            return [start_node]
        if end_node not in prev:
            return None

        chain: List[int] = []
        curr = end_node
        seen: Set[int] = set()
        while True:
            if curr in seen:
                return None
            seen.add(curr)
            chain.append(curr)
            if curr == start_node:
                break
            if curr not in prev:
                return None
            curr = prev[curr][0]
        chain.reverse()
        return chain

    def stitch_coords_from_prev(
        self,
        prev: Dict[int, Tuple[int, Dict[str, Any]]],
        node_chain: List[int],
        start_pos: int,
        end_pos: int
    ) -> List[List[float]]:
        """
        Stitches edge coordinates along node_chain[start_pos..end_pos] (inclusive) into one
        continuous coordinate list using the prev-tree edges. Nodes must be consecutive on
        the chain; edges are contiguous by construction so the output has no gaps.
        """
        combined: List[List[float]] = []
        for t in range(max(1, start_pos + 1), min(end_pos, len(node_chain) - 1) + 1):
            _, edge_d = prev[node_chain[t]]
            c = edge_d["coords"]
            if not combined:
                combined.extend(c)
            else:
                combined.extend(c[1:])
        return combined


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
    max_step_km: float = 0.5,
    label: str = ""
) -> List[List[float]]:
    """
    Simplifies LineString coordinates using Douglas-Peucker algorithm (tolerance ~35m).
    Eliminates redundant collinear raster stair steps while preserving curves, bends,
    and exact station endpoints.
    Strictly splits at any artificial straight-line jump > max_step_km (500m): only the
    contiguous chunk starting at the path origin is kept (endpoints are always re-appended
    so both ends stay exact), and the discard is reported.
    `label` identifies the calling feature in diagnostics.
    """
    if not coords or len(coords) < 2:
        return [[round(p[0], 5), round(p[1], 5)] for p in coords]

    # 1. Topological Continuity Sanitization: split at any artificial leap > max_step_km
    chunks: List[List[List[float]]] = [[coords[0]]]
    max_jump_km = 0.0
    for i in range(len(coords) - 1):
        p1, p2 = coords[i], coords[i + 1]
        d_km = math.hypot((p2[0] - p1[0]) * 111.32 * 0.95, (p2[1] - p1[1]) * 110.54)
        if d_km > max_step_km:
            max_jump_km = max(max_jump_km, d_km)
            chunks.append([p2])
        else:
            chunks[-1].append(p2)

    clean_coords = chunks[0]
    if len(chunks) > 1:
        dropped = sum(len(ch) for ch in chunks[1:])
        if dropped >= 5:
            who = f" [{label}]" if label else ""
            print(f"  [WARN] simplify_linestring{who}: split at {len(chunks) - 1} jump(s) "
                  f"(max {max_jump_km:.1f}km) > {max_step_km}km; "
                  f"kept {len(clean_coords)} pts from origin, discarded {dropped} pts")
        # Preserve the exact final destination point ONLY when the end gap is a small
        # station-access stub (<= 2km); a large mid-path jump must NOT be re-drawn.
        tail = coords[-1]
        tail_gap_km = math.hypot(
            (tail[0] - clean_coords[-1][0]) * 111.32 * 0.95,
            (tail[1] - clean_coords[-1][1]) * 110.54
        )
        if 1e-9 < tail_gap_km <= 2.0:
            clean_coords.append(tail)

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


def _extract_basin_polygon(basin_boundary_geojson: Optional[Dict[str, Any]]):
    """
    Extracts the first Polygon/MultiPolygon geometry from a basin boundary GeoJSON
    (accepts FeatureCollection, Feature, or bare geometry dicts). Returns a shapely
    geometry or None.
    """
    if not basin_boundary_geojson:
        return None
    try:
        gj = basin_boundary_geojson
        geom = None
        gtype = gj.get("type", "")
        if gtype == "FeatureCollection":
            for f in gj.get("features", []):
                g = (f or {}).get("geometry") or {}
                if g.get("type") in ("Polygon", "MultiPolygon"):
                    geom = g
                    break
        elif gtype == "Feature":
            g = gj.get("geometry") or {}
            if g.get("type") in ("Polygon", "MultiPolygon"):
                geom = g
        elif gtype in ("Polygon", "MultiPolygon"):
            geom = gj
        if geom is not None:
            return shape(geom)
    except Exception:
        return None
    return None


def _clip_line_to_basin(coords: List[List[float]], basin_poly) -> Optional[List[List[float]]]:
    """
    Clips a LineString coordinate list to the basin polygon. When the line crosses the
    boundary the piece containing the ORIGINAL START point is kept (upstream continuity
    to the from-station); if the start is outside, the longest piece is kept.
    Returns None when the line lies entirely outside the basin (feature must be dropped).
    """
    try:
        line = LineString(coords)
        if basin_poly.covers(line):
            return coords
        inter = line.intersection(basin_poly)
        parts: List[Any] = []
        if inter.geom_type == "LineString":
            parts = [inter]
        elif inter.geom_type in ("MultiLineString", "GeometryCollection"):
            parts = [g for g in inter.geoms if g.geom_type == "LineString"]
        if not parts:
            return None
        start = Point(coords[0])
        chosen = None
        for p in parts:
            if p.distance(start) <= 1e-9:
                chosen = p
                break
        if chosen is None:
            chosen = max(parts, key=lambda p: p.length)
        out = [[round(x, 6), round(y, 6)] for x, y in chosen.coords]
        return out if len(out) >= 2 else None
    except Exception:
        return coords


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
    min_acc_cells: int = 100,
    water_polygons_geojson: Optional[Dict[str, Any]] = None
) -> List[Dict[str, Any]]:
    """
    Snaps each water level station to the closest OpenStreetMap river vector channel (if available)
    or the highest flow accumulation cell within `search_radius_cells`.
    Water polygon boundaries (reservoirs / wide rivers) act as snapping targets as well,
    so gauges located next to open water surfaces stay on the network.
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
            if not geom:
                continue
            props = feat.get("properties", {})
            if geom.get("type") == "LineString":
                parts = [geom.get("coordinates", [])]
            elif geom.get("type") == "MultiLineString":
                parts = geom.get("coordinates", [])
            else:
                continue
            for coords in parts:
                if len(coords) >= 2:
                    try:
                        osm_lines.append((LineString(coords), props))
                    except Exception:
                        pass

    # Water polygon boundaries (reservoirs / wide rivers) as snapping targets
    if water_polygons_geojson and water_polygons_geojson.get("features"):
        for feat in water_polygons_geojson.get("features", []):
            geom = feat.get("geometry")
            if not geom or geom.get("type") not in ("Polygon", "MultiPolygon"):
                continue
            try:
                poly_geom = shape(geom)
                polys = list(poly_geom.geoms) if poly_geom.geom_type == "MultiPolygon" else [poly_geom]
                props = feat.get("properties", {})
                for poly in polys:
                    if poly.exterior is not None and len(poly.exterior.coords) >= 2:
                        osm_lines.append((LineString(poly.exterior.coords), props))
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
    max_steps: int = 5000,
    river_mask: Optional[np.ndarray] = None,
    water_poly_mask: Optional[np.ndarray] = None,
    water_poly_ids: Optional[np.ndarray] = None,
    start_poly_id: int = 0
) -> Tuple[List[List[float]], Optional[Any], List[Tuple[int, int]]]:
    """
    Traces D8 flow path downstream cell by cell with high performance vectorized coordinate conversion.
    Stops immediately if path reaches min_lat (southernmost basin boundary).
    When `river_mask` is provided (OSM waterway footprint), the trace also stops — with
    stop_data=RIVER_STOP — as soon as it steps onto a river cell AFTER the first cell,
    so overland runoff merges into the nearest river instead of crossing it.
    Round 6 (Phase A2): when `water_poly_mask` is provided (OSM water polygons), the
    trace stops — with stop_data=WATER_POLY_STOP — on the FIRST open-water cell whose
    polygon id differs from `start_poly_id`. A trace that STARTS inside a water body
    (e.g. a gauge on a reservoir) keeps tracing through its own polygon and only stops
    when it enters a different one, so reservoir-crossing teleports are impossible
    while downstream traces out of a reservoir still work.
    Returns (coordinates_list [[lon, lat], ...], stop_data, path_cells [(r, c), ...]).
    The cell list enables downstream reuse (e.g. upstream drainage-branch BFS).
    """
    nrows, ncols = fdir.shape
    curr_r, curr_c = start_r, start_c
    visited: Set[Tuple[int, int]] = set()
    stop_data = None
    path_rc = []

    def _poly_id_at(r: int, c: int) -> int:
        if water_poly_ids is not None and 0 <= r < nrows and 0 <= c < ncols:
            return int(water_poly_ids[r, c])
        return 0

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

        # River-aware stop: never let overland D8 cross an OSM river channel.
        # The starting cell is exempt (a station already on a river must trace away).
        if river_mask is not None and len(path_rc) > 1 and river_mask[curr_r, curr_c]:
            stop_data = RIVER_STOP
            break

        # Open-water stop (round 6): stop on the first water-polygon cell that is not
        # the polygon the trace started in (start cell exempt, own polygon exempt).
        if (water_poly_mask is not None and len(path_rc) > 1
                and water_poly_mask[curr_r, curr_c]
                and _poly_id_at(curr_r, curr_c) != start_poly_id):
            stop_data = WATER_POLY_STOP
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
        return [], stop_data, path_rc

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

    return coords, stop_data, path_rc


def extract_station_drainage_branches(
    branch_seeds: Dict[str, List[Tuple[int, int]]],
    fdir: np.ndarray,
    acc: np.ndarray,
    transform: Affine,
    crs: Any = None,
    min_branch_acc: int = 500,
    min_length_km: float = 1.0,
    max_cells_per_station: int = 400_000,
    southern_limit_lat: Optional[float] = None,
    max_branches_per_station: int = 30,
    river_mask: Optional[np.ndarray] = None,
    river_merge_max_cells: int = 50,
    water_poly_mask: Optional[np.ndarray] = None
) -> Tuple[List[Dict[str, Any]], bool]:
    """
    E3 (round 6 rewrite — FIRST-CLAIM ownership): upstream D8 channel branches
    (dendritic tributaries) draining into the rain stations' flow paths, one feature
    per branch tagged with the OWNING rain station (from_station_id).

    Round-5 bug this replaces: the per-station reverse BFS had NO boundary, so every
    station whose path touched a major channel claimed the ENTIRE upstream network of
    that channel; the geometry dedupe then kept whichever station was processed FIRST
    as the owner — one far-away station (1137134 on nan) ended up owning ~20 branches
    scattered over the whole basin with 60-70 stations in `shared_with`.

    Round-6 algorithm (one global pass, O(K) amortized):
      1. FIRST-CLAIM station ownership of every seed (path) cell — dict insertion
         order = station processing order; overlapping path cells resolve to the
         first station, but all such candidates are LOCAL to that cell anyway.
      2. ONE global reverse BFS upstream from all seeds claims the shared channel
         network once (acc >= min_branch_acc; cells inside open water polygons are
         never claimed — branches must not run across reservoirs).
      3. Channel heads (claimed cells with no in-branch upstream neighbour) walk D8
         DOWNSTREAM until they reach the first owned seed cell — that station owns
         the whole reach. This is the hydrologic first-contact semantics: a branch
         belongs to the station whose path the water actually reaches first, so a
         far-away downstream station can never steal upstream branches.
      4. Per-owner length filter / cap (longest first) + stable ids.

    Returns (features, truncated) where truncated=True means the cell guard fired
    (caller may retry with a higher min_branch_acc).
    """
    nrows, ncols = fdir.shape

    is_geographic = (crs is None) or getattr(crs, 'is_geographic', False) or (str(crs) == "EPSG:4326")
    transformer = None
    if not is_geographic and crs is not None:
        try:
            from pyproj import Transformer
            transformer = Transformer.from_crs(crs, "EPSG:4326", always_xy=True)
        except Exception:
            transformer = None

    def cells_to_coords(path_cells: List[Tuple[int, int]]) -> List[List[float]]:
        r_arr = np.array([p[0] for p in path_cells], dtype=np.float64)
        c_arr = np.array([p[1] for p in path_cells], dtype=np.float64)
        xs = transform[2] + (c_arr + 0.5) * transform[0] + (r_arr + 0.5) * transform[1]
        ys = transform[5] + (c_arr + 0.5) * transform[3] + (r_arr + 0.5) * transform[4]
        if transformer is not None:
            lons, lats = transformer.transform(xs, ys)
        else:
            lons, lats = xs, ys
        return [[round(float(lo), 6), round(float(la), 6)] for lo, la in zip(lons, lats)]

    # 1. first-claim ownership of seed cells
    station_of_cell: Dict[Tuple[int, int], str] = {}
    for r_id, seed_cells in branch_seeds.items():
        for cell in seed_cells:
            station_of_cell.setdefault(cell, r_id)
    if not station_of_cell:
        return [], False

    # 2. one global reverse BFS upstream from ALL seeds (shared network claimed once)
    visited = np.zeros((nrows, ncols), dtype=bool)
    branch_cells: List[Tuple[int, int]] = []
    branch_set: Set[Tuple[int, int]] = set()
    stack: List[Tuple[int, int]] = []
    for cell in station_of_cell.keys():
        if 0 <= cell[0] < nrows and 0 <= cell[1] < ncols and not visited[cell[0], cell[1]]:
            visited[cell[0], cell[1]] = True
            branch_cells.append(cell)
            branch_set.add(cell)
            stack.append(cell)

    any_truncated = False
    # Cell guard scaled to the number of participating stations (the BFS is global now)
    max_cells_total = max_cells_per_station * max(1, len(branch_seeds))
    while stack:
        r, c = stack.pop()
        for code, (dr, dc) in REVERSE_D8.items():
            nr, nc = r + dr, c + dc
            if (0 <= nr < nrows and 0 <= nc < ncols and not visited[nr, nc]
                    and acc[nr, nc] >= min_branch_acc and int(fdir[nr, nc]) == code):
                if water_poly_mask is not None and water_poly_mask[nr, nc]:
                    continue  # round 6: never claim channel cells inside open water
                # (nr, nc) drains into (r, c) -> genuine upstream channel cell
                visited[nr, nc] = True
                branch_cells.append((nr, nc))
                branch_set.add((nr, nc))
                stack.append((nr, nc))
        if len(branch_cells) > max_cells_total:
            any_truncated = True
            print(f"  [WARN] Drainage branches truncated at {max_cells_total:,} cells "
                  f"(global guard over {len(branch_seeds)} stations)")
            break

    # 3. in-branch in-degree -> channel heads (cells that no in-branch cell flows into).
    # Seed cells are never heads — branches are tributaries only.
    upstream_deg: Dict[Tuple[int, int], int] = {}
    for (r, c) in branch_cells:
        for code, (dr, dc) in REVERSE_D8.items():
            nr, nc = r + dr, c + dc
            if (nr, nc) in branch_set and int(fdir[nr, nc]) == code:
                # (nr, nc) drains into (r, c): increments the IN-degree of (r, c)
                upstream_deg[(r, c)] = upstream_deg.get((r, c), 0) + 1

    heads = [cell for cell in branch_cells
             if upstream_deg.get(cell, 0) == 0 and cell not in station_of_cell]

    # 4. walk each head downstream to the FIRST owned seed cell (memoized — cells
    # shared by many head walks resolve once, so the total work stays O(K))
    resolved: Dict[Tuple[int, int], Optional[str]] = {}
    per_owner: Dict[str, List[Tuple[float, Dict[str, Any]]]] = {}
    n_orphan = 0
    for (hr, hc) in heads:
        chain: List[Tuple[int, int]] = []
        river_ext = 0
        river_merged = False
        owner: Optional[str] = None
        curr = (hr, hc)
        while True:
            own = station_of_cell.get(curr)
            if own is not None:
                owner = own
                break
            if curr in resolved:
                owner = resolved[curr]
                break
            chain.append(curr)
            code = int(fdir[curr[0], curr[1]])
            if code not in D8_DELTAS:
                break
            dr, dc = D8_DELTAS[code]
            nxt = (curr[0] + dr, curr[1] + dc)
            if nxt in branch_set:
                river_ext = 0
                curr = nxt
                continue
            # River-merge extension: the claimed set can end a few cells short of the
            # OSM river (junction cell below --branch-min-acc). Keep walking while the
            # D8 step stays on river-mask cells so the branch visually meets the river.
            if (river_mask is not None
                    and 0 <= nxt[0] < nrows and 0 <= nxt[1] < ncols
                    and river_mask[nxt[0], nxt[1]]
                    and river_ext < river_merge_max_cells):
                river_ext += 1
                river_merged = True
                curr = nxt
                continue
            break

        for cell in chain:
            resolved[cell] = owner
        if owner is None:
            n_orphan += 1
            continue
        # the terminating (owned seed) cell closes the reach for geometric continuity
        if chain and station_of_cell.get(curr) is not None:
            chain.append(curr)
        if len(chain) < 2:
            continue

        coords = cells_to_coords(chain)
        if southern_limit_lat is not None:
            filtered = []
            for pt in coords:
                if pt[1] >= southern_limit_lat:
                    filtered.append(pt)
                else:
                    filtered.append([pt[0], round(southern_limit_lat, 6)])
                    break
            coords = filtered
        if len(coords) < 2:
            continue

        length_km = linestring_length_km(coords)
        if length_km < min_length_km:
            continue

        per_owner.setdefault(owner, []).append((length_km, {
            "type": "Feature",
            "properties": {
                "feature_type": "rainfall_drainage_branch",
                "from_station_id": owner,
                "branch_length_km": round(length_km, 2),
                "flow_acc_cells": int(acc[hr, hc]),
                "branch_cells": len(chain),
                "river_merge": river_merged
            },
            "geometry": {
                "type": "LineString",
                "coordinates": simplify_linestring_coords(coords, tolerance_deg=0.00035,
                                                         label=f"branch_{owner}")
            }
        }))

    if n_orphan:
        print(f"  [BRANCH] {n_orphan:,} channel reach(es) dropped: their downstream walk "
              f"never reached any station path (no owner — first-claim semantics)")

    # 5. assemble with per-owner cap (longest kept) + stable ids
    features: List[Dict[str, Any]] = []
    for r_id, lst in per_owner.items():
        if max_branches_per_station and len(lst) > max_branches_per_station:
            lst.sort(key=lambda x: -x[0])
            lst = lst[:max_branches_per_station]
        features.extend(fd for _, fd in lst)

    id_counters: Dict[str, int] = {}
    for feat in features:
        r_id = feat["properties"]["from_station_id"]
        id_counters[r_id] = id_counters.get(r_id, 0) + 1
        feat["properties"]["branch_index"] = id_counters[r_id]
        feat["id"] = f"branch_{r_id}_{id_counters[r_id]:03d}"

    return features, any_truncated


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


def build_water_body_transits(
    river_graph: "DirectedRiverGraph",
    water_polygons_geojson: Optional[Dict[str, Any]],
    transform: Affine,
    out_shape: Tuple[int, int],
    acc: np.ndarray,
    crs: Any = None,
    min_area_cells: int = 50,
    max_transit_edges_per_poly: int = 50
) -> Tuple[Dict[int, Dict[str, Any]], Optional[np.ndarray], Dict[str, Any]]:
    """
    Round 6 (Phase A2/A3 — generic for every basin, nothing hardcoded):

    Open water bodies (OSM water polygons: reservoirs / lakes / wide rivers) break the
    OSM backbone: river centerlines usually STOP at the shoreline (mappers do not draw
    centerlines across lakes), so the graph splits into islands and D8 used to teleport
    straight across the water. For every polygon this helper:

      1. finds the OUTLET cell = highest flow-accumulation cell inside the polygon
         (the dam / spill point of a reservoir, the downstream exit of a wide river);
      2. snaps it onto the backbone (splitting edges at the projection);
      3. connects every backbone component that has nodes INSIDE the polygon but is
         disconnected from the outlet with a transit edge (closest node -> outlet,
         direction checked by elevation). When OSM DOES map a centerline through the
         water, its nodes already share the outlet's component and NO transit edge is
         added, so the real geometry always wins over the straight fallback.

    Returns (transits, poly_ids, stats):
      transits[poly_index] = {outlet_node, outlet_lonlat, osm_id, name, mode}
      poly_ids             = uint16 raster of polygon indices (+1) from the same
                             rasterization used here (shared with the D8 water stop)
    """
    from scripts.modules.terrain_engine import build_water_polygon_mask

    empty_stats = {"polygons": 0, "with_outlet": 0, "transit_edges": 0, "skipped_small": 0}
    try:
        mask, poly_ids = build_water_polygon_mask(water_polygons_geojson, transform, out_shape, crs=crs)
    except Exception as ex:
        print(f"  [WARN] build_water_body_transits: mask failed ({ex})")
        return {}, None, empty_stats
    if poly_ids is None:
        return {}, None, empty_stats

    n_poly = int(poly_ids.max())
    feats = [f for f in (water_polygons_geojson or {}).get("features", [])
             if (f.get("geometry") or {}).get("type") in ("Polygon", "MultiPolygon")]
    # NOTE: poly_ids values map to the ORDER of successfully rasterized features;
    # build_water_polygon_mask rasterizes the same filter (Polygon/MultiPolygon), so
    # feature i in `feats` corresponds to poly id i+1 when the counts match.
    if len(feats) != n_poly:
        # fall back to a per-feature correspondence only when counts agree; otherwise
        # keep the mask for stopping but skip per-poly outlet refinement.
        feats = feats[:n_poly]

    nrows, ncols = out_shape
    is_geographic = (crs is None) or getattr(crs, 'is_geographic', False) or (str(crs) == "EPSG:4326")
    # TWO distinct transformers for projected rasters (round 6 hotfix): raster
    # coordinates -> lon/lat for the outlet cell, and lon/lat -> raster coordinates
    # for the node mapping. Using one transformer for both directions fed projected
    # metres into pyproj as degrees and produced inf (OverflowError in the snap).
    to_lonlat = None    # raster crs -> EPSG:4326
    to_raster = None    # EPSG:4326 -> raster crs
    if not is_geographic and crs is not None:
        try:
            from pyproj import Transformer
            to_lonlat = Transformer.from_crs(crs, "EPSG:4326", always_xy=True)
            to_raster = Transformer.from_crs("EPSG:4326", crs, always_xy=True)
        except Exception:
            to_lonlat = None
            to_raster = None

    def lonlat_to_rc(lons, lats):
        if to_raster is not None:
            lons, lats = to_raster.transform(lons, lats)
        col_f, row_f = (~transform) * (lons, lats)
        return np.floor(row_f).astype(np.int64), np.floor(col_f).astype(np.int64)

    def cell_to_lonlat(col, row):
        x = transform[2] + (col + 0.5) * transform[0] + (row + 0.5) * transform[1]
        y = transform[5] + (col + 0.5) * transform[3] + (row + 0.5) * transform[4]
        if to_lonlat is not None:
            x, y = to_lonlat.transform(x, y)
        return float(x), float(y)

    # vectorized node -> cell mapping (one pass over all nodes). Interior detection
    # uses a 3x3-max-filtered id raster so shoreline nodes that rasterize 1 cell
    # outside the polygon (center-based rasterization) still count as interior.
    node_ids = list(river_graph.nodes.keys())
    if not node_ids:
        return {}, poly_ids, empty_stats
    n_lon = np.array([river_graph.nodes[n][0] for n in node_ids], dtype=np.float64)
    n_lat = np.array([river_graph.nodes[n][1] for n in node_ids], dtype=np.float64)
    n_row, n_col = lonlat_to_rc(n_lon, n_lat)
    n_in = (n_row >= 0) & (n_row < nrows) & (n_col >= 0) & (n_col < ncols)
    node_poly = np.zeros(len(node_ids), dtype=np.int64)
    if n_in.any():
        try:
            from scipy.ndimage import maximum_filter
            poly_ids_dil = maximum_filter(poly_ids, size=3)
        except Exception:
            poly_ids_dil = poly_ids
        node_poly[n_in] = poly_ids_dil[n_row[n_in], n_col[n_in]].astype(np.int64)
    node_poly_by_id = dict(zip(node_ids, node_poly.tolist()))

    # undirected component root per node (union-find over adj)
    parent: Dict[int, int] = {}

    def find(x: int) -> int:
        # Tolerant lookup: snap_point_to_graph_ranked may SPLIT an edge and create
        # brand-new node ids after this map was built — unseen nodes root themselves
        # and are unioned with their edge neighbours by the caller.
        parent.setdefault(x, x)
        r = x
        while parent[r] != r:
            r = parent[r]
        while parent[x] != r:
            parent[x], x = r, parent[x]
        return r

    for nid in river_graph.nodes:
        parent[nid] = nid
    for a, outs in river_graph.adj.items():
        for (b, _d) in outs:
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[ra] = rb

    def union_new_node(nid: int) -> None:
        """Places a freshly created (split) node into its way's component."""
        parent.setdefault(nid, nid)
        for (nb, _d) in river_graph.adj.get(nid, ()):
            parent.setdefault(nb, nb)
            ra, rb = find(nid), find(nb)
            if ra != rb:
                parent[ra] = rb

    transits: Dict[int, Dict[str, Any]] = {}
    stats = dict(empty_stats)
    stats["polygons"] = n_poly

    for i in range(n_poly):
        pid = i + 1
        rows_i, cols_i = np.nonzero(poly_ids == pid)
        if rows_i.size < min_area_cells:
            stats["skipped_small"] += 1
            continue
        r0, r1 = int(rows_i.min()), int(rows_i.max()) + 1
        c0, c1 = int(cols_i.min()), int(cols_i.max()) + 1
        sub_ids = poly_ids[r0:r1, c0:c1]
        sub_acc = np.where(sub_ids == pid, acc[r0:r1, c0:c1], np.int32(-1))
        flat_idx = int(np.argmax(sub_acc))
        if int(sub_acc.flat[flat_idx]) <= 0:
            continue
        orow = r0 + flat_idx // sub_acc.shape[1]
        ocol = c0 + flat_idx % sub_acc.shape[1]
        # outlet cell -> lon/lat (cell center)
        o_lon, o_lat = cell_to_lonlat(ocol, orow)
        if not (math.isfinite(o_lon) and math.isfinite(o_lat)):
            continue  # CRS conversion failed for this polygon — skip its transit

        outlet_node, _d, _m = river_graph.snap_point_to_graph_ranked(o_lon, o_lat, max_dist_deg=0.01)
        if outlet_node is None:
            outlet_node, _d = river_graph.snap_point_to_graph(o_lon, o_lat, max_dist_deg=0.01)
        if outlet_node is None:
            continue
        # the snap may have split an edge and created a new node — adopt it into
        # its way's component (a split node connects a-x-b on the same way)
        union_new_node(outlet_node)
        stats["with_outlet"] += 1

        props = {}
        if i < len(feats):
            props = feats[i].get("properties", {}) or {}
        outlet_comp = find(outlet_node)
        interior = [nid for nid, pv in node_poly_by_id.items() if pv == pid]
        comp_nodes: Dict[int, int] = {}
        for nid in interior:
            comp = find(nid)
            if comp == outlet_comp:
                continue
            # keep the node of each foreign component closest to the outlet
            p = river_graph.nodes[nid]
            d = (p[0] - o_lon) ** 2 + (p[1] - o_lat) ** 2
            if comp not in comp_nodes or d < comp_nodes[comp]:
                comp_nodes[comp] = nid

        n_edges = 0
        for comp, nid in comp_nodes.items():
            if n_edges >= max_transit_edges_per_poly:
                break
            z_n = river_graph.nodes[nid][2]
            z_o = river_graph.nodes[outlet_node][2]
            if z_n is not None and z_o is not None and z_n < z_o - 0.5:
                continue  # that fragment sits BELOW the outlet — never route uphill
            seg = [[river_graph.nodes[nid][0], river_graph.nodes[nid][1]], [o_lon, o_lat]]
            edge = {
                "coords": seg,
                "length_km": max(0.001, linestring_length_km(seg)),
                "z_start": z_n, "z_end": z_o,
                "dz": max(0.0, (z_n - z_o) if (z_n is not None and z_o is not None) else 0.0),
                "river_name": str(props.get("name", "") or ""),
                "direction_source": "reservoir_transit",
                "way": -1,
                "reservoir_transit": {
                    "poly_index": i,
                    "osm_id": str(props.get("osm_id", "") or ""),
                    "name": str(props.get("name", "") or props.get("name_th", "") or ""),
                    "mode": "straight"
                }
            }
            river_graph.adj.setdefault(nid, []).append((outlet_node, edge))
            river_graph._index_edge(nid, outlet_node)
            parent[nid] = outlet_comp
            n_edges += 1
        stats["transit_edges"] += n_edges

        transits[i] = {
            "outlet_node": outlet_node,
            "outlet_lonlat": [round(o_lon, 6), round(o_lat, 6)],
            "osm_id": str(props.get("osm_id", "") or ""),
            "name": str(props.get("name", "") or props.get("name_th", "") or ""),
            "mode": "transit_edges" if n_edges else "backbone"
        }

    print(f"  [TRANSIT] water bodies: {stats['polygons']} rasterized, "
          f"{stats['with_outlet']} with backbone outlet, "
          f"{stats['transit_edges']} transit edges added "
          f"(small skipped: {stats['skipped_small']})")
    return transits, poly_ids, stats


def build_flow_paths_and_relations(
    water_stations: List[Dict[str, Any]],
    rain_stations: List[Dict[str, Any]],
    fdir: np.ndarray,
    acc: np.ndarray,
    filled_dem: np.ndarray,
    transform: Affine,
    osm_waterways_geojson: Optional[Dict[str, Any]] = None,
    crs: Any = None,
    min_flow_km: float = 1.0,
    cascade_max_km: float = 60.0,
    branch_min_acc: int = 500,
    include_branches: bool = True,
    include_osm_layer: bool = True,
    branch_max_cells: int = 400_000,
    branch_max_count: int = 30,
    branch_min_km: float = 1.0,
    river_mask: Optional[np.ndarray] = None,
    water_polygons_geojson: Optional[Dict[str, Any]] = None,
    overland_max_km: float = 5.0,
    basin_boundary_geojson: Optional[Dict[str, Any]] = None,
    clip_to_basin: bool = True
) -> Tuple[Dict[str, Any], List[Dict[str, Any]], List[Dict[str, Any]]]:
    """
    2-Layer Hybrid Flow Path & River Topology Generator:
    - Layer 1: Gauge-to-Gauge River Backbone Flowpaths (12.5m Hydro-D8 routing,
      with OSM-backbone continuation fallback when D8 terminates early)
    - Layer 2: Rainfall-to-Gauge Overland Flowpaths (Hillslope D8 -> River Backbone),
      hydrologically-correct downstream CASCADE: one non-overlapping segment feature per
      receiving gauge along the flow (entry->G1, G1->G2, ...), relations for every gauge passed
    - Drainage Branches: per-rain-station upstream D8 channel network (dendritic tributaries)
    - OSM River Layer: the raw OSM waterway network as its own display layer
      (feature_type="osm_river"), separable in the frontend like the branches
    - Water-body handling (round 6): D8 never traces across OSM water polygons;
      open water is crossed via OSM centerlines / reservoir-transit edges instead.
    - Southern Limit: Strictly bounded to southernmost water station + 5 km.

    Returns:
    1. flow_paths_geojson (LineString vector features for Frontend Map)
    2. station_relations (Gauge -> Downstream Gauge)
    3. rainfall_relations (Rain Gauge -> Receiving Water Gauge, one per gauge passed)
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

    # Optimized elevation sampling with memoization cache (capped to bound RAM)
    inv_trans = ~transform
    elev_cache: Dict[Tuple[float, float], float] = {}
    ELEV_CACHE_MAX = 500_000

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
        if len(elev_cache) < ELEV_CACHE_MAX:
            elev_cache[r_key] = res
        return res

    # Batched elevation sampling for OSM vertices: one vectorized affine (+pyproj) call per
    # feature instead of per-vertex calls. Vertices outside the DEM / nodata -> None (unknown).
    def batch_sample_elevations(coords_list: List[List[float]]) -> List[Optional[float]]:
        n = len(coords_list)
        lons = np.fromiter((c[0] for c in coords_list), dtype=np.float64, count=n)
        lats = np.fromiter((c[1] for c in coords_list), dtype=np.float64, count=n)
        if inv_transformer is not None:
            lons, lats = inv_transformer.transform(lons, lats)
        col_f, row_f = inv_trans * (lons, lats)
        rows = np.floor(row_f).astype(np.int64)
        cols = np.floor(col_f).astype(np.int64)
        valid = (rows >= 0) & (rows < nrows) & (cols >= 0) & (cols < ncols)
        out: List[Optional[float]] = [None] * n
        if valid.any():
            vals = filled_dem[rows[valid], cols[valid]]
            ok = ~np.isnan(vals) & (vals != -9999.0)
            idxs = np.nonzero(valid)[0][ok]
            for i, v in zip(idxs.tolist(), vals[ok].tolist()):
                out[i] = float(v)
        return out

    # 1. Construct Directed River Backbone Graph from OSM with Spatial Grid Indexing
    river_graph = DirectedRiverGraph(snap_tolerance_deg=0.00035)
    if osm_waterways_geojson and osm_waterways_geojson.get("features"):
        for feat in osm_waterways_geojson.get("features", []):
            geom = feat.get("geometry")
            if not geom:
                continue
            p_name = feat.get("properties", {}).get("name", "")
            p_class = feat.get("properties", {}).get("waterway", "stream") or "stream"
            p_osm_id = str(feat.get("properties", {}).get("osm_id", ""))
            if geom.get("type") == "LineString":
                line_coords_list = [geom.get("coordinates", [])]
            elif geom.get("type") == "MultiLineString":
                line_coords_list = geom.get("coordinates", [])
            else:
                continue
            for coords in line_coords_list:
                if len(coords) >= 2:
                    river_graph.add_river_segment(
                        coords, sample_elev_fn=None, river_name=p_name,
                        elevs=batch_sample_elevations(coords),
                        waterway_class=p_class, osm_id=p_osm_id
                    )
    # Noding + endpoint welding: heal the fragmented OSM way graph (crossing ways
    # without shared vertices, and way ends that stop tens of meters apart) so the
    # backbone forms one routable network instead of thousands of islands.
    river_graph.finalize_connectivity()
    river_graph.build_spatial_index()

    # 2. Map Water Stations onto Grid and Graph Nodes
    water_grid_map = {}
    water_prox_map = {}
    water_node_map = {}
    for st in water_stations:
        st_id = str(st.get('station_id', '')).strip()
        if not st_id:
            continue
        r, c = st.get('grid_row'), st.get('grid_col')
        st_lat, st_lon = float(st.get('latitude', 0.0)), float(st.get('longitude', 0.0))

        if r is not None and c is not None:
            # Generous 11x11 capture footprint (~135m) around each station so flowing stream D8 channel always hits it
            # setdefault: the first station claims overlapping cells (no silent overwrite)
            for dr in (-5, -4, -3, -2, -1, 0, 1, 2, 3, 4, 5):
                for dc in (-5, -4, -3, -2, -1, 0, 1, 2, 3, 4, 5):
                    nr, nc = r + dr, c + dc
                    if 0 <= nr < nrows and 0 <= nc < ncols:
                        water_grid_map.setdefault((nr, nc), st_id)

        # A3: proximity fallback map (±12 cells ≈ 150m around the station coordinate).
        # Catches D8 channels that pass just outside the acc-refined footprint so the
        # traced path always terminates at the gauge.
        try:
            pr, pc = rowcol(transform, st_lon, st_lat)
            pr, pc = int(pr), int(pc)
            PROX_R = 12
            for dr in range(-PROX_R, PROX_R + 1):
                for dc in range(-PROX_R, PROX_R + 1):
                    nr, nc = pr + dr, pc + dc
                    if 0 <= nr < nrows and 0 <= nc < ncols:
                        water_prox_map.setdefault((nr, nc), st_id)
        except Exception:
            pass

        # Anchor the gauge onto the graph by projecting it onto the nearest EDGE
        # (splitting the edge if the projection is mid-segment) — not just the nearest
        # vertex, which can sit on a different river in confluence areas.
        nid, d_nid = river_graph.snap_point_to_graph(st_lon, st_lat, max_dist_deg=0.015)
        if nid:
            water_node_map[st_id] = nid

    # 2b. Water-body transits (round 6, Phase A2/A3): outlet nodes + transit edges
    # for OSM water polygons, then the open-water D8 stop mask shared by all traces.
    # Must run BEFORE compute_gauge_reachability so ranking sees transit edges.
    water_transits, water_poly_ids, _transit_stats = build_water_body_transits(
        river_graph, water_polygons_geojson, transform, (nrows, ncols), acc, crs=crs
    )
    water_poly_mask = (water_poly_ids > 0) if water_poly_ids is not None else None

    def poly_id_at_cell(cell: Tuple[int, int]) -> int:
        if water_poly_ids is None:
            return 0
        r, c = cell
        if 0 <= r < nrows and 0 <= c < ncols:
            return int(water_poly_ids[r, c])
        return 0

    features = []
    gauge_relations = []
    rainfall_relations = []

    # Downstream target nodes for backbone routing (shared by Layer 1 fallback and Layer 2).
    # SSSP results are cached per entry node with a cap to bound memory.
    target_water_nodes = set(water_node_map.values())
    # Phase 4.12: precompute gauge-downstream reachability once per graph —
    # attach candidate ranking uses this to prefer ways that actually flow to a gauge
    # over topology islands (O(E) reverse BFS, amortized over every attach query).
    river_graph.compute_gauge_reachability(target_water_nodes)
    dijkstra_cache: Dict[int, Tuple[Dict[int, float], Dict[int, Tuple[int, Dict[str, Any]]]]] = {}
    DIJKSTRA_CACHE_MAX = 256

    def get_sssp(entry_node: int) -> Tuple[Dict[int, float], Dict[int, Tuple[int, Dict[str, Any]]]]:
        if entry_node not in dijkstra_cache:
            if len(dijkstra_cache) >= DIJKSTRA_CACHE_MAX:
                dijkstra_cache.clear()
            dijkstra_cache[entry_node] = river_graph.dijkstra_single_source(
                entry_node, target_water_nodes, max_dist_km=cascade_max_km
            )
        return dijkstra_cache[entry_node]

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
                # Footprint (acc-refined) takes priority, then coordinate-proximity fallback
                target_id = water_grid_map.get((r, c)) or water_prox_map.get((r, c))
                if target_id and target_id != origin_id:
                    return True, target_id
                return False, None
            return stop_fn

        # Identify downstream candidate via D8 downhill step
        code = int(fdir[start_r, start_c])
        first_r, first_c = (start_r + D8_DELTAS[code][0], start_c + D8_DELTAS[code][1]) if code in D8_DELTAS else (start_r, start_c)
        # Round 6: a gauge sitting IN open water keeps tracing through its own polygon
        # (start_poly_id) and only stops when it enters a DIFFERENT water body.
        start_poly = poly_id_at_cell((start_r, start_c))
        raster_coords, target_station_id, _ = trace_downstream_path(
            first_r, first_c, fdir, transform, crs=crs,
            stop_condition_fn=make_stop_fn(st_id),
            min_lat=southern_limit_lat,
            max_steps=5000,
            water_poly_mask=water_poly_mask,
            water_poly_ids=water_poly_ids,
            start_poly_id=start_poly
        )
        # stop sentinels (RIVER_STOP / WATER_POLY_STOP) are NOT station ids
        if target_station_id in (RIVER_STOP, WATER_POLY_STOP):
            target_station_id = None

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
                    "coordinates": simplify_linestring_coords(coords, tolerance_deg=0.00035, label=feature_id)
                }
            }
            features.append(feature)
            gauge_relations.append(feature["properties"])
        elif raster_coords and len(raster_coords) >= 2:
            coords = merge_coordinates([[st_lon, st_lat]], raster_coords)
            dist_km = linestring_length_km(coords)

            # A4: D8 ended without reaching a gauge (pit / nodata / reservoir / step limit).
            # Continue along the OSM river backbone toward the nearest downstream gauge so the
            # path does not silently terminate mid-river.
            backbone_target = None
            backbone_coords = None
            backbone_dist_km = 0.0
            end_lon, end_lat = raster_coords[-1]
            z_end = sample_elevation(end_lon, end_lat)

            # Attach the D8 end onto the backbone by projecting onto the best-scoring
            # EDGE (Phase 4 candidate ranking: gauge-connectivity > class > length >
            # distance — never "nearest island wins"). If the very end misses the
            # network (pit / reservoir / nodata), scan the path backwards for the last
            # place it ran alongside a river reach and attach there — never leave the
            # gauge hanging with no continuation.
            nid, d_nid, attach_meta = river_graph.snap_point_to_graph_ranked(end_lon, end_lat, max_dist_deg=0.003)
            if nid is None:
                scan_from = max(0, len(raster_coords) - 1000)
                for p_idx in range(len(raster_coords) - 2, scan_from - 1, -4):
                    pt = raster_coords[p_idx]
                    cand_nid, cand_d, cand_meta = river_graph.snap_point_to_graph_ranked(pt[0], pt[1], max_dist_deg=0.003)
                    if cand_nid is None:
                        continue
                    z_here = sample_elevation(pt[0], pt[1])
                    z_cand = river_graph.nodes.get(cand_nid, (0.0, 0.0, None))[2]
                    # The attachment node must not sit clearly ABOVE the path point
                    # (that would be an upstream reach of a different river).
                    if (z_cand is not None and z_here is not None
                            and not math.isnan(z_here) and z_cand > z_here + 2.0):
                        continue
                    nid, d_nid, attach_meta = cand_nid, cand_d, cand_meta
                    end_lon, end_lat = pt
                    z_end = sample_elevation(end_lon, end_lat)
                    break
            # Round 6 (Phase A2): D8 ended on an open-water boundary (reservoir /
            # lake) with no attachable backbone nearby — attach the water body's
            # OUTLET node via the reservoir transit instead of leaving the gauge
            # hanging (the outlet was precomputed by build_water_body_transits).
            transit_meta = None
            if nid is None:
                end_poly = poly_id_at_cell((start_r, start_c))  # fallback below uses trace end
                end_cell = raster_coords[-1] if raster_coords else None
                if end_cell is not None:
                    r_end, c_end = rowcol(transform, end_cell[0], end_cell[1])
                    end_poly = poly_id_at_cell((int(r_end), int(c_end))) if water_poly_ids is not None else 0
                tr = water_transits.get(end_poly - 1) if end_poly else None
                if tr and tr.get("outlet_node") is not None:
                    onode = tr["outlet_node"]
                    o_lon, o_lat = tr["outlet_lonlat"]
                    seg = [[end_lon, end_lat], [o_lon, o_lat]]
                    if linestring_length_km(seg) <= 80.0:
                        nid = onode
                        end_lon, end_lat = o_lon, o_lat
                        z_end = sample_elevation(end_lon, end_lat)
                        transit_meta = {
                            "attach_osm_id": tr.get("osm_id", ""),
                            "attach_class": "reservoir_transit",
                            "attach_quality": "ok",
                            "reservoir_transit": True,
                            "transit_mode": "straight"
                        }
            if nid is not None:
                dist_map, prev_map = get_sssp(nid)
                candidates = []
                for wst in water_stations:
                    w_id = str(wst.get('station_id', '')).strip()
                    v_node = water_node_map.get(w_id)
                    if not v_node or v_node == nid or v_node not in dist_map:
                        continue
                    if dist_map[v_node] < min_flow_km:
                        continue
                    w_lat = float(wst.get('latitude', 0.0))
                    if southern_limit_lat is not None and w_lat < southern_limit_lat:
                        continue
                    w_lon = float(wst.get('longitude', 0.0))
                    if sample_elevation(w_lon, w_lat) <= z_end + 2.0:
                        candidates.append((dist_map[v_node], w_id, wst))
                candidates.sort(key=lambda x: x[0])
                if candidates:
                    backbone_dist_km, w_id, target_st = candidates[0]
                    v_node = water_node_map.get(w_id)
                    chain = river_graph.reconstruct_node_path(prev_map, nid, v_node) if v_node else None
                    if chain:
                        backbone_coords = river_graph.stitch_coords_from_prev(prev_map, chain, 0, len(chain) - 1)
                        if backbone_coords and len(backbone_coords) >= 2:
                            backbone_target = target_st

            if backbone_target is not None and backbone_coords:
                if transit_meta:
                    # draw the straight reservoir-transit hop between the D8 end
                    # (shoreline) and the outlet node on the backbone
                    coords = merge_coordinates(coords, [[end_lon, end_lat]])
                coords = merge_coordinates(coords, backbone_coords)
                dist_km = linestring_length_km(coords)
                tgt_lon = float(backbone_target.get('longitude', 0.0))
                tgt_lat = float(backbone_target.get('latitude', 0.0))

                # Snap the final vertex to the receiving gauge (stub <= ~1.5km is acceptable)
                last_dist = math.hypot(coords[-1][0] - tgt_lon, coords[-1][1] - tgt_lat)
                if last_dist <= 0.015:
                    coords.append([round(tgt_lon, 6), round(tgt_lat, 6)])

                z_up = sample_elevation(st_lon, st_lat)
                z_down = sample_elevation(tgt_lon, tgt_lat)
                dz = max(0.0, z_up - z_down)
                slope = (dz / (dist_km * 1000.0)) if dist_km > 0.001 else 0.0001

                feature_id = f"flow_gauge_{st_id}_to_{backbone_target.get('station_id')}"
                backbone_props = {
                    "feature_type": "gauge_to_gauge_flowpath",
                    "routing": "d8_plus_osm_backbone",
                    "from_station_id": st_id,
                    "from_station_name": st.get('station_name', ''),
                    "to_station_id": str(backbone_target.get('station_id', '')),
                    "to_station_name": backbone_target.get('station_name', ''),
                    "distance_km": round(dist_km, 2),
                    "backbone_distance_km": round(backbone_dist_km, 2),
                    "river_slope": round(slope, 6),
                    "elevation_diff_m": round(dz, 2),
                    "upstream_elev_m": round(z_up, 2),
                    "downstream_elev_m": round(z_down, 2),
                }
                if transit_meta:
                    backbone_props.update(transit_meta)
                if attach_meta:
                    backbone_props.update(attach_meta)
                feature = {
                    "type": "Feature",
                    "id": feature_id,
                    "properties": backbone_props,
                    "geometry": {
                        "type": "LineString",
                        "coordinates": simplify_linestring_coords(coords, tolerance_deg=0.00035, label=feature_id)
                    }
                }
                features.append(feature)
                gauge_relations.append(feature["properties"])
            elif dist_km >= min_flow_km:
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
                        "coordinates": simplify_linestring_coords(coords, tolerance_deg=0.00035, label=feature_id)
                    }
                }
                features.append(feature)

    # =========================================================================
    # LAYER 2: Rain-to-Gauge Overland Connectors (Overland -> River Backbone)
    # =========================================================================
    # Stop condition: stops when encountering any water level station on the 12.5m grid
    def stop_at_water_station(curr_r, curr_c):
        t_id = water_grid_map.get((curr_r, curr_c)) or water_prox_map.get((curr_r, curr_c))
        if t_id:
            return True, t_id
        return False, None

    def stop_at_water_station_excluding(exclude_ids):
        def stop_fn(curr_r, curr_c):
            t_id = water_grid_map.get((curr_r, curr_c)) or water_prox_map.get((curr_r, curr_c))
            if t_id and t_id not in exclude_ids:
                return True, t_id
            return False, None
        return stop_fn

    # E2: overland path cells per rain station, feeding the drainage-branch extraction
    branch_seed_cells: Dict[str, List[Tuple[int, int]]] = {}

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

        z_rain = sample_elevation(lon, lat)
        seg_count = 0  # cascade segment counter (Case 2 increments per emitted segment)

        # 1. Trace Continuous 12.5m DEM D8 flow path downstream from rain station.
        # River-aware ("river first"): the trace stops (stop_data=RIVER_STOP) at the
        # FIRST OSM river footprint it reaches, so runoff merges into the adjacent
        # river instead of crossing it or running cross-country to a distant gauge.
        #
        # Dead-end fallback: if the contacted river is a topology island (isolated OSM
        # fragment with NO reachable downstream gauge — common where OSM ways stop
        # kilometers apart), the water physically keeps flowing: re-trace on pure
        # terrain (no river stop) so gauge relations are not lost to mapping gaps.
        overland_coords, stop_data, overland_cells = [], None, []
        river_stopped = False
        poly_stopped = False
        direct_target_water_id = None
        entry_idx = None
        entry_node = None
        downstream_targets: List[Tuple[float, str, Dict[str, Any]]] = []
        prev_map = None
        # Rain station's own polygon (a station on a reservoir traces out of it first)
        rain_start_poly = poly_id_at_cell((r, c))
        for _attempt in range(2):
            eff_river_mask = river_mask if _attempt == 0 else None
            # Round 6: the open-water stop stays active on BOTH attempts — runoff must
            # never teleport across a reservoir even when the river contact was a
            # topology island (the transit / overland fallback handles those instead).
            overland_coords, stop_data, overland_cells = trace_downstream_path(
                r, c, fdir, transform, crs=crs,
                stop_condition_fn=stop_at_water_station,
                min_lat=southern_limit_lat,
                max_steps=5000,
                river_mask=eff_river_mask,
                water_poly_mask=water_poly_mask,
                water_poly_ids=water_poly_ids,
                start_poly_id=rain_start_poly
            )
            river_stopped = (stop_data == RIVER_STOP)
            poly_stopped = (stop_data == WATER_POLY_STOP)
            direct_target_water_id = stop_data if (stop_data and stop_data not in (RIVER_STOP, WATER_POLY_STOP)) else None
            if direct_target_water_id:
                break  # Case 1: gauge hit before any river contact

            # Case 2 scan: find the river backbone contact (< 300m).
            # When the trace stopped on a river cell, scan BACKWARDS from the end —
            # the river contact is the path terminus. Otherwise coarse STRIDE pass
            # first, then refine backwards to the exact closest approach.
            # snap_point_to_graph projects onto edges (splitting them), so rivers
            # whose nearest vertex is far away are still found reliably.
            entry_idx = None
            entry_node = None
            entry_meta = None
            if len(overland_coords) >= 2:
                MAX_ENTRY_DIST = 0.003  # tight 300m
                if river_stopped or poly_stopped:
                    for p_idx in range(len(overland_coords) - 1, -1, -1):
                        pt = overland_coords[p_idx]
                        nid, d_nid, m = river_graph.snap_point_to_graph_ranked(pt[0], pt[1], max_dist_deg=MAX_ENTRY_DIST)
                        if nid is not None:
                            entry_idx, entry_node, entry_meta = p_idx, nid, m
                            break
                    if entry_node is None:
                        pt = overland_coords[-1]
                        nid, d_nid, m = river_graph.snap_point_to_graph_ranked(pt[0], pt[1], max_dist_deg=0.006)
                        if nid is not None:
                            entry_idx, entry_node, entry_meta = len(overland_coords) - 1, nid, m
                else:
                    STRIDE = 8
                    coarse_idx = None
                    coarse_d = MAX_ENTRY_DIST
                    coarse_m = None
                    for p_idx in range(0, len(overland_coords), STRIDE):
                        pt = overland_coords[p_idx]
                        nid, d_nid, m = river_graph.snap_point_to_graph_ranked(pt[0], pt[1], max_dist_deg=MAX_ENTRY_DIST)
                        if nid is not None and d_nid <= MAX_ENTRY_DIST:
                            coarse_idx = p_idx
                            entry_node = nid
                            coarse_d = d_nid
                            coarse_m = m
                            break
                    if coarse_idx is not None:
                        # The coarse pass can be up to STRIDE cells past the true river approach;
                        # refine backwards to the index nearest the backbone.
                        best_idx, best_node, best_d, best_m = coarse_idx, entry_node, coarse_d, coarse_m
                        for p_idx in range(coarse_idx - 1, max(0, coarse_idx - STRIDE + 1) - 1, -1):
                            pt = overland_coords[p_idx]
                            nid2, d2, m2 = river_graph.snap_point_to_graph_ranked(pt[0], pt[1], max_dist_deg=MAX_ENTRY_DIST)
                            if nid2 is not None and d2 <= MAX_ENTRY_DIST and d2 < best_d:
                                best_idx, best_node, best_d, best_m = p_idx, nid2, d2, m2
                        entry_idx, entry_node, entry_meta = best_idx, best_node, best_m

            # Round 6 (Phase A2): the trace stopped on an open-water boundary but no
            # backbone centerline is reachable there (mappers rarely draw rivers across
            # reservoirs). Enter the backbone at the water body's OUTLET node through
            # the precomputed reservoir transit instead of losing the gauge chain.
            if entry_node is None and poly_stopped and overland_coords:
                end_poly = poly_id_at_cell(overland_cells[-1]) if overland_cells else 0
                tr = water_transits.get(end_poly - 1) if end_poly else None
                if tr and tr.get("outlet_node") is not None:
                    o_lon, o_lat = tr["outlet_lonlat"]
                    hop = math.hypot((o_lon - overland_coords[-1][0]) * 111.32 * 0.95,
                                     (o_lat - overland_coords[-1][1]) * 110.54)
                    if hop <= 80.0:
                        overland_coords = overland_coords + [[o_lon, o_lat]]
                        if overland_cells:
                            overland_cells = overland_cells + [overland_cells[-1]]
                        entry_idx = len(overland_coords) - 1
                        entry_node = tr["outlet_node"]
                        entry_meta = {
                            "attach_osm_id": tr.get("osm_id", ""),
                            "attach_class": "reservoir_transit",
                            "attach_quality": "ok",
                            "reservoir_transit": True,
                            "transit_mode": "straight"
                        }

            # D1: collect ALL downstream-receiving gauges (hydrological cascade), ordered
            # by channel distance from the entry node. Directed backbone edges guarantee
            # gauges on upstream tributaries are unreachable, so the set is hydrologically
            # correct.
            downstream_targets = []
            prev_map = None
            if entry_node is not None:
                dist_map, prev_map = get_sssp(entry_node)

                for wst in water_stations:
                    w_id = str(wst.get('station_id', '')).strip()
                    v_node = water_node_map.get(w_id)
                    if v_node and v_node != entry_node and v_node in dist_map:
                        b_dist = dist_map[v_node]
                        if b_dist < min_flow_km:
                            continue  # D2: gauge sits effectively at the entry point -> skip stub
                        w_lon, w_lat = float(wst['longitude']), float(wst['latitude'])
                        if southern_limit_lat is not None and w_lat < southern_limit_lat:
                            continue
                        w_elev = sample_elevation(w_lon, w_lat)
                        if w_elev <= z_rain + 2.0:
                            downstream_targets.append((b_dist, w_id, wst))

                downstream_targets.sort(key=lambda x: x[0])

            if downstream_targets:
                break  # viable river entry with receiving gauges
            if entry_node is None and not river_stopped:
                break  # no river contact at all -> Case 3 (pure overland)
            if _attempt == 1:
                break  # second pass: accept the dead-end river / overland result
            # River contact but NO reachable gauge (topology island) -> retry on terrain

        # Case 1: D8 flow path directly encountered a water level gauge.
        # D1/D3: hydrological cascade — after the first receiving gauge, keep tracing D8
        # downstream: one non-overlapping segment feature per gauge the water passes.
        if direct_target_water_id:
            branch_seed_cells[r_id] = list(overland_cells)
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
                if dist_km >= min_flow_km:
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
                            "cascade_segment": 0,
                            "previous_gauge_id": "",
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
                            "coordinates": simplify_linestring_coords(coords, tolerance_deg=0.00035, label=feature_id)
                        }
                    }
                    features.append(feature)
                    rainfall_relations.append(feature["properties"])

                # Cascade: continue D8 downstream from the last reached gauge
                visited_targets = {direct_target_water_id}
                cum_km = dist_km
                seg_count = 1  # segment 0 = the first receiving gauge (drawn above)
                seg_prev_id = direct_target_water_id
                seg_prev_lonlat = (tgt_lon, tgt_lat)
                resume_cell = overland_cells[-1]
                while cum_km < cascade_max_km:
                    code2 = int(fdir[resume_cell[0], resume_cell[1]])
                    if code2 not in D8_DELTAS:
                        break
                    nr2 = resume_cell[0] + D8_DELTAS[code2][0]
                    nc2 = resume_cell[1] + D8_DELTAS[code2][1]
                    seg_coords2, next_target_id, seg_cells2 = trace_downstream_path(
                        nr2, nc2, fdir, transform, crs=crs,
                        stop_condition_fn=stop_at_water_station_excluding(visited_targets),
                        min_lat=southern_limit_lat,
                        max_steps=5000,
                        water_poly_mask=water_poly_mask,
                        water_poly_ids=water_poly_ids,
                        start_poly_id=poly_id_at_cell(resume_cell)
                    )
                    if not next_target_id or len(seg_coords2) < 2:
                        break
                    target_st2 = next((s for s in water_stations if s['station_id'] == next_target_id), None)
                    if not target_st2:
                        break
                    t2_lon = float(target_st2.get('longitude', 0.0))
                    t2_lat = float(target_st2.get('latitude', 0.0))

                    coords2 = merge_coordinates([[seg_prev_lonlat[0], seg_prev_lonlat[1]]], seg_coords2)
                    last_dist2 = math.hypot(coords2[-1][0] - t2_lon, coords2[-1][1] - t2_lat)
                    if last_dist2 <= 0.005:
                        coords2[-1] = [round(t2_lon, 6), round(t2_lat, 6)]
                    seg_len_km = linestring_length_km(coords2)
                    if cum_km + seg_len_km > cascade_max_km * 1.05:
                        break

                    if seg_len_km >= min_flow_km:
                        z_water2 = sample_elevation(t2_lon, t2_lat)
                        dz2 = max(0.0, z_rain - z_water2)
                        slope2 = (dz2 / (cum_km * 1000.0)) if cum_km > 0.001 else 0.005
                        lag2 = compute_rainfall_lag_bounds(
                            overland_dist_km=dist_km,
                            overland_slope=slope2,
                            channel_dist_km=max(0.0, cum_km - dist_km),
                            channel_slope=slope2,
                            total_dz_m=dz2
                        )
                        lag_min_m, lag_avg_m, lag_max_m, lag_min_h, lag_avg_h, lag_max_h = lag2
                        feature_id = f"flow_rain_{r_id}_to_{next_target_id}"
                        feature = {
                            "type": "Feature",
                            "id": feature_id,
                            "properties": {
                                "feature_type": "rainfall_to_gauge_flowpath",
                                "cascade_segment": seg_count,
                                "previous_gauge_id": seg_prev_id,
                                "from_station_id": r_id,
                                "from_station_name": r_st.get('station_name', ''),
                                "to_station_id": next_target_id,
                                "to_station_name": target_st2.get('station_name', ''),
                                "total_distance_km": round(cum_km + seg_len_km, 2),
                                "distance_km": round(seg_len_km, 2),
                                "segment_length_km": round(seg_len_km, 2),
                                "response_lag_minutes": lag_avg_m,
                                "response_lag_minutes_min": lag_min_m,
                                "response_lag_minutes_max": lag_max_m,
                                "response_lag_hours": lag_avg_h,
                                "response_lag_hours_min": lag_min_h,
                                "response_lag_hours_max": lag_max_h,
                                "elevation_diff_m": round(dz2, 2),
                                "slope": round(slope2, 6),
                                "upstream_elev_m": round(z_rain, 2),
                                "downstream_elev_m": round(z_water2, 2),
                                "influence_weight_percent": 100.0
                            },
                            "geometry": {
                                "type": "LineString",
                                "coordinates": simplify_linestring_coords(coords2, tolerance_deg=0.00035, label=feature_id)
                            }
                        }
                        features.append(feature)
                        rainfall_relations.append(feature["properties"])
                        seg_count += 1

                    visited_targets.add(next_target_id)
                    seg_prev_id = next_target_id
                    seg_prev_lonlat = (t2_lon, t2_lat)
                    cum_km += seg_len_km
                    resume_cell = seg_cells2[-1]
                continue

        if entry_idx is not None:
            branch_seed_cells[r_id] = list(overland_cells[:entry_idx + 1])
            # Case 2: Hit OSM River. We MUST stop overland runoff at entry_idx.
            coords = merge_coordinates(
                [[lon, lat]],
                overland_coords[:entry_idx + 1]
            )
            overland_dist_km = linestring_length_km(coords)
            entry_pt = overland_coords[entry_idx]

            if downstream_targets:
                # D3: cascade segments — one NON-OVERLAPPING LineString per receiving gauge:
                #   segment 0   = overland part + channel entry->G1
                #   segment k>0 = channel G(k-1)->Gk (starts exactly where the previous ends)
                prev_gauge_node = entry_node
                for seg_i, (b_dist, target_water_id, target_st) in enumerate(downstream_targets):
                    tgt_lon = float(target_st.get('longitude', 0.0))
                    tgt_lat = float(target_st.get('latitude', 0.0))
                    z_water = sample_elevation(tgt_lon, tgt_lat)
                    v_node = water_node_map.get(target_water_id)
                    if not v_node or prev_map is None:
                        continue
                    chain = river_graph.reconstruct_node_path(prev_map, entry_node, v_node)
                    if not chain:
                        continue
                    # Round 6: tag segments whose backbone chain uses a reservoir
                    # transit edge (an honest straight hop across open water) so the
                    # validator does not flag it as a D8 flat teleport.
                    chain_tr = None
                    for _t in range(1, len(chain)):
                        _pe = prev_map.get(chain[_t])
                        if _pe and _pe[1].get("reservoir_transit"):
                            chain_tr = _pe[1]["reservoir_transit"]
                            break

                    # Cut the channel piece for this segment
                    if seg_i == 0 or prev_gauge_node not in chain:
                        start_pos = 0
                    else:
                        start_pos = chain.index(prev_gauge_node)
                    channel_seg = river_graph.stitch_coords_from_prev(prev_map, chain, start_pos, len(chain) - 1)
                    if not channel_seg or len(channel_seg) < 2:
                        continue

                    if seg_i == 0:
                        seg_coords = merge_coordinates(coords, channel_seg)
                    else:
                        seg_coords = list(channel_seg)

                    # Snap the final vertex to the receiving gauge (access stub <= ~1.5km)
                    last_dist = math.hypot(seg_coords[-1][0] - tgt_lon, seg_coords[-1][1] - tgt_lat)
                    if last_dist <= 0.015:
                        seg_coords.append([round(tgt_lon, 6), round(tgt_lat, 6)])

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

                    seg_len_km = linestring_length_km(seg_coords)
                    feature_id = f"flow_rain_{r_id}_to_{target_water_id}"
                    seg_props = {
                        "feature_type": "rainfall_to_gauge_flowpath",
                        "cascade_segment": seg_count,
                        "previous_gauge_id": "" if seg_i == 0 else str(downstream_targets[seg_i - 1][1]),
                        "from_station_id": r_id,
                        "from_station_name": r_st.get('station_name', ''),
                        "to_station_id": target_water_id,
                        "to_station_name": target_st.get('station_name', ''),
                        "total_distance_km": round(total_dist_km, 2),
                        "distance_km": round(overland_dist_km, 2) if seg_i == 0 else round(channel_dist_km, 2),
                        "channel_distance_km": round(channel_dist_km, 2),
                        "segment_length_km": round(seg_len_km, 2),
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
                    }
                    if entry_meta:
                        seg_props.update(entry_meta)
                    if chain_tr:
                        seg_props["reservoir_transit"] = True
                        seg_props["transit_osm_id"] = chain_tr.get("osm_id", "")
                        seg_props["transit_mode"] = chain_tr.get("mode", "straight")
                    feature = {
                        "type": "Feature",
                        "id": feature_id,
                        "properties": seg_props,
                        "geometry": {
                            "type": "LineString",
                            "coordinates": simplify_linestring_coords(seg_coords, tolerance_deg=0.00035, label=feature_id)
                        }
                    }
                    features.append(feature)
                    rainfall_relations.append(feature["properties"])
                    prev_gauge_node = v_node
                    seg_count += 1
            else:
                # Hit the river, but NO water station within the cascade reach.
                # Stop at the river, don't wander off.
                if overland_dist_km < min_flow_km:
                    continue  # D2: below minimum flow length -> skip tiny stubs
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
                river_entry_props = {
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
                }
                if entry_meta:
                    river_entry_props.update(entry_meta)
                feature = {
                    "type": "Feature",
                    "id": feature_id,
                    "properties": river_entry_props,
                    "geometry": {
                        "type": "LineString",
                        "coordinates": simplify_linestring_coords(coords, tolerance_deg=0.00035, label=feature_id)
                    }
                }
                features.append(feature)
        else:
            # Case 3: Standalone overland drainage along terrain D8 (never hit OSM river)
            if len(overland_coords) >= 2:
                coords = merge_coordinates([[lon, lat]], overland_coords)
                overland_capped = False
                if overland_max_km and overland_max_km > 0:
                    # Cap runaway cross-country overland lines: without a river contact
                    # the runoff must not be drawn wandering tens of kilometers.
                    acc_km = 0.0
                    cut_idx = len(coords) - 1
                    for ci in range(1, len(coords)):
                        acc_km += math.hypot(
                            (coords[ci][0] - coords[ci - 1][0]) * 111.32 * 0.95,
                            (coords[ci][1] - coords[ci - 1][1]) * 110.54
                        )
                        if acc_km > overland_max_km:
                            cut_idx = ci
                            overland_capped = True
                            break
                    if overland_capped and cut_idx >= 2:
                        coords = coords[:cut_idx + 1]
                # Round 6 (Phase B): branch seeds follow the DRAWN path (post-cap) —
                # uncapped wild D8 traces used to seed basin-wide branch networks.
                if overland_capped:
                    branch_seed_cells[r_id] = list(overland_cells[:max(2, cut_idx)])
                else:
                    branch_seed_cells[r_id] = list(overland_cells)
                dist_km = linestring_length_km(coords)
                if dist_km >= min_flow_km:  # D2: user-defined minimum flow length (1km)
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
                            "routing": "overland_capped" if overland_capped else "overland",
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
                            "coordinates": simplify_linestring_coords(coords, tolerance_deg=0.00035, label=feature_id)
                        }
                    }
                    features.append(feature)

    # E3: Drainage branches — upstream D8 channel network per rain station.
    # On large basins the upstream network of a main-river station can exceed the cell
    # guard: escalate min_branch_acc (x4, max 2 retries) so the layer degrades gracefully
    # to bigger channels instead of being truncated arbitrarily.
    if include_branches and branch_seed_cells:
        eff_branch_acc = branch_min_acc
        branch_features: List[Dict[str, Any]] = []
        for attempt in range(3):
            branch_features, truncated = extract_station_drainage_branches(
                branch_seed_cells, fdir, acc, transform, crs=crs,
                min_branch_acc=eff_branch_acc, min_length_km=branch_min_km,
                max_cells_per_station=branch_max_cells,
                southern_limit_lat=southern_limit_lat,
                max_branches_per_station=branch_max_count,
                river_mask=river_mask,
                water_poly_mask=water_poly_mask
            )
            if not truncated:
                break
            eff_branch_acc *= 4
            print(f"  [WARN] Retrying drainage branches with --branch-min-acc={eff_branch_acc} "
                  f"(attempt {attempt + 2}/3)")
        if branch_features:
            features.extend(branch_features)
            print(f"        Drainage Branches: {len(branch_features)} features "
                  f"for {len(branch_seed_cells)} rain station path(s) (acc>={eff_branch_acc})")

    # F: OSM river network as a separate display layer (feature_type="osm_river").
    # NO length filter — the full OSM network is preserved; simplified with the same
    # 35m tolerance to bound file size.
    if include_osm_layer and osm_waterways_geojson and osm_waterways_geojson.get("features"):
        n_osm_added = 0
        for feat in osm_waterways_geojson.get("features", []):
            geom = feat.get("geometry")
            if not geom or geom.get("type") not in ("LineString", "MultiLineString"):
                continue
            props = feat.get("properties", {})
            if geom.get("type") == "LineString":
                parts = [geom.get("coordinates", [])]
            else:
                parts = geom.get("coordinates", [])
            osm_id = str(props.get("osm_id", ""))
            for part_i, part in enumerate(parts):
                if len(part) < 2:
                    continue
                part_len_km = linestring_length_km(part)
                fid = f"osm_river_{osm_id}" if len(parts) == 1 else f"osm_river_{osm_id}_{part_i}"
                coords_s = simplify_linestring_coords(part, tolerance_deg=0.00035, label=fid)
                if len(coords_s) < 2:
                    continue
                features.append({
                    "type": "Feature",
                    "id": fid,
                    "properties": {
                        "feature_type": "osm_river",
                        "osm_id": osm_id,
                        "river_name": props.get("name", "") or props.get("name_th", "") or props.get("name_en", ""),
                        "waterway": props.get("waterway", "stream"),
                        "length_km": round(part_len_km, 2)
                    },
                    "geometry": {
                        "type": "LineString",
                        "coordinates": coords_s
                    }
                })
                n_osm_added += 1
        if n_osm_added:
            print(f"        OSM River Layer: {n_osm_added} features (feature_type=osm_river, no length filter)")

    # Basin-boundary clipping (OUTPUT filter, G3/G2): every output line EXCEPT the
    # osm_river display layer is cut to the official ThaiWater basin polygon.
    # osm_river is NEVER re-cut here (G2): OSM was already cropped with the real
    # polygon (+ buffer) at fetch time (SOURCE filter), so the output layer must
    # stay byte-identical to that crop-set — no `basin_clipped`, no double cut.
    basin_poly = _extract_basin_polygon(basin_boundary_geojson) if clip_to_basin else None
    filter_report: Dict[str, Dict[str, Any]] = {}
    if basin_poly is not None:
        kept_features: List[Dict[str, Any]] = []
        clip_stats: Dict[str, Dict[str, int]] = {}
        for feat in features:
            props = feat.get("properties", {})
            ftype = props.get("feature_type", "unknown")
            st = clip_stats.setdefault(ftype, {"n_in": 0, "n_out": 0, "clipped": 0, "dropped": 0})
            g = feat.get("geometry")
            if (not g or g.get("type") != "LineString") or ftype == "osm_river":
                kept_features.append(feat)
                st["n_in"] += 1
                st["n_out"] += 1
                continue
            st["n_in"] += 1
            coords = g.get("coordinates") or []
            if len(coords) < 2:
                kept_features.append(feat)
                st["n_out"] += 1
                continue
            new_coords = _clip_line_to_basin(coords, basin_poly)
            if new_coords is None:
                st["dropped"] += 1
                continue
            if len(new_coords) != len(coords):
                st["clipped"] += 1
                feat["properties"]["basin_clipped"] = True
                g["coordinates"] = new_coords
            st["n_out"] += 1
            kept_features.append(feat)
        features = kept_features
        n_clipped = sum(s["clipped"] for s in clip_stats.values())
        n_dropped = sum(s["dropped"] for s in clip_stats.values())
        print(f"        Basin clip: {n_clipped:,} features trimmed to the basin polygon, "
              f"{n_dropped:,} outside features dropped (osm_river layer untouched — G2)")
        filter_report["basin_clip"] = clip_stats
    else:
        layer_counts: Dict[str, int] = {}
        for feat in features:
            t = feat.get("properties", {}).get("feature_type", "unknown")
            layer_counts[t] = layer_counts.get(t, 0) + 1
        filter_report["basin_clip"] = {
            t: {"n_in": n, "n_out": n, "clipped": 0, "dropped": 0} for t, n in layer_counts.items()
        }
        filter_report["basin_clip_disabled"] = True

    # Filter Matrix report (F1/F4): per-layer n_in -> n_out summary embedded in the
    # output `_meta` so the validator can verify every layer was filtered and reported.
    osm_meta = (osm_waterways_geojson or {}).get("_meta") or {}
    filter_report["osm_source"] = osm_meta.get("source", "unknown")
    filter_report["osm_crop_polygon"] = osm_meta.get("crop_polygon", "")
    # Round 6 (Phase C): OSM layer audit counters — the validator (and humans) must be
    # able to account for every way that was split or dropped before reaching the output
    # (answers "did the pipeline silently delete OSM lines?").
    if osm_meta.get("way_jump_stats"):
        filter_report["osm_way_jump_split"] = osm_meta["way_jump_stats"]
    if osm_meta.get("crop_stats"):
        filter_report["osm_crop"] = osm_meta["crop_stats"]
    for lt, st in sorted(filter_report["basin_clip"].items()):
        if isinstance(st, dict):
            print(f"        [FILTER] {lt}: n_in={st['n_in']:,} -> n_out={st['n_out']:,} "
                  f"(clipped={st['clipped']:,}, dropped={st['dropped']:,})")

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
        "_meta": {
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "osm_source_label": filter_report["osm_source"],
            "filters": filter_report
        },
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

    # Map D8 reverse lookups (module-level REVERSE_D8: code -> upstream neighbor offset)
    reverse_d8 = REVERSE_D8

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
