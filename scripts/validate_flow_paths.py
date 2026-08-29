#!/usr/bin/env python3
"""
Flow Paths GeoJSON Validator (C1)
Scans flow_paths.geojson for the known defect classes:
  1. Straight-line jumps > threshold (default 1km) — including mid-path jumps
     where the line continues after the gap (stitched disconnected pieces)
  2. Truncated paths (jump at the end / suspiciously few points)
  3. Tiny stub segments
  4. River crossings without following ("orphan crossings") — flow paths that
     geometrically cross an OSM river centerline without running alongside it,
     i.e. lines that cut across rivers instead of merging into them
  5. Feature/relation inventory per type
  6. (v2) Layer integrity & Filter Matrix checks:
     - osm_river must NEVER carry basin_clipped (G2 — layer passes through untouched)
     - flow-path endpoints must sit on a gauge OR on an OSM river (<= 30m) (G4)
     - attach metadata must reference a real river within tolerance
     - output `_meta` must carry a filter report; OSM source must not be station_bbox
     - (with --boundary) no flow/branch point may lie outside the basin polygon

Pure standard library — no geo dependencies required (shapely is optional and only
used for the --boundary point-in-polygon check).

Usage:
  python scripts/validate_flow_paths.py --geojson dataset/yom/processed/flow_paths.geojson
  python scripts/validate_flow_paths.py --geojson flow.geojson --max-jump-km 0.5 --min-length-km 1.0
  python scripts/validate_flow_paths.py --geojson flow.geojson --boundary dataset/yom/gis/yom_boundary.geojson
Exit code 0 = PASS, 1 = issues found.
"""

import argparse
import gzip
import json
import math
import sys
from collections import Counter, defaultdict


def load_geojson_any(path):
    """Loads a .geojson or .geojson.gz file (sniffs gzip magic bytes)."""
    with open(path, 'rb') as fh:
        magic = fh.read(2)
    if magic == b'\x1f\x8b':
        with gzip.open(path, 'rt', encoding='utf-8') as f:
            return json.load(f)
    with open(path, 'r', encoding='utf-8') as f:
        return json.load(f)


def seg_km(a, b):
    return math.hypot((b[0] - a[0]) * 111.32 * 0.95, (b[1] - a[1]) * 110.54)


# ---------------------------------------------------------------------------
# River crossing analysis (orphan crossings = crossing a river without following)
# ---------------------------------------------------------------------------

CELL = 0.01  # spatial index cell size in degrees (~1.1 km)


def _cells_for_bbox(x0, y0, x1, y1):
    cx0, cx1 = int(math.floor(min(x0, x1) / CELL)), int(math.floor(max(x0, x1) / CELL))
    cy0, cy1 = int(math.floor(min(y0, y1) / CELL)), int(math.floor(max(y0, y1) / CELL))
    for cx in range(cx0, cx1 + 1):
        for cy in range(cy0, cy1 + 1):
            yield (cx, cy)


def _orient(ax, ay, bx, by, px, py):
    v = (bx - ax) * (py - ay) - (by - ay) * (px - ax)
    if v > 1e-12:
        return 1
    if v < -1e-12:
        return -1
    return 0


def _segments_properly_cross(a, b, c, d):
    """True iff open segments ab and cd properly intersect (no collinear/touch)."""
    o1 = _orient(a[0], a[1], b[0], b[1], c[0], c[1])
    o2 = _orient(a[0], a[1], b[0], b[1], d[0], d[1])
    o3 = _orient(c[0], c[1], d[0], d[1], a[0], a[1])
    o4 = _orient(c[0], c[1], d[0], d[1], b[0], b[1])
    return o1 * o2 < 0 and o3 * o4 < 0


def _pt_seg_dist_km(px, py, ax, ay, bx, by):
    dx, dy = bx - ax, by - ay
    l2 = dx * dx + dy * dy
    if l2 <= 0:
        return seg_km((px, py), (ax, ay))
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / l2))
    cx, cy = ax + t * dx, ay + t * dy
    return seg_km((px, py), (cx, cy))


def check_river_crossings(geojson, follow_window=8, follow_dist_km=0.08):
    """
    For every flow-path feature, finds proper crossings with osm_river centerlines.
    A crossing counts as "followed" when the path keeps within follow_dist_km of that
    river around the crossing (the path runs ALONG it); otherwise it is an orphan
    crossing (the line cuts across a river it should have merged into).
    """
    features = geojson.get("features", [])
    river_segs = []       # (ax, ay, bx, by)
    seg_river = []        # river feature index per segment
    grid = defaultdict(list)

    flow_types = {"gauge_to_gauge_flowpath", "rainfall_to_gauge_flowpath", "rainfall_drainage_branch"}

    river_count = 0
    for feat in features:
        props = feat.get("properties", {})
        if props.get("feature_type") != "osm_river":
            continue
        geom = feat.get("geometry", {})
        coords = geom.get("coordinates", [])
        if geom.get("type") == "MultiLineString":
            coords = [pt for line in coords for pt in line]
        r_idx = river_count
        river_count += 1
        for i in range(len(coords) - 1):
            sid = len(river_segs)
            river_segs.append((coords[i][0], coords[i][1], coords[i + 1][0], coords[i + 1][1]))
            seg_river.append(r_idx)
            for cell in _cells_for_bbox(coords[i][0], coords[i][1], coords[i + 1][0], coords[i + 1][1]):
                grid[cell].append(sid)

    report = {
        "flowpaths_checked": 0,
        "features_with_crossings": 0,
        "features_with_orphans": 0,
        "total_crossings": 0,
        "total_orphans": 0,
        "examples": [],
    }
    if not river_segs:
        return report

    for feat in features:
        props = feat.get("properties", {})
        if props.get("feature_type") not in flow_types:
            continue
        geom = feat.get("geometry", {})
        coords = geom.get("coordinates", [])
        if geom.get("type") == "MultiLineString":
            coords = [pt for line in coords for pt in line]
        if len(coords) < 2:
            continue
        report["flowpaths_checked"] += 1

        crossings = []  # (path_idx, seg_id)
        seen_pairs = set()
        for i in range(len(coords) - 1):
            a, b = coords[i], coords[i + 1]
            cand = set()
            for cell in _cells_for_bbox(a[0], a[1], b[0], b[1]):
                cand.update(grid.get(cell, ()))
            for sid in cand:
                pair = (i, sid)
                if pair in seen_pairs:
                    continue
                seen_pairs.add(pair)
                s = river_segs[sid]
                if _segments_properly_cross(a, b, (s[0], s[1]), (s[2], s[3])):
                    crossings.append(pair)
        if not crossings:
            continue
        report["features_with_crossings"] += 1
        report["total_crossings"] += len(crossings)

        orphans = []
        for (i, sid) in crossings:
            r_idx = seg_river[sid]
            followed = False
            for j in range(max(0, i - follow_window), min(len(coords), i + follow_window + 1)):
                pt = coords[j]
                # distance from pt to that river's segments near pt
                px, py = pt[0], pt[1]
                cx, cy = int(math.floor(px / CELL)), int(math.floor(py / CELL))
                best = float('inf')
                for dx in (-1, 0, 1):
                    for dy in (-1, 0, 1):
                        for sid2 in grid.get((cx + dx, cy + dy), ()):
                            if seg_river[sid2] != r_idx:
                                continue
                            s = river_segs[sid2]
                            d = _pt_seg_dist_km(px, py, s[0], s[1], s[2], s[3])
                            if d < best:
                                best = d
                if best <= follow_dist_km:
                    followed = True
                    break
            if not followed:
                orphans.append((i, sid))
        report["total_orphans"] += len(orphans)
        if orphans:
            report["features_with_orphans"] += 1
            if len(report["examples"]) < 10:
                report["examples"].append({
                    "feature_id": feat.get("id", ""),
                    "feature_type": props.get("feature_type", ""),
                    "from_station_id": props.get("from_station_id", ""),
                    "to_station_id": props.get("to_station_id", ""),
                    "orphans": len(orphans),
                    "crossings": len(crossings),
                })
    return report


def validate(geojson, max_jump_km, min_length_km):
    features = geojson.get("features", [])
    report = {
        "total_features": len(features),
        "by_type": Counter(),
        "points_by_type": Counter(),
        "jumps": [],
        "mid_path_jumps": 0,
        "end_jumps": 0,
        "axis_jumps": 0,
        "axis_jumps_transit": 0,
        "stubs": 0,
        "degenerate": 0,
        "max_jump_km": 0.0,
    }

    for feat in features:
        props = feat.get("properties", {})
        ftype = props.get("feature_type", "unknown")
        report["by_type"][ftype] += 1
        coords = feat.get("geometry", {}).get("coordinates", [])
        report["points_by_type"][ftype] += len(coords)
        if len(coords) < 2:
            report["degenerate"] += 1
            continue
        is_transit = bool(props.get("reservoir_transit"))

        for i in range(len(coords) - 1):
            d = seg_km(coords[i], coords[i + 1])
            if d < 0.005:
                report["stubs"] += 1
            if d > max_jump_km:
                report["max_jump_km"] = max(report["max_jump_km"], d)
                kind = "mid_path" if i < len(coords) - 2 else "at_end"
                if kind == "mid_path":
                    report["mid_path_jumps"] += 1
                else:
                    report["end_jumps"] += 1
                # Round 6: classify axis-aligned teleports (N-S / E-W straight walls).
                # These are D8 flat-resolution trenches, NOT real rivers. A tagged
                # reservoir_transit hop is an honest straight crossing and is exempt.
                dlon_m = abs(coords[i + 1][0] - coords[i][0]) * 111.32 * 0.95
                dlat_m = abs(coords[i + 1][1] - coords[i][1]) * 110.54
                axis = ""
                if min(dlon_m, dlat_m) <= 50.0:
                    axis = "N-S" if dlon_m < dlat_m else "E-W"
                    if is_transit:
                        report["axis_jumps_transit"] += 1
                    else:
                        report["axis_jumps"] += 1
                report["jumps"].append({
                    "feature_id": feat.get("id", ""),
                    "feature_type": ftype,
                    "from_station_id": props.get("from_station_id", ""),
                    "to_station_id": props.get("to_station_id", ""),
                    "jump_km": round(d, 2),
                    "at_index": i,
                    "kind": kind,
                    "axis": axis,
                    "reservoir_transit": is_transit,
                })

        total_len = sum(seg_km(coords[i], coords[i + 1]) for i in range(len(coords) - 1))
        if 0 < total_len < min_length_km:
            report.setdefault("below_min_length", []).append({
                "feature_id": feat.get("id", ""),
                "length_km": round(total_len, 3),
            })

    report["jumps"].sort(key=lambda j: -j["jump_km"])
    return report


# ---------------------------------------------------------------------------
# v2: layer integrity / Filter Matrix / endpoint attachment checks (G2-G5, F1-F4)
# ---------------------------------------------------------------------------

ATTACH_TOL_KM = 0.030  # flow-path endpoint / attach point must sit within 30m of a river

FLOWPATH_TYPES = {"gauge_to_gauge_flowpath", "rainfall_to_gauge_flowpath"}


def _build_river_segment_grid(geojson):
    """
    Buckets every osm_river segment into a spatial hash grid (O(1) average lookup).
    Returns (segments, grid) where segments[i] = (x0, y0, x1, y1).
    """
    segments = []
    grid = defaultdict(list)
    for feat in geojson.get("features", []):
        if feat.get("properties", {}).get("feature_type") != "osm_river":
            continue
        geom = feat.get("geometry", {})
        coords = geom.get("coordinates", [])
        if geom.get("type") == "MultiLineString":
            coords = [pt for line in coords for pt in line]
        for i in range(len(coords) - 1):
            sid = len(segments)
            segments.append((coords[i][0], coords[i][1], coords[i + 1][0], coords[i + 1][1]))
            for cell in _cells_for_bbox(coords[i][0], coords[i][1], coords[i + 1][0], coords[i + 1][1]):
                grid[cell].append(sid)
    return segments, grid


def _min_dist_to_river_km(px, py, segments, grid):
    """Nearest distance (km) from point (px, py) to any osm_river segment via the grid."""
    cx, cy = int(math.floor(px / CELL)), int(math.floor(py / CELL))
    best = float('inf')
    for dx in (-1, 0, 1):
        for dy in (-1, 0, 1):
            for sid in grid.get((cx + dx, cy + dy), ()):
                s = segments[sid]
                d = _pt_seg_dist_km(px, py, s[0], s[1], s[2], s[3])
                if d < best:
                    best = d
    return best


def check_layer_integrity(geojson):
    """
    v2 checks (G2/G4/F4):
      1. osm_river features must never carry basin_clipped
      2. every flow-path endpoint sits at a gauge OR on an OSM river (<= 30m),
         or carries attach metadata; floating endpoints = FAIL
      3. attach metadata must point at a real river line within 30m
      4. `_meta` must contain the per-layer filter report; OSM source must not be
         station_bbox (rectangular fallback = FAIL)
    """
    features = geojson.get("features", [])
    report = {
        "osm_river_clipped": [],
        "floating_endpoints": [],
        "bad_attach_points": [],
        "degraded_attaches": 0,
        "meta": {"has_meta": bool(geojson.get("_meta")),
                 "osm_source_label": None,
                 "filter_report_ok": False,
                 "osm_audit_ok": False,
                 "jump_split": None},
        "checked_flowpaths": 0,
    }

    # 1. osm_river layer integrity (G2)
    for feat in features:
        props = feat.get("properties", {})
        if props.get("feature_type") == "osm_river" and props.get("basin_clipped"):
            report["osm_river_clipped"].append(feat.get("id", ""))

    # 2/3. endpoint attachment (G4)
    segments, grid = _build_river_segment_grid(geojson)
    for feat in features:
        props = feat.get("properties", {})
        if props.get("feature_type") not in FLOWPATH_TYPES:
            continue
        geom = feat.get("geometry", {})
        coords = geom.get("coordinates", [])
        if geom.get("type") == "MultiLineString":
            coords = [pt for line in coords for pt in line]
        if len(coords) < 2:
            continue
        report["checked_flowpaths"] += 1

        if props.get("attach_quality") == "degraded":
            report["degraded_attaches"] += 1
        attach_osm = str(props.get("attach_osm_id") or "")
        # Round 6: an honest straight reservoir-transit hop is exempt from the
        # "must sit on an osm_river line" checks (it spans open water by design).
        is_transit = bool(props.get("reservoir_transit"))

        # Pure overland paths (never contacted a river) are exempt — their endpoints
        # are terrain pits by design and the routing field records that honestly.
        if str(props.get("routing", "")).startswith("overland") or is_transit:
            continue

        end = coords[-1]
        at_gauge = bool(str(props.get("to_station_id") or "").strip())
        on_river = (attach_osm != "") or (_min_dist_to_river_km(end[0], end[1], segments, grid) <= ATTACH_TOL_KM)
        if not (at_gauge or on_river):
            report["floating_endpoints"].append({
                "feature_id": feat.get("id", ""),
                "to_station_id": props.get("to_station_id", ""),
                "endpoint": [round(end[0], 5), round(end[1], 5)],
            })

        # 3. attach metadata must reference a real river location (<= 30m)
        if attach_osm and not at_gauge:
            d = _min_dist_to_river_km(end[0], end[1], segments, grid)
            if d > ATTACH_TOL_KM:
                report["bad_attach_points"].append({
                    "feature_id": feat.get("id", ""),
                    "attach_osm_id": attach_osm,
                    "distance_km": round(d, 3),
                })

    # 4. `_meta` filter report (F1/F4 + item 17: station_bbox = FAIL)
    meta = geojson.get("_meta") or {}
    report["meta"]["osm_source_label"] = meta.get("osm_source_label") or meta.get("source")
    filters = meta.get("filters") or {}
    clip_stats = filters.get("basin_clip") or {}
    needed = {"gauge_to_gauge_flowpath", "rainfall_to_gauge_flowpath",
              "rainfall_drainage_branch", "osm_river"}
    present_types = {f.get("properties", {}).get("feature_type") for f in features}
    report["meta"]["filter_report_ok"] = all(
        isinstance(clip_stats.get(t), dict) and clip_stats[t].get("n_out", 0) >= 0
        for t in needed if t in present_types
    )
    # Round 6 (Phase C/D): OSM layer audit counters must be present — without them
    # it is impossible to verify that no OSM way was silently deleted.
    jump_split = filters.get("osm_way_jump_split")
    report["meta"]["jump_split"] = jump_split
    report["meta"]["osm_audit_ok"] = isinstance(jump_split, dict) and "n_ways_in" in jump_split
    return report


def check_points_inside_basin(geojson, boundary_path):
    """
    v2 (G3): with a basin boundary provided, no flow-path / branch / osm_river point
    may lie outside the basin polygon. Uses shapely (optional) with a prepared polygon.
    Returns (outside_count, examples, skipped_reason_or_None).
    """
    try:
        from shapely.geometry import shape
        from shapely.prepared import prep
    except ImportError:
        return 0, [], "shapely not installed — point-in-polygon check skipped"

    try:
        boundary = load_geojson_any(boundary_path)
        feats = boundary.get("features", [])
        if not feats:
            return 0, [], "boundary file has no features"
        poly = shape(feats[0].get("geometry"))
        prepared = prep(poly.buffer(0.0005))  # ~55m tolerance at the edge
    except Exception as ex:
        return 0, [], f"cannot read boundary ({ex}) — point-in-polygon check skipped"

    outside = 0
    examples = []
    for feat in geojson.get("features", []):
        geom = feat.get("geometry", {})
        coords = geom.get("coordinates", [])
        if geom.get("type") == "MultiLineString":
            coords = [pt for line in coords for pt in line]
        for (x, y) in coords:
            if not prepared.contains(_pt(x, y)):
                outside += 1
                if len(examples) < 10:
                    examples.append({"feature_id": feat.get("id", ""),
                                     "point": [round(x, 5), round(y, 5)]})
                break  # one report per feature is enough
    return outside, examples, None


def _pt(x, y):
    from shapely.geometry import Point
    return Point(x, y)


def main():
    parser = argparse.ArgumentParser(description="Validate flow_paths.geojson quality")
    parser.add_argument("--geojson", required=True, help="Path to flow_paths.geojson")
    parser.add_argument("--max-jump-km", type=float, default=1.0,
                        help="Segment length above this is reported as a straight-line jump (default: 1.0)")
    parser.add_argument("--min-length-km", type=float, default=1.0,
                        help="Features shorter than this are reported (default: 1.0)")
    parser.add_argument("--top", type=int, default=20, help="How many worst jumps to print")
    parser.add_argument("--max-orphan-crossings", type=int, default=10,
                        help="FAIL when more than this many flow-path features cross OSM rivers "
                             "without following them (default: 10; 0 = fail on any)")
    parser.add_argument("--no-crossing-check", action="store_true",
                        help="Skip the river crossing analysis (faster)")
    parser.add_argument("--boundary", type=str, default=None,
                        help="Path to the basin boundary GeoJSON — enables the "
                             "point-inside-basin polygon check (v2/G3)")
    parser.add_argument("--skip-v2", action="store_true",
                        help="Skip the v2 layer-integrity / Filter Matrix checks")
    args = parser.parse_args()

    geojson = load_geojson_any(args.geojson)

    report = validate(geojson, args.max_jump_km, args.min_length_km)
    crossing_report = None
    if not args.no_crossing_check:
        print("Analyzing river crossings (orphan crossing detection)...")
        crossing_report = check_river_crossings(geojson)

    print("=" * 72)
    print("FLOW PATHS VALIDATION REPORT")
    print("=" * 72)
    print(f"File                  : {args.geojson}")
    print(f"Total features        : {report['total_features']}")
    print("By feature_type (features / points):")
    for ftype, n in sorted(report["by_type"].items()):
        print(f"    {ftype:<35s} {n:>7,} / {report['points_by_type'][ftype]:>9,}")
    print(f"Degenerate (<2 pts)   : {report['degenerate']}")
    print(f"Stub segments (<5m)   : {report['stubs']}")
    print(f"Jumps > {args.max_jump_km:g} km      : {len(report['jumps'])} "
          f"(mid_path={report['mid_path_jumps']}, at_end={report['end_jumps']})")
    print(f"Axis-aligned teleports: {report['axis_jumps']} "
          f"(+{report['axis_jumps_transit']} tagged reservoir_transit — exempt)")
    print(f"Worst jump            : {report['max_jump_km']:.2f} km")

    if report.get("below_min_length"):
        print(f"Features < {args.min_length_km:g} km   : {len(report['below_min_length'])}")

    if report["jumps"]:
        print("\nWORST JUMPS:")
        for j in report["jumps"][:args.top]:
            axis = f" [{j['axis']}]" if j.get("axis") else ""
            tr = " (transit)" if j.get("reservoir_transit") else ""
            print(f"    {j['jump_km']:>8.2f} km  [{j['kind']:<8s}]{axis}{tr}  {j['feature_id']} "
                  f"({j['feature_type']}, from={j['from_station_id']} to={j['to_station_id'] or '-'}) @ idx {j['at_index']}")

    orphan_fail = False
    if crossing_report is not None:
        cr = crossing_report
        print("\nRIVER CROSSING ANALYSIS (flow paths vs OSM river centerlines)")
        print(f"    Flow paths checked         : {cr['flowpaths_checked']:,}")
        print(f"    Features crossing rivers   : {cr['features_with_crossings']:,} "
              f"({cr['total_crossings']:,} crossings)")
        print(f"    Orphan crossings           : {cr['total_orphans']:,} "
              f"(cross a river WITHOUT following it)")
        print(f"    Features w/ orphan crossings: {cr['features_with_orphans']:,}")
        if cr["examples"]:
            print("    Worst offenders:")
            for ex in cr["examples"]:
                print(f"        {ex['feature_id']} ({ex['feature_type']}, from={ex['from_station_id']} "
                      f"to={ex['to_station_id'] or '-'}): {ex['orphans']} orphan / {ex['crossings']} crossings")
        orphan_fail = cr["features_with_orphans"] > args.max_orphan_crossings

    # ---- v2 checks (G2-G5, F1-F4) -----------------------------------------
    v2_fail_reasons = []
    if not args.skip_v2:
        v2 = check_layer_integrity(geojson)
        print("\nLAYER INTEGRITY & FILTER MATRIX (v2)")
        m = v2["meta"]
        print(f"    Output `_meta` present        : {m['has_meta']}")
        print(f"    OSM source label              : {m['osm_source_label'] or '-'}")
        print(f"    Per-layer filter report (F1)  : {'OK' if m['filter_report_ok'] else 'MISSING'}")
        print(f"    OSM jump-split audit counters : {'OK' if m['osm_audit_ok'] else 'MISSING'}")
        if isinstance(m.get("jump_split"), dict):
            js = m["jump_split"]
            print(f"        ways {js.get('n_ways_in', '-')} -> {js.get('n_ways_out', '-')}, "
                  f"dropped: {js.get('n_ways_dropped', '-')}, parts dropped: {js.get('n_parts_dropped', '-')}")
        print(f"    osm_river features w/ basin_clipped : {len(v2['osm_river_clipped'])} (must be 0 — G2)")
        print(f"    Flow paths checked (endpoints)      : {v2['checked_flowpaths']:,}")
        print(f"    Floating endpoints (> {ATTACH_TOL_KM * 1000:.0f} m from any river, not at gauge): {len(v2['floating_endpoints'])}")
        print(f"    Attach points off the referenced river: {len(v2['bad_attach_points'])}")
        print(f"    Degraded attaches (tagged)      : {v2['degraded_attaches']}")
        for ex in (v2["osm_river_clipped"][:5] + [f.get("feature_id") for f in v2["bad_attach_points"][:5]]):
            print(f"        offender: {ex}")
        for ex in v2["floating_endpoints"][:5]:
            print(f"        floating endpoint: {ex['feature_id']} at {ex['endpoint']} "
                  f"(to_station_id={ex['to_station_id'] or '-'})")

        if v2["osm_river_clipped"]:
            v2_fail_reasons.append("osm_river layer was re-clipped (G2 violation)")
        if v2["floating_endpoints"]:
            v2_fail_reasons.append("floating flow-path endpoints (G4)")
        if v2["bad_attach_points"]:
            v2_fail_reasons.append("attach points off the referenced river (G4)")
        if not m["has_meta"] or not m["filter_report_ok"]:
            v2_fail_reasons.append("missing per-layer filter report (F1/F4)")
        if not m["osm_audit_ok"]:
            v2_fail_reasons.append("missing OSM jump-split audit counters (cannot verify OSM integrity)")
        if (m["osm_source_label"] or "") == "station_bbox":
            v2_fail_reasons.append("OSM source is station_bbox — rectangular fallback is forbidden (F3/G5)")
        if report["axis_jumps"] > 0:
            v2_fail_reasons.append(
                f"{report['axis_jumps']} axis-aligned straight teleports "
                "(D8 flat trenches / untagged water crossings)")

        if args.boundary:
            outside, examples, skipped = check_points_inside_basin(geojson, args.boundary)
            if skipped:
                print(f"    Basin polygon check           : SKIPPED ({skipped})")
            else:
                print(f"    Features w/ points outside basin : {outside:,}")
                for ex in examples[:5]:
                    print(f"        outside: {ex['feature_id']} at {ex['point']}")
                if outside > 0:
                    v2_fail_reasons.append("points outside the basin polygon (G3)")

    failed = (report["mid_path_jumps"] > 0 or report["end_jumps"] > 0
              or report["degenerate"] > 0 or orphan_fail
              or bool(v2_fail_reasons))
    reasons = []
    if report["mid_path_jumps"] > 0 or report["end_jumps"] > 0:
        reasons.append("straight-line jumps")
    if report["degenerate"] > 0:
        reasons.append("degenerate features")
    if orphan_fail:
        reasons.append("orphan river crossings")
    reasons.extend(v2_fail_reasons)
    print("\n" + ("❌ FAIL — " + ", ".join(reasons) if failed
                  else "✅ PASS — no straight-line jumps above threshold, no orphan river crossings, "
                       "layer integrity & filter matrix OK"))
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
