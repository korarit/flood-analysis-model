"""
Script to remove all DWR (กรมทรัพยากรน้ำ / ทน.) stations from Waterlevel Station Datasets
and purge corresponding relations across all basins in flood-analysis-model.
"""

import os
import sys
import json
import csv
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

BASE_DIR = Path(__file__).resolve().parent.parent / "dataset"

def is_dwr_station(item: dict) -> bool:
    ag = item.get("agency", {})
    ag_short = (ag.get("agency_shortname", {}).get("th") or ag.get("agency_shortname", {}).get("en") or "").strip()
    ag_name = (ag.get("agency_name", {}).get("th") or "").strip()
    return ag_short == "ทน." or ("กรมทรัพยากรน้ำ" in ag_name and "บาดาล" not in ag_name)

def update_csv(csv_path: Path, remaining_stations: list):
    if not remaining_stations:
        if csv_path.exists():
            csv_path.unlink()
        return
        
    # Standard CSV field structure based on station object
    sample = remaining_stations[0]
    fieldnames = [
        "id", "oldcode", "station_type", "name_th", "name_en", "lat", "lon",
        "ground_level", "min_bank", "qmax", "river_name", "sponsor_by",
        "agency_name_th", "agency_shortname_th", "agency_code",
        "subdistrict_name_th", "district_name_th", "province_name_th"
    ]
    
    with open(csv_path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for item in remaining_stations:
            st = item.get("station", {})
            ag = item.get("agency", {})
            geo = item.get("geocode", {})
            row = {
                "id": st.get("id") or item.get("id"),
                "oldcode": st.get("tele_station_oldcode"),
                "station_type": "waterlevel",
                "name_th": st.get("tele_station_name", {}).get("th"),
                "name_en": st.get("tele_station_name", {}).get("en") or st.get("tele_station_name", {}).get("th"),
                "lat": st.get("tele_station_lat"),
                "lon": st.get("tele_station_long"),
                "ground_level": st.get("ground_level"),
                "min_bank": st.get("min_bank"),
                "qmax": st.get("qmax"),
                "river_name": item.get("river_name"),
                "sponsor_by": st.get("sponsor_by"),
                "agency_name_th": ag.get("agency_name", {}).get("th"),
                "agency_shortname_th": ag.get("agency_shortname", {}).get("th"),
                "agency_code": ag.get("agency_code"),
                "subdistrict_name_th": geo.get("tumbon_name", {}).get("th"),
                "district_name_th": geo.get("amphoe_name", {}).get("th"),
                "province_name_th": geo.get("province_name", {}).get("th")
            }
            writer.writerow(row)

def main():
    print("================================================================")
    print("  PURGING DWR (ทน.) STATIONS FROM WATERLEVEL DATASETS & RELATIONS")
    print("================================================================")
    
    basins = [d.name for d in BASE_DIR.iterdir() if d.is_dir() and not d.name.startswith(".")]
    basins.sort()
    
    all_removed_dwr_ids = set()
    total_stations_before = 0
    total_stations_after = 0
    total_dwr_removed = 0
    
    # 1. Clean Waterlevel Station JSON & CSV
    for basin in basins:
        station_dir = BASE_DIR / basin / "station"
        wl_json_path = station_dir / f"{basin}_waterlevel_stations.json"
        wl_csv_path = station_dir / f"{basin}_waterlevel_stations.csv"
        
        if not wl_json_path.exists():
            continue
            
        with open(wl_json_path, "r", encoding="utf-8") as f:
            data = json.load(f)
            
        total_stations_before += len(data)
        
        filtered = []
        basin_removed = 0
        for item in data:
            if is_dwr_station(item):
                sid = str(item.get("station", {}).get("id") or item.get("id") or "")
                if sid:
                    all_removed_dwr_ids.add(sid)
                basin_removed += 1
            else:
                filtered.append(item)
                
        total_stations_after += len(filtered)
        total_dwr_removed += basin_removed
        
        # Save updated JSON
        with open(wl_json_path, "w", encoding="utf-8") as f:
            json.dump(filtered, f, ensure_ascii=False, indent=2)
            
        # Save updated CSV if it exists
        if wl_csv_path.exists():
            update_csv(wl_csv_path, filtered)
            
        print(f"Basin {basin:12}: {len(data):3} -> {len(filtered):3} stations (Removed {basin_removed:2} DWR)")

    print(f"\nTotal Waterlevel Stations: {total_stations_before} -> {total_stations_after} (-{total_dwr_removed})")
    print(f"Total Unique DWR Station IDs removed: {len(all_removed_dwr_ids)}")
    
    # 2. Clean Relations in processed/
    print("\nCleaning relationship files...")
    for basin in basins:
        processed_dir = BASE_DIR / basin / "processed"
        if not processed_dir.exists():
            continue
            
        # relations_frontend.json
        rel_front_path = processed_dir / "relations_frontend.json"
        if rel_front_path.exists():
            with open(rel_front_path, "r", encoding="utf-8") as f:
                rels = json.load(f)
            new_rels = [r for r in rels if str(r.get("stationId", "")) not in all_removed_dwr_ids]
            with open(rel_front_path, "w", encoding="utf-8") as f:
                json.dump(new_rels, f, ensure_ascii=False, indent=2)
            print(f"  {basin}/relations_frontend.json: {len(rels)} -> {len(new_rels)} (-{len(rels)-len(new_rels)})")
            
        # relation_waterlevel_frontend.json
        rel_wl_path = processed_dir / "relation_waterlevel_frontend.json"
        if rel_wl_path.exists():
            with open(rel_wl_path, "r", encoding="utf-8") as f:
                rels = json.load(f)
            new_rels = [r for r in rels if str(r.get("stationId", "")) not in all_removed_dwr_ids]
            with open(rel_wl_path, "w", encoding="utf-8") as f:
                json.dump(new_rels, f, ensure_ascii=False, indent=2)
            print(f"  {basin}/relation_waterlevel_frontend.json: {len(rels)} -> {len(new_rels)} (-{len(rels)-len(new_rels)})")
            
        # station_relations_db.json
        rel_db_path = processed_dir / "station_relations_db.json"
        if rel_db_path.exists():
            with open(rel_db_path, "r", encoding="utf-8") as f:
                rels = json.load(f)
            new_rels = [
                r for r in rels 
                if str(r.get("parent_station_id", "")) not in all_removed_dwr_ids
                and str(r.get("child_station_id", "")) not in all_removed_dwr_ids
            ]
            with open(rel_db_path, "w", encoding="utf-8") as f:
                json.dump(new_rels, f, ensure_ascii=False, indent=2)
            print(f"  {basin}/station_relations_db.json: {len(rels)} -> {len(new_rels)} (-{len(rels)-len(new_rels)})")

    # Save removed IDs list for backend sync script
    cache_path = Path(__file__).resolve().parent / "purged_dwr_waterlevel_ids.json"
    with open(cache_path, "w", encoding="utf-8") as f:
        json.dump(sorted(list(all_removed_dwr_ids)), f, indent=2)
    print(f"\nSaved {len(all_removed_dwr_ids)} purged IDs to {cache_path}")
    print("\n✅ Model dataset cleanup complete!")

if __name__ == "__main__":
    main()
