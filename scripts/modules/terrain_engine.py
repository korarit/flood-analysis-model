"""
Terrain Engine Module
Handles DEM reading, Hydrological Pit Filling, D8 Flow Direction,
Flow Accumulation, River Stream Extraction, and River Slope Calculation.
"""

import math
import os
from typing import Dict, List, Tuple, Any, Optional
import numpy as np
import rasterio
from rasterio.transform import Affine
from shapely.geometry import LineString, mapping
from .gis_utils import haversine_distance, linestring_length_km

# D8 Direction encoding:
# 1: East, 2: Southeast, 4: South, 8: Southwest, 16: West, 32: Northwest, 64: North, 128: Northeast
D8_DELTAS = {
    1: (0, 1),
    2: (1, 1),
    4: (1, 0),
    8: (1, -1),
    16: (0, -1),
    32: (-1, -1),
    64: (-1, 0),
    128: (-1, 1),
}


def clip_dem_to_polygon(
    dem_path: str,
    polygon_geom: Any,
    buffer_deg: float = 0.015
) -> Tuple[np.ndarray, Affine, Any, float]:
    """
    Clips DEM raster directly to a specific sub-basin polygon at native 30m resolution.
    Handles CRS coordinate transformation automatically (e.g. EPSG:4326 to UTM/native CRS).
    Returns (clipped_elev, clipped_transform, crs, nodata).
    """
    from rasterio.mask import mask
    from rasterio.warp import transform_geom
    from shapely.geometry import shape, mapping

    if isinstance(polygon_geom, dict):
        poly_obj = shape(polygon_geom)
    else:
        poly_obj = polygon_geom

    if buffer_deg > 0:
        poly_buffered = poly_obj.buffer(buffer_deg)
    else:
        poly_buffered = poly_obj

    with rasterio.open(dem_path) as src:
        nodata = src.nodata if src.nodata is not None else -9999.0
        crs = src.crs

        # Reproject polygon from EPSG:4326 to raster native CRS if needed
        poly_geojson = mapping(poly_buffered)
        if crs and crs.to_string() != "EPSG:4326":
            try:
                poly_in_raster_crs = transform_geom("EPSG:4326", crs, poly_geojson)
            except Exception:
                poly_in_raster_crs = poly_geojson
        else:
            poly_in_raster_crs = poly_geojson

        out_image, out_transform = mask(src, [poly_in_raster_crs], crop=True, nodata=nodata)
        clipped_elev = out_image[0].astype(np.float32)

    return clipped_elev, out_transform, crs, nodata


def read_dem_geotiff(
    dem_path: str
) -> Tuple[np.ndarray, Affine, Any, float]:
    """
    Read DEM raster at native resolution.
    With FABDEM (30m), the memory footprint is naturally optimized, so we load it at 100% scale
    to preserve maximum hydrological routing accuracy.
    """
    with rasterio.open(dem_path) as src:
        nodata = src.nodata if src.nodata is not None else -9999.0
        elev = src.read(1).astype(np.float32)
        transform = src.transform
        crs = src.crs

    return elev, transform, crs, nodata


import threading
import time


class LiveProgressBar:
    """Live active progress bar thread for C-compiled hydrological routines."""
    def __init__(self, desc: str, total_sec: int = 60):
        self.desc = desc
        self.total_sec = total_sec
        self.running = False
        self.thread = None

    def __enter__(self):
        try:
            from tqdm import tqdm
            self.tqdm = tqdm
        except ImportError:
            self.tqdm = None
            return self

        self.running = True
        self.start_t = time.time()
        self.pbar = self.tqdm(
            total=self.total_sec,
            desc=f"  │  [Progress] {self.desc}",
            unit="s",
            ncols=85,
            leave=True
        )
        self.thread = threading.Thread(target=self._animate, daemon=True)
        self.thread.start()
        return self

    def _animate(self):
        while self.running:
            time.sleep(0.3)
            elapsed = int(time.time() - self.start_t)
            if elapsed <= self.total_sec:
                self.pbar.n = elapsed
                self.pbar.refresh()
            else:
                self.pbar.total = elapsed + 5
                self.pbar.n = elapsed
                self.pbar.set_postfix_str(f"Processing ({elapsed}s)...")
                self.pbar.refresh()

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.running = False
        if self.thread:
            self.thread.join(timeout=0.5)
        if hasattr(self, 'pbar') and self.pbar:
            elapsed = max(1, int(time.time() - self.start_t))
            self.pbar.n = elapsed
            self.pbar.total = elapsed
            self.pbar.set_postfix_str(f"Completed ({elapsed}s)")
            self.pbar.refresh()
            self.pbar.close()


def fill_depressions_priority_flood(
    dem: np.ndarray,
    transform: Optional[Affine] = None,
    crs: Any = None,
    nodata: float = -9999.0
) -> Tuple[np.ndarray, Optional[Any]]:
    """
    Hydrological conditioning (Pit filling) using pyflwdir C engine for O(N) speed and minimal RAM.
    Displays live active progress bar.
    """
    try:
        import pyflwdir
        trans = transform if transform is not None else Affine.identity()
        is_latlon = (crs is None) or getattr(crs, 'is_geographic', False) or (str(crs) == "EPSG:4326")
        
        est_seconds = max(10, int(dem.size / 1_500_000))
        with LiveProgressBar(f"Pit-Filling & Flow Graph ({dem.size/1e6:.1f}M cells)", total_sec=est_seconds):
            flw = pyflwdir.from_dem(
                data=dem,
                nodata=nodata,
                transform=trans,
                latlon=is_latlon
            )
        return dem, flw
    except Exception as e:
        print(f"  [WARN] pyflwdir fallback: {e}")
        return dem, None


def enforce_geodesic_flat_slope(
    filled_dem: np.ndarray,
    nodata: float = -9999.0,
    slope_epsilon_m_per_cell: float = 1e-5
) -> np.ndarray:
    """
    Enforces a monotonic geodesic downhill gradient across flat plateaus (filled depressions /
    reservoirs / lakes) towards their natural downstream outlets (Wang & Liu 2006 post-conditioning).

    Why this is essential:
    Wang & Liu (2006) depression filling raises depressions to the exact sill elevation Z_sill.
    On the resulting flat plateau, all cells share the exact same elevation, causing pyflwdir's
    D8 routing to pop cells in raster scan order, creating tens-of-km straight trenches along
    cardinal axes (North/South/East/West).

    This function:
    1. Identifies flat plateau cells (cells sharing elevation with equal neighbors).
    2. Identifies boundary outlet cells (flat cells adjacent to lower valid terrain).
    3. Computes the Multi-Source Euclidean Distance Transform from all outlets simultaneously.
    4. Imposes a micro-slope ΔZ = epsilon * distance_from_outlet, ensuring all D8 vectors
       smoothly and naturally curve toward the true hydrological outlet with zero straight lines.
    """
    valid = (filled_dem != nodata) & ~np.isnan(filled_dem)
    if not valid.any():
        return filled_dem

    nrows, ncols = filled_dem.shape
    has_lower_neighbor = np.zeros((nrows, ncols), dtype=bool)
    has_equal_neighbor = np.zeros((nrows, ncols), dtype=bool)

    # Fast slice-shifted 8-neighbor scan
    for dr in (-1, 0, 1):
        for dc in (-1, 0, 1):
            if dr == 0 and dc == 0:
                continue
            r_src_start = max(0, dr)
            r_src_end = nrows + min(0, dr)
            c_src_start = max(0, dc)
            c_src_end = ncols + min(0, dc)

            r_dst_start = max(0, -dr)
            r_dst_end = nrows + min(0, -dr)
            c_dst_start = max(0, -dc)
            c_dst_end = ncols + min(0, -dc)

            src_val = filled_dem[r_src_start:r_src_end, c_src_start:c_src_end]
            src_valid = valid[r_src_start:r_src_end, c_src_start:c_src_end]
            dst_val = filled_dem[r_dst_start:r_dst_end, c_dst_start:c_dst_end]

            has_lower_neighbor[r_dst_start:r_dst_end, c_dst_start:c_dst_end] |= (src_valid & (src_val < dst_val))
            has_equal_neighbor[r_dst_start:r_dst_end, c_dst_start:c_dst_end] |= (src_valid & (src_val == dst_val))

    flat_mask = valid & has_equal_neighbor
    outlet_mask = flat_mask & has_lower_neighbor

    if outlet_mask.any() and flat_mask.any():
        try:
            from scipy.ndimage import distance_transform_edt
            dist_from_outlet = distance_transform_edt(~outlet_mask).astype(np.float32)
            slope_mod = np.where(flat_mask, dist_from_outlet * np.float32(slope_epsilon_m_per_cell), np.float32(0.0))
            filled_dem += slope_mod
            print(f"  [TERRAIN] Geodesic flat slope enforced on {int(flat_mask.sum()):,} plateau cells "
                  f"towards {int(outlet_mask.sum()):,} outlet points (max slope offset: {float(slope_mod.max()):.4f}m)")
        except Exception as ex:
            print(f"  [WARN] enforce_geodesic_flat_slope distance transform failed: {ex}")

    # Micro-hash jitter to break any concentric equidistant ties
    return break_exact_flats(filled_dem, nodata=nodata)


def compute_d8_flow_direction(
    filled_dem: np.ndarray,
    transform: Affine,
    flw_obj: Optional[Any] = None,
    nodata: float = -9999.0
) -> np.ndarray:
    """
    Computes D8 flow direction grid using pyflwdir C-accelerated engine.
    """
    if flw_obj is not None:
        return flw_obj.to_array(ftype='d8').astype(np.uint8)

    try:
        import pyflwdir
        flw = pyflwdir.from_dem(
            data=filled_dem,
            nodata=nodata,
            transform=transform,
            latlon=True
        )
        return flw.to_array(ftype='d8').astype(np.uint8)
    except Exception:
        pass

    # Vectorized fallback
    nrows, ncols = filled_dem.shape
    fdir = np.zeros((nrows, ncols), dtype=np.uint8)
    cell_x_m = abs(transform[0]) * 111320.0
    cell_y_m = abs(transform[4]) * 110540.0
    cell_diag_m = math.sqrt(cell_x_m**2 + cell_y_m**2)

    dist_map = {
        1: cell_x_m, 2: cell_diag_m, 4: cell_y_m, 8: cell_diag_m,
        16: cell_x_m, 32: cell_diag_m, 64: cell_y_m, 128: cell_diag_m,
    }

    for r in range(1, nrows - 1):
        for c in range(1, ncols - 1):
            center_z = filled_dem[r, c]
            if center_z == nodata or np.isnan(center_z):
                continue
            max_slope = 0.0
            best_dir = 0
            for code, (dr, dc) in D8_DELTAS.items():
                nr, nc = r + dr, c + dc
                neighbor_z = filled_dem[nr, nc]
                if neighbor_z != nodata and not np.isnan(neighbor_z):
                    dz = center_z - neighbor_z
                    if dz > 0:
                        slope = dz / dist_map[code]
                        if slope > max_slope:
                            max_slope = slope
                            best_dir = code
            fdir[r, c] = best_dir

    return fdir


def compute_flow_accumulation(
    fdir: np.ndarray,
    flw_obj: Optional[Any] = None
) -> np.ndarray:
    """
    Computes Flow Accumulation Grid using pyflwdir C-accelerated engine.
    """
    if flw_obj is not None:
        return flw_obj.upstream_area(unit='cell').astype(np.int32)

    try:
        import pyflwdir
        flw = pyflwdir.from_array(
            ftype='d8',
            data=fdir,
            latlon=True
        )
        return flw.upstream_area(unit='cell').astype(np.int32)
    except Exception:
        pass

    nrows, ncols = fdir.shape
    in_degree = np.zeros((nrows, ncols), dtype=np.int32)
    acc = np.ones((nrows, ncols), dtype=np.int32)

    for r in range(nrows):
        for c in range(ncols):
            code = int(fdir[r, c])
            if code in D8_DELTAS:
                dr, dc = D8_DELTAS[code]
                nr, nc = r + dr, c + dc
                if 0 <= nr < nrows and 0 <= nc < ncols:
                    in_degree[nr, nc] += 1

    queue = [(r, c) for r in range(nrows) for c in range(ncols) if in_degree[r, c] == 0]
    head = 0
    while head < len(queue):
        r, c = queue[head]
        head += 1

        code = int(fdir[r, c])
        if code in D8_DELTAS:
            dr, dc = D8_DELTAS[code]
            nr, nc = r + dr, c + dc
            if 0 <= nr < nrows and 0 <= nc < ncols:
                acc[nr, nc] += acc[r, c]
                in_degree[nr, nc] -= 1
                if in_degree[nr, nc] == 0:
                    queue.append((nr, nc))

    return acc
def extract_river_network_reaches(
    filled_dem: np.ndarray,
    fdir: np.ndarray,
    acc: np.ndarray,
    transform: Affine,
    crs: Any = None,
    min_stream_acc_cells: int = 300,
    min_lat: Optional[float] = None
) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    """
    Extracts vectorized river network (GeoJSON FeatureCollection) and segment features
    along cells with Flow Accumulation >= min_stream_acc_cells.
    Clips river reaches to never extend south beyond min_lat (southernmost station + 5 km).
    Uses ultra-fast pyproj.Transformer and tqdm progress bar.
    """
    try:
        from tqdm import tqdm
    except ImportError:
        def tqdm(iterable, **kwargs):
            return iterable

    nrows, ncols = fdir.shape
    stream_mask = acc >= min_stream_acc_cells
    visited = np.zeros((nrows, ncols), dtype=bool)

    features = []
    segments_summary = []
    reach_counter = 0

    is_geographic = (crs is None) or getattr(crs, 'is_geographic', False) or (str(crs) == "EPSG:4326")
    
    # Initialize pyproj Transformer (10,000x faster than individual warp_coords calls)
    transformer = None
    if not is_geographic and crs is not None:
        try:
            from pyproj import Transformer
            transformer = Transformer.from_crs(crs, "EPSG:4326", always_xy=True)
        except Exception:
            transformer = None

    def rc_to_lonlat(r: int, c: int) -> Tuple[float, float]:
        x, y = transform * (c + 0.5, r + 0.5)
        if transformer is not None:
            lon, lat = transformer.transform(x, y)
            return lon, lat
        return x, y

    # 1. Compute in-degree specifically within the stream network to find Stream Heads & Junctions
    stream_in_degree = np.zeros((nrows, ncols), dtype=np.int32)
    stream_r, stream_c = np.where(stream_mask)

    for r, c in zip(stream_r, stream_c):
        code = int(fdir[r, c])
        if code in D8_DELTAS:
            dr, dc = D8_DELTAS[code]
            nr, nc = r + dr, c + dc
            if 0 <= nr < nrows and 0 <= nc < ncols and stream_mask[nr, nc]:
                stream_in_degree[nr, nc] += 1

    # Stream starting nodes: Channel Heads (in_degree == 0) and Confluences (in_degree >= 2)
    start_r, start_c = np.where(stream_mask & ((stream_in_degree == 0) | (stream_in_degree >= 2)))
    
    pbar = tqdm(
        total=len(start_r),
        desc="        [Progress] Extracting River Reaches",
        unit="reach",
        ncols=85,
        leave=True
    )

    for sr, sc in zip(start_r, start_c):
        pbar.update(1)
        if visited[sr, sc] and stream_in_degree[sr, sc] < 2:
            continue

        curr_r, curr_c = sr, sc
        pts_r = []
        pts_c = []
        elevs = []

        while 0 <= curr_r < nrows and 0 <= curr_c < ncols and stream_mask[curr_r, curr_c]:
            if len(pts_r) > 0 and visited[curr_r, curr_c]:
                # Reached an already-visited downstream segment
                pts_r.append(curr_r)
                pts_c.append(curr_c)
                elevs.append(float(filled_dem[curr_r, curr_c]))
                break

            visited[curr_r, curr_c] = True
            pts_r.append(curr_r)
            pts_c.append(curr_c)
            elevs.append(float(filled_dem[curr_r, curr_c]))

            next_code = int(fdir[curr_r, curr_c])
            if next_code not in D8_DELTAS:
                break
            dr, dc = D8_DELTAS[next_code]
            next_r, next_c = curr_r + dr, curr_c + dc

            # Stop at downstream confluence junction to chunk cleanly
            if len(pts_r) > 1 and 0 <= next_r < nrows and 0 <= next_c < ncols and stream_in_degree[next_r, next_c] >= 2:
                pts_r.append(next_r)
                pts_c.append(next_c)
                elevs.append(float(filled_dem[next_r, next_c]))
                break

            curr_r, curr_c = next_r, next_c

        if len(pts_r) >= 2:
            # Batch coordinate conversion using Affine & Transformer
            xs = [transform[2] + (c + 0.5) * transform[0] for c in pts_c]
            ys = [transform[5] + (r + 0.5) * transform[4] for r in pts_r]

            if transformer is not None:
                lons, lats = transformer.transform(xs, ys)
                coords = [[round(lo, 6), round(la, 6)] for lo, la in zip(lons, lats)]
            else:
                coords = [[round(x, 6), round(y, 6)] for x, y in zip(xs, ys)]

            # Clip coordinates at southernmost boundary if specified
            if min_lat is not None:
                filtered_coords = []
                for pt in coords:
                    if pt[1] >= min_lat:
                        filtered_coords.append(pt)
                    else:
                        filtered_coords.append([pt[0], round(min_lat, 6)])
                        break
                coords = filtered_coords

            if len(coords) < 2:
                continue

            reach_counter += 1
            reach_id = f"REACH_{reach_counter:05d}"
            length_km = linestring_length_km(coords)
            z_up = elevs[0]
            z_down = elevs[-1]
            dz = max(0.0, z_up - z_down)
            slope = (dz / (length_km * 1000.0)) if length_km > 0 else 0.0

            feature = {
                "type": "Feature",
                "id": reach_id,
                "properties": {
                    "reach_id": reach_id,
                    "length_km": round(length_km, 3),
                    "upstream_elev_m": round(z_up, 2),
                    "downstream_elev_m": round(z_down, 2),
                    "elevation_diff_m": round(dz, 2),
                    "river_slope": round(slope, 6),
                    "start_acc_cells": int(acc[sr, sc]),
                    "end_acc_cells": int(acc[pts_r[-1], pts_c[-1]]),
                },
                "geometry": {
                    "type": "LineString",
                    "coordinates": coords
                }
            }
            features.append(feature)
            segments_summary.append(feature["properties"])

    pbar.close()

    del stream_mask, visited, stream_in_degree, stream_r, stream_c, start_r, start_c

    geojson = {
        "type": "FeatureCollection",
        "features": features
    }
    return geojson, segments_summary


def save_geotiff_raster(data: np.ndarray, transform: Affine, crs: Any, output_path: str, nodata: float = -9999.0):
    """Write 2D numpy array to GeoTIFF raster."""
    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
    dtype = rasterio.float32 if data.dtype == np.float32 else rasterio.int32
    if data.dtype == np.uint8:
        dtype = rasterio.uint8

    with rasterio.open(
        output_path,
        'w',
        driver='GTiff',
        height=data.shape[0],
        width=data.shape[1],
        count=1,
        dtype=dtype,
        crs=crs,
        transform=transform,
        nodata=nodata,
        compress='deflate',
    ) as dst:
        dst.write(data, 1)


def build_river_mask(
    osm_waterways_geojson: Dict[str, Any],
    transform: Affine,
    out_shape: Tuple[int, int],
    crs: Any = None,
    dilate_cells: int = 2
) -> Optional[np.ndarray]:
    """
    Builds a boolean river-footprint mask from OSM waterway lines for river-aware D8
    stopping: overland traces stop when they step onto a river cell instead of crossing
    the river. The mask is the waterway-line rasterization dilated by `dilate_cells`
    (~25m per cell) so paths that pass within a couple of cells of a channel still merge.
    Returns a bool array, or None when no waterways are available / rasterization fails.
    """
    if not osm_waterways_geojson or not osm_waterways_geojson.get("features"):
        return None

    from rasterio.features import rasterize
    from shapely.geometry import shape

    is_geographic = (crs is None) or getattr(crs, 'is_geographic', False) or (str(crs) == "EPSG:4326")
    inv_transformer = None
    if not is_geographic and crs is not None:
        try:
            from pyproj import Transformer
            inv_transformer = Transformer.from_crs("EPSG:4326", crs, always_xy=True)
        except Exception:
            inv_transformer = None

    def _to_raster_crs(geom_dict):
        if inv_transformer is not None:
            from shapely.ops import transform as shp_transform
            geom_obj = shape(geom_dict)
            return shp_transform(lambda x, y: inv_transformer.transform(x, y), geom_obj)
        return shape(geom_dict)

    shapes = []
    for feat in osm_waterways_geojson.get("features", []):
        geom = feat.get("geometry")
        if not geom or geom.get("type") not in ("LineString", "MultiLineString"):
            continue
        try:
            shapes.append((_to_raster_crs(geom), 1))
        except Exception:
            continue

    if not shapes:
        return None

    try:
        raw = rasterize(
            shapes,
            out_shape=tuple(out_shape),
            transform=transform,
            fill=0,
            dtype=np.uint8,
            all_touched=True
        )
    except Exception as ex:
        print(f"  [WARN] River mask rasterization failed: {ex}. River-aware stopping disabled.")
        return None

    mask = raw == 1
    if dilate_cells > 0:
        try:
            from scipy.ndimage import binary_dilation
            mask = binary_dilation(mask, structure=np.ones((3, 3), dtype=bool), iterations=dilate_cells)
        except Exception:
            # Manual fallback: 8-neighbor dilation, `dilate_cells` iterations
            work = mask.copy()
            for _ in range(dilate_cells):
                grown = work.copy()
                grown[1:, :] |= work[:-1, :]
                grown[:-1, :] |= work[1:, :]
                grown[:, 1:] |= work[:, :-1]
                grown[:, :-1] |= work[:, 1:]
                grown[1:, 1:] |= work[:-1, :-1]
                grown[:-1, :-1] |= work[1:, 1:]
                grown[1:, :-1] |= work[:-1, 1:]
                grown[:-1, 1:] |= work[1:, :-1]
                work = grown
            mask = work
    return mask


def build_water_polygon_mask(
    water_polygons_geojson: Dict[str, Any],
    transform: Affine,
    out_shape: Tuple[int, int],
    crs: Any = None
) -> Tuple[Optional[np.ndarray], Optional[np.ndarray]]:
    """
    Round 6 (Phase A2): rasterizes OSM water polygons (reservoirs / lakes / wide rivers)
    into (mask, ids):
      mask = bool array, True on open-water cells
      ids  = uint16 array, 0 = none, else feature_index+1 of the polygon covering the cell
    Used to STOP D8 traces at open-water boundaries (water must not be traced
    cell-by-cell across a reservoir — it enters the OSM backbone / reservoir transit
    instead) and to keep drainage-branch BFS out of water bodies.
    Returns (None, None) when no polygons are available / rasterization fails.
    """
    if not water_polygons_geojson or not water_polygons_geojson.get("features"):
        return None, None

    from rasterio.features import rasterize
    from shapely.geometry import shape

    is_geographic = (crs is None) or getattr(crs, 'is_geographic', False) or (str(crs) == "EPSG:4326")
    inv_transformer = None
    if not is_geographic and crs is not None:
        try:
            from pyproj import Transformer
            inv_transformer = Transformer.from_crs("EPSG:4326", crs, always_xy=True)
        except Exception:
            inv_transformer = None

    def _to_raster_crs(geom_dict):
        if inv_transformer is not None:
            from shapely.ops import transform as shp_transform
            geom_obj = shape(geom_dict)
            return shp_transform(lambda x, y: inv_transformer.transform(x, y), geom_obj)
        return shape(geom_dict)

    shapes = []
    n_used = 0
    for feat in water_polygons_geojson.get("features", []):
        geom = feat.get("geometry")
        if not geom or geom.get("type") not in ("Polygon", "MultiPolygon"):
            continue
        try:
            shapes.append((_to_raster_crs(geom), n_used + 1))
            n_used += 1
        except Exception:
            continue

    if not shapes:
        return None, None
    try:
        ids = rasterize(
            shapes,
            out_shape=tuple(out_shape),
            transform=transform,
            fill=0,
            dtype=np.uint16,
            all_touched=False
        )
    except Exception as ex:
        print(f"  [WARN] Water polygon mask rasterization failed: {ex}.")
        return None, None
    mask = ids > 0
    if not mask.any():
        return None, None
    return mask, ids


def break_exact_flats(
    dem: np.ndarray,
    nodata: float = -9999.0,
    period: int = 64
) -> np.ndarray:
    """
    Breaks D8 straight-trench artifacts in place (round 6 rewrite — hash ULP noise).

    pyflwdir's depression fill (Wang & Liu 2006, used inside `pyflwdir.from_dem`)
    assigns D8 by heap pop order, and its fill raises depressions to their sill level
    EXACTLY (delv = z0 - z1, no epsilon). Two degenerate cases therefore produce
    tens-of-km STRAIGHT trenches along raster rows/columns/diagonals:
      1. EXACT-constant plateaus (calm water returns, constant polygon-burn offsets):
         every plateau cell pops at the same elevation, so the pop order degenerates
         into raster/insertion order (verified: a 2,384-cell same-code run sits exactly
         on a 2,389-cell identical-value run).
      2. Planar slopes: a cell's lower neighbours pop at identical elevations and the
         heap tie-break (row, col) claims every cell from the same side, drawing one
         continuous diagonal for the whole slope.
    The round-5 sawtooth fixed both but encoded a systematic (row+col) mod P pattern
    whose equal-"gutter" lines re-created 30-40 km axis-aligned jumps on the real
    Sirikit reservoir plateau (v5 output, --force on).

    Round-6 fix: superpose a deterministic per-cell micro-noise of ULP scale with NO
    spatial structure (integer hash of row/col). Adjacent cells almost never tie, so
    the fill's pop order follows scrambled micro-topography instead of raster order —
    straight runs collapse to a couple of cells — while the macro drainage is still
    decided by the depression sill, i.e. hydrologically identical. The absolute offset
    stays sub-millimetre wherever the float32 ULP allows (the amplitude adapts to the
    local ULP), always far below any burn depth or real terrain relief. Tiny pits
    created by the noise are healed by pyflwdir's fill.

    `period` is kept for API compatibility and is no longer used.
    """
    valid = (dem != nodata) & ~np.isnan(dem)
    if not valid.any():
        return dem
    nrows, ncols = dem.shape

    # float32 ULP of the local elevation (bounded away from zero near nodata/0 m)
    ulp = np.maximum(np.abs(dem) * np.float32(2.0 ** -23), np.float32(2.0 ** -30))
    # noise amplitude in ULPs: keep the absolute offset <= ~0.5 mm where the ULP
    # allows it, but never below 64 ULPs (enough distinct levels to break local ties)
    amp = np.clip(np.float32(0.0005) / ulp, np.float32(64.0), np.float32(1024.0)).astype(np.int64)
    rr = np.arange(nrows, dtype=np.int64)[:, None]
    cc = np.arange(ncols, dtype=np.int64)[None, :]
    h = np.abs(rr * np.int64(73856093) ^ cc * np.int64(19349663)) % np.int64(1024)
    noise = (h % amp).astype(np.float32) * ulp
    dem += np.where(valid, noise, np.float32(0.0)).astype(np.float32)
    return dem


def burn_stream_network_into_dem(
    dem: np.ndarray,
    transform: Affine,
    osm_waterways_geojson: Dict[str, Any],
    crs: Any = None,
    burn_depth_m: float = 15.0,
    nodata: float = -9999.0,
    water_polygons_geojson: Optional[Dict[str, Any]] = None,
    polygon_burn_depth_m: Optional[float] = None
) -> np.ndarray:
    """
    Applies Hydro-Enforcement (Stream Burning / AGREE technique) to DEM.
    Carves vector river/stream channels from OpenStreetMap into the DEM surface by lowering
    elevation along river channels by `burn_depth_m`.
    When `water_polygons_geojson` is provided, open water surfaces (reservoirs / wide rivers)
    are burned at `polygon_burn_depth_m` (shallower than channels) so D8 flow can cross
    flat water bodies toward the deeper carved channel.
    Single combined rasterization (value 2 = line, 1 = polygon) keeps peak RAM at one uint8 mask.
    """
    if not osm_waterways_geojson or not osm_waterways_geojson.get("features"):
        return dem

    from rasterio.features import rasterize
    from shapely.geometry import shape

    nrows, ncols = dem.shape
    shapes = []

    is_geographic = (crs is None) or getattr(crs, 'is_geographic', False) or (str(crs) == "EPSG:4326")
    inv_transformer = None
    if not is_geographic and crs is not None:
        try:
            from pyproj import Transformer
            inv_transformer = Transformer.from_crs("EPSG:4326", crs, always_xy=True)
        except Exception:
            inv_transformer = None

    def _to_raster_crs(geom_dict):
        if inv_transformer is not None:
            from shapely.ops import transform as shp_transform
            geom_obj = shape(geom_dict)
            return shp_transform(lambda x, y: inv_transformer.transform(x, y), geom_obj)
        return shape(geom_dict)

    # Priority 2 (shallower): open water polygons
    n_polygons = 0
    if water_polygons_geojson and water_polygons_geojson.get("features") and polygon_burn_depth_m and polygon_burn_depth_m > 0:
        for feat in water_polygons_geojson.get("features", []):
            geom = feat.get("geometry")
            if not geom or geom.get("type") not in ("Polygon", "MultiPolygon"):
                continue
            try:
                shapes.append((_to_raster_crs(geom), 1))
                n_polygons += 1
            except Exception:
                continue

    # Priority 1 (deepest): river/stream lines carve through everything
    for feat in osm_waterways_geojson.get("features", []):
        geom = feat.get("geometry")
        if not geom or geom.get("type") not in ("LineString", "MultiLineString"):
            continue
        try:
            shapes.append((_to_raster_crs(geom), 2))
        except Exception:
            continue

    if not shapes:
        return dem

    print(f"  [STREAM BURN] Hydro-enforcing {len(shapes) - n_polygons:,} OSM river lines"
          + (f" + {n_polygons:,} water polygons (-{burn_depth_m}m / -{polygon_burn_depth_m}m)" if n_polygons else f" (-{burn_depth_m}m)")
          + " into DEM...")
    try:
        burn_mask = rasterize(
            shapes,
            out_shape=(nrows, ncols),
            transform=transform,
            fill=0,
            dtype=np.uint8,
            all_touched=True
        )

        valid_mask = (dem != nodata) & ~np.isnan(dem)
        line_mask = burn_mask == 2
        dem[valid_mask & line_mask] -= burn_depth_m
        if n_polygons:
            dem[valid_mask & (burn_mask == 1)] -= polygon_burn_depth_m
        del shapes, burn_mask, valid_mask, line_mask
        import gc
        gc.collect()
        return dem
    except Exception as ex:
        print(f"  [WARN] Stream burning failed: {ex}. Using original DEM.")
        return dem
