#!/usr/bin/env python3
"""
River Network Optimization & Simplification Engine
Reduces massive 2GB+ DEM 12.5m River GeoJSON files down to lightweight (5-20MB)
web-ready GeoJSON assets for Leaflet/Mapbox frontend rendering.

Techniques applied:
1. Ramer-Douglas-Peucker (RDP) geometric simplification (removes ~80-95% redundant vertices).
2. Coordinate precision truncation (5 decimal places ~ 1.1m resolution).
3. Compact serialization (removes multi-gigabyte whitespace & indentation overhead).
4. Stream processing (< 50MB RAM usage even on 5GB+ input files).
5. Optional micro-creek filtering & multi-tier generation (main rivers vs full network).
"""

import argparse
import json
import math
import os
import sys
import time
from typing import List, Tuple, Generator, Dict, Any


def perpendicular_distance(pt: List[float], line_start: List[float], line_end: List[float]) -> float:
    """Calculate Euclidean distance from point to line segment."""
    dx = line_end[0] - line_start[0]
    dy = line_end[1] - line_start[1]
    line_len_sq = dx * dx + dy * dy
    if line_len_sq == 0.0:
        return math.hypot(pt[0] - line_start[0], pt[1] - line_start[1])
    t = max(0.0, min(1.0, ((pt[0] - line_start[0]) * dx + (pt[1] - line_start[1]) * dy) / line_len_sq))
    proj_x = line_start[0] + t * dx
    proj_y = line_start[1] + t * dy
    return math.hypot(pt[0] - proj_x, pt[1] - proj_y)


def rdp_simplify(points: List[List[float]], tolerance: float) -> List[List[float]]:
    """
    Non-recursive (iterative stack) Ramer-Douglas-Peucker simplification.
    Avoids Python recursion limits on long continuous river channels.
    """
    if len(points) <= 2 or tolerance <= 0.0:
        return points

    stack = [(0, len(points) - 1)]
    keep_indices = {0, len(points) - 1}

    while stack:
        start, end = stack.pop()
        dmax = 0.0
        index = start
        p_start, p_end = points[start], points[end]
        for i in range(start + 1, end):
            d = perpendicular_distance(points[i], p_start, p_end)
            if d > dmax:
                index = i
                dmax = d
        if dmax > tolerance:
            keep_indices.add(index)
            if index - start > 1:
                stack.append((start, index))
            if end - index > 1:
                stack.append((index, end))

    return [points[i] for i in sorted(keep_indices)]


def stream_geojson_features(filepath: str) -> Generator[Dict[str, Any], None, None]:
    """
    Memory-efficient streaming generator that reads GeoJSON features one by one
    without loading multi-gigabyte files into RAM.
    """
    decoder = json.JSONDecoder()
    with open(filepath, 'r', encoding='utf-8') as f:
        buffer = ""
        in_features = False
        while True:
            chunk = f.read(65536)
            if not chunk:
                break
            buffer += chunk

            if not in_features:
                idx = buffer.find('"features"')
                if idx != -1:
                    bracket_idx = buffer.find('[', idx)
                    if bracket_idx != -1:
                        buffer = buffer[bracket_idx + 1:]
                        in_features = True

            if in_features:
                while buffer:
                    buffer = buffer.lstrip(' \t\r\n,')
                    if not buffer:
                        break
                    if buffer.startswith(']'):
                        return
                    try:
                        obj, idx = decoder.raw_decode(buffer)
                        yield obj
                        buffer = buffer[idx:].lstrip(' \t\r\n,')
                    except json.JSONDecodeError:
                        break


def simplify_river_geojson(
    input_path: str,
    output_path: str,
    tolerance_deg: float = 0.0001,  # ~11 meters in Thailand
    min_length_km: float = 0.1,    # Filter out tiny sub-100m artifacts
    min_acc_cells: int = 0,         # Optional accumulation filter
    precision: int = 5,             # 5 decimals (~1.1m precision)
    create_backup: bool = True
) -> Dict[str, Any]:
    """
    Reads large river GeoJSON and writes an optimized, lightweight GeoJSON file.
    """
    if not os.path.exists(input_path):
        raise FileNotFoundError(f"Input file not found: {input_path}")

    orig_size_mb = os.path.getsize(input_path) / (1024.0 * 1024.0)
    print(f"\n🌊 [RIVER OPTIMIZER] Processing: {input_path}")
    print(f"   • Original File Size   : {orig_size_mb:.2f} MB")
    print(f"   • Simplification Tol   : {tolerance_deg}° (~{tolerance_deg*111320:.1f} m)")
    print(f"   • Coordinate Precision : {precision} decimal places")
    print(f"   • Min Length Filter    : {min_length_km} km")

    # Backup original if overwriting same path
    if create_backup and os.path.abspath(input_path) == os.path.abspath(output_path):
        backup_path = input_path.replace(".geojson", "_raw.geojson")
        if not os.path.exists(backup_path):
            import shutil
            shutil.copy2(input_path, backup_path)
            print(f"   • Raw Backup Saved     : {backup_path}")

    temp_out_path = output_path + ".tmp"
    total_in_features = 0
    total_out_features = 0
    orig_total_vertices = 0
    simplified_total_vertices = 0
    t0 = time.time()

    with open(temp_out_path, 'w', encoding='utf-8') as out_f:
        out_f.write('{"type":"FeatureCollection","features":[\n')
        first_feature = True

        for feat in stream_geojson_features(input_path):
            total_in_features += 1
            props = feat.get("properties", {})
            geom = feat.get("geometry", {})
            gtype = geom.get("type", "")

            # Filter by minimum length or accumulation if specified
            length_km = float(props.get("length_km", props.get("distance_km", 0.0)))
            start_acc = int(props.get("start_acc_cells", 0))

            if min_length_km > 0 and length_km < min_length_km and length_km > 0:
                continue
            if min_acc_cells > 0 and start_acc < min_acc_cells:
                continue

            if gtype == "LineString":
                raw_coords = geom.get("coordinates", [])
                orig_total_vertices += len(raw_coords)

                # Simplify coordinates
                sim_coords = rdp_simplify(raw_coords, tolerance_deg)
                rounded_coords = [[round(c[0], precision), round(c[1], precision)] for c in sim_coords]
                simplified_total_vertices += len(rounded_coords)
                geom["coordinates"] = rounded_coords
            elif gtype == "MultiLineString":
                raw_lines = geom.get("coordinates", [])
                sim_lines = []
                for line in raw_lines:
                    orig_total_vertices += len(line)
                    sim_line = rdp_simplify(line, tolerance_deg)
                    rounded_line = [[round(c[0], precision), round(c[1], precision)] for c in sim_line]
                    simplified_total_vertices += len(rounded_line)
                    sim_lines.append(rounded_line)
                geom["coordinates"] = sim_lines
            else:
                orig_total_vertices += 1
                simplified_total_vertices += 1

            if not first_feature:
                out_f.write(',\n')
            else:
                first_feature = False

            # Compact JSON output for this feature
            out_f.write(json.dumps(feat, separators=(',', ':'), ensure_ascii=False))
            total_out_features += 1

            if total_in_features % 10000 == 0:
                print(f"     ... processed {total_in_features} features ({orig_total_vertices} vertices -> {simplified_total_vertices} vertices)")

        out_f.write('\n]}')

    # Atomic rename
    if os.path.exists(output_path):
        os.remove(output_path)
    os.rename(temp_out_path, output_path)

    new_size_mb = os.path.getsize(output_path) / (1024.0 * 1024.0)
    reduction_pct = (1.0 - (new_size_mb / orig_size_mb)) * 100.0 if orig_size_mb > 0 else 0.0
    vertex_reduction_pct = (1.0 - (simplified_total_vertices / max(1, orig_total_vertices))) * 100.0
    elapsed = time.time() - t0

    print(f"\n   ✅ [OPTIMIZATION COMPLETE] in {elapsed:.1f}s")
    print(f"      • Original Size      : {orig_size_mb:.2f} MB")
    print(f"      • Optimized Size     : {new_size_mb:.2f} MB ({reduction_pct:.1f}% reduction!)")
    print(f"      • Total Features     : {total_in_features} -> {total_out_features} kept")
    print(f"      • Total Vertices     : {orig_total_vertices:,} -> {simplified_total_vertices:,} ({vertex_reduction_pct:.1f}% reduction)")
    print(f"      • Output Saved       : {output_path}")

    return {
        "original_size_mb": round(orig_size_mb, 2),
        "optimized_size_mb": round(new_size_mb, 2),
        "reduction_percent": round(reduction_pct, 1),
        "original_vertices": orig_total_vertices,
        "simplified_vertices": simplified_total_vertices,
        "features_count": total_out_features,
        "elapsed_seconds": round(elapsed, 2)
    }


def main():
    parser = argparse.ArgumentParser(description="Simplify and optimize large river network GeoJSON files for Web GIS")
    parser.add_argument("--basin", type=str, default="yom", help="Basin slug (e.g. yom, nan, ping, all)")
    parser.add_argument("--dir", type=str, default="./dataset", help="Dataset root directory")
    parser.add_argument("--input", type=str, default=None, help="Custom input GeoJSON path")
    parser.add_argument("--output", type=str, default=None, help="Custom output GeoJSON path")
    parser.add_argument("--tolerance", type=float, default=0.0001, help="RDP tolerance in degrees (0.0001 ~ 11m, 0.0002 ~ 22m)")
    parser.add_argument("--min-length", type=float, default=0.1, help="Minimum river reach length in km to keep (default: 0.1 km)")
    parser.add_argument("--min-acc", type=int, default=0, help="Minimum accumulation cells threshold (0 = keep all)")
    parser.add_argument("--precision", type=int, default=5, help="Coordinate decimal places (default: 5)")
    parser.add_argument("--no-backup", action="store_true", help="Skip creating _raw.geojson backup")
    parser.add_argument("--create-main-tier", action="store_true", help="Also generate river_network_main.geojson for major rivers only")
    args = parser.parse_args()

    basin_list = ["yom", "nan", "ping", "wang", "chao-phraya"] if args.basin == "all" else [args.basin]

    for b in basin_list:
        basin_dir = os.path.join(args.dir, b)
        processed_dir = os.path.join(basin_dir, "processed")
        river_dir = os.path.join(basin_dir, "river")

        in_file = args.input or os.path.join(processed_dir, "river_network.geojson")
        if not os.path.exists(in_file):
            in_file = os.path.join(river_dir, "river_network.geojson")

        if not os.path.exists(in_file):
            print(f"⚠️ Warning: river_network.geojson not found in {in_file}. Skipping basin '{b}'.")
            continue

        out_file = args.output or os.path.join(processed_dir, "river_network.geojson")
        simplify_river_geojson(
            input_path=in_file,
            output_path=out_file,
            tolerance_deg=args.tolerance,
            min_length_km=args.min_length,
            min_acc_cells=args.min_acc,
            precision=args.precision,
            create_backup=not args.no_backup
        )

        # Also sync to river_dir
        river_dir_out = os.path.join(river_dir, "river_network.geojson")
        if os.path.abspath(out_file) != os.path.abspath(river_dir_out) and os.path.exists(out_file):
            import shutil
            shutil.copy2(out_file, river_dir_out)

        # Optional: Generate ultra-lightweight main rivers tier (~2-4 MB)
        if args.create_main_tier:
            main_out = os.path.join(processed_dir, "river_network_main.geojson")
            print("\n🌟 Generating Tier-1 Main River Network layer (river_network_main.geojson)...")
            simplify_river_geojson(
                input_path=in_file,
                output_path=main_out,
                tolerance_deg=args.tolerance * 1.5,
                min_length_km=max(0.5, args.min_length * 2),
                min_acc_cells=1500,  # Only significant tributary flow
                precision=args.precision,
                create_backup=False
            )


if __name__ == "__main__":
    main()
