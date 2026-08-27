"""
High-Speed Multi-Threaded Web Scraper for Department of Water Resources (DWR / กรมทรัพยากรน้ำ)
Historical Hourly Rainfall Data (01/2025 - 07/2026)

Key Performance & UX Optimizations:
- Connection Pooling & HTTP Keep-Alive (requests.Session) eliminating TLS handshake latency.
- Concurrent date-window fetching (High-throughput parallel requests).
- Per-station execution timer (Took: Xs) & Total Session Elapsed Time (Elapsed: Xm Ys).
- Rolling-average speed tracker & Accurate Real-time ETA.
- Auto-resume & checkpointing in dataset/{basin}/rainfall/dwr_station_cache/ (skips already scraped stations).
- Strict 1-Hour resolution (24 points/day at :00:00).

Endpoint: https://ews.dwr.go.th/ews/show-rain
Saves output into dataset/{basin}/rainfall/
"""

import os
import sys
import json
import csv
import re
import time
import argparse
import threading
from pathlib import Path
from typing import Dict, List, Set, Any, Optional, Tuple
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
import urllib3

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

BASE_URL = "https://ews.dwr.go.th/ews/show-rain"
BASINS = ["yom", "nan", "ping", "wang", "chao-phraya"]

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
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


def create_http_session(pool_size: int = 50) -> requests.Session:
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


def parse_thai_dwr_datetime(thai_str: str, base_date: Optional[str] = None) -> Optional[str]:
    """
    Parses DWR Thai datetime string:
    e.g. '20/08/69 02:00 น.' -> '2026-08-20 02:00:00'
    or '02:00' with base_date='2026-08-20' -> '2026-08-20 02:00:00'
    """
    clean = thai_str.strip().replace("น.", "").strip()

    # Full date format: DD/MM/YY HH:mm
    match_full = re.search(r"(\d{1,2})/(\d{1,2})/(\d{2,4})\s+(\d{1,2}):(\d{2})", clean)
    if match_full:
        day = int(match_full.group(1))
        month = int(match_full.group(2))
        year_raw = int(match_full.group(3))
        hour = int(match_full.group(4))
        minute = int(match_full.group(5))

        year_be = (2500 + year_raw) if year_raw < 100 else year_raw
        year_ce = year_be - 543 if year_be > 2400 else year_be

        try:
            dt = datetime(year_ce, month, day, hour, minute)
            return dt.strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            return None

    # Time-only format: HH:mm
    match_time = re.search(r"^(\d{1,2}):(\d{2})$", clean)
    if match_time and base_date:
        hour = int(match_time.group(1))
        minute = int(match_time.group(2))
        try:
            base_dt = datetime.strptime(base_date, "%Y-%m-%d")
            dt = datetime(base_dt.year, base_dt.month, base_dt.day, hour, minute)
            return dt.strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            return None

    return None


def scrape_dwr_station_date_session(
    session: requests.Session,
    station_code: str,
    target_date: str,
    filter_type: str = "1D",
    timeout: int = 10,
    hourly_only: bool = True
) -> List[Dict[str, Any]]:
    """Fetches and parses a single date request using requests.Session Keep-Alive."""
    num_data = 48 if filter_type == "1D" else 100
    url = f"{BASE_URL}?FilterSTN={station_code}&FilterType={filter_type}&FilterDate={target_date}&FilterTime=00%3A00&FilterNumData={num_data}"

    try:
        resp = session.get(url, timeout=timeout)
        if resp.status_code != 200:
            return []
        html = resp.text
    except Exception:
        return []

    cat_match = re.search(r"categories:\s*\[(.*?)\]", html, re.DOTALL)
    rain_match = re.search(r"name:\s*['\"][^'\"]*ปริมาณน้ำฝน[^'\"]*['\"].*?data:\s*\[(.*?)\]", html, re.DOTALL)

    if not cat_match or not rain_match:
        return []

    raw_cats = [c.strip().strip("'\"") for c in cat_match.group(1).split(",") if c.strip()]
    raw_rains = [r.strip() for r in rain_match.group(1).split(",") if r.strip()]

    records = []
    for raw_time, raw_val in zip(raw_cats, raw_rains):
        iso_time = parse_thai_dwr_datetime(raw_time, base_date=target_date)
        if not iso_time:
            continue

        if hourly_only and not iso_time.endswith(":00:00"):
            continue

        try:
            val = float(raw_val)
        except ValueError:
            val = 0.0

        records.append({
            "station_code": station_code,
            "datetime": iso_time,
            "rainfall_mm": val,
        })

    return records


class StationProgressTracker:
    """Thread-safe live terminal progress tracker for active station date queries."""
    def __init__(self):
        self.lock = threading.Lock()
        self.active_stations: Dict[str, Tuple[int, int]] = {}
        self.station_start_times: Dict[str, float] = {}
        self.last_render_time = 0.0

    def start_station(self, code: str, total_dates: int):
        with self.lock:
            self.active_stations[code] = (0, total_dates)
            self.station_start_times[code] = time.time()
            self._render(force=True)

    def update_date(self, code: str, done: int, total: int):
        with self.lock:
            self.active_stations[code] = (done, total)
            now = time.time()
            if now - self.last_render_time >= 0.08 or done == total:
                self.last_render_time = now
                self._render(force=False)

    def complete_station(self, code: str):
        with self.lock:
            if code in self.active_stations:
                del self.active_stations[code]
            if code in self.station_start_times:
                del self.station_start_times[code]
            sys.stdout.write("\r" + " " * 160 + "\r")
            sys.stdout.flush()

    def _render(self, force: bool = False):
        if not self.active_stations:
            return

        if len(self.active_stations) == 1:
            code, (done, total) = next(iter(self.active_stations.items()))
            stn_start = self.station_start_times.get(code, time.time())
            elapsed = max(0.001, time.time() - stn_start)
            speed = done / elapsed if elapsed > 0 else 0
            rem_sec = (total - done) / speed if speed > 0 else 0
            pct = (done / total * 100) if total > 0 else 0

            bar_len = 15
            filled = int(bar_len * done / total) if total > 0 else 0
            bar = "=" * filled + (">" if filled < bar_len else "")
            bar = f"{bar:<{bar_len}}"

            msg = f"\r    -> [{code}] [{bar}] {done:>3}/{total} dates ({pct:>5.1f}%) | {speed:>4.1f} req/s | ETA: {format_duration(rem_sec)}"
            sys.stdout.write(f"{msg:<160}")
            sys.stdout.flush()
        else:
            parts = [f"{code}:{done}/{total}" for code, (done, total) in list(self.active_stations.items())]
            status_str = " | ".join(parts)
            n_active = len(self.active_stations)
            msg = f"\r    -> Active ({n_active} workers): {status_str}"
            sys.stdout.write(f"{msg:<160}")
            sys.stdout.flush()


def scrape_station_fast(
    session: requests.Session,
    stn_info: Dict[str, Any],
    date_windows: List[str],
    filter_type: str,
    station_cache_file: Path,
    inner_workers: int = 15,
    hourly_only: bool = True,
    progress_tracker: Optional[StationProgressTracker] = None,
) -> Tuple[str, int, float]:
    """
    Scrapes a single station using internal multi-threading over its date windows.
    Returns (station_code, record_count, duration_seconds).
    """
    t_start = time.time()
    code = stn_info["station_code"]
    records_dict = {}
    total_dates = len(date_windows)

    if progress_tracker:
        progress_tracker.start_station(code, total_dates)

    def fetch_date(d_str):
        return scrape_dwr_station_date_session(
            session=session,
            station_code=code,
            target_date=d_str,
            filter_type=filter_type,
            hourly_only=hourly_only
        )

    with ThreadPoolExecutor(max_workers=inner_workers) as inner_exec:
        future_to_date = {inner_exec.submit(fetch_date, d): d for d in date_windows}
        done_dates = 0
        for fut in as_completed(future_to_date):
            done_dates += 1
            if progress_tracker:
                progress_tracker.update_date(code, done_dates, total_dates)
            try:
                recs = fut.result()
                for r in recs:
                    records_dict[r["datetime"]] = r
            except Exception:
                pass

    if progress_tracker:
        progress_tracker.complete_station(code)

    duration = time.time() - t_start

    if records_dict:
        sorted_records = sorted(records_dict.values(), key=lambda x: x["datetime"])
        station_cache_file.parent.mkdir(parents=True, exist_ok=True)
        with open(station_cache_file, "w", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=["station_code", "datetime", "rainfall_mm"])
            writer.writeheader()
            writer.writerows(sorted_records)
        return code, len(sorted_records), duration

    return code, 0, duration


def check_dwr_connectivity(session: requests.Session) -> Tuple[bool, str]:
    """Tests if DWR server is reachable from the current environment."""
    test_url = f"{BASE_URL}?FilterSTN=STN0913&FilterType=1D&FilterDate=2025-06-01&FilterTime=00%3A00&FilterNumData=48"
    try:
        resp = session.get(test_url, timeout=8)
        if resp.status_code == 200 and "ปริมาณน้ำฝน" in resp.text:
            return True, "OK"
        return False, f"Server responded with HTTP {resp.status_code}"
    except Exception as e:
        return False, str(e)


def load_dwr_stations_for_basin(dataset_dir: Path, basin: str) -> List[Dict[str, Any]]:
    """Loads DWR stations from dataset/{basin}/station/{basin}_rain_stations_dwr.json"""
    dwr_file = dataset_dir / basin / "station" / f"{basin}_rain_stations_dwr.json"
    if not dwr_file.exists():
        print(f"  [WARNING] Station metadata file not found at: {dwr_file}")
        return []

    with open(dwr_file, "r", encoding="utf-8") as f:
        items = json.load(f)

    stations = []
    for it in items:
        st = it.get("station") or {}
        oldcode = st.get("tele_station_oldcode", "").strip()
        if oldcode:
            stations.append({
                "station_id": st.get("id"),
                "station_code": oldcode,
                "station_name": (st.get("tele_station_name") or {}).get("th", ""),
                "lat": st.get("tele_station_lat"),
                "long": st.get("tele_station_long"),
            })
    return stations


def generate_date_windows(start_date_str: str, end_date_str: str, step_days: int = 1) -> List[str]:
    """Generates step dates (YYYY-MM-DD) between start and end date."""
    start_dt = datetime.strptime(start_date_str, "%Y-%m-%d")
    end_dt = datetime.strptime(end_date_str, "%Y-%m-%d")

    dates = []
    curr = start_dt
    while curr <= end_dt:
        dates.append(curr.strftime("%Y-%m-%d"))
        curr += timedelta(days=step_days)
    return dates


def run_dwr_scraper(
    dataset_dir: Path,
    target_basins: List[str],
    start_date: str = "2025-01-01",
    end_date: str = "2026-07-31",
    filter_type: str = "1D",
    workers: int = 4,
    inner_workers: int = 10,
    smoke_test: bool = False,
    specific_station: Optional[str] = None,
    force_restart: bool = False,
    hourly_only: bool = True,
):
    print("=" * 85)
    print("  HIGH-SPEED DWR HOURLY RAINFALL SCRAPER (01/2025 - 07/2026)")
    print("=" * 85)
    print(f"Target Basins  : {', '.join(target_basins)}")
    print(f"Date Range     : {start_date} to {end_date}")
    print(f"FilterType     : {filter_type}")
    print(f"Concurrency    : {workers} parallel station workers × {inner_workers} date threads")
    print(f"Resolution     : {'Strict 1-Hour (24 records/day at :00:00)' if hourly_only else 'All points'}")
    print(f"Auto-Resume    : {'Disabled (--force-restart)' if force_restart else 'Enabled (Checkpoints)'}")
    if smoke_test:
        print(">>> RUNNING IN SMOKE TEST MODE (Testing 1 station on 2 dates) <<<")

    step_days = 1 if filter_type == "1D" else 3 if filter_type == "3D" else 7
    date_windows = generate_date_windows(start_date, end_date, step_days=step_days)
    if smoke_test:
        date_windows = date_windows[:2]

    # Create high-capacity HTTP session pool
    session = create_http_session(pool_size=workers * inner_workers + 10)
    summary_stats = {b: 0 for b in target_basins}

    # Pre-flight Connectivity Check
    is_connected, conn_msg = check_dwr_connectivity(session)
    if not is_connected:
        print("\n" + "!" * 85)
        print("  [CRITICAL NETWORK ERROR] Cannot connect to DWR server (ews.dwr.go.th)")
        print(f"  Details: {conn_msg}")
        print("  Reason : DWR firewall blocks connections from outside Thailand / Cloud IPs (e.g. Google Colab).")
        print("  Action : Please run this script on your local computer in Thailand, then upload the output.")
        print("!" * 85 + "\n")
        return

    for basin in target_basins:
        stations = load_dwr_stations_for_basin(dataset_dir, basin)
        if specific_station:
            stations = [s for s in stations if s["station_code"].upper() == specific_station.upper()]

        if not stations:
            print(f"\n[INFO] No DWR stations found for basin: {basin}")
            continue

        if smoke_test:
            stations = stations[:1]

        out_dir = dataset_dir / basin / "rainfall"
        station_cache_dir = out_dir / "dwr_station_cache"
        station_cache_dir.mkdir(parents=True, exist_ok=True)
        final_csv = out_dir / f"{basin}_dwr_hourly_rain.csv"

        print(f"\n--- Basin: {basin.upper()} ({len(stations)} stations, {len(date_windows)} date windows per station) ---")

        # Check existing cached stations for auto-resume
        pending_stations = []
        already_cached_count = 0

        for stn in stations:
            code = stn["station_code"]
            cache_file = station_cache_dir / f"{code}.csv"
            if not force_restart and cache_file.exists() and cache_file.stat().st_size > 100:
                already_cached_count += 1
            else:
                pending_stations.append(stn)

        if already_cached_count > 0:
            print(f"  [Auto-Resume] Found {already_cached_count} already completed stations. Remaining to scrape: {len(pending_stations)}")

        if pending_stations:
            start_time = time.time()
            completed_count = already_cached_count
            recent_durations = []  # rolling window of station durations
            progress_tracker = StationProgressTracker()

            with ThreadPoolExecutor(max_workers=workers) as executor:
                future_to_station = {
                    executor.submit(
                        scrape_station_fast,
                        session,
                        stn,
                        date_windows,
                        filter_type,
                        station_cache_dir / f"{stn['station_code']}.csv",
                        inner_workers,
                        hourly_only,
                        progress_tracker,
                    ): stn for stn in pending_stations
                }

                for future in as_completed(future_to_station):
                    stn = future_to_station[future]
                    completed_count += 1
                    try:
                        code, n_records, stn_dur = future.result()
                        recent_durations.append(stn_dur)
                        if len(recent_durations) > 10:
                            recent_durations.pop(0)

                        elapsed_total = time.time() - start_time
                        done_now = completed_count - already_cached_count
                        remain_count = len(pending_stations) - done_now
                        pct = (completed_count / len(stations)) * 100

                        # Calculate accurate smoothed ETA
                        # Average time per station divided by number of parallel workers
                        avg_stn_time = sum(recent_durations) / len(recent_durations)
                        effective_parallel_workers = min(workers, max(1, remain_count))
                        eta_seconds = (remain_count * avg_stn_time) / effective_parallel_workers

                        took_str = format_duration(stn_dur)
                        elapsed_str = format_duration(elapsed_total)
                        eta_str = format_duration(eta_seconds) if remain_count > 0 else "0s"
                        stn_speed = (done_now / elapsed_total * 60) if elapsed_total > 0 else 0

                        print(f"  [{completed_count:>3}/{len(stations)}] ({pct:>5.1f}%) {code:<7} ({stn['station_name']:<18}) -> {n_records:>6,} pts | Took: {took_str:>6} | Elapsed: {elapsed_str:>7} | ETA: ~{eta_str:>7} ({stn_speed:>4.1f} stn/min)")
                    except Exception as e:
                        print(f"  [ERROR] Failed scraping {stn['station_code']}: {e}")

        # Consolidate all station cache CSVs into the final basin CSV
        print(f"\n  Consolidating all {len(stations)} stations into {final_csv.name}...")
        total_rows = 0
        all_basin_records = []

        for stn in stations:
            cache_file = station_cache_dir / f"{stn['station_code']}.csv"
            if cache_file.exists():
                with open(cache_file, "r", encoding="utf-8-sig") as f:
                    reader = csv.DictReader(f)
                    for r in reader:
                        all_basin_records.append(r)

        if all_basin_records:
            all_basin_records.sort(key=lambda x: (x["station_code"], x["datetime"]))
            with open(final_csv, "w", encoding="utf-8-sig", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=["station_code", "datetime", "rainfall_mm"])
                writer.writeheader()
                writer.writerows(all_basin_records)
            total_rows = len(all_basin_records)
            print(f"  -> Successfully generated {final_csv} with {total_rows:,} total hourly records!")
        else:
            print(f"  [WARNING] No records found for basin {basin}. Output file was not generated.")

        summary_stats[basin] = total_rows

    print("\n" + "=" * 85)
    print("  DWR SCRAPER COMPLETED SUMMARY")
    print("=" * 85)
    for b in target_basins:
        print(f"Basin {b:<15} | Total DWR Hourly Rain Records: {summary_stats[b]:>10,}")
    print("=" * 85)


def main():
    parser = argparse.ArgumentParser(description="High-Speed Multi-Threaded DWR hourly rainfall scraper.")
    parser.add_argument("--dir", default=None, help="Root directory for dataset (default: dataset/)")
    parser.add_argument("--basin", default="all", help="Target basin: yom, nan, ping, wang, chao-phraya, or all")
    parser.add_argument("--station", help="Scrape specific station code (e.g. STN1226)")
    parser.add_argument("--start-date", default="2025-01-01", help="Start date (YYYY-MM-DD), default: 2025-01-01")
    parser.add_argument("--end-date", default="2026-07-31", help="End date (YYYY-MM-DD), default: 2026-07-31")
    parser.add_argument("--filter-type", default="1D", choices=["1D", "3D"], help="FilterType: 1D for true 1-hour resolution, 3D for 2-hour window")
    parser.add_argument("--workers", type=int, default=4, help="Number of concurrent station workers (default: 4)")
    parser.add_argument("--inner-workers", type=int, default=10, help="Number of concurrent date workers per station (default: 10)")
    parser.add_argument("--smoke-test", action="store_true", help="Run quick smoke test on 1 station & 2 dates")
    parser.add_argument("--force-restart", action="store_true", help="Ignore existing station cache and re-download all")
    args = parser.parse_args()

    base_dir = Path(__file__).parent
    dataset_dir = Path(args.dir) if args.dir else base_dir / "dataset"

    target_basins = BASINS if args.basin == "all" else [args.basin.lower()]

    run_dwr_scraper(
        dataset_dir=dataset_dir,
        target_basins=target_basins,
        start_date=args.start_date,
        end_date=args.end_date,
        filter_type=args.filter_type,
        workers=args.workers,
        inner_workers=args.inner_workers,
        smoke_test=args.smoke_test,
        specific_station=args.station,
        force_restart=args.force_restart,
        hourly_only=True,
    )


if __name__ == "__main__":
    main()
