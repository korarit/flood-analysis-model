"""
Script to download historical hourly Rainfall and Water Level data from HII Open Data Catalog
(covering Non-MOU and MOU datasets for 01/2025 - 07/2026).

Saves data into dataset/{basin}/rainfall/ and dataset/{basin}/waterlevel/.
"""

import os
import sys
import json
import csv
import re
import argparse
import urllib.request
import ssl
from pathlib import Path
from typing import Dict, List, Set, Any, Optional
from datetime import datetime

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

# Ignore SSL verification for older institutional certs
SSL_CTX = ssl.create_default_context()
SSL_CTX.check_hostname = False
SSL_CTX.verify_mode = ssl.CERT_NONE

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}

HII_CATALOGS = {
    "rain_non_mou": {
        "base_url": "https://tiservice.hii.or.th/opendata/data_catalog/hourly_rain/",
        "data_type": "rainfall",
        "agency_category": "hii",
    },
    "rain_mou": {
        "base_url": "https://tiservice.hii.or.th/opendata/data_catalog_mou/hourly_rain_mou/",
        "data_type": "rainfall",
        "agency_category": "mou",
    },
    "wl_non_mou": {
        "base_url": "https://tiservice.hii.or.th/opendata/data_catalog/water_level/",
        "data_type": "waterlevel",
        "agency_category": "hii",
    },
    "wl_mou": {
        "base_url": "https://tiservice.hii.or.th/opendata/data_catalog_mou/water_level_mou/",
        "data_type": "waterlevel",
        "agency_category": "mou",
    },
}

from scripts.modules.basin_registry import get_all_slugs, get_basin

BASINS = get_all_slugs()




def generate_month_list(start_year_month: str = "202501", end_year_month: str = "202607") -> List[str]:
    """Generates a list of YYYYMM strings between start and end (inclusive)."""
    start_dt = datetime.strptime(start_year_month.replace("-", ""), "%Y%m")
    end_dt = datetime.strptime(end_year_month.replace("-", ""), "%Y%m")
    
    months = []
    curr = start_dt
    while curr <= end_dt:
        months.append(curr.strftime("%Y%m"))
        # Move to next month
        year = curr.year + (1 if curr.month == 12 else 0)
        month = 1 if curr.month == 12 else curr.month + 1
        curr = datetime(year, month, 1)
    return months


def load_basin_station_codes(dataset_dir: Path, target_basins: List[str]) -> Dict[str, Dict[str, Set[str]]]:
    """
    Loads known station oldcodes from dataset/{basin}/station/
    Returns {basin: {'rain_hii': {...}, 'rain_mou': {...}, 'wl_hii': {...}, 'wl_mou': {...}}}
    """
    basin_stations = {}
    for basin in target_basins:
        stn_dir = dataset_dir / basin / "station"
        basin_stations[basin] = {
            "rain_hii": set(),
            "rain_mou": set(),
            "wl_hii": set(),
            "wl_mou": set(),
        }
        
        if not stn_dir.exists():
            print(f"  [WARN] Station directory not found: {stn_dir}")
            print(f"         Please run 'python generate_station_dataset.py --basin {basin}' first.")
            continue

        # Rain HII + MOU
        rain_files = [
            stn_dir / f"{basin}_rain_stations_hii.json",
            stn_dir / f"{basin}_rainfall_stations_hii.json",
            stn_dir / f"{basin}_rain_stations.json",
            stn_dir / f"{basin}_rainfall_stations.json",
        ]
        rain_loaded = False
        for rf in rain_files:
            if rf.exists() and not rain_loaded:
                with open(rf, "r", encoding="utf-8") as f:
                    items = json.load(f)
                    for it in items:
                        oldcode = (it.get("station") or {}).get("tele_station_oldcode", "").strip()
                        if oldcode:
                            if oldcode.upper().startswith("MOU"):
                                basin_stations[basin]["rain_mou"].add(oldcode)
                            else:
                                basin_stations[basin]["rain_hii"].add(oldcode)
                rain_loaded = True

        # Waterlevel HII + MOU
        wl_files = [
            stn_dir / f"{basin}_waterlevel_stations_hii.json",
            stn_dir / f"{basin}_waterlevel_stations.json",
        ]
        wl_loaded = False
        for wf in wl_files:
            if wf.exists() and not wl_loaded:
                with open(wf, "r", encoding="utf-8") as f:
                    items = json.load(f)
                    for it in items:
                        oldcode = (it.get("station") or {}).get("tele_station_oldcode", "").strip()
                        if oldcode:
                            # oldcode might be G23069-RES033 or MOUxxx
                            raw_code = oldcode.split("-")[-1] if "-" in oldcode else oldcode
                            if raw_code.upper().startswith("MOU"):
                                basin_stations[basin]["wl_mou"].add(raw_code)
                            else:
                                basin_stations[basin]["wl_hii"].add(raw_code)
                                basin_stations[basin]["wl_hii"].add(oldcode)
                wl_loaded = True

        total_stn = (
            len(basin_stations[basin]["rain_hii"])
            + len(basin_stations[basin]["rain_mou"])
            + len(basin_stations[basin]["wl_hii"])
            + len(basin_stations[basin]["wl_mou"])
        )
        if total_stn == 0:
            print(f"  [WARN] No HII/MOU stations found in {stn_dir}.")
            print(f"         Please run 'python generate_station_dataset.py --basin {basin}' first.")

    return basin_stations



def fetch_file_content(url: str, timeout: int = 15) -> Optional[str]:
    """Fetches text content from URL with retry."""
    req = urllib.request.Request(url, headers=HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=SSL_CTX) as resp:
            return resp.read().decode("utf-8", errors="ignore")
    except Exception as e:
        return None


def download_hii_data(
    dataset_dir: Path,
    target_basins: List[str],
    months: List[str],
    smoke_test: bool = False,
    catalogs_to_run: Optional[List[str]] = None
):
    print("=" * 75)
    print("  HII OPEN DATA DOWNLOADER (01/2025 - 07/2026)")
    print("=" * 75)
    print(f"Target Basins: {', '.join(target_basins)}")
    print(f"Months to fetch: {months[0]} to {months[-1]} (Total: {len(months)} months)")
    if smoke_test:
        print(">>> RUNNING IN SMOKE TEST MODE (Sampling first month & limited stations) <<<")

    station_mapping = load_basin_station_codes(dataset_dir, target_basins)
    selected_catalogs = catalogs_to_run or list(HII_CATALOGS.keys())

    stats = {b: {"rainfall_records": 0, "waterlevel_records": 0} for b in target_basins}

    # If smoke test, limit to first 1 month and max 2 stations per basin
    active_months = months[:1] if smoke_test else months

    for cat_key in selected_catalogs:
        cat_info = HII_CATALOGS[cat_key]
        base_url = cat_info["base_url"]
        data_type = cat_info["data_type"]
        agency_cat = cat_info["agency_category"]

        print(f"\n--- Processing Catalog: {cat_key} ({base_url}) ---")

        for yyyymm in active_months:
            year = yyyymm[:4]
            month_dir_url = f"{base_url}{year}/{yyyymm}/"
            print(f"  Fetching directory listing: {month_dir_url}")

            dir_html = fetch_file_content(month_dir_url)
            if not dir_html:
                print(f"    [WARN] Could not read directory {month_dir_url} (or not available)")
                continue

            available_files = re.findall(r'href="([^"]+\.csv)"', dir_html, re.IGNORECASE)
            available_stn_map = {Path(f).stem.upper(): f for f in available_files if f != "0station_metadata.csv"}
            print(f"    Available station CSV files in {yyyymm}: {len(available_stn_map):,}")

            for basin in target_basins:
                # Find matching stations for this basin & catalog
                key = f"{'rain' if data_type == 'rainfall' else 'wl'}_{agency_cat}"
                basin_codes = station_mapping[basin].get(key, set())
                
                # Intersect with available files
                matched_codes = [c for c in basin_codes if c.upper() in available_stn_map]
                if smoke_test:
                    matched_codes = matched_codes[:2]

                if not matched_codes:
                    continue

                out_folder = dataset_dir / basin / data_type
                out_folder.mkdir(parents=True, exist_ok=True)
                out_csv = out_folder / f"{basin}_hii_{cat_key}_{yyyymm}.csv"

                print(f"    [{basin.upper()}] Downloading {len(matched_codes)} stations -> {out_csv.name}")

                all_rows = []
                headers = []

                for code in matched_codes:
                    file_name = available_stn_map[code.upper()]
                    csv_url = f"{month_dir_url}{file_name}"
                    csv_text = fetch_file_content(csv_url)
                    if not csv_text:
                        continue

                    lines = [l.strip() for l in csv_text.splitlines() if l.strip()]
                    if len(lines) > 1:
                        if not headers:
                            headers = lines[0].split(",")
                        for row in lines[1:]:
                            all_rows.append(row.split(","))

                if all_rows:
                    with open(out_csv, "w", encoding="utf-8-sig", newline="") as f:
                        writer = csv.writer(f)
                        writer.writerow(headers or ["station_code", "datetime", "value", "flag"])
                        writer.writerows(all_rows)
                    
                    if data_type == "rainfall":
                        stats[basin]["rainfall_records"] += len(all_rows)
                    else:
                        stats[basin]["waterlevel_records"] += len(all_rows)
                    print(f"      -> Saved {len(all_rows):,} rows to {out_csv}")

    print("\n" + "=" * 75)
    print("  HII DATA DOWNLOAD SUMMARY")
    print("=" * 75)
    for b in target_basins:
        print(f"Basin {b:<15} | Rain Records: {stats[b]['rainfall_records']:>8,} | WaterLevel Records: {stats[b]['waterlevel_records']:>8,}")
    print("=" * 75)


def main():
    parser = argparse.ArgumentParser(description="Download historical HII rainfall and water level data.")
    parser.add_argument("--dir", default=None, help="Root directory for dataset (default: dataset/)")
    parser.add_argument("--basin", default="all", help="Target basin: yom, nan, ping, wang, chao-phraya, or all")
    parser.add_argument("--start", default="202501", help="Start month (YYYYMM), default: 202501")
    parser.add_argument("--end", default="202607", help="End month (YYYYMM), default: 202607")
    parser.add_argument("--catalog", choices=list(HII_CATALOGS.keys()), help="Specific catalog to download")
    parser.add_argument("--smoke-test", action="store_true", help="Run quick smoke test on 1 month and sample stations")
    args = parser.parse_args()

    base_dir = Path(__file__).parent
    if args.dir:
        d_path = Path(args.dir).resolve()
        if d_path.name in BASINS and (args.basin != "all" and d_path.name == args.basin.lower()):
            dataset_dir = d_path.parent
        else:
            dataset_dir = d_path
    else:
        dataset_dir = base_dir / "dataset"

    target_basins = BASINS if args.basin == "all" else [args.basin.lower()]
    months = generate_month_list(args.start, args.end)
    catalogs = [args.catalog] if args.catalog else None

    download_hii_data(
        dataset_dir=dataset_dir,
        target_basins=target_basins,
        months=months,
        smoke_test=args.smoke_test,
        catalogs_to_run=catalogs,
    )



if __name__ == "__main__":
    main()
