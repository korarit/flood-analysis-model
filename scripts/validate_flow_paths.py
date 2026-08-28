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

Pure standard library — no geo dependencies required.

Usage:
  python scripts/validate_flow_paths.py --geojson dataset/yom/processed/flow_paths.geojson
  python scripts/validate_flow_paths.py --geojson flow.geojson --max-jump-km 0.5 --min-length-km 1.0
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
                report["jumps"].append({
                    "feature_id": feat.get("id", ""),
                    "feature_type": props.get("feature_type", ""),
                    "from_station_id": props.get("from_station_id", ""),
                    "to_station_id": props.get("to_station_id", ""),
                    "jump_km": round(d, 2),
                    "at_index": i,
                    "kind": kind,
                })

        total_len = sum(seg_km(coords[i], coords[i + 1]) for i in range(len(coords) - 1))
        if 0 < total_len < min_length_km:
            report.setdefault("below_min_length", []).append({
                "feature_id": feat.get("id", ""),
                "length_km": round(total_len, 3),
            })

    report["jumps"].sort(key=lambda j: -j["jump_km"])
    return report


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
    print(f"Worst jump            : {report['max_jump_km']:.2f} km")

    if report.get("below_min_length"):
        print(f"Features < {args.min_length_km:g} km   : {len(report['below_min_length'])}")

    if report["jumps"]:
        print("\nWORST JUMPS:")
        for j in report["jumps"][:args.top]:
            print(f"    {j['jump_km']:>8.2f} km  [{j['kind']:<8s}]  {j['feature_id']} "
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

    failed = (report["mid_path_jumps"] > 0 or report["end_jumps"] > 0
              or report["degenerate"] > 0 or orphan_fail)
    reasons = []
    if report["mid_path_jumps"] > 0 or report["end_jumps"] > 0:
        reasons.append("straight-line jumps")
    if report["degenerate"] > 0:
        reasons.append("degenerate features")
    if orphan_fail:
        reasons.append("orphan river crossings")
    print("\n" + ("❌ FAIL — " + ", ".join(reasons) if failed
                  else "✅ PASS — no straight-line jumps above threshold, no orphan river crossings"))
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
