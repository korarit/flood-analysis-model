#!/usr/bin/env python3
"""
Terrain Artifact Diagnostics (Step 0 of Flow Path Fix round 5)
Hunts for the root cause of axis-aligned "straight trench" teleports in flow paths.

Checks (each prints a verdict + evidence):
  1. Boundary file   — coarse/rectangular cache (the round-4 rectangle bug)
  2. Water polygons  — garbage rectangles (< 8 exterior vertices, big area) that get
                       burned as flat plateaus whose EDGES become D8 trenches
  3. OSM waterways   — ways with implausible internal vertex jumps
  4. Rasters         — long axis-aligned nodata runs (mosaic seams) in the DEM,
                       long straight same-code D8 runs in flow_direction.tif
  5. Hub correlation — (with --geojson) do the flow-file teleport hubs land on any
                       of the above? (polygon bounds / void run / fdir run)

Read-only — never modifies caches or rasters.
RAM-bounded: rasters are read in horizontal strips (no full-grid load).

Usage (on the machine that has the data):
  python scripts/diagnose_terrain_artifacts.py --basin nan \
      --geojson "../flow_paths(19).geojson"
Exit code 0 = clean, 1 = suspects found (report printed either way).
"""

import argparse
import json
import math
import os
import sys

import numpy as np
import rasterio

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# D8 codes (terrain_engine): 1=E, 2=SE, 4=S, 8=SW, 16=W, 32=NW, 64=N, 128=NE
CODE_S, CODE_N = 4, 64        # vertical D8 codes (runs along a COLUMN)
CODE_E, CODE_W = 1, 16        # horizontal D8 codes (runs along a ROW)

GARBAGE_POLY_MIN_VERTICES = 8      # fewer exterior vertices = suspicious box
GARBAGE_POLY_MIN_AREA_KM2 = 0.5    # ...and big enough to carve a real plateau
WAY_JUMP_KM = 2.0
RUN_MIN_CELLS = 800                # straight run worth reporting (~10 km at 12.5m)


def seg_km(a, b):
    return math.hypot((b[0] - a[0]) * 111.32 * 0.95, (b[1] - a[1]) * 110.54)


def load_geojson_any(path):
    import gzip
    with open(path, 'rb') as fh:
        magic = fh.read(2)
    if magic == b'\x1f\x8b':
        with gzip.open(path, 'rt', encoding='utf-8') as f:
            return json.load(f)
    with open(path, 'r', encoding='utf-8') as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# 1. Boundary
# ---------------------------------------------------------------------------
def check_boundary(path):
    print("\n" + "=" * 74)
    print("1. BOUNDARY FILE")
    print("=" * 74)
    if not os.path.exists(path):
        print(f"    MISSING: {path}")
        return {"status": "missing", "path": path}
    try:
        from shapely.geometry import shape
        data = load_geojson_any(path)
        feats = data.get("features") or []
        if not feats:
            print(f"    INVALID: no features in {path}")
            return {"status": "invalid", "path": path}
        geom = feats[0].get("geometry") or {}
        props = feats[0].get("properties") or {}
        if geom.get("type") not in ("Polygon", "MultiPolygon"):
            print(f"    INVALID: geometry type = {geom.get('type')}")
            return {"status": "invalid", "path": path}
        g = shape(geom)
        n_verts = 0
        polys = list(g.geoms) if g.geom_type == "MultiPolygon" else [g]
        for p in polys:
            if p.exterior is not None:
                n_verts += len(p.exterior.coords)
            n_verts += sum(len(r.coords) for r in p.interiors)
        minx, miny, maxx, maxy = g.bounds
        source = str(props.get("source", ""))
        verdict = "OK"
        if "bounding box" in source.lower() or "bbox" in source.lower():
            verdict = "REJECT (rectangular fallback cache)"
        elif n_verts < 50:
            verdict = "REJECT (coarse — likely a box, < 50 vertices)"
        print(f"    path     : {path}")
        print(f"    source   : {source or '-'}")
        print(f"    vertices : {n_verts:,}")
        print(f"    extent   : lon[{minx:.4f}, {maxx:.4f}] lat[{miny:.4f}, {maxy:.4f}]")
        print(f"    verdict  : {verdict}")
        return {"status": verdict.split()[0].lower(), "verdict": verdict, "vertices": n_verts,
                "source": source, "extent": [minx, miny, maxx, maxy], "path": path}
    except Exception as ex:
        print(f"    ERROR reading boundary: {ex}")
        return {"status": "error", "error": str(ex)}


# ---------------------------------------------------------------------------
# 2. Water polygons — garbage rectangles
# ---------------------------------------------------------------------------
def poly_exterior_vertices(geom):
    from shapely.geometry import shape
    g = shape(geom)
    polys = list(g.geoms) if g.geom_type == "MultiPolygon" else [g]
    return sum(len(p.exterior.coords) for p in polys if p.exterior is not None)


def check_water_polygons(path, hubs=None):
    print("\n" + "=" * 74)
    print("2. WATER POLYGONS — garbage rectangles (< %d vertices, > %.1f km2)"
          % (GARBAGE_POLY_MIN_VERTICES, GARBAGE_POLY_MIN_AREA_KM2))
    print("=" * 74)
    if not os.path.exists(path):
        print(f"    MISSING: {path}")
        return {"status": "missing"}
    data = load_geojson_any(path)
    meta = data.get("_meta") or {}
    print(f"    cache meta: source={meta.get('source', '-')} "
          f"crop_polygon={'yes' if meta.get('crop_polygon') else 'NO'} "
          f"n_features={len(data.get('features', [])):,}")
    garbage = []
    n_in = 0
    for feat in data.get("features", []):
        geom = feat.get("geometry") or {}
        if geom.get("type") not in ("Polygon", "MultiPolygon"):
            continue
        n_in += 1
        try:
            from shapely.geometry import shape
            g = shape(geom)
            lat = max(-89.0, min(89.0, g.centroid.y))
            area_km2 = g.area * 111.32 * 0.95 * 110.54 * math.cos(math.radians(lat))
        except Exception:
            continue
        n_verts = poly_exterior_vertices(geom)
        if n_verts < GARBAGE_POLY_MIN_VERTICES and area_km2 > GARBAGE_POLY_MIN_AREA_KM2:
            garbage.append({"osm_id": feat.get("properties", {}).get("osm_id"),
                            "vertices": n_verts, "area_km2": round(area_km2, 1),
                            "bounds": [round(v, 4) for v in g.bounds]})
    garbage.sort(key=lambda x: -x["area_km2"])
    print(f"    polygons scanned: {n_in:,} | garbage rectangles: {len(garbage):,}")
    for g in garbage[:10]:
        print(f"      osm_id={g['osm_id']} verts={g['vertices']} area={g['area_km2']}km2 bounds={g['bounds']}")
    hub_hits = None
    big_bounds = []
    for feat in data.get("features", []):
        geom = feat.get("geometry") or {}
        if geom.get("type") not in ("Polygon", "MultiPolygon"):
            continue
        try:
            from shapely.geometry import shape
            b = shape(geom).bounds
            if (b[2] - b[0]) * (b[3] - b[1]) > 0.005:  # ~sub-km² and up
                big_bounds.append(b)
        except Exception:
            continue
    if hubs:
        hub_hits = sum(1 for (x, y), _n in hubs
                       if any(b[0] <= x <= b[2] and b[1] <= y <= b[3] for b in (g["bounds"] for g in garbage)))
        poly_hits = sum(1 for (x, y), _n in hubs
                        if any(b[0] <= x <= b[2] and b[1] <= y <= b[3] for b in big_bounds))
        print(f"    flow-file hubs inside garbage polygons: {hub_hits}")
        print(f"    flow-file hubs inside ANY water polygon: {poly_hits} "
              f"(round 6: those teleports are now stopped by the water-polygon D8 stop)")
    if garbage:
        print("    >>> LIKELY ROOT CAUSE: these burn as flat plateaus; their EDGES become")
        print("        the axis-aligned D8 trenches. Fix = Step 2 polygon filter.")
    return {"status": "ok", "n_in": n_in, "garbage": garbage, "hub_hits": hub_hits}


# ---------------------------------------------------------------------------
# 3. OSM waterways — internal vertex jumps
# ---------------------------------------------------------------------------
def check_waterways(path):
    print("\n" + "=" * 74)
    print(f"3. OSM WATERWAYS — ways with internal vertex jumps > {WAY_JUMP_KM} km")
    print("=" * 74)
    if not os.path.exists(path):
        print(f"    MISSING: {path}")
        return {"status": "missing"}
    data = load_geojson_any(path)
    meta = data.get("_meta") or {}
    print(f"    cache meta: source={meta.get('source', '-')} "
          f"crop_polygon={'yes' if meta.get('crop_polygon') else 'NO'} "
          f"n_features={len(data.get('features', [])):,}")
    jumps = []
    for feat in data.get("features", []):
        geom = feat.get("geometry") or {}
        if geom.get("type") not in ("LineString", "MultiLineString"):
            continue
        parts = [geom.get("coordinates", [])] if geom.get("type") == "LineString" else geom.get("coordinates", [])
        for part in parts:
            for i in range(len(part) - 1):
                d = seg_km(part[i], part[i + 1])
                if d > WAY_JUMP_KM:
                    jumps.append({"osm_id": feat.get("properties", {}).get("osm_id"),
                                  "jump_km": round(d, 1),
                                  "a": [round(v, 4) for v in part[i]],
                                  "b": [round(v, 4) for v in part[i + 1]]})
    jumps.sort(key=lambda x: -x["jump_km"])
    print(f"    ways with jumps > {WAY_JUMP_KM} km: {len(jumps):,}")
    for j in jumps[:10]:
        print(f"      osm_id={j['osm_id']} jump={j['jump_km']}km {j['a']} -> {j['b']}")
    if jumps:
        print("    >>> These become straight edges in the backbone graph.")
    return {"status": "ok", "jumps": jumps}


# ---------------------------------------------------------------------------
# 3b. OSM layer audit — was anything silently deleted? (round 6, Phase 0)
# ---------------------------------------------------------------------------
def check_osm_audit(path):
    print("\n" + "=" * 74)
    print("3b. OSM LAYER AUDIT — jump-split / crop counters (answers 'was OSM deleted?')")
    print("=" * 74)
    if not os.path.exists(path):
        print(f"    MISSING: {path}")
        return {"status": "missing"}
    data = load_geojson_any(path)
    meta = data.get("_meta") or {}
    jump = meta.get("way_jump_stats") or {}
    crop = meta.get("crop_stats") or {}
    print(f"    features on disk      : {len(data.get('features', [])):,}")
    print(f"    cache source          : {meta.get('source', '-')}")
    print(f"    crop_polygon fingerprint: {'yes' if meta.get('crop_polygon') else 'NO'}")
    if crop:
        print(f"    crop stats            : n_in={crop.get('n_in', '-')} n_out={crop.get('n_out', '-')} "
              f"dropped_outside={crop.get('dropped_outside', '-')} clipped={crop.get('clipped', '-')}")
    else:
        print("    crop stats            : MISSING (legacy cache — re-run fetch with --force-osm)")
    if jump:
        print(f"    jump-split stats      : ways {jump.get('n_ways_in', '-')} -> {jump.get('n_ways_out', '-')} "
              f"(split: {jump.get('n_split', '-')}, ways dropped: {jump.get('n_ways_dropped', '-')}, "
              f"parts dropped: {jump.get('n_parts_dropped', '-')})")
        dropped = jump.get("dropped_osm_ids") or []
        if dropped:
            print(f"    dropped way osm_ids   : {', '.join(str(d) for d in dropped[:50])}"
                  + (" ..." if len(dropped) > 50 else ""))
    else:
        print("    jump-split stats      : MISSING (sanitize never ran on this cache)")
        return {"status": "no_audit", "n_features": len(data.get("features", []))}
    return {"status": "ok", "jump": jump, "crop": crop}


# ---------------------------------------------------------------------------
# 4. Rasters — straight void runs (DEM) / straight same-code D8 runs (fdir)
# ---------------------------------------------------------------------------
def _longest_true_run_per_row(mask2d):
    """Per-row longest run of True -> (best_len[nr], best_start[nr])."""
    nr, _nc = mask2d.shape
    best_len = np.zeros(nr, dtype=np.int64)
    best_start = np.full(nr, -1, dtype=np.int64)
    for r in range(nr):
        row = mask2d[r]
        if not row.any():
            continue
        padded = np.concatenate(([False], row, [False]))
        d = np.diff(padded.astype(np.int8))
        starts = np.nonzero(d == 1)[0]
        ends = np.nonzero(d == -1)[0]
        lens = ends - starts
        k = int(np.argmax(lens))
        best_len[r] = lens[k]
        best_start[r] = starts[k]
    return best_len, best_start


def _run_centers(row_idx, starts, lens, transform):
    """Runs >= RUN_MIN_CELLS -> [(lon, lat, len)] of the run midpoint."""
    out = []
    for r, s, l in zip(row_idx, starts, lens):
        if l < RUN_MIN_CELLS:
            continue
        c = int(s) + int(l) // 2
        x = transform[2] + (c + 0.5) * transform[0]
        y = transform[5] + (r + 0.5) * transform[4]
        out.append((round(x, 4), round(y, 4), int(l)))
    return out


def _print_runs(runs, hubs):
    for kind, lst in runs.items():
        lst.sort(key=lambda t: -t[2])
        for (x, y, l) in lst[:5]:
            note = ""
            if hubs:
                if any(abs(hx - x) < 0.02 and abs(hy - y) < 0.02 for (hx, hy), _n in hubs):
                    note = "  <-- FLOW HUB"
                elif any(min(abs(hx - x), abs(hy - y)) < 0.02 for (hx, hy), _n in hubs):
                    note = "  <-- near a flow hub axis"
            print(f"        [{kind}] len={l:,} cells @ ({x}, {y}){note}")


def scan_raster(path, strip_rows=2048):
    """
    Streams the raster in row strips (RAM-bounded) and returns straight runs:
      DEM: per-row longest NODATA run (void seam) + per-row longest
           equal-neighbor run (flat seam / plateau edge).
      fdir: per-row longest horizontal same-code run (E=1 / W=16), and
            per-column longest vertical same-code run (S=4 / N=64).
    """
    runs = {}
    with rasterio.open(path) as src:
        transform = src.transform
        nodata = src.nodata if src.nodata is not None else -9999.0
        H, W = src.height, src.width
        is_dem = "dem" in os.path.basename(path).lower()
        is_fdir = "flow_direction" in os.path.basename(path).lower()

        if is_dem:
            runs = {"void_row": [], "flat_row": []}
        elif is_fdir:
            runs = {"d8_row_EW": [], "d8_col_S": [], "d8_col_N": []}
            col_best_S = np.zeros(W, dtype=np.int64)
            col_best_N = np.zeros(W, dtype=np.int64)

        for r0 in range(0, H, strip_rows):
            r1 = min(H, r0 + strip_rows)
            win = rasterio.windows.Window(0, r0, W, r1 - r0)
            data = src.read(1, window=win)
            rows = range(r0, r1)

            if is_dem:
                if data.dtype.kind == 'f':
                    void = (data == nodata) | np.isnan(data)
                else:
                    void = data == nodata
                bl, bs = _longest_true_run_per_row(void)
                runs["void_row"] += _run_centers(rows, bs, bl, transform)
                # flat seam proxy: cells equal to their LEFT neighbor (long chains
                # of identical values = plateau edge / seam line)
                if data.dtype.kind == 'f':
                    eq = (~void[:, 1:]) & (~void[:, :-1]) & (data[:, 1:] == data[:, :-1])
                else:
                    eq = data[:, 1:] == data[:, :-1]
                bl, bs = _longest_true_run_per_row(eq)
                runs["flat_row"] += _run_centers(rows, bs, bl, transform)

            elif is_fdir:
                code = data.astype(np.int16)
                horiz = (code == CODE_E) | (code == CODE_W)
                bl, bs = _longest_true_run_per_row(horiz)
                runs["d8_row_EW"] += _run_centers(rows, bs, bl, transform)
                # vertical accumulation across strips
                is_S = (code == CODE_S)
                is_N = (code == CODE_N)
                col_best_S = np.where(is_S.any(axis=0),
                                      np.maximum(col_best_S, _col_runs(is_S)),
                                      col_best_S)
                col_best_N = np.where(is_N.any(axis=0),
                                      np.maximum(col_best_N, _col_runs(is_N[::-1])),
                                      col_best_N)

        if is_fdir:
            # report only columns whose best run >= threshold
            for c in np.nonzero(col_best_S >= RUN_MIN_CELLS)[0]:
                x = transform[2] + (c + 0.5) * transform[0]
                y = transform[5] + (H / 2.0) * transform[4]
                runs["d8_col_S"].append((round(x, 4), round(y, 4), int(col_best_S[c])))
            for c in np.nonzero(col_best_N >= RUN_MIN_CELLS)[0]:
                x = transform[2] + (c + 0.5) * transform[0]
                y = transform[5] + (H / 2.0) * transform[4]
                runs["d8_col_N"].append((round(x, 4), round(y, 4), int(col_best_N[c])))
    return runs


def _col_runs(is_code):
    """Per-column longest vertical run of a boolean mask (within the strip)."""
    nr, nc = is_code.shape
    best = np.zeros(nc, dtype=np.int64)
    run = np.zeros(nc, dtype=np.int64)
    for r in range(nr):
        run = np.where(is_code[r], run + 1, 0)
        best = np.maximum(best, run)
    return best


def check_rasters(terrain_dir, hubs=None, strip_rows=2048):
    print("\n" + "=" * 74)
    print(f"4. RASTERS — straight nodata / flat / same-code D8 runs (> {RUN_MIN_CELLS} cells)")
    print("=" * 74)
    report = {}
    for name in ("raw_dem.tif", "conditioned_dem.tif", "flow_direction.tif", "river_mask.tif"):
        path = os.path.join(terrain_dir, name)
        if not os.path.exists(path):
            print(f"    {name}: MISSING")
            report[name] = {"status": "missing"}
            continue
        with rasterio.open(path) as src:
            print(f"    {name}: {src.width}x{src.height} dtype={src.dtypes[0]} "
                  f"res={src.res} nodata={src.nodata}")
            report[name] = {"shape": [src.height, src.width], "dtype": src.dtypes[0]}
            runs = scan_raster(path, strip_rows)
            # projected rasters: convert run centers to lon/lat so they can be
            # correlated with the flow-file hubs (which are lon/lat)
            if src.crs is not None and not src.crs.is_geographic:
                try:
                    from pyproj import Transformer
                    tr = Transformer.from_crs(src.crs, "EPSG:4326", always_xy=True)
                    for kind, lst in runs.items():
                        if lst:
                            lons, lats = tr.transform([p[0] for p in lst], [p[1] for p in lst])
                            runs[kind] = [(round(lo, 4), round(la, 4), p[2])
                                          for lo, la, p in zip(lons, lats, lst)]
                except Exception as ex:
                    print(f"      [WARN] cannot convert run coords to lon/lat: {ex}")
            n_reported = sum(len(v) for v in runs.values())
            print(f"      straight runs >= {RUN_MIN_CELLS} cells: {n_reported}")
            _print_runs(runs, hubs)
            report[name]["runs"] = {k: len(v) for k, v in runs.items()}
    return report


# ---------------------------------------------------------------------------
# Hub extraction from the flow file
# ---------------------------------------------------------------------------
def extract_hubs(geojson, min_km=10.0, top=15):
    """Axis-aligned jump endpoints (>min_km), most frequent first."""
    from collections import Counter
    hub = Counter()
    for f in geojson.get("features", []):
        p = f.get("properties", {})
        if p.get("feature_type") == "osm_river":
            continue
        c = f.get("geometry", {}).get("coordinates") or []
        for i in range(len(c) - 1):
            d = seg_km(c[i], c[i + 1])
            if d < min_km:
                continue
            dlon = abs(c[i + 1][0] - c[i][0])
            dlat = abs(c[i + 1][1] - c[i][1])
            if min(dlon, dlat) > 0.05 * max(dlon, dlat, 1e-9):
                continue  # not axis-aligned
            hub[(round(c[i][0], 3), round(c[i][1], 3))] += 1
            hub[(round(c[i + 1][0], 3), round(c[i + 1][1], 3))] += 1
    return hub.most_common(top)


def main():
    parser = argparse.ArgumentParser(description="Diagnose straight-trench artifacts in flow paths")
    parser.add_argument("--basin", type=str, default="nan", help="Basin slug (default: nan)")
    parser.add_argument("--dir", type=str, default="./dataset", help="Dataset root directory")
    parser.add_argument("--terrain-dir", type=str, default="./terrain", help="Terrain raster directory")
    parser.add_argument("--geojson", type=str, default=None,
                        help="flow_paths(.geojson[.gz]) file — enables hub correlation")
    parser.add_argument("--strip-rows", type=int, default=2048, help="Raster strip size (RAM control)")
    args = parser.parse_args()

    basin = args.basin.lower().strip()
    basin_dir = args.dir if os.path.basename(os.path.normpath(args.dir)) == basin \
        else os.path.join(args.dir, basin)
    terrain_dir = args.terrain_dir if os.path.basename(os.path.normpath(args.terrain_dir)) == basin \
        else os.path.join(args.terrain_dir, basin)
    if not os.path.isdir(terrain_dir) and os.path.isdir(args.terrain_dir):
        terrain_dir = args.terrain_dir

    hubs = None
    if args.geojson:
        try:
            hubs = extract_hubs(load_geojson_any(args.geojson))
            print(f"Flow-file hubs (axis-aligned jump endpoints > 10 km): {len(hubs)}")
            for (x, y), n in hubs[:8]:
                print(f"    ({x}, {y})  x{n}")
        except Exception as ex:
            print(f"[WARN] cannot read flow file {args.geojson}: {ex}")
            hubs = None

    report = {
        "boundary": check_boundary(os.path.join(basin_dir, "gis", f"{basin}_boundary.geojson")),
        "water_polygons": check_water_polygons(
            os.path.join(basin_dir, "gis", "osm_water_polygons.geojson"), hubs),
        "waterways": check_waterways(os.path.join(basin_dir, "gis", "osm_waterways.geojson")),
        "osm_audit": check_osm_audit(os.path.join(basin_dir, "gis", "osm_waterways.geojson")),
        "rasters": check_rasters(terrain_dir, hubs, strip_rows=args.strip_rows),
    }

    print("\n" + "=" * 74)
    print("VERDICT SUMMARY")
    print("=" * 74)
    suspects = 0
    if report["boundary"].get("status") == "reject":
        print("  [X] boundary cache is coarse/rectangular -> refetch (Step 1)")
        suspects += 1
    if report["water_polygons"].get("garbage"):
        print(f"  [X] {len(report['water_polygons']['garbage'])} garbage water polygon(s) -> "
              f"polygon filter (Step 2)")
        suspects += 1
    if report["waterways"].get("jumps"):
        print(f"  [X] {len(report['waterways']['jumps'])} waterway jump(s) -> way split (Step 2)")
        suspects += 1
    if report["osm_audit"].get("status") == "no_audit":
        print("  [X] OSM cache has no jump-split/crop audit counters -> refetch with --force-osm "
              "(cannot verify whether OSM ways were silently dropped)")
        suspects += 1
    r = report["rasters"]
    void = sum(v.get("runs", {}).get("void_row", 0) for v in r.values() if isinstance(v, dict))
    flat = sum(v.get("runs", {}).get("flat_row", 0) for v in r.values() if isinstance(v, dict))
    d8col = sum(v.get("runs", {}).get("d8_col_S", 0) + v.get("runs", {}).get("d8_col_N", 0)
                for v in r.values() if isinstance(v, dict))
    d8row = sum(v.get("runs", {}).get("d8_row_EW", 0) for v in r.values() if isinstance(v, dict))
    if void or flat:
        print(f"  [X] DEM straight void/flat runs (void={void}, flat={flat}) — "
              f"void = tile-coverage edges (benign); exact FLAT runs are calm-water "
              f"plateaus -> fixed by break_exact_flats (Step 2a)")
        suspects += 1
    if d8col or d8row:
        print(f"  [X] fdir straight same-code runs (col={d8col}, row={d8row}) -> the trenches "
              f"themselves; regenerate fdir with --force AFTER the fixes")
        suspects += 1
    if suspects == 0:
        print("  No smoking gun found — send this full report back for manual review.")
    else:
        print(f"\n  {suspects} suspect source(s). Fix in order: Step 1 -> 2 -> 3, then "
              f"regenerate with --force (re-burn + re-fdir).")
    sys.exit(1 if suspects else 0)


if __name__ == "__main__":
    main()
