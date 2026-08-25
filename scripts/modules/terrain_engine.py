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


def read_dem_geotiff(dem_path: str) -> Tuple[np.ndarray, Affine, Any, float]:
    """Read DEM raster and return (elevation_array, affine_transform, crs, nodata)."""
    with rasterio.open(dem_path) as src:
        elev = src.read(1).astype(np.float32)
        transform = src.transform
        crs = src.crs
        nodata = src.nodata if src.nodata is not None else -9999.0
    return elev, transform, crs, nodata


def fill_depressions_priority_flood(dem: np.ndarray, nodata: float = -9999.0) -> np.ndarray:
    """
    Hydrological conditioning (Pit filling) using Priority-Flood algorithm.
    Ensures water flows continuously without getting trapped in local depressions.
    """
    import heapq

    nrows, ncols = dem.shape
    filled = np.copy(dem)
    is_nodata = (dem == nodata) | np.isnan(dem) | (dem < -500.0)
    visited = np.zeros((nrows, ncols), dtype=bool)

    pq: List[Tuple[float, int, int]] = []

    # Push all border cells and nodata boundary cells to priority queue
    for r in range(nrows):
        for c in (0, ncols - 1):
            if not is_nodata[r, c]:
                heapq.heappush(pq, (float(filled[r, c]), r, c))
            visited[r, c] = True

    for c in range(ncols):
        for r in (0, nrows - 1):
            if not visited[r, c]:
                if not is_nodata[r, c]:
                    heapq.heappush(pq, (float(filled[r, c]), r, c))
                visited[r, c] = True

    # Directions (8-connected)
    dr = [-1, -1, -1, 0, 0, 1, 1, 1]
    dc = [-1, 0, 1, -1, 1, -1, 0, 1]

    while pq:
        elev_cur, r, c = heapq.heappop(pq)
        for i in range(8):
            nr, nc = r + dr[i], c + dc[i]
            if 0 <= nr < nrows and 0 <= nc < ncols and not visited[nr, nc]:
                visited[nr, nc] = True
                if not is_nodata[nr, nc]:
                    if filled[nr, nc] < elev_cur:
                        filled[nr, nc] = elev_cur
                    heapq.heappush(pq, (float(filled[nr, nc]), nr, nc))

    return filled


def compute_d8_flow_direction(filled_dem: np.ndarray, transform: Affine, nodata: float = -9999.0) -> np.ndarray:
    """
    Computes D8 flow direction (steepest downhill slope among 8 neighbors).
    Returns D8 code (1, 2, 4, 8, 16, 32, 64, 128) or 0 if flat/pit/sink.
    """
    nrows, ncols = filled_dem.shape
    fdir = np.zeros((nrows, ncols), dtype=np.uint8)
    
    # Cell dimensions in meters approximately from affine transform
    cell_x_m = abs(transform[0]) * 111320.0
    cell_y_m = abs(transform[4]) * 110540.0
    cell_diag_m = math.sqrt(cell_x_m**2 + cell_y_m**2)

    dist_map = {
        1: cell_x_m,
        2: cell_diag_m,
        4: cell_y_m,
        8: cell_diag_m,
        16: cell_x_m,
        32: cell_diag_m,
        64: cell_y_m,
        128: cell_diag_m,
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


def compute_flow_accumulation(fdir: np.ndarray) -> np.ndarray:
    """
    Computes Flow Accumulation Grid: number of contributing upstream cells for each cell.
    Uses topological sort on the in-degree of the D8 flow directed graph.
    """
    nrows, ncols = fdir.shape
    in_degree = np.zeros((nrows, ncols), dtype=np.int32)
    acc = np.ones((nrows, ncols), dtype=np.int32)

    # Calculate in-degree for each cell
    for r in range(nrows):
        for c in range(ncols):
            code = int(fdir[r, c])
            if code in D8_DELTAS:
                dr, dc = D8_DELTAS[code]
                nr, nc = r + dr, c + dc
                if 0 <= nr < nrows and 0 <= nc < ncols:
                    in_degree[nr, nc] += 1

    # Queue all headwater cells (in-degree == 0)
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
    min_stream_acc_cells: int = 500
) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    """
    Extracts vectorized river network (GeoJSON FeatureCollection) and segment features
    along cells with Flow Accumulation >= min_stream_acc_cells.
    """
    nrows, ncols = fdir.shape
    stream_mask = acc >= min_stream_acc_cells
    visited = np.zeros((nrows, ncols), dtype=bool)

    features = []
    segments_summary = []
    reach_counter = 0

    # Convert (row, col) to (lon, lat) using transform
    def rc_to_lonlat(r: int, c: int) -> Tuple[float, float]:
        lon, lat = transform * (c + 0.5, r + 0.5)
        return lon, lat

    # Trace stream lines from stream heads
    for r in range(nrows):
        for c in range(ncols):
            if stream_mask[r, c] and not visited[r, c]:
                # Check if this is a stream head or confluence start
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
                    
                    # Stop segment at confluences or stream end
                    curr_r, curr_c = next_r, next_c
                    if len(coords) > 200: # chunk long continuous lines
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
