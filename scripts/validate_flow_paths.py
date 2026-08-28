#!/usr/bin/env python3
"""
Flow Paths GeoJSON Validator (C1)
Scans flow_paths.geojson for the known defect classes:
  1. Straight-line jumps > threshold (default 1km) — including mid-path jumps
     where the line continues after the gap (stitched disconnected pieces)
  2. Truncated paths (jump at the end / suspiciously few points)
  3. Tiny stub segments
  4. Feature/relation inventory per type

Pure standard library — no geo dependencies required.

Usage:
  python scripts/validate_flow_paths.py --geojson dataset/yom/processed/flow_paths.geojson
  python scripts/validate_flow_paths.py --geojson flow.geojson --max-jump-km 0.5 --min-length-km 1.0
Exit code 0 = PASS, 1 = issues found.
"""

import argparse
import json
import math
import sys
from collections import Counter


def seg_km(a, b):
    return math.hypot((b[0] - a[0]) * 111.32 * 0.95, (b[1] - a[1]) * 110.54)


def validate(geojson, max_jump_km, min_length_km):
    features = geojson.get("features", [])
    report = {
        "total_features": len(features),
        "by_type": Counter(),
        "jumps": [],
        "mid_path_jumps": 0,
        "end_jumps": 0,
        "stubs": 0,
        "degenerate": 0,
        "max_jump_km": 0.0,
    }

    for feat in features:
        props = feat.get("properties", {})
        report["by_type"][props.get("feature_type", "unknown")] += 1
        coords = feat.get("geometry", {}).get("coordinates", [])
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
    args = parser.parse_args()

    with open(args.geojson, 'r', encoding='utf-8') as f:
        geojson = json.load(f)

    report = validate(geojson, args.max_jump_km, args.min_length_km)

    print("=" * 72)
    print("FLOW PATHS VALIDATION REPORT")
    print("=" * 72)
    print(f"File                  : {args.geojson}")
    print(f"Total features        : {report['total_features']}")
    print("By feature_type       :")
    for ftype, n in sorted(report["by_type"].items()):
        print(f"    {ftype:<35s} {n}")
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

    failed = report["mid_path_jumps"] > 0 or report["end_jumps"] > 0 or report["degenerate"] > 0
    print("\n" + ("❌ FAIL — straight-line jumps / degenerate features found" if failed
                   else "✅ PASS — no straight-line jumps above threshold"))
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
