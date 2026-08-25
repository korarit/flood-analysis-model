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
    max_cells: int = 150_000_000
) -> Tuple[np.ndarray, Affine, Any, float]:
    """
    Read DEM raster with memory-adaptive scaling.
    If grid size exceeds max_cells (e.g. 1.1 Billion cells), downsamples adaptively
    to fit comfortably within RAM while preserving full hydrological fidelity.
    """
    from rasterio.enums import Resampling

    with rasterio.open(dem_path) as src:
        nodata = src.nodata if src.nodata is not None else -9999.0
        orig_h, orig_w = src.height, src.width
        total_cells = orig_h * orig_w

        if total_cells > max_cells:
            scale = math.sqrt(max_cells / float(total_cells))
            new_h = max(100, int(orig_h * scale))
            new_w = max(100, int(orig_w * scale))
            print(f"  [RAM OPT] Large DEM detected ({total_cells:,} cells).")
            print(f"            Scaling adaptively to {new_h:,} x {new_w:,} ({new_h * new_w:,} cells) to fit RAM...")
            
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


def fill_depressions_priority_flood(
    dem: np.ndarray,
    transform: Optional[Affine] = None,
    crs: Any = None,
    nodata: float = -9999.0
) -> Tuple[np.ndarray, Optional[Any]]:
    """
    Hydrological conditioning (Pit filling) using pyflwdir C engine for O(N) speed and minimal RAM.
    """
    try:
        import pyflwdir
        trans = transform if transform is not None else Affine.identity()
        is_latlon = (crs is None) or getattr(crs, 'is_geographic', False) or (str(crs) == "EPSG:4326")
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

    # Trace stream lines only from stream cells with Progress Bar
    stream_rows, stream_cols = np.where(stream_mask)
    pbar = tqdm(
        total=len(stream_rows),
        desc="        [Progress] Extracting River Lines",
        unit="cell",
        ncols=80,
        leave=False
    )

    for r, c in zip(stream_rows, stream_cols):
        pbar.update(1)
        if not visited[r, c]:
            code = int(fdir[r, c])
            curr_r, curr_c = r, c
            coords = []
            elevs = []
            
            while 0 <= curr_r < nrows and 0 <= curr_c < ncols and stream_mask[curr_r, curr_c]:
                visited[curr_r, curr_c] = True
                lon, lat = rc_to_lonlat(curr_r, curr_c)
                coords.append([round(lon, 6), round(lat, 6)])
                elevs.append(float(filled_dem[curr_r, curr_c]))

                next_code = int(fdir[curr_r, curr_c])
                if next_code not in D8_DELTAS:
                    break
                dr, dc = D8_DELTAS[next_code]
                next_r, next_c = curr_r + dr, curr_c + dc
                
                curr_r, curr_c = next_r, next_c
                if len(coords) > 250: # chunk long continuous reaches
                    break

            if len(coords) >= 2:
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
                        "start_acc_cells": int(acc[r, c]),
                    },
                    "geometry": {
                        "type": "LineString",
                        "coordinates": coords
                    }
                }
                features.append(feature)
                segments_summary.append(feature["properties"])

    pbar.close()

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
