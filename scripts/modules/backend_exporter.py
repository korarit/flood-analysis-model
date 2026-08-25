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
        st_id = str(rel['station_id'])
        target_id = str(rel['target_station_id'])

        record = {
            "stationId": st_id,
            "targetStationId": target_id,
            "relationType": "downstream_gauge",
            "distanceKm": float(rel.get('distance_km', 0.0)),
            "travelTimeHours": float(rel.get('travel_time_hours', 0.0)),
            "travelTimeHoursMin": float(rel.get('travel_time_hours_min', 0.0)),
            "travelTimeHoursMax": float(rel.get('travel_time_hours_max', 0.0)),
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
            "distanceKm": rel.get('distance_km', 0.0),
            "travelTimeHours": rel.get('travel_time_hours', 0.0),
            "confidence": rel.get('confidence', 'MEDIUM'),
            "responseType": rel.get('response_type', 'ESTIMATED')
        })

    # 2. Process Rainfall-to-Gauge Relations
    for rel in rainfall_relations:
        r_id = str(rel['from_station_id'])
        target_water_id = str(rel['to_station_id'])

        record = {
            "stationId": target_water_id,
            "targetStationId": r_id,
            "relationType": "rainfall_influence",
            "distanceKm": float(rel.get('total_distance_km', 0.0)),
            "travelTimeHours": float(rel.get('response_lag_hours', 4.0)),
            "travelTimeHoursMin": float(rel.get('response_lag_hours', 4.0) * 0.8),
            "travelTimeHoursMax": float(rel.get('response_lag_hours', 4.0) * 1.3),
            "influenceWeightPercent": float(rel.get('influence_weight_percent', 30.0)),
            "responseType": "ESTIMATED",
            "confidenceLevel": "MEDIUM",
            "metadata": {
                "typicalRainThresholdMm": rel.get('typical_rain_threshold_mm', 45.0),
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
            "distanceKm": rel.get('total_distance_km', 0.0),
            "travelTimeHours": rel.get('response_lag_hours', 4.0),
            "influenceWeightPercent": rel.get('influence_weight_percent', 30.0)
        })

    save_json(db_records, output_db_path)
    save_json(list(frontend_map.values()), output_frontend_path)
