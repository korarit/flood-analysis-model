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
    Clips DEM raster directly to a specific sub-basin polygon at native 12.5m resolution.
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
    dem_path: str,
    max_cells: int = 25_000_000
) -> Tuple[np.ndarray, Affine, Any, float]:
    """
    Read DEM raster with memory-adaptive scaling.
    If grid size exceeds max_cells (default 25 Million cells, ~100 MB RAM), downsamples adaptively
    to fit comfortably within RAM while preserving full hydrological fidelity.
    """
    from rasterio.enums import Resampling

    env_max = os.environ.get("MAX_DEM_CELLS")
    if env_max:
        try:
            max_cells = int(env_max)
        except Exception:
            pass

    with rasterio.open(dem_path) as src:
        nodata = src.nodata if src.nodata is not None else -9999.0
        orig_h, orig_w = src.height, src.width
        total_cells = orig_h * orig_w

        if total_cells > max_cells:
            scale = math.sqrt(max_cells / float(total_cells))
            new_h = max(100, int(orig_h * scale))
            new_w = max(100, int(orig_w * scale))
            print(f"  [RAM OPT] Large DEM detected ({total_cells:,} cells).")
            print(f"            Scaling adaptively to {new_h:,} x {new_w:,} ({new_h * new_w:,} cells, Low-RAM)...")
            
            elev = src.read(
                1,
                out_shape=(new_h, new_w),
                resampling=Resampling.bilinear
            ).astype(np.float32)

            transform = src.transform * src.transform.scale(
                (src.width / new_w),
                (src.height / new_h)
            )
        else:
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
    min_stream_acc_cells: int = 500
) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    """
    Extracts vectorized river network (GeoJSON FeatureCollection) and segment features
    along cells with Flow Accumulation >= min_stream_acc_cells.
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
            if len(pts_r) >= 300: # chunk long continuous main rivers
                break

        if len(pts_r) >= 2:
            # Batch coordinate conversion using Affine & Transformer
            xs = [transform[2] + (c + 0.5) * transform[0] for c in pts_c]
            ys = [transform[5] + (r + 0.5) * transform[4] for r in pts_r]

            if transformer is not None:
                lons, lats = transformer.transform(xs, ys)
                coords = [[round(lo, 6), round(la, 6)] for lo, la in zip(lons, lats)]
            else:
                coords = [[round(x, 6), round(y, 6)] for x, y in zip(xs, ys)]

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


def burn_stream_network_into_dem(
    dem: np.ndarray,
    transform: Affine,
    osm_waterways_geojson: Dict[str, Any],
    crs: Any = None,
    burn_depth_m: float = 15.0,
    nodata: float = -9999.0
) -> np.ndarray:
    """
    Applies Hydro-Enforcement (Stream Burning / AGREE technique) to DEM.
    Carves vector river/stream channels from OpenStreetMap into the DEM surface by lowering
    elevation along river channels by `burn_depth_m`.
    This forces D8 hydrological flow directions to strictly follow natural river beds in flat terrain.
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

    for feat in osm_waterways_geojson.get("features", []):
        geom = feat.get("geometry")
        if not geom or geom.get("type") not in ("LineString", "MultiLineString"):
            continue
        try:
            if inv_transformer is not None:
                from shapely.ops import transform as shp_transform
                geom_obj = shape(geom)
                geom_proj = shp_transform(lambda x, y: inv_transformer.transform(x, y), geom_obj)
                shapes.append((geom_proj, 1))
            else:
                shapes.append((shape(geom), 1))
        except Exception:
            continue

    if not shapes:
        return dem

    print(f"  [STREAM BURN] Hydro-enforcing {len(shapes):,} OSM river lines into DEM (-{burn_depth_m}m)...")
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
        dem[valid_mask & (burn_mask > 0)] -= burn_depth_m
        del shapes, burn_mask, valid_mask
        import gc
        gc.collect()
        return dem
    except Exception as ex:
        print(f"  [WARN] Stream burning failed: {ex}. Using original DEM.")
        return dem
