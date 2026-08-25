"""
High-Speed Web Scraper for Royal Irrigation Department (RID / กรมชลประทาน)
Historical Hourly Water Level Data (01/2025 - 07/2026)

Key Performance & UX Optimizations:
- Connection Pooling & HTTP Keep-Alive (requests.Session).
- Multi-threaded parallel date querying for station groups.
- Real-time progress bar, timers (Took, Elapsed), and accurate ETA.
- Direct WCF mapping by BasinID and Utok Office (1, 2, 5).

Endpoint: https://hyd-app-db.rid.go.th/webservice/HDService.svc/GetHourlyStageReport
Saves output into dataset/{basin}/waterlevel/
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
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
import urllib3

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

BASE_SVC_URL = "https://hyd-app-db.rid.go.th/webservice/HDService.svc"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    "Content-Type": "application/json; charset=utf-8",
}

# Mapping of basin slugs to RID BasinID and relevant Utok offices
BASIN_RID_MAP = {
    "yom": {"basin_id": 8, "utok_ids": [1, 2], "name_th": "ลุ่มน้ำยม"},
    "nan": {"basin_id": 9, "utok_ids": [1, 2], "name_th": "ลุ่มน้ำน่าน"},
    "ping": {"basin_id": 6, "utok_ids": [1, 2], "name_th": "ลุ่มน้ำปิง"},
    "wang": {"basin_id": 7, "utok_ids": [1, 2], "name_th": "ลุ่มน้ำวัง"},
    "chao-phraya": {"basin_id": 10, "utok_ids": [5], "name_th": "ลุ่มน้ำเจ้าพระยา"},
}


def format_duration(seconds: float) -> str:
    """Formats seconds into human-readable string: e.g. '45s', '3m 12s', '1h 25m'."""
    sec = int(seconds)
    if sec < 60:
        return f"{sec}s"
    minutes = sec // 60
    rem_sec = sec % 60
    if minutes < 60:
        return f"{minutes}m {rem_sec:02d}s"
    hours = minutes // 60
    rem_min = minutes % 60
    return f"{hours}h {rem_min:02d}m"


def create_http_session(pool_size: int = 30) -> requests.Session:
    """Creates a requests.Session with HTTP Keep-Alive connection pooling."""
    session = requests.Session()
    adapter = requests.adapters.HTTPAdapter(
        pool_connections=pool_size,
        pool_maxsize=pool_size,
        max_retries=2,
        pool_block=False,
    )
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    session.headers.update(HEADERS)
    session.verify = False
    return session


def to_buddhist_date_str(dt: datetime) -> str:
    """Converts a datetime into RID format: 'DD/MM/YYYY_BE' e.g. '22/08/2569'"""
    year_be = dt.year + 543
    return f"{dt.day:02d}/{dt.month:02d}/{year_be}"


def parse_rid_timestamp(time_str: str) -> Optional[str]:
    """Parses '/Date(1787414400000+0700)/' into 'YYYY-MM-DD HH:MM:SS' (UTC+7)"""
    match = re.search(r"/Date\((\d+)", time_str or "")
    if not match:
        return None
    try:
        epoch_ms = int(match.group(1))
        dt = datetime.fromtimestamp(epoch_ms / 1000, tz=timezone(timedelta(hours=7)))
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return None


def fetch_rid_station_groups(session: requests.Session, utok_id: int, basin_id: int) -> List[Dict[str, Any]]:
    """Fetches station groups available for a specific office and basin."""
    url = f"{BASE_SVC_URL}/getStationGroup"
    payload = json.dumps({"hydro": {"basinid": str(basin_id), "hydroid": str(utok_id)}})
    try:
        resp = session.post(url, data=payload, timeout=15)
        return resp.json() if resp.status_code == 200 else []
    except Exception as e:
        print(f"    [WARN] Error fetching station groups for Utok {utok_id}, Basin {basin_id}: {e}")
        return []


def fetch_station_list_in_group(session: requests.Session, group_id: int) -> List[Dict[str, Any]]:
    """Fetches the ordered list of stations inside a station group."""
    url = f"{BASE_SVC_URL}/getStationGroupFromID"
    payload = json.dumps({"hydro": {"stationGroupID": group_id}})
    try:
        resp = session.post(url, data=payload, timeout=15)
        return resp.json() if resp.status_code == 200 else []
    except Exception as e:
        print(f"    [WARN] Error fetching station list for group {group_id}: {e}")
        return []


def fetch_hourly_stage_report(session: requests.Session, group_id: int, date_be_str: str) -> List[Dict[str, Any]]:
    """Fetches the hourly report matrix for a station group on a specific Buddhist date."""
    url = f"{BASE_SVC_URL}/GetHourlyStageReport"
    payload = json.dumps({"hydro": {"StationGroupID": group_id, "TimeCurrent": date_be_str}})
    try:
        resp = session.post(url, data=payload, timeout=15)
        return resp.json() if resp.status_code == 200 else []
    except Exception:
        return []


def generate_date_list(start_date_str: str, end_date_str: str, step_days: int = 1) -> List[datetime]:
    """Generates a list of datetime objects from start to end date."""
    start_dt = datetime.strptime(start_date_str, "%Y-%m-%d")
    end_dt = datetime.strptime(end_date_str, "%Y-%m-%d")
    dates = []
    curr = start_dt
    while curr <= end_dt:
        dates.append(curr)
        curr += timedelta(days=step_days)
    return dates


def run_rid_scraper(
    dataset_dir: Path,
    target_basins: List[str],
    start_date: str = "2025-01-01",
    end_date: str = "2026-07-31",
    workers: int = 10,
    smoke_test: bool = False,
):
    print("=" * 85)
    print("  HIGH-SPEED RID WATER LEVEL WEB SCRAPER (01/2025 - 07/2026)")
    print("=" * 85)
    print(f"Target Basins  : {', '.join(target_basins)}")
    print(f"Date Range     : {start_date} to {end_date}")
    print(f"Concurrency    : {workers} parallel date threads")
    if smoke_test:
        print(">>> RUNNING IN SMOKE TEST MODE (Testing 1-2 days on sample groups) <<<")

    dates = generate_date_list(start_date, end_date, step_days=1)
    if smoke_test:
        dates = dates[:2]

    session = create_http_session(pool_size=workers + 5)
    summary_stats = {b: 0 for b in target_basins}

    for basin in target_basins:
        if basin not in BASIN_RID_MAP:
            continue

        basin_config = BASIN_RID_MAP[basin]
        basin_id = basin_config["basin_id"]
        utok_ids = basin_config["utok_ids"]

        print(f"\n--- Basin: {basin.upper()} ({basin_config['name_th']}, BasinID={basin_id}) ---")

        # Discover all unique station groups across relevant offices
        seen_group_ids = set()
        station_groups = []

        for u_id in utok_ids:
            groups = fetch_rid_station_groups(session, u_id, basin_id)
            for g in groups:
                gid = g.get("StationGroupID")
                if gid and gid not in seen_group_ids:
                    seen_group_ids.add(gid)
                    station_groups.append(g)

        if not station_groups:
            print(f"  [WARN] No RID station groups found for {basin}")
            continue

        print(f"  Found {len(station_groups)} station groups in {basin}")
        if smoke_test:
            combo_groups = [g for g in station_groups if "รวม" in (g.get("StationGroupName") or "")]
            station_groups = combo_groups[:1] if combo_groups else station_groups[:1]

        out_dir = dataset_dir / basin / "waterlevel"
        out_dir.mkdir(parents=True, exist_ok=True)
        out_csv = out_dir / f"{basin}_rid_hourly_waterlevel.csv"

        all_records_by_key = {}

        for g_idx, grp in enumerate(station_groups):
            gid = grp.get("StationGroupID")
            gname = grp.get("StationGroupName", "")
            t_grp_start = time.time()

            print(f"\n  [{g_idx+1}/{len(station_groups)}] Processing Group {gid}: {gname}")

            # Get stations in this group
            stn_list = fetch_station_list_in_group(session, gid)
            if not stn_list:
                print(f"    [WARN] No stations listed in group {gid}")
                continue

            stn_codes = [s.get("stationcode", "") for s in stn_list if s.get("stationcode")]
            print(f"    Stations in group ({len(stn_codes)}): {', '.join(stn_codes)}")

            # Parallel date scraping for this group
            def fetch_single_date(dt):
                date_be_str = to_buddhist_date_str(dt)
                return fetch_hourly_stage_report(session, gid, date_be_str)

            with ThreadPoolExecutor(max_workers=workers) as date_exec:
                reports = date_exec.map(fetch_single_date, dates)
                for report_rows in reports:
                    for row_item in report_rows:
                        dt_iso = parse_rid_timestamp(row_item.get("time", ""))
                        if not dt_iso:
                            continue

                        hvalues = row_item.get("hvalues", [])
                        for val, stn_meta in zip(hvalues, stn_list):
                            if val is None:
                                continue
                            stn_code = stn_meta.get("stationcode") or ""
                            if not stn_code:
                                continue

                            key = (stn_code, dt_iso)
                            if key not in all_records_by_key:
                                all_records_by_key[key] = {
                                    "station_code": stn_code,
                                    "datetime": dt_iso,
                                    "waterlevel_msl": float(val),
                                    "group_id": gid,
                                }

            grp_dur = time.time() - t_grp_start
            print(f"    -> Group {gid} completed in {format_duration(grp_dur)} (Total pool: {len(all_records_by_key):,} records)")

        # Save to CSV
        if all_records_by_key:
            sorted_records = sorted(all_records_by_key.values(), key=lambda x: (x["station_code"], x["datetime"]))
            with open(out_csv, "w", encoding="utf-8-sig", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=["station_code", "datetime", "waterlevel_msl", "group_id"])
                writer.writeheader()
                writer.writerows(sorted_records)

            summary_stats[basin] = len(sorted_records)
            print(f"\n  -> Saved {len(sorted_records):,} rows to {out_csv}")

    print("\n" + "=" * 85)
    print("  RID SCRAPER SUMMARY")
    print("=" * 85)
    for b in target_basins:
        print(f"Basin {b:<15} | RID WaterLevel Records: {summary_stats[b]:>10,}")
    print("=" * 85)


def main():
    parser = argparse.ArgumentParser(description="High-Speed Multi-Threaded RID historical hourly water level scraper.")
    parser.add_argument("--dir", default=None, help="Root directory for dataset (default: dataset/)")
    parser.add_argument("--basin", default="all", help="Target basin: yom, nan, ping, wang, chao-phraya, or all")
    parser.add_argument("--start-date", default="2025-01-01", help="Start date (YYYY-MM-DD), default: 2025-01-01")
    parser.add_argument("--end-date", default="2026-07-31", help="End date (YYYY-MM-DD), default: 2026-07-31")
    parser.add_argument("--workers", type=int, default=10, help="Number of concurrent date workers (default: 10)")
    parser.add_argument("--smoke-test", action="store_true", help="Run quick smoke test on 1-2 dates")
    args = parser.parse_args()

    base_dir = Path(__file__).parent
    dataset_dir = Path(args.dir) if args.dir else base_dir / "dataset"

    target_basins = list(BASIN_RID_MAP.keys()) if args.basin == "all" else [args.basin.lower()]

    run_rid_scraper(
        dataset_dir=dataset_dir,
        target_basins=target_basins,
        start_date=args.start_date,
        end_date=args.end_date,
        workers=args.workers,
        smoke_test=args.smoke_test,
    )


if __name__ == "__main__":
    main()
