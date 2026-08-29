#!/usr/bin/env python3
"""
Synthetic unit tests for the flow topology pipeline (C2).
No real data required — builds a small synthetic DEM + OSM network and verifies:

  1. DirectedRiverGraph direction enforcement + connectivity + path contiguity
  2. reconstruct_node_path / stitch_coords_from_prev (cascade segment math)
  3. simplify_linestring_coords jump splitting (A1)
  4. merge_coordinates dedup
  5. burn_stream_network_into_dem line vs polygon depths (B4)
  6. End-to-end build_flow_paths_and_relations on a synthetic basin:
     gauge chain, rain cascade segments, drainage branches, no long straight jumps

Run:  python tests/test_flow_topology.py
"""

import json
import math
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from rasterio.transform import Affine

from scripts.modules.graph_topology import (
    DirectedRiverGraph,
    build_flow_paths_and_relations,
    extract_station_drainage_branches,
    merge_coordinates,
    simplify_linestring_coords,
    snap_stations_to_stream,
    trace_downstream_path,
)
from scripts.modules.terrain_engine import burn_stream_network_into_dem
from scripts.modules.gis_utils import linestring_length_km

RES = 0.0001  # ~11 m per cell (EPSG:4326)


# ---------------------------------------------------------------------------
# 1. Graph direction, connectivity, contiguity
# ---------------------------------------------------------------------------
def test_graph_direction_and_connectivity():
    g = DirectedRiverGraph(snap_tolerance_deg=1e-4)

    # Elevation decreases to the south (lower lat = lower elevation)
    def elev_fn(lon, lat):
        return 200.0 + (lat - 18.0) * 2000.0

    main = [[100.000, 18.000], [100.000, 18.050], [100.000, 18.100]]
    g.add_river_segment(main, sample_elev_fn=elev_fn, river_name="main")

    # Tributary digitized FROM head TO junction (downstream direction preserved)
    trib = [[100.050, 18.100], [100.025, 18.100], [100.000, 18.100]]
    g.add_river_segment(trib, sample_elev_fn=elev_fn, river_name="trib")
    g.build_spatial_index()

    head, _ = g.find_nearest_node(100.050, 18.100, max_dist_deg=0.01)
    mouth, _ = g.find_nearest_node(100.000, 18.000, max_dist_deg=0.01)
    assert head is not None and mouth is not None

    coords, dist = g.shortest_path(head, mouth, max_dist_km=50.0)
    assert coords is not None and dist > 0.0
    assert len(coords) >= 4

    # Contiguity: no gap larger than one vertex spacing (max 0.05 deg here)
    for i in range(len(coords) - 1):
        gap = math.hypot(coords[i][0] - coords[i + 1][0], coords[i][1] - coords[i + 1][1])
        assert gap < 0.051, f"discontinuity at {i}: {gap}"
    # Ends at the mouth
    assert math.hypot(coords[-1][0] - 100.0, coords[-1][1] - 18.0) < 1e-3

    # Direction flip: segment digitized upstream (low elev first) must be reversed,
    # so the edge flows from high elevation (north) down to low elevation (south)
    g2 = DirectedRiverGraph(snap_tolerance_deg=1e-4)
    g2.add_river_segment([[100.0, 17.950], [100.0, 18.000]], sample_elev_fn=elev_fn)
    south, _ = g2.find_nearest_node(100.0, 17.950, max_dist_deg=0.01)
    north, _ = g2.find_nearest_node(100.0, 18.000, max_dist_deg=0.01)
    outs = [v for v, _ in g2.adj.get(north, [])]
    assert south in outs, "edge must flow from high (north) to low (south) elevation after flip"

    # Unknown elevation (outside DEM) -> no flip, OSM direction kept
    g3 = DirectedRiverGraph(snap_tolerance_deg=1e-4)
    g3.add_river_segment(
        [[100.0, 19.0], [100.0, 19.01]],
        sample_elev_fn=lambda lo, la: None,  # everything unknown
    )
    n_a, _ = g3.find_nearest_node(100.0, 19.0, max_dist_deg=0.01)
    n_b, _ = g3.find_nearest_node(100.0, 19.01, max_dist_deg=0.01)
    assert [v for v, _ in g3.adj.get(n_a, [])] == [n_b], "unknown elevations must keep OSM digitization direction"


# ---------------------------------------------------------------------------
# 2. Node path reconstruction + segment stitching
# ---------------------------------------------------------------------------
def test_reconstruct_node_path_and_stitch():
    g = DirectedRiverGraph(snap_tolerance_deg=1e-4)
    pts = [[100.0 + i * 0.01, 18.0] for i in range(5)]  # 4 edges
    g.add_river_segment(pts, sample_elev_fn=lambda lo, la: -la * 1000.0)  # flows east
    g.build_spatial_index()

    n0, _ = g.find_nearest_node(*pts[0], max_dist_deg=0.01)
    n4, _ = g.find_nearest_node(*pts[-1], max_dist_deg=0.01)
    dist, prev = g.dijkstra_single_source(n0, max_dist_km=50.0)

    chain = g.reconstruct_node_path(prev, n0, n4)
    assert chain is not None and len(chain) == 5 and chain[0] == n0 and chain[-1] == n4

    full = g.stitch_coords_from_prev(prev, chain, 0, len(chain) - 1)
    legacy = g.reconstruct_path_from_prev(prev, n0, n4)
    assert len(full) == len(legacy)
    for a, b in zip(full, legacy):
        assert math.hypot(a[0] - b[0], a[1] - b[1]) < 1e-9

    # Middle segment (node 2 -> node 4) shares its first point with the previous segment end
    seg = g.stitch_coords_from_prev(prev, chain, 2, len(chain) - 1)
    assert len(seg) >= 2
    tail_of_prefix = g.stitch_coords_from_prev(prev, chain, 0, 2)
    assert math.hypot(seg[0][0] - tail_of_prefix[-1][0], seg[0][1] - tail_of_prefix[-1][1]) < 1e-9

    # Broken chain returns None
    assert g.reconstruct_node_path(prev, n4, n0) is None


# ---------------------------------------------------------------------------
# 3. simplify_linestring_coords jump splitting
# ---------------------------------------------------------------------------
def test_simplify_splits_at_jump():
    # Mid-path 55km jump: keep chunk from origin, big tail must NOT be re-attached
    c = [[100.0, 18.0], [100.001, 18.0], [100.500, 18.0], [100.501, 18.0]]
    out = simplify_linestring_coords(c, tolerance_deg=0.00035, max_step_km=0.5)
    assert out[0] == [100.0, 18.0]
    for i in range(len(out) - 1):
        assert seg_km_local(out[i], out[i + 1]) <= 0.51, f"straight jump survived: {out}"

    # End-of-path small stub (<= 2km): contiguous start + one trailing gauge-access jump
    c2 = [[100.0, 18.0], [100.001, 18.0], [100.008, 18.0]]
    out2 = simplify_linestring_coords(c2, tolerance_deg=0.00035, max_step_km=0.5)
    assert out2[0] == [100.0, 18.0]
    assert out2[-1] == [100.008, 18.0], f"small end stub must be kept: {out2}"
    for i in range(len(out2) - 1):
        assert seg_km_local(out2[i], out2[i + 1]) <= 2.01

    # No-jump line: endpoints exact
    c3 = [[100.0, 18.0], [100.001, 18.001], [100.002, 18.002]]
    out3 = simplify_linestring_coords(c3, tolerance_deg=0.00035, max_step_km=0.5)
    assert out3[0] == [100.0, 18.0] and out3[-1] == [100.002, 18.002]


def seg_km_local(a, b):
    return math.hypot((b[0] - a[0]) * 111.32 * 0.95, (b[1] - a[1]) * 110.54)


# ---------------------------------------------------------------------------
# 4. merge_coordinates dedup
# ---------------------------------------------------------------------------
def test_merge_coordinates():
    a = [[100.0, 18.0], [100.001, 18.0]]
    b = [[100.001, 18.0], [100.002, 18.001]]
    m = merge_coordinates(a, b)
    assert len(m) == 3
    assert m[0] == [100.0, 18.0] and m[-1] == [100.002, 18.001]
    assert merge_coordinates(None, []) == []


# ---------------------------------------------------------------------------
# 5. Stream burning: lines (deep) vs polygons (shallow)
# ---------------------------------------------------------------------------
def test_burn_polygons():
    dem = np.full((50, 50), 100.0, dtype=np.float32)
    transform = Affine(RES, 0, 100.0, 0, -RES, 18.0)

    osm = {"features": [
        {"geometry": {"type": "LineString",
                      "coordinates": [[100.000, 17.99995], [100.0049, 17.99995]]}},
    ]}
    poly = {"features": [
        {"geometry": {"type": "Polygon",
                      "coordinates": [[[100.001, 17.9980], [100.004, 17.9980],
                                       [100.004, 17.9990], [100.001, 17.9990],
                                       [100.001, 17.9980]]]}},
    ]}

    out = burn_stream_network_into_dem(
        dem.copy(), transform, osm, crs=None, burn_depth_m=15.0,
        water_polygons_geojson=poly, polygon_burn_depth_m=10.0
    )
    assert abs(out[0, 2] - 85.0) < 1e-4, f"line cell should burn -15m, got {out[0, 2]}"
    assert abs(out[15, 15] - 90.0) < 1e-4, f"polygon-only cell should burn -10m, got {out[15, 15]}"
    assert out[45, 45] == 100.0, "untouched cell must keep original elevation"

    # Polygons absent -> only lines burn
    out2 = burn_stream_network_into_dem(dem.copy(), transform, osm, crs=None, burn_depth_m=15.0)
    assert abs(out2[0, 2] - 85.0) < 1e-4
    assert out2[15, 15] == 100.0


# ---------------------------------------------------------------------------
# 6. End-to-end synthetic basin
# ---------------------------------------------------------------------------
def _channel_col(r):
    return 80 + int(round(6 * math.sin(r / 18.0)))


def _build_synthetic_basin():
    H, W = 800, 160
    x0, y0 = 100.0, 18.2
    transform = Affine(RES, 0, x0, 0, -RES, y0)

    dem = np.zeros((H, W), dtype=np.float32)
    for r in range(H):
        dem[r, :] = 300.0 - 0.5 * r
    # Carve main channel (wiggle) + V-shaped valley so the channel collects drainage
    for r in range(H):
        c0 = _channel_col(r)
        for c in range(max(0, c0 - 20), min(W, c0 + 21)):
            dem[r, c] = min(dem[r, c], 300.0 - 0.5 * r - (1.0 + (20 - abs(c - c0)) * 0.5))
        dem[r, max(0, c0 - 1):c0 + 2] = 300.0 - 0.5 * r - 8.0
    # East tributary joining the main channel (used by the rain stations);
    # placed deep enough that the upstream branch exceeds the 1.5km default filter
    tr = 185
    c_main = _channel_col(tr)
    for c in range(c_main + 2, 140):
        dem[tr, c] = min(dem[tr, c], 300.0 - 0.5 * tr - 8.0 + (c - c_main) * 0.05)

    osm = {"type": "FeatureCollection", "features": [
        {"type": "Feature", "id": "osm_way_1",
         "properties": {"name": "main", "waterway": "river"},
         "geometry": {"type": "LineString",
                      "coordinates": [[x0 + (_channel_col(r) + 0.5) * RES,
                                       y0 - (r + 0.5) * RES] for r in range(H)]}}
    ]}

    burned = burn_stream_network_into_dem(
        dem.copy(), transform, osm, crs=None, burn_depth_m=15.0
    )

    import pyflwdir
    flw = pyflwdir.from_dem(burned, nodata=-9999.0, transform=transform, latlon=True)
    fdir = flw.to_array(ftype='d8')
    acc = flw.upstream_area(unit='cell')
    del flw

    def st_at(r, sid):
        c = _channel_col(r)
        return {"station_id": sid, "station_name": f"gauge_{sid}",
                "latitude": y0 - (r + 0.5) * RES, "longitude": x0 + (c + 0.5) * RES,
                "riverName": "main"}

    # Gauges ~2km apart so cascade segments pass the 1km minimum
    water = [st_at(30, "W30"), st_at(400, "W400"), st_at(600, "W600")]
    # Two rain stations in the SAME tributary catchment (row-185 trib) — their shared
    # upstream branch network must dedupe into one feature with shared_with
    rain = [
        {"station_id": "R1", "station_name": "rain_R1",
         "latitude": y0 - (150 + 0.5) * RES, "longitude": x0 + (130 + 0.5) * RES},
        {"station_id": "R2", "station_name": "rain_R2",
         "latitude": y0 - (158 + 0.5) * RES, "longitude": x0 + (126 + 0.5) * RES},
    ]

    return dict(H=H, W=W, x0=x0, y0=y0, transform=transform, dem=burned,
                fdir=fdir, acc=acc, osm=osm, water=water, rain=rain)


def test_end_to_end_synthetic_basin():
    B = _build_synthetic_basin()

    snapped = snap_stations_to_stream(
        B["water"], B["fdir"], B["acc"], B["transform"],
        osm_waterways_geojson=B["osm"], crs=None
    )
    for st in snapped:
        assert st.get("snapped_via_osm") is True, f"{st['station_id']} must snap to OSM line"
        assert st.get("grid_row") is not None

    # branch_min_km=1.0 explicit: the synthetic basin's longest branch fragment is ~1.5km
    # (the 1.5km DEFAULT is asserted separately via signature check below)
    geojson, gauge_relations, rain_relations = build_flow_paths_and_relations(
        snapped, B["rain"], B["fdir"], B["acc"], B["dem"], B["transform"],
        osm_waterways_geojson=B["osm"], crs=None,
        min_flow_km=1.0, cascade_max_km=60.0, branch_min_acc=500,
        include_branches=True, branch_min_km=1.0
    )

    features = geojson["features"]
    assert len(features) > 0

    # Gauge chain: W30 -> W400 -> W600 via D8
    gauge_pairs = {(r.get("from_station_id"), r.get("to_station_id")) for r in gauge_relations}
    assert ("W30", "W400") in gauge_pairs, f"missing W30->W400, got {gauge_pairs}"
    assert ("W400", "W600") in gauge_pairs

    # Rain cascade: R1 enters at row 165 (downstream of W30) -> reaches W400 (seg 0),
    # then the water keeps flowing downstream to W600 (seg 1)
    rain_pairs = {(r.get("from_station_id"), r.get("to_station_id")) for r in rain_relations}
    assert ("R1", "W400") in rain_pairs, f"rain must reach W400, got {rain_pairs}"
    assert ("R1", "W600") in rain_pairs, f"rain cascade must reach W600, got {rain_pairs}"
    w400_rels = [r for r in rain_relations if r.get("to_station_id") == "W400"]
    assert w400_rels and w400_rels[0].get("cascade_segment") == 0
    w600_rels = [r for r in rain_relations if r.get("to_station_id") == "W600"]
    assert w600_rels and w600_rels[0].get("cascade_segment") == 1
    assert w600_rels[0].get("previous_gauge_id") == "W400"
    assert w600_rels[0].get("total_distance_km", 0) > w400_rels[0].get("total_distance_km", 0), \
        "cumulative distance must grow along the cascade"

    # IDW weights per target group must sum to ~100
    groups = {}
    for r in rain_relations:
        groups.setdefault(r["to_station_id"], []).append(r.get("influence_weight_percent", 0.0))
    for target, weights in groups.items():
        assert abs(sum(weights) - 100.0) < 0.5, f"weights of {target} sum to {sum(weights)}"

    # Geometry quality: no straight-line jump above 2 km in ANY feature.
    # (Douglas-Peucker legitimately collapses long straight D8 runs into single segments;
    #  anything beyond ~2km in this 4.4km basin would be a stitching defect.)
    for feat in features:
        coords = feat["geometry"]["coordinates"]
        assert len(coords) >= 2, f"degenerate feature {feat['id']}"
        for i in range(len(coords) - 1):
            gap = seg_km_local(coords[i], coords[i + 1])
            assert gap <= 2.01, f"{feat['id']} has {gap:.2f} km straight segment at idx {i}"

    # Drainage branches tied to the rain station exist; identical networks shared by
    # R1/R2 must be deduped into one feature carrying shared_with
    branch_feats = [f for f in features
                    if f["properties"].get("feature_type") == "rainfall_drainage_branch"]
    assert branch_feats, "expected at least one drainage branch for R1"
    assert all(f["properties"].get("from_station_id") in ("R1", "R2") for f in branch_feats)
    assert all(f["properties"]["branch_length_km"] >= 1.0 for f in branch_feats)
    assert any("R2" in (f["properties"].get("shared_with") or []) for f in branch_feats), \
        "R1/R2 share the same upstream network -> expect shared_with dedupe"
    # no duplicated geometries remain
    seen_geoms = set()
    for f in branch_feats:
        key = tuple(map(tuple, f["geometry"]["coordinates"]))
        assert key not in seen_geoms, f"unduplicated branch geometry: {f['id']}"
        seen_geoms.add(key)

    # OSM river display layer is present, separable, and NOT length-filtered
    osm_feats = [f for f in features if f["properties"].get("feature_type") == "osm_river"]
    assert osm_feats, "expected OSM river layer features"
    assert all(f["properties"].get("osm_id") is not None for f in osm_feats)
    assert all(f["properties"].get("osm_id") is not None for f in osm_feats)
    layer_types = {f["properties"].get("feature_type") for f in features}
    assert {"gauge_to_gauge_flowpath", "rainfall_to_gauge_flowpath",
            "rainfall_drainage_branch", "osm_river"} <= layer_types


def test_branch_min_km_default():
    """--branch-min-km default must be 1.0 while flow paths stay at 1.0."""
    import inspect
    sig = inspect.signature(build_flow_paths_and_relations)
    assert sig.parameters["branch_min_km"].default == 1.0
    assert sig.parameters["min_flow_km"].default == 1.0


# ---------------------------------------------------------------------------
# 9. Crossing noding: ways crossing WITHOUT a shared vertex must connect
# ---------------------------------------------------------------------------
def test_noding_crossing_ways_connect():
    g = DirectedRiverGraph(snap_tolerance_deg=1e-4)

    def elev_fn(lon, lat):
        # Elevation falls eastward (main stem flows east) and southward (tributary
        # flows south into the main stem at the crossing).
        return 100.0 - lon * 100.0 + (lat - 18.0) * 1000.0

    # Horizontal main stem flowing EAST (elev falls with lon)
    main = [[100.000, 18.000], [100.050, 18.000]]
    g.add_river_segment(main, sample_elev_fn=elev_fn, river_name="main")

    # Vertical tributary flowing SOUTH, crossing the main stem mid-segment at
    # (100.025, 18.000) — no shared vertex anywhere.
    trib = [[100.025, 18.050], [100.025, 17.950]]
    g.add_river_segment(trib, sample_elev_fn=elev_fn, river_name="trib")

    diag = g.finalize_connectivity()
    g.build_spatial_index()
    assert diag["crossings_split"] >= 1, "crossing must be noded"

    # Route from the tributary head to the main stem's downstream mouth:
    # possible only when the crossing junction exists.
    head, _ = g.find_nearest_node(100.025, 18.050, max_dist_deg=0.01)
    mouth, _ = g.find_nearest_node(100.050, 18.000, max_dist_deg=0.01)
    coords, dist = g.shortest_path(head, mouth, max_dist_km=50.0)
    assert coords is not None, "graph must be connected across the noded crossing"
    assert dist > 0.0
    # Junction must lie at the crossing point
    junction, _ = g.find_nearest_node(100.025, 18.000, max_dist_deg=0.0005)
    assert junction is not None


# ---------------------------------------------------------------------------
# 10. Endpoint welding: consecutive ways with a small gap must connect
# ---------------------------------------------------------------------------
def test_endpoint_weld_connects_gapped_ways():
    g = DirectedRiverGraph(snap_tolerance_deg=1e-4)

    def elev_fn(lon, lat):
        return 200.0 - lon * 1000.0  # flows east

    # Way A ends at (100.050, 18.0); way B starts ~80m away (beyond the 39m
    # vertex-weld tolerance) and continues east.
    a = [[100.000, 18.000], [100.050, 18.000]]
    b = [[100.0508, 18.0000], [100.100, 18.000]]
    g.add_river_segment(a, sample_elev_fn=elev_fn, river_name="A")
    g.add_river_segment(b, sample_elev_fn=elev_fn, river_name="B")

    diag = g.finalize_connectivity(endpoint_snap_deg=0.002)
    g.build_spatial_index()
    assert diag["ends_welded"] >= 1, "the gapped way end must be welded"

    head, _ = g.find_nearest_node(100.000, 18.000, max_dist_deg=0.01)
    tail, _ = g.find_nearest_node(100.100, 18.000, max_dist_deg=0.01)
    coords, dist = g.shortest_path(head, tail, max_dist_km=50.0)
    assert coords is not None, "gapped ways must form one routable channel after welding"
    assert dist > 0.0


# ---------------------------------------------------------------------------
# 11. snap_point_to_graph: projects onto EDGES (splits mid-segment)
# ---------------------------------------------------------------------------
def test_snap_point_to_graph_splits_edge():
    g = DirectedRiverGraph(snap_tolerance_deg=1e-4)
    g.add_river_segment(
        [[100.000, 18.000], [100.100, 18.000]],  # one long 2-vertex edge
        sample_elev_fn=lambda lo, la: -lo * 100.0
    )
    g.finalize_connectivity()
    g.build_spatial_index()

    before_nodes = len(g.nodes)
    # Query point ~55m off the line, near the middle (no vertex anywhere near)
    q = (100.050, 18.0005)
    nid, d = g.snap_point_to_graph(q[0], q[1], max_dist_deg=0.003)
    assert nid is not None, "projection onto the edge must succeed"
    assert d <= 0.003
    lon, lat, _ = g.nodes[nid]
    assert abs(lat - 18.0) < 1e-6, "snapped node must sit exactly on the line"
    assert abs(lon - 100.050) < 1e-3
    assert len(g.nodes) == before_nodes + 1, "mid-edge projection must split the edge"
    # The graph must now route THROUGH the new node
    outs = [v for v, _ in g.adj.get(nid, [])]
    ins = [a for a, outs2 in g.adj.items() for v, _ in outs2 if v == nid]
    assert outs and ins, "split node must have in- and out-edges"


# ---------------------------------------------------------------------------
# 12. River-aware D8 stop: overland trace stops at the river mask
# ---------------------------------------------------------------------------
def test_river_stop_stops_d8():
    from scripts.modules.graph_topology import RIVER_STOP
    H, W = 30, 10
    fdir = np.zeros((H, W), dtype=np.uint8)
    fdir[:, :] = 4  # everything flows south
    transform = Affine(RES, 0, 100.0, 0, -RES, 18.3)

    river_mask = np.zeros((H, W), dtype=bool)
    river_mask[10, :] = True  # river across row 10

    coords, stop_data, cells = trace_downstream_path(
        0, 5, fdir, transform, crs=None, river_mask=river_mask, max_steps=100
    )
    assert stop_data == RIVER_STOP, f"trace must stop on the river, got {stop_data!r}"
    assert cells[-1] == (10, 5), f"must stop at the first river cell, got {cells[-1]}"
    assert len(cells) == 11, "must stop AT the river, not cross it"

    # Without the mask the same trace runs to the grid edge / step limit
    coords2, stop_data2, cells2 = trace_downstream_path(
        0, 5, fdir, transform, crs=None, max_steps=100
    )
    assert stop_data2 != RIVER_STOP
    assert len(cells2) > len(cells)

    # Starting ON the river must still trace away (start cell exempt)
    coords3, stop_data3, cells3 = trace_downstream_path(
        10, 5, fdir, transform, crs=None, river_mask=river_mask, max_steps=100
    )
    assert stop_data3 is None and len(cells3) > 1


# ---------------------------------------------------------------------------
# 13. River-first cascade: rain runoff enters the river instead of the distant gauge
# ---------------------------------------------------------------------------
def test_river_first_cascade_with_mask():
    from scripts.modules.terrain_engine import build_river_mask
    B = _build_synthetic_basin()

    river_mask = build_river_mask(B["osm"], B["transform"], out_shape=(B["H"], B["W"]))
    assert river_mask is not None

    snapped = snap_stations_to_stream(
        B["water"], B["fdir"], B["acc"], B["transform"],
        osm_waterways_geojson=B["osm"], crs=None
    )
    geojson, gauge_relations, rain_relations = build_flow_paths_and_relations(
        snapped, B["rain"], B["fdir"], B["acc"], B["dem"], B["transform"],
        osm_waterways_geojson=B["osm"], crs=None,
        min_flow_km=1.0, cascade_max_km=60.0, branch_min_acc=500,
        include_branches=True, branch_min_km=1.0,
        river_mask=river_mask
    )

    rain_pairs = {(r.get("from_station_id"), r.get("to_station_id")) for r in rain_relations}
    # River-first: R1 hits the river mask long before W400's footprint, enters the
    # river, and still reaches the same gauge chain via the backbone cascade.
    assert ("R1", "W400") in rain_pairs, f"rain must reach W400, got {rain_pairs}"
    assert ("R1", "W600") in rain_pairs, f"cascade must reach W600, got {rain_pairs}"

    # The overland (segment 0) part must stop at the river ENTRY, not run overland to
    # the distant gauge: its 'distance_km' is the overland-only length (~5km to the
    # main channel here), and the channel part is reported separately.
    seg0 = [f for f in geojson["features"]
            if f["properties"].get("from_station_id") == "R1"
            and f["properties"].get("cascade_segment") == 0]
    assert seg0, "expected a segment-0 feature for R1"
    p0 = seg0[0]["properties"]
    assert "channel_distance_km" in p0, f"river-entry (Case 2) feature expected: {p0}"
    assert p0["distance_km"] < 10.0, \
        f"overland part must stop at the river entry, got {p0['distance_km']} km"
    assert p0["total_distance_km"] > p0["distance_km"], "channel part must add distance"


def test_branch_cap_and_dedupe():
    """G1/G2: per-station cap (longest kept) + cross-station geometry dedupe."""
    from collections import Counter
    B = _build_synthetic_basin()
    col = lambda r: _channel_col(r)

    # Two stations with overlapping upstream networks; both enter the channel at row 200
    seeds = {
        "RA": [(r, col(r)) for r in range(200, 320)],
        "RB": [(r, col(r)) for r in range(200, 325)],
    }
    # cap = 1 -> at most one branch per station
    feats, truncated = extract_station_drainage_branches(
        seeds, B["fdir"], B["acc"], B["transform"], crs=None,
        min_branch_acc=500, min_length_km=0.2, max_branches_per_station=1
    )
    assert not truncated
    per = Counter(f["properties"]["from_station_id"] for f in feats)
    assert all(v <= 1 for v in per.values()), f"cap violated: {per}"
    # RA/RB walk the identical channel with the same entry cell -> deduped to ONE feature
    assert len(feats) == 1, f"expected 1 deduped feature, got {len(feats)}"
    assert feats[0]["properties"]["from_station_id"] == "RA"
    assert feats[0]["properties"].get("shared_with") == ["RB"]
    assert feats[0]["id"].startswith("branch_RA_")


# ---------------------------------------------------------------------------
# 14. Basin-boundary clipping: flow paths cut to the polygon, osm_river untouched
# ---------------------------------------------------------------------------
def test_basin_boundary_clipping():
    B = _build_synthetic_basin()

    # Boundary polygon covering only the NORTHERN half of the synthetic basin
    cut_lat = B["y0"] - 0.045  # ~row 450
    poly = {"type": "Feature", "properties": {}, "geometry": {"type": "Polygon", "coordinates": [[
        [B["x0"] - 0.01, cut_lat],
        [B["x0"] + 0.02, cut_lat],
        [B["x0"] + 0.02, B["y0"] + 0.01],
        [B["x0"] - 0.01, B["y0"] + 0.01],
        [B["x0"] - 0.01, cut_lat],
    ]]}}

    snapped = snap_stations_to_stream(
        B["water"], B["fdir"], B["acc"], B["transform"],
        osm_waterways_geojson=B["osm"], crs=None
    )
    geojson, _gr, _rr = build_flow_paths_and_relations(
        snapped, B["rain"], B["fdir"], B["acc"], B["dem"], B["transform"],
        osm_waterways_geojson=B["osm"], crs=None,
        min_flow_km=1.0, cascade_max_km=60.0, branch_min_acc=500,
        include_branches=True, branch_min_km=1.0,
        basin_boundary_geojson=poly, clip_to_basin=True
    )

    feats = geojson["features"]
    assert feats, "features inside the boundary must survive"

    # v2 (G2): the osm_river display layer must NEVER be re-cut — it passes through
    # exactly as it was cropped at fetch time (it may legitimately extend south).
    osm_feats = [f for f in feats if f["properties"].get("feature_type") == "osm_river"]
    assert osm_feats, "osm_river layer must survive the clip stage untouched"
    assert all(not f["properties"].get("basin_clipped") for f in osm_feats), \
        "osm_river must never carry basin_clipped (G2)"
    assert any(f["geometry"]["coordinates"][-1][1] < cut_lat for f in osm_feats), \
        "osm_river geometry must be byte-identical to the crop-set (extends past the boundary)"

    # v2 (G3): every NON-osm_river line must be cut to the polygon (concave clip),
    # and the W400->W600 gauge path crosses cut_lat -> at least one trimmed flowpath.
    flow_feats = [f for f in feats if f["properties"].get("feature_type") != "osm_river"]
    assert flow_feats
    for feat in flow_feats:
        for lon, lat in feat["geometry"]["coordinates"]:
            assert lat >= cut_lat - 1e-9, \
                f"{feat['id']} has coordinate outside the basin polygon: ({lon}, {lat})"
    trimmed = [f for f in flow_feats if f["properties"].get("basin_clipped")]
    assert trimmed, "expected at least one flow feature trimmed by the basin clip"

    # Filter report (F1) must be embedded in the output `_meta`
    meta = geojson.get("_meta") or {}
    clip_stats = (meta.get("filters") or {}).get("basin_clip") or {}
    assert clip_stats.get("osm_river", {}).get("clipped") == 0, "osm_river clip counter must be 0"
    assert any(v.get("clipped", 0) > 0 for k, v in clip_stats.items() if k != "osm_river"), \
        "at least one flow layer must report clipped features in the filter report"

    # With the FULL basin polygon nothing is dropped or trimmed
    full_poly = {"type": "Feature", "properties": {}, "geometry": {"type": "Polygon", "coordinates": [[
        [B["x0"] - 0.01, B["y0"] - B["H"] * RES - 0.01],
        [B["x0"] + 0.02, B["y0"] - B["H"] * RES - 0.01],
        [B["x0"] + 0.02, B["y0"] + 0.01],
        [B["x0"] - 0.01, B["y0"] + 0.01],
        [B["x0"] - 0.01, B["y0"] - B["H"] * RES - 0.01],
    ]]}}
    geojson2, _a, _b = build_flow_paths_and_relations(
        snapped, B["rain"], B["fdir"], B["acc"], B["dem"], B["transform"],
        osm_waterways_geojson=B["osm"], crs=None,
        min_flow_km=1.0, cascade_max_km=60.0, branch_min_acc=500,
        include_branches=True, branch_min_km=1.0,
        basin_boundary_geojson=full_poly, clip_to_basin=True
    )
    assert not any(f["properties"].get("basin_clipped") for f in geojson2["features"])
    assert len(geojson2["features"]) >= len(feats), "full boundary must keep all features"


# ---------------------------------------------------------------------------
# 15. (v2/Phase 4) Candidate ranking: main river beats a nearby island stream
# ---------------------------------------------------------------------------
def test_ranked_snap_prefers_main_river():
    g = DirectedRiverGraph(snap_tolerance_deg=1e-4)

    # Main river flowing south (elevation falls with decreasing latitude)
    def elev_fn(lon, lat):
        return 200.0 + (lat - 18.0) * 2000.0

    main_coords = [[100.000, 18.100], [100.000, 18.000]]
    g.add_river_segment(main_coords, sample_elev_fn=elev_fn,
                        river_name="main", waterway_class="river", osm_id="318800000")

    # Topology island: short stream fragment ~100m east of the main river,
    # connected to nothing and flowing nowhere.
    island_coords = [[100.0010, 18.0500], [100.0015, 18.0500]]
    g.add_river_segment(island_coords, sample_elev_fn=elev_fn,
                        river_name="island", waterway_class="stream", osm_id="318801683")

    g.finalize_connectivity()
    g.build_spatial_index()

    # Gauge at the main river's downstream end -> main river is gauge-connected
    south_node, _ = g.find_nearest_node(100.000, 18.000, max_dist_deg=0.01)
    assert south_node is not None
    g.compute_gauge_reachability({south_node})

    # Query point sits ON the island stream (~0m) and ~107m from the main river.
    # "Nearest wins" would attach the island; ranking must pick the main river.
    q = (100.0010, 18.0500)
    nid, dist, meta = g.snap_point_to_graph_ranked(q[0], q[1], max_dist_deg=0.003)
    assert nid is not None, "ranked snap must find the main river"
    assert meta is not None
    assert meta["attach_osm_id"] == "318800000", \
        f"must attach the main river, got {meta['attach_osm_id']}"
    assert meta["attach_quality"] == "ok"
    assert meta["attach_class"] == "river"
    assert meta["attach_distance_m"] <= 300.0

    # The attach node must lie exactly ON the selected main-river line (projection)
    lon, lat, _ = g.nodes[nid]
    assert abs(lon - 100.000) < 1e-6, "attach node must be projected onto the main river line"
    assert abs(lat - 18.0500) < 1e-3


def test_ranked_snap_degraded_on_island_only():
    g = DirectedRiverGraph(snap_tolerance_deg=1e-4)

    def elev_fn(lon, lat):
        return 200.0 + (lat - 18.0) * 2000.0

    g.add_river_segment([[100.0010, 18.0500], [100.0015, 18.0500]],
                        sample_elev_fn=elev_fn, river_name="island",
                        waterway_class="stream", osm_id="318801683")
    g.finalize_connectivity()
    g.build_spatial_index()

    # NO gauge anywhere on the backbone -> every attachment is a topology island
    g.compute_gauge_reachability(set())
    nid, dist, meta = g.snap_point_to_graph_ranked(100.0012, 18.0498, max_dist_deg=0.003)
    assert nid is not None
    assert meta is not None and meta["attach_quality"] == "degraded", \
        f"island-only attach must be tagged degraded, got {meta}"


# ---------------------------------------------------------------------------
# 16. (v2/Phase 1) Boundary missing -> generate must fail fast (no rectangle)
# ---------------------------------------------------------------------------
def test_boundary_missing_fails_fast():
    import tempfile
    from scripts.generate_flow_paths import resolve_basin_boundary_or_fail

    with tempfile.TemporaryDirectory() as td:
        missing = os.path.join(td, "x_boundary.geojson")
        try:
            resolve_basin_boundary_or_fail("x", missing)
            raise AssertionError("missing boundary must raise SystemExit")
        except SystemExit as ex:
            msg = str(ex)
            assert "fetch_basin_gis.py" in msg, "error must tell the user how to fix it"

        # A boundary that exists but is not a polygon must also fail
        with open(missing, 'w', encoding='utf-8') as f:
            f.write('{"type": "FeatureCollection", "features": [{"type": "Feature", '
                    '"properties": {}, "geometry": {"type": "Point", "coordinates": [100, 18]}}]}')
        try:
            resolve_basin_boundary_or_fail("x", missing)
            raise AssertionError("non-polygon boundary must raise SystemExit")
        except SystemExit:
            pass


# ---------------------------------------------------------------------------
# 17. (v2/Phase 2) OSM features are cropped to the basin polygon at fetch time
# ---------------------------------------------------------------------------
def test_crop_geojson_to_basin():
    from scripts.fetch_basin_gis import crop_geojson_to_basin, _boundary_fingerprint

    basin = {"type": "FeatureCollection", "features": [{"type": "Feature", "properties": {},
              "geometry": {"type": "Polygon", "coordinates": [[
                  [100.00, 18.00], [100.10, 18.00], [100.10, 18.10], [100.00, 18.10], [100.00, 18.00]]]}}]}

    fc = {"type": "FeatureCollection", "features": [
        {"type": "Feature", "id": "inside",
         "properties": {"osm_id": 1},
         "geometry": {"type": "LineString", "coordinates": [[100.02, 18.02], [100.04, 18.04]]}},
        {"type": "Feature", "id": "outside",
         "properties": {"osm_id": 2},
         "geometry": {"type": "LineString", "coordinates": [[100.20, 18.20], [100.22, 18.22]]}},
        {"type": "Feature", "id": "crossing",
         "properties": {"osm_id": 3},
         "geometry": {"type": "LineString", "coordinates": [[100.05, 18.05], [100.15, 18.15]]}},
    ]}

    cropped, stats = crop_geojson_to_basin(fc, basin, buffer_m=0.0, label="test")
    assert stats["n_in"] == 3
    assert stats["dropped_outside"] == 1, "line fully outside the basin must be dropped"
    assert stats["clipped"] == 1, "crossing line must be clipped to the basin"
    assert stats["n_out"] == 2

    by_id = {f["id"]: f for f in cropped["features"]}
    assert "outside" not in by_id
    cross = by_id["crossing"]["geometry"]["coordinates"]
    for lon, lat in cross:
        assert 100.00 - 1e-9 <= lon <= 100.10 + 1e-9, f"clipped point outside basin: {lon}"
        assert 18.00 - 1e-9 <= lat <= 18.10 + 1e-9, f"clipped point outside basin: {lat}"
    # crop-set identity: inside feature must be untouched
    assert by_id["inside"]["geometry"]["coordinates"] == [[100.02, 18.02], [100.04, 18.04]]

    meta = cropped.get("_meta") or {}
    assert meta.get("crop_polygon") == _boundary_fingerprint(basin), "crop fingerprint must be recorded"
    assert meta.get("crop_stats", {}).get("n_out") == 2


# ---------------------------------------------------------------------------
# 18. (round 5 / Step 1) boundary cache validation — coarse boxes are rejected
# ---------------------------------------------------------------------------
def _detailed_polygon_geojson(n_verts=60, source="ThaiWater (HII Official)"):
    """(Multi)Polygon with n_verts vertices around a rough basin-ish ring."""
    import math as _m
    cx, cy, rx, ry = 100.7, 18.0, 0.9, 1.7
    ring = []
    for i in range(n_verts):
        ang = 2.0 * _m.pi * i / n_verts
        # deterministic wobble so the ring is concave, not a circle-fitting box
        w = 1.0 + 0.25 * _m.sin(5 * ang)
        ring.append([round(cx + rx * w * _m.cos(ang), 6),
                     round(cy + ry * w * _m.sin(ang), 6)])
    ring.append(ring[0])
    return {"type": "FeatureCollection", "features": [{"type": "Feature",
            "properties": {"basin_slug": "x", "source": source},
            "geometry": {"type": "Polygon", "coordinates": [ring]}}]}


def _box_boundary_geojson(source="Station Bounding Box Fallback"):
    return {"type": "FeatureCollection", "features": [{"type": "Feature",
            "properties": {"basin_slug": "x", "source": source},
            "geometry": {"type": "Polygon", "coordinates": [[
                [99.5, 15.5], [101.5, 15.5], [101.5, 19.9], [99.5, 19.9], [99.5, 15.5]]]}}]}


def test_boundary_cache_rejects_coarse_box():
    import tempfile
    from scripts.fetch_basin_gis import load_valid_boundary

    with tempfile.TemporaryDirectory() as td:
        # 1. the classic round-4 rectangle with the fallback source label -> REJECT
        p = os.path.join(td, "x_boundary.geojson")
        with open(p, 'w', encoding='utf-8') as f:
            json.dump(_box_boundary_geojson(), f)
        assert load_valid_boundary("x", p) is None, "rectangular fallback cache must be rejected"

        # 2. a box even with an innocent source label (< 50 vertices) -> REJECT
        with open(p, 'w', encoding='utf-8') as f:
            json.dump(_box_boundary_geojson(source="ThaiWater (HII Official)"), f)
        assert load_valid_boundary("x", p) is None, "coarse < 50-vertex cache must be rejected"

        # 3. detailed polygon with a legit source -> ACCEPT
        with open(p, 'w', encoding='utf-8') as f:
            json.dump(_detailed_polygon_geojson(), f)
        data = load_valid_boundary("x", p)
        assert data is not None, "a real detailed basin polygon must be accepted"

        # 4. missing file -> None (no crash)
        assert load_valid_boundary("x", os.path.join(td, "nope.geojson")) is None


def test_generate_fails_on_coarse_boundary_cache():
    import tempfile
    from scripts.generate_flow_paths import resolve_basin_boundary_or_fail

    with tempfile.TemporaryDirectory() as td:
        # existing coarse rectangle -> SystemExit with a DELETE hint
        p = os.path.join(td, "x_boundary.geojson")
        with open(p, 'w', encoding='utf-8') as f:
            json.dump(_box_boundary_geojson(), f)
        try:
            resolve_basin_boundary_or_fail("x", p)
            raise AssertionError("coarse boundary cache must raise SystemExit")
        except SystemExit as ex:
            assert "DELETE" in str(ex), "error must tell the user to delete the bad cache"

        # detailed polygon -> accepted
        with open(p, 'w', encoding='utf-8') as f:
            json.dump(_detailed_polygon_geojson(), f)
        data = resolve_basin_boundary_or_fail("x", p)
        assert data.get("features"), "valid boundary must pass through"


def main():
    tests = [
        test_graph_direction_and_connectivity,
        test_reconstruct_node_path_and_stitch,
        test_simplify_splits_at_jump,
        test_merge_coordinates,
        test_burn_polygons,
        test_end_to_end_synthetic_basin,
        test_branch_min_km_default,
        test_branch_cap_and_dedupe,
        test_noding_crossing_ways_connect,
        test_endpoint_weld_connects_gapped_ways,
        test_snap_point_to_graph_splits_edge,
        test_river_stop_stops_d8,
        test_river_first_cascade_with_mask,
        test_basin_boundary_clipping,
        test_ranked_snap_prefers_main_river,
        test_ranked_snap_degraded_on_island_only,
        test_boundary_missing_fails_fast,
        test_crop_geojson_to_basin,
        test_boundary_cache_rejects_coarse_box,
        test_generate_fails_on_coarse_boundary_cache,
    ]
    for t in tests:
        t()
        print(f"PASS  {t.__name__}")
    print("\nALL TESTS PASSED")


if __name__ == "__main__":
    main()
