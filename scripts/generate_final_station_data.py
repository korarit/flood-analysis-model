#!/usr/bin/env python3
"""
Generate Final Station Dataset (Key-Value Map by Station ID)
============================================================
Merges station metadata (from {basin}_waterlevel_stations.json) with
hydrological topological network relations (from relations_frontend.json)
into a single high-performance O(1) keyed JSON dataset:

    dataset/{basin}/final_station_data.json

Key Features:
- O(1) Direct ID Lookup: Output is a JSON Object with station ID as key (e.g. {"2477": {...}})
- Hydrological Linkage: Embeds influencingStations (upstream rain) and downstreamStations (downstream gauges)
- Clean Standard Schema: Normalized camelCase schema ready for Frontend & Backend consumption

Usage:
  python scripts/generate_final_station_data.py --basin yom
  python scripts/generate_final_station_data.py --basin all
  python scripts/generate_final_station_data.py --basin yom --dir ./dataset
"""

import os
import sys
import json
import argparse
from pathlib import Path
from typing import Dict, List, Any, Optional

# Add parent directory to sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from scripts.modules.basin_registry import get_all_slugs, get_basin

BASIN_LIST = get_all_slugs()



def load_json_file(file_path: Path) -> Optional[Any]:
    """Safely loads a JSON file or returns None if not found/corrupted."""
    if not file_path.exists():
        return None
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"  [WARN] Failed to load JSON from {file_path}: {e}")
        return None


def save_json_file(data: Any, file_path: Path, indent: int = 2):
    """Saves data to JSON with UTF-8 encoding."""
    file_path.parent.mkdir(parents=True, exist_ok=True)
    with open(file_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=indent)


def build_final_station_dataset(basin: str, dataset_dir: Path, out_filename: str = "final_station_data.json") -> bool:
    """
    Merges waterlevel stations metadata and relations_frontend.json for a given basin.
    """
    basin_dir = dataset_dir / basin
    if not basin_dir.exists():
        print(f"❌ Basin directory not found: {basin_dir}")
        return False

    water_stations_path = basin_dir / "station" / f"{basin}_waterlevel_stations.json"
    relations_path = basin_dir / "processed" / "relations_frontend.json"
    output_path = basin_dir / out_filename

    print(f"\n🌊 [{basin.upper()}] Merging Station Data & Hydrological Relations...")
    print(f"  • Stations Source  : {water_stations_path.relative_to(dataset_dir.parent) if water_stations_path.exists() else 'NOT FOUND'}")
    print(f"  • Relations Source : {relations_path.relative_to(dataset_dir.parent) if relations_path.exists() else 'NOT FOUND'}")

    water_stations_raw = load_json_file(water_stations_path)
    if not water_stations_raw:
        print(f"  ⚠️ No waterlevel stations found at {water_stations_path}. Skipping.")
        return False

    # 1. Index relations by stationId for O(1) lookup
    relations_raw = load_json_file(relations_path) or []
    relations_map: Dict[str, Dict[str, List[Any]]] = {}
    for item in relations_raw:
        st_id = str(item.get("stationId", "")).strip()
        if st_id:
            relations_map[st_id] = {
                "influencingStations": item.get("influencingStations", []),
                "downstreamStations": item.get("downstreamStations", [])
            }

    # 2. Merge into Keyed Dictionary (stationId as Key)
    final_stations_map: Dict[str, Dict[str, Any]] = {}
    influencing_count = 0
    downstream_count = 0

    for item in water_stations_raw:
        st_info = item.get("station", {})
        agency_info = item.get("agency", {})
        geocode_info = item.get("geocode", {})
        basin_info = item.get("basin", {})

        st_id = str(st_info.get("id", "")).strip()
        if not st_id:
            continue

        rel = relations_map.get(st_id, {})
        influencing = rel.get("influencingStations", [])
        downstream = rel.get("downstreamStations", [])

        if influencing:
            influencing_count += 1
        if downstream:
            downstream_count += 1

        final_entry: Dict[str, Any] = {
            "id": st_id,
            "code": st_info.get("tele_station_oldcode") or "",
            "name": st_info.get("tele_station_name") or {"th": "", "en": ""},
            "stationType": "water_level",
            "lat": st_info.get("tele_station_lat"),
            "long": st_info.get("tele_station_long"),
            "riverName": st_info.get("river_name") or "",
            "groundLevel": st_info.get("ground_level"),
            "qmax": st_info.get("qmax"),
            "minBank": st_info.get("min_bank"),
            "sponsorBy": st_info.get("sponsor_by"),
            "subBasinId": st_info.get("sub_basin_id"),
            "subBasinName": st_info.get("sub_basin_name"),
            "agency": {
                "name": agency_info.get("agency_name") or {"th": "", "en": ""},
                "shortname": agency_info.get("agency_shortname") or {"th": "", "en": ""},
                "code": agency_info.get("agency_code") or ""
            },
            "geocode": {
                "areaCode": geocode_info.get("area_code"),
                "areaName": geocode_info.get("area_name") or {"th": "", "en": ""},
                "amphoe": geocode_info.get("amphoe_name") or {"th": "", "en": ""},
                "tumbon": geocode_info.get("tumbon_name") or {"th": "", "en": ""},
                "province": geocode_info.get("province_name") or {"th": "", "en": ""},
                "provinceCode": geocode_info.get("province_code"),
                "geoCode": geocode_info.get("geo_code"),
                "ridCode": geocode_info.get("rid_code"),
                "tmdCode": geocode_info.get("tmd_code"),
                "warningZone": geocode_info.get("warning_zone")
            },
            "basin": {
                "id": basin_info.get("id"),
                "code": basin_info.get("basin_code"),
                "name": basin_info.get("basin_name") or {"th": "", "en": ""}
            },
            "influencingStations": influencing,
            "downstreamStations": downstream
        }

        final_stations_map[st_id] = final_entry

    # 3. Save Final Keyed Dataset
    save_json_file(final_stations_map, output_path)
    file_size_kb = output_path.stat().st_size / 1024.0

    print(f"  ✅ Saved: {output_path.relative_to(dataset_dir.parent)} ({file_size_kb:.1f} KB)")
    print(f"  📊 Summary: {len(final_stations_map)} stations total | {influencing_count} with upstream rain relations | {downstream_count} with downstream river connections")
    return True


def main():
    parser = argparse.ArgumentParser(description="Merge station metadata and hydrological relations into final_station_data.json keyed by station ID")
    parser.add_argument("--basin", type=str, default="yom", help="River basin slug (yom, nan, ping, wang, chao-phraya, or all)")
    parser.add_argument("--dir", type=str, default="./dataset", help="Root path to dataset directory")
    parser.add_argument("--out-name", type=str, default="final_station_data.json", help="Output filename inside dataset/{basin}/")
    args = parser.parse_args()

    dataset_dir = Path(args.dir).resolve()
    basins_to_process = BASIN_LIST if args.basin.lower() == "all" else [args.basin.lower()]

    print("=" * 70)
    print("🚀 Final Station Dataset Generator (Key-Value Dictionary by Station ID)")
    print("=" * 70)

    success_count = 0
    for b in basins_to_process:
        if build_final_station_dataset(b, dataset_dir, args.out_name):
            success_count += 1

    print("\n" + "=" * 70)
    print(f"✨ Completed: Processed {success_count}/{len(basins_to_process)} basin(s).")
    print("=" * 70)


if __name__ == "__main__":
    main()
