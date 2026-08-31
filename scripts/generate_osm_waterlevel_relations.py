import os
import sys
import json
import math
import argparse
import numpy as np

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from scripts.modules.gis_utils import load_stations_for_basin, save_json, linestring_length_km
from scripts.modules.terrain_engine import read_dem_geotiff
from scripts.modules.graph_topology import DirectedRiverGraph, compute_rainfall_lag_bounds, simplify_linestring_coords

def generate_osm_relations(basin: str, basin_dir: str, terrain_dir: str, force: bool = False):
    print(f"\n==================================================================")
    print(f"🗺️ Generating OSM Water Level Relations for Basin: {basin.upper()}")
    print(f"==================================================================")

    station_dir = os.path.join(basin_dir, "station")
    gis_dir = os.path.join(basin_dir, "gis")
    processed_dir = os.path.join(basin_dir, "processed")
    os.makedirs(station_dir, exist_ok=True)
    os.makedirs(processed_dir, exist_ok=True)

    out_raw_path = os.path.join(station_dir, "osm-waterlevel-relations.json")
    out_frontend_path = os.path.join(processed_dir, "relation_waterlevel_frontend.json")

    if not force and os.path.exists(out_raw_path) and os.path.exists(out_frontend_path):
        print(f"  [CACHE] OSM Relations already exist. Use --force to regenerate.")
        return

    # 1. Load Stations and OSM Waterways
    water_st, _ = load_stations_for_basin(basin_dir)
    osm_path = os.path.join(gis_dir, "osm_waterways.geojson")
    if not os.path.exists(osm_path):
        print(f"  ❌ ERROR: Missing OSM waterways at {osm_path}")
        return

    print("  [1/4] Loading OSM Waterways...")
    with open(osm_path, 'r', encoding='utf-8') as f:
        osm_data = json.load(f)

    # 2. Load DEM for elevation sampling
    cond_dem_path = os.path.join(terrain_dir, "conditioned_dem.tif")
    if not os.path.exists(cond_dem_path):
        for candidate in ["cond_dem.tif", "dem.tif", "raw_dem.tif", f"{basin}_cond_dem.tif", f"{basin}_dem.tif", "elevation.tif"]:
            candidate_path = os.path.join(terrain_dir, candidate)
            if os.path.exists(candidate_path):
                cond_dem_path = candidate_path
                break

    if not os.path.exists(cond_dem_path):
        print(f"  ❌ ERROR: Missing DEM at {cond_dem_path}")
        return
    
    print(f"  [2/4] Loading DEM from {cond_dem_path} for slope calculations...")
    dem, transform, crs, nodata = read_dem_geotiff(cond_dem_path)
    
    def sample_elevation(lon: float, lat: float) -> float:
        try:
            # Handle PyProj transformation if necessary, otherwise direct affine
            r, c = ~transform * (lon, lat)
            r, c = int(r), int(c)
            r = max(0, min(r, dem.shape[0] - 1))
            c = max(0, min(c, dem.shape[1] - 1))
            val = float(dem[r, c])
            if val == nodata or val < -100:
                return 0.0
            return val
        except Exception:
            return 0.0

    # 3. Build Directed River Graph
    print("  [3/4] Building Directed River Graph from OSM...")
    river_graph = DirectedRiverGraph(snap_tolerance_deg=0.00035)
    
    for feat in osm_data.get('features', []):
        geom = feat.get('geometry', {})
        props = feat.get('properties', {})
        if geom.get('type') == 'LineString':
            river_graph.add_river_segment(geom['coordinates'], props, sample_elev_fn=sample_elevation)
        elif geom.get('type') == 'MultiLineString':
            for line in geom['coordinates']:
                river_graph.add_river_segment(line, props, sample_elev_fn=sample_elevation)

    river_graph.build_spatial_index()
    print(f"        Graph built with {len(river_graph.nodes)} nodes and {len(river_graph.adj)} interconnected junctions.")

    # 4. Snap Stations and Trace Downstream
    print("  [4/4] Snapping stations and tracing downstream topology...")
    
    # Map station ID to Graph Node ID
    station_nodes = {}
    node_to_station = {}
    for st in water_st:
        st_id = str(st['station_id'])
        lon, lat = float(st['longitude']), float(st['latitude'])
        # Use ranked snapping to prioritize main river classes
        nid, d_deg, attach_meta = river_graph.snap_point_to_graph_ranked(lon, lat, max_dist_deg=0.03) # ~3km
        if nid is not None:
            station_nodes[st_id] = nid
            node_to_station[nid] = st
    
    target_nodes_set = set(node_to_station.keys())
    print(f"        Successfully snapped {len(station_nodes)}/{len(water_st)} water stations to OSM graph.")

    raw_relations = []
    frontend_map = {}

    for st in water_st:
        st_id = str(st['station_id'])
        frontend_map[st_id] = {
            "stationId": st_id,
            "influencingStations": [],
            "downstreamStations": []
        }
        
        start_node = station_nodes.get(st_id)
        if start_node is None:
            continue
            
        # Trace downstream (Dijkstra) up to 250km searching for the NEXT station node
        dist, prev = river_graph.dijkstra_single_source(start_node, target_nodes_set, max_dist_km=250.0)
        
        # Find the closest reachable station that is NOT itself
        best_target_node = None
        min_dist = float('inf')
        for t_node in target_nodes_set:
            if t_node != start_node and t_node in dist:
                if dist[t_node] < min_dist:
                    min_dist = dist[t_node]
                    best_target_node = t_node
                    
        if best_target_node is not None:
            target_st = node_to_station[best_target_node]
            target_id = str(target_st['station_id'])
            
            # Reconstruct exact OSM coordinates
            coords = river_graph.reconstruct_path_from_prev(prev, start_node, best_target_node)
            if not coords:
                continue
                
            dist_km = linestring_length_km(coords)
            z_up = sample_elevation(float(st['longitude']), float(st['latitude']))
            z_down = sample_elevation(float(target_st['longitude']), float(target_st['latitude']))
            dz = max(0.0, z_up - z_down)
            slope = (dz / (dist_km * 1000.0)) if dist_km > 0.001 else 0.0001
            
            lag_min_m, lag_avg_m, lag_max_m, lag_min_h, lag_avg_h, lag_max_h = compute_rainfall_lag_bounds(
                overland_dist_km=0.0, overland_slope=0.0,
                channel_dist_km=dist_km, channel_slope=slope, total_dz_m=dz
            )
            
            # Format 1: Raw Relation for ML (Similar to station-relations.json)
            raw_props = {
                "feature_type": "gauge_to_gauge_flowpath",
                "routing": "osm_pure_vector",
                "from_station_id": st_id,
                "from_station_name": st.get('station_name', ''),
                "to_station_id": target_id,
                "to_station_name": target_st.get('station_name', ''),
                "distance_km": round(dist_km, 2),
                "river_slope": round(slope, 6),
                "elevation_diff_m": round(dz, 2),
                "upstream_elev_m": round(z_up, 2),
                "downstream_elev_m": round(z_down, 2),
                "travel_time_minutes": lag_avg_m,
                "travel_time_minutes_min": lag_min_m,
                "travel_time_minutes_max": lag_max_m,
                "travel_time_hours": lag_avg_h,
                "travel_time_hours_min": lag_min_h,
                "travel_time_hours_max": lag_max_h
            }
            raw_relations.append(raw_props)
            
            # Format 2: Frontend Object directly inside the script
            frontend_map[st_id]["downstreamStations"].append({
                "stationId": target_id,
                "stationName": target_st.get('station_name', ''),
                "stationType": "water_level",
                "distanceKm": round(dist_km, 2),
                "travelTimeMinutes": lag_avg_m,
                "travelTimeMinutesMin": lag_min_m,
                "travelTimeMinutesMax": lag_max_m,
                "travelTimeHours": lag_avg_h,
                "travelTimeHoursMin": lag_min_h,
                "travelTimeHoursMax": lag_max_h,
                "confidence": "HIGH",
                "responseType": "ESTIMATED"
            })

    save_json(raw_relations, out_raw_path)
    save_json(list(frontend_map.values()), out_frontend_path)
    
    print(f"        Extracted {len(raw_relations)} pure OSM waterlevel-to-waterlevel connections.")
    print(f"        ✅ Saved raw ML relations to: {out_raw_path}")
    print(f"        ✅ Saved frontend relations to: {out_frontend_path}")

def main():
    parser = argparse.ArgumentParser(description="Generate pure OSM vector-based Gauge-to-Gauge relations")
    parser.add_argument("--basin", type=str, default="nan", help="River basin slug (e.g. yom, nan, ping, wang, chao-phraya, all)")
    parser.add_argument("--dir", type=str, default="./dataset", help="Dataset directory (supports root e.g. ./dataset or basin dir e.g. ./dataset/nan)")
    parser.add_argument("--terrain-dir", type=str, default="./terrain", help="Terrain DEM directory (independent of dataset --dir)")
    parser.add_argument("--force", action="store_true", help="Force re-generation of relations")
    args = parser.parse_args()

    basin_list = ["yom", "nan", "ping", "wang", "chao-phraya"] if args.basin == "all" else [args.basin]

    for b in basin_list:
        # Smart path resolution for --dir (supports both './dataset' and './dataset/nan')
        if os.path.basename(os.path.normpath(args.dir)) == b:
            basin_dir = args.dir
        else:
            basin_dir = os.path.join(args.dir, b)

        # Smart path resolution for --terrain-dir
        if os.path.basename(os.path.normpath(args.terrain_dir)) == b:
            terrain_basin_dir = args.terrain_dir
        else:
            terrain_basin_dir = os.path.join(args.terrain_dir, b)
            if not os.path.exists(terrain_basin_dir) and os.path.exists(args.terrain_dir):
                terrain_basin_dir = args.terrain_dir

        if not os.path.exists(basin_dir):
            print(f"❌ ERROR: Basin directory not found: {basin_dir} (Check --dir path)", file=sys.stderr)
            continue

        generate_osm_relations(b, basin_dir, terrain_basin_dir, force=args.force)

if __name__ == "__main__":
    main()
