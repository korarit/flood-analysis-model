#!/usr/bin/env python3
"""
Scrape RID Station Metadata & Hydrological Thresholds
=====================================================
Fetches official station master metadata (Zero Gauge Datum, Relative Bank Level,
QMax Discharge Capacity, Ground Level) directly from the Royal Irrigation Department (RID / กรมชลประทาน)
WCF Web Service (Utok Offices 1 to 8) across all river basins in Thailand.

Computes:
- bank_level_msl = Zero Gauge (ZG) + Relative Bank Level (braelevel)
- warning_level_msl = Bank Level MSL - 0.50 m
- critical_level_msl = Bank Level MSL
- QMax (m3/s)

Enriches:
1. dataset/{basin}/station/{basin}_waterlevel_stations.json and .csv
2. dataset/{basin}/station/{basin}_waterlevel_stations_rid.json and .csv
3. dataset/rid_station_master.json and .csv (Master lookup of all 395+ RID stations in Thailand)

Endpoint: https://hyd-app-db.rid.go.th/webservice/HDService.svc
"""

import os
import sys
import json
import csv
import re
import time
import argparse
from pathlib import Path
from typing import Dict, List, Set, Any, Optional, Tuple
from collections import defaultdict

import requests
import urllib3

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

BASE_SVC_URL = "https://hyd-app-db.rid.go.th/webservice/HDService.svc"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Content-Type": "application/json; charset=utf-8",
}

# Mapping of basin slugs to RID Basin Names / IDs
TARGET_BASIN_MAP = {
    "yom": {"id": 8, "name_th": "ลุ่มน้ำยม", "name_en": "Yom River Basin"},
    "nan": {"id": 9, "name_th": "ลุ่มน้ำน่าน", "name_en": "Nan River Basin"},
    "ping": {"id": 6, "name_th": "ลุ่มน้ำปิง", "name_en": "Ping River Basin"},
    "wang": {"id": 7, "name_th": "ลุ่มน้ำวัง", "name_en": "Wang River Basin"},
    "chao-phraya": {"id": 10, "name_th": "ลุ่มน้ำเจ้าพระยา", "name_en": "Chao Phraya Basin"},
}


def create_http_session() -> requests.Session:
    """Creates a requests.Session with connection pooling and retries."""
    session = requests.Session()
    adapter = requests.adapters.HTTPAdapter(
        pool_connections=20,
        pool_maxsize=20,
        max_retries=3,
        pool_block=False,
    )
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    session.headers.update(HEADERS)
    session.verify = False
    return session


def clean_station_code(code: str) -> str:
    """Extracts base station code, stripping prefixes like 'G07003-'."""
    if not code:
        return ""
    c = str(code).strip()
    if "-" in c:
        parts = c.split("-")
        return parts[-1].strip()
    return c


def fetch_all_rid_master_stations(session: requests.Session) -> Dict[str, Dict[str, Any]]:
    """
    Harvests all RID water level stations across all 8 Utok regional offices.
    Returns dictionary indexed by cleaned uppercase station code.
    """
    print("🌊 [1/3] Querying RID Web Service across Utok Regional Offices (1 - 8)...")
    master_stations: Dict[str, Dict[str, Any]] = {}
    total_groups_found = 0

    for utok_id in range(1, 9):
        url_b = f"{BASE_SVC_URL}/getBasinUtokStationGroup"
        try:
            resp_b = session.post(url_b, json={"hydro": {"hydroid": str(utok_id)}}, timeout=15)
            if resp_b.status_code != 200:
                continue
            basins = resp_b.json()
            if not isinstance(basins, list):
                continue

            for b in basins:
                bid = b.get("BasinID")
                bname = b.get("BasinName")
                if bid is None:
                    continue

                url_g = f"{BASE_SVC_URL}/getStationGroup"
                resp_g = session.post(url_g, json={"hydro": {"basinid": str(bid), "hydroid": str(utok_id)}}, timeout=15)
                if resp_g.status_code != 200:
                    continue
                groups = resp_g.json()
                if not isinstance(groups, list):
                    continue

                total_groups_found += len(groups)

                for g in groups:
                    gid = g.get("StationGroupID")
                    gname = g.get("StationGroupName")
                    if gid is None:
                        continue

                    url_st = f"{BASE_SVC_URL}/getStationGroupFromID"
                    resp_st = session.post(url_st, json={"hydro": {"stationGroupID": gid}}, timeout=15)
                    if resp_st.status_code != 200:
                        continue
                    st_list = resp_st.json()
                    if not isinstance(st_list, list):
                        continue

                    for st in st_list:
                        raw_code = st.get("stationcode", "")
                        clean_code = clean_station_code(raw_code).upper()
                        if not clean_code:
                            continue

                        zg = st.get("ZG")
                        brae = st.get("braelevel")
                        qmax = st.get("QMax")
                        ground = st.get("GroundLevel")

                        zg_val = float(zg) if zg is not None else None
                        brae_val = float(brae) if brae is not None else None
                        qmax_val = float(qmax) if qmax is not None else None
                        ground_val = float(ground) if ground is not None else None

                        # Calculate Bank Level MSL:
                        bank_msl = None
                        if zg_val is not None and brae_val is not None:
                            bank_msl = round(zg_val + brae_val, 3)
                        elif brae_val is not None and brae_val > 10.0:  # Direct MSL
                            bank_msl = round(brae_val, 3)

                        warning_msl = round(bank_msl - 0.50, 3) if bank_msl is not None else None
                        critical_msl = bank_msl

                        record = {
                            "station_code": clean_code,
                            "raw_station_code": raw_code,
                            "station_id": st.get("stationid"),
                            "station_name": st.get("stationdetail", ""),
                            "province": st.get("provincename", ""),
                            "amphoe": st.get("amphurname", ""),
                            "utok_id": utok_id,
                            "basin_id": bid,
                            "basin_name": bname,
                            "group_id": gid,
                            "group_name": gname,
                            "zero_gauge_msl": zg_val,
                            "bank_level_relative_m": brae_val,
                            "bank_level_msl": bank_msl,
                            "warning_level_msl": warning_msl,
                            "critical_level_msl": critical_msl,
                            "ground_level": ground_val if ground_val is not None else zg_val,
                            "qmax": qmax_val,
                        }

                        # If station already exists, update if new record has more complete data
                        if clean_code not in master_stations or (bank_msl is not None and master_stations[clean_code].get("bank_level_msl") is None):
                            master_stations[clean_code] = record

        except Exception as e:
            print(f"    [WARN] Utok {utok_id} querying error: {e}", file=sys.stderr)

    print(f"        ✓ Scraped {total_groups_found} station groups.")
    print(f"        ✓ Successfully compiled {len(master_stations)} unique RID hydrological master stations.")
    return master_stations


def save_master_catalog(master_stations: Dict[str, Dict[str, Any]], dataset_dir: Path):
    """Saves complete RID master lookup to dataset/rid_station_master.json and .csv."""
    dataset_dir.mkdir(parents=True, exist_ok=True)
    json_path = dataset_dir / "rid_station_master.json"
    csv_path = dataset_dir / "rid_station_master.csv"

    records = sorted(list(master_stations.values()), key=lambda x: (x.get("basin_name") or "", x["station_code"]))

    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(records, f, ensure_ascii=False, indent=2)

    if records:
        fieldnames = list(records[0].keys())
        with open(csv_path, "w", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(records)

    print(f"  📁 Saved Master Catalog: {json_path} & {csv_path}")


def update_basin_station_files(
    basin_slug: str,
    basin_dir: Path,
    master_stations: Dict[str, Dict[str, Any]]
) -> Dict[str, int]:
    """Enriches station JSON & CSV files in dataset/{basin}/station/ with RID master data."""
    station_dir = basin_dir / "station"
    if not station_dir.exists():
        return {"updated": 0, "total": 0}

    stats = {"updated": 0, "total": 0}
    json_files = [
        station_dir / f"{basin_slug}_waterlevel_stations.json",
        station_dir / f"{basin_slug}_waterlevel_stations_rid.json",
    ]

    for jpath in json_files:
        if not jpath.exists():
            continue

        with open(jpath, "r", encoding="utf-8") as f:
            data = json.load(f)

        if not isinstance(data, list):
            continue

        modified = False
        for item in data:
            st = item.get("station") or {}
            oldcode = st.get("tele_station_oldcode", "")
            st_name = (st.get("tele_station_name") or {}).get("th", "")
            clean_code = clean_station_code(oldcode).upper()

            # Match by clean code or raw oldcode
            matched_master = master_stations.get(clean_code) or master_stations.get(str(oldcode).strip().upper())
            if not matched_master:
                # Try partial match (e.g. 'Y.1C' inside 'Y.1C บ้านน้ำโค้ง')
                for m_code, m_data in master_stations.items():
                    if m_code and m_code in clean_code:
                        matched_master = m_data
                        break

            if matched_master:
                # Enrich fields
                if matched_master.get("bank_level_msl") is not None:
                    st["min_bank"] = matched_master["bank_level_msl"]
                    st["bank_level_msl"] = matched_master["bank_level_msl"]
                if matched_master.get("warning_level_msl") is not None:
                    st["warning_level_msl"] = matched_master["warning_level_msl"]
                if matched_master.get("critical_level_msl") is not None:
                    st["critical_level_msl"] = matched_master["critical_level_msl"]
                if matched_master.get("zero_gauge_msl") is not None:
                    st["zero_gauge_msl"] = matched_master["zero_gauge_msl"]
                if matched_master.get("bank_level_relative_m") is not None:
                    st["bank_level_relative_m"] = matched_master["bank_level_relative_m"]
                if matched_master.get("qmax") is not None:
                    st["qmax"] = matched_master["qmax"]
                if matched_master.get("ground_level") is not None and st.get("ground_level") is None:
                    st["ground_level"] = matched_master["ground_level"]

                stats["updated"] += 1
                modified = True
            stats["total"] += 1

        if modified:
            with open(jpath, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)

            # Also update corresponding CSV file
            cpath = jpath.with_suffix(".csv")
            # Flatten to CSV
            flat_rows = []
            for item in data:
                st = item.get("station") or {}
                agency = item.get("agency") or {}
                geocode = item.get("geocode") or {}
                basin = item.get("basin") or {}
                st_name_dict = st.get("tele_station_name") or {}
                sub_basin_dict = st.get("sub_basin_name") or {}
                agency_short = (agency.get("agency_shortname") or {}).get("th", "")
                agency_name = (agency.get("agency_name") or {}).get("th", "")

                flat_rows.append({
                    "station_id": st.get("id"),
                    "station_oldcode": st.get("tele_station_oldcode", ""),
                    "station_name_th": st_name_dict.get("th", "") if isinstance(st_name_dict, dict) else str(st_name_dict),
                    "station_name_en": st_name_dict.get("en", "") if isinstance(st_name_dict, dict) else "",
                    "latitude": st.get("tele_station_lat"),
                    "longitude": st.get("tele_station_long"),
                    "station_type": st.get("tele_station_type", ""),
                    "sub_basin_id": st.get("sub_basin_id", ""),
                    "sub_basin_name_th": sub_basin_dict.get("th", "") if isinstance(sub_basin_dict, dict) else "",
                    "sponsor_by": st.get("sponsor_by", ""),
                    "river_name": st.get("river_name", ""),
                    "ground_level": st.get("ground_level", ""),
                    "min_bank": st.get("min_bank", ""),
                    "bank_level_msl": st.get("bank_level_msl", st.get("min_bank", "")),
                    "warning_level_msl": st.get("warning_level_msl", ""),
                    "critical_level_msl": st.get("critical_level_msl", ""),
                    "zero_gauge_msl": st.get("zero_gauge_msl", ""),
                    "bank_level_relative_m": st.get("bank_level_relative_m", ""),
                    "qmax": st.get("qmax", ""),
                    "province_name_th": (geocode.get("province_name") or {}).get("th", ""),
                    "amphoe_name_th": (geocode.get("amphoe_name") or {}).get("th", ""),
                    "tumbon_name_th": (geocode.get("tumbon_name") or {}).get("th", ""),
                    "agency_shortname_th": agency_short,
                    "agency_name_th": agency_name,
                    "basin_name_th": (basin.get("basin_name") or {}).get("th", ""),
                })

            if flat_rows:
                with open(cpath, "w", encoding="utf-8-sig", newline="") as f:
                    writer = csv.DictWriter(f, fieldnames=list(flat_rows[0].keys()))
                    writer.writeheader()
                    writer.writerows(flat_rows)

    return stats


def main():
    parser = argparse.ArgumentParser(description="Scrape and Enrich RID Hydrological Station Master Metadata")
    parser.add_argument("--basin", type=str, default="all", help="Target basin slug (yom, nan, ping, wang, chao-phraya, all)")
    parser.add_argument("--dir", type=str, default="./dataset", help="Root dataset directory")
    args = parser.parse_args()

    t0 = time.time()
    dataset_dir = Path(args.dir)

    print("\n" + "═" * 70)
    print("🚀 [RID STATION METADATA SCRAPER & HYDRAULIC CALIBRATOR]")
    print(f"   • Dataset Root : {dataset_dir.resolve()}")
    print(f"   • Target Basin : {args.basin.upper()}")
    print("═" * 70)

    session = create_http_session()

    # Step 1: Scrape all RID Master Stations across Thailand
    master_stations = fetch_all_rid_master_stations(session)
    if not master_stations:
        print("❌ ERROR: Failed to scrape RID stations. Check network/endpoint.", file=sys.stderr)
        sys.exit(1)

    # Step 2: Save Master Catalog
    print("\n💾 [2/3] Exporting RID Master Hydrological Catalog...")
    save_master_catalog(master_stations, dataset_dir)

    # Step 3: Enrich Basin Station Files
    print("\n🔗 [3/3] Updating Basin Water Level Station Files...")
    basins_to_process = list(TARGET_BASIN_MAP.keys()) if args.basin == "all" else [args.basin]

    for b in basins_to_process:
        basin_dir = dataset_dir / b
        if basin_dir.exists():
            res = update_basin_station_files(b, basin_dir, master_stations)
            print(f"   • Basin {b.upper():<12}: Matched & Enriched {res['updated']} station records in {basin_dir}/station/")

    elapsed = time.time() - t0
    print("\n" + "═" * 70)
    print(f"✅ [FINISHED] All RID Station Metadata Scraped and Station Files Updated in {elapsed:.2f}s")
    print("═" * 70 + "\n")


if __name__ == "__main__":
    main()
