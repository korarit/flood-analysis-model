"""
Backend Exporter Module
Formats and exports model results to:
1. station_relations_db.json (matching PostgreSQL station_relations table schema in backend-req.md)
2. relations_frontend.json (matching frontend StationRelations.tsx)
3. Model Metadata & Summary Statistics
"""

from typing import Dict, List, Any
import json
import os
from .gis_utils import save_json


def export_backend_station_relations(
    gauge_relations: List[Dict[str, Any]],
    rainfall_relations: List[Dict[str, Any]],
    output_db_path: str,
    output_frontend_path: str
):
    """
    Exports relations in backend database schema and frontend UI structure.
    """
    db_records = []
    frontend_map: Dict[str, Dict[str, Any]] = {}

    # 1. Process Gauge-to-Gauge Relations
    for rel in gauge_relations:
        st_id = str(rel.get('station_id', rel.get('from_station_id', ''))).strip()
        target_id = str(rel.get('target_station_id', rel.get('to_station_id', ''))).strip()
        if not st_id or not target_id:
            continue

        tt_hours = float(rel.get('travel_time_hours', 0.0))
        tt_min_h = float(rel.get('travel_time_hours_min', tt_hours))
        tt_max_h = float(rel.get('travel_time_hours_max', tt_hours))

        tt_avg_m = int(rel.get('travel_time_minutes', int(round(tt_hours * 60))))
        tt_min_m = int(rel.get('travel_time_minutes_min', int(round(tt_min_h * 60))))
        tt_max_m = int(rel.get('travel_time_minutes_max', int(round(tt_max_h * 60))))

        record = {
            "stationId": st_id,
            "targetStationId": target_id,
            "relationType": "downstream_gauge",
            "distanceKm": float(rel.get('distance_km', 0.0)),
            "travelTimeMinutes": tt_avg_m,
            "travelTimeMinutesMin": tt_min_m,
            "travelTimeMinutesMax": tt_max_m,
            "travelTimeHours": tt_hours,
            "travelTimeHoursMin": tt_min_h,
            "travelTimeHoursMax": tt_max_h,
            "influenceWeightPercent": 100.0,
            "responseType": rel.get('response_type', 'ESTIMATED'),
            "confidenceLevel": rel.get('confidence', 'MEDIUM'),
            "metadata": {
                "riverSlope": rel.get('river_slope', 0.0),
                "elevationDiffM": rel.get('elevation_diff_m', 0.0),
                "avgHoldingDurationHours": rel.get('avg_holding_duration_hours', 0.0),
                "eventCount": rel.get('event_count', 0),
                "detectionRule": rel.get('detection_rule', 'continuous_rise_4h_with_plateau_midpoint')
            }
        }
        db_records.append(record)

        # Frontend map grouping
        if st_id not in frontend_map:
            frontend_map[st_id] = {"stationId": st_id, "influencingStations": [], "downstreamStations": []}
        if target_id not in frontend_map:
            frontend_map[target_id] = {"stationId": target_id, "influencingStations": [], "downstreamStations": []}

        frontend_map[st_id]["downstreamStations"].append({
            "stationId": target_id,
            "stationName": rel.get('to_station_name', ''),
            "stationType": "water_level",
            "distanceKm": float(rel.get('distance_km', 0.0)),
            "travelTimeMinutes": tt_avg_m,
            "travelTimeMinutesMin": tt_min_m,
            "travelTimeMinutesMax": tt_max_m,
            "travelTimeHours": tt_hours,
            "travelTimeHoursMin": tt_min_h,
            "travelTimeHoursMax": tt_max_h,
            "confidence": rel.get('confidence', 'MEDIUM'),
            "responseType": rel.get('response_type', 'ESTIMATED')
        })

    # 2. Process Rainfall-to-Gauge Relations
    for rel in rainfall_relations:
        r_id = str(rel.get('from_station_id', rel.get('station_id', ''))).strip()
        target_water_id = str(rel.get('to_station_id', rel.get('target_station_id', ''))).strip()
        if not r_id or not target_water_id:
            continue

        lag_hours = float(rel.get('response_lag_hours', 4.0))
        lag_min_h = float(rel.get('response_lag_hours_min', lag_hours))
        lag_max_h = float(rel.get('response_lag_hours_max', lag_hours))

        lag_avg_m = int(rel.get('response_lag_minutes', int(round(lag_hours * 60))))
        lag_min_m = int(rel.get('response_lag_minutes_min', int(round(lag_min_h * 60))))
        lag_max_m = int(rel.get('response_lag_minutes_max', int(round(lag_max_h * 60))))

        dist_km = float(rel.get('total_distance_km', 0.0))
        weight_pct = float(rel.get('influence_weight_percent', 30.0))

        record = {
            "stationId": target_water_id,
            "targetStationId": r_id,
            "relationType": "rainfall_influence",
            "distanceKm": dist_km,
            "travelTimeMinutes": lag_avg_m,
            "travelTimeMinutesMin": lag_min_m,
            "travelTimeMinutesMax": lag_max_m,
            "travelTimeHours": lag_hours,
            "travelTimeHoursMin": lag_min_h,
            "travelTimeHoursMax": lag_max_h,
            "influenceWeightPercent": weight_pct,
            "responseType": "ESTIMATED",
            "confidenceLevel": "HIGH" if dist_km <= 20.0 else "MEDIUM",
            "metadata": {
                "typicalRainThresholdMm": rel.get('typical_rain_threshold_mm', 45.0),
                "elevationDiffM": rel.get('elevation_diff_m', 0.0),
                "slope": rel.get('slope', 0.0),
                "detectionRule": "continuous_rise_4h_with_plateau_midpoint"
            }
        }
        db_records.append(record)

        if target_water_id not in frontend_map:
            frontend_map[target_water_id] = {"stationId": target_water_id, "influencingStations": [], "downstreamStations": []}

        frontend_map[target_water_id]["influencingStations"].append({
            "stationId": r_id,
            "stationName": rel.get('from_station_name', ''),
            "stationType": "rainfall",
            "distanceKm": dist_km,
            "travelTimeMinutes": lag_avg_m,
            "travelTimeMinutesMin": lag_min_m,
            "travelTimeMinutesMax": lag_max_m,
            "travelTimeHours": lag_hours,
            "travelTimeHoursMin": lag_min_h,
            "travelTimeHoursMax": lag_max_h,
            "influenceWeightPercent": weight_pct
        })

    save_json(db_records, output_db_path)
    save_json(list(frontend_map.values()), output_frontend_path)
