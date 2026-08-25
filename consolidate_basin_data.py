"""
Data Consolidation & Standardization Pipeline for Flood Analysis Model
======================================================================
Consolidates raw, multi-source, and monthly partitioned files into unified,
continuous, model-ready time-series datasets (Rainfall and Water Level) for any river basin.

Inputs:
- Rainfall:
  * dataset/{basin}/rainfall/{basin}_dwr_hourly_rain.csv (DWR full scrape)
  * dataset/{basin}/rainfall/{basin}_hii_rain_mou_YYYYMM.csv (HII MOU monthly)
  * dataset/{basin}/rainfall/{basin}_hii_rain_non_mou_YYYYMM.csv (HII Non-MOU monthly)
- Water Level:
  * dataset/{basin}/waterlevel/{basin}_rid_hourly_waterlevel.csv (RID full scrape)
  * dataset/{basin}/waterlevel/{basin}_hii_wl_non_mou_YYYYMM.csv (HII 10-minute monthly)

Outputs:
- dataset/{basin}/processed/{basin}_hourly_rainfall.csv
- dataset/{basin}/processed/{basin}_hourly_waterlevel.csv
- dataset/{basin}/processed/{basin}_consolidation_summary.json

Usage:
  python consolidate_basin_data.py --basin yom
  python consolidate_basin_data.py --basin all
  python consolidate_basin_data.py --basin yom --dir ./dataset --start-date 2025-01-01 --end-date 2026-07-31
"""

import os
import sys
import json
import csv
import glob
import time
import argparse
from pathlib import Path
from datetime import datetime
from collections import defaultdict
from typing import Dict, List, Set, Any, Optional, Tuple

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

BASINS = ["yom", "nan", "ping", "wang", "chao-phraya"]

DEFAULT_START_DATE = "2025-01-01 00:00:00"
DEFAULT_END_DATE = "2026-07-31 23:00:00"


def format_duration(seconds: float) -> str:
    sec = int(seconds)
    if sec < 60:
        return f"{seconds:.2f}s"
    minutes = sec // 60
    rem_sec = sec % 60
    return f"{minutes}m {rem_sec:02d}s"


def parse_float(val: Any) -> Optional[float]:
    """Safely parses float or returns None if null / corrupted."""
    if val is None:
        return None
    s = str(val).strip()
    if s == "" or s.lower() in ("null", "none", "nan", "-999", "-9999", "-999.0", "-9999.0"):
        return None
    try:
        f = float(s)
        return f
    except (ValueError, TypeError):
        return None


def normalize_datetime(dt_str: str) -> Optional[str]:
    """Normalizes various datetime string formats into YYYY-MM-DD HH:MM:SS."""
    s = str(dt_str).strip()
    if not s or s.lower() in ("null", "none", "nan"):
        return None
    # If standard ISO YYYY-MM-DD HH:MM:SS
    if len(s) == 19 and s[10] in (" ", "T") and s[4] == "-" and s[7] == "-":
        return s[:10] + " " + s[11:]
    # If date only YYYY-MM-DD
    if len(s) == 10 and s[4] == "-" and s[7] == "-":
        return f"{s} 00:00:00"
    # Try parsing
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            pass
    return None


def consolidate_rainfall(
    basin: str,
    dataset_dir: Path,
    start_dt: str,
    end_dt: str,
    out_dir: Path,
) -> Dict[str, Any]:
    """
    Consolidates DWR, HII MOU, and HII Non-MOU rainfall data into a single continuous time-series CSV.
    """
    t0 = time.time()
    rain_dir = dataset_dir / basin / "rainfall"
    print(f"\n--- [{basin.upper()}] Consolidating Hourly Rainfall Data ---")
    print(f"  Input Folder : {rain_dir}")
    print(f"  Date Filter  : {start_dt} to {end_dt}")

    # Map to store: (station_code, datetime) -> {rainfall_mm, agency, quality_flag}
    records_map: Dict[Tuple[str, str], Dict[str, Any]] = {}
    agency_counts: Dict[str, int] = defaultdict(int)
    agency_nulls: Dict[str, int] = defaultdict(int)
    station_sources: Dict[str, Set[str]] = defaultdict(set)

    # 1. DWR Scraped Rain File
    dwr_file = rain_dir / f"{basin}_dwr_hourly_rain.csv"
    if dwr_file.exists():
        print(f"  Reading DWR rain file: {dwr_file.name}")
        dwr_rows = 0
        with open(dwr_file, "r", encoding="utf-8-sig") as f:
            reader = csv.reader(f)
            header = next(reader, None)
            for row in reader:
                if len(row) < 3:
                    continue
                raw_stn, raw_dt, raw_val = row[0].strip(), row[1].strip(), row[2].strip()
                norm_dt = normalize_datetime(raw_dt)
                if not norm_dt or not (start_dt <= norm_dt <= end_dt):
                    continue
                # Enforce top-of-hour
                hour_dt = norm_dt[:13] + ":00:00"
                stn_code = raw_stn.upper()
                val = parse_float(raw_val)

                key = (stn_code, hour_dt)
                records_map[key] = {
                    "station_code": stn_code,
                    "datetime": hour_dt,
                    "rainfall_mm": f"{val:.1f}" if val is not None else "",
                    "agency": "DWR",
                    "quality_flag": "",
                }
                agency_counts["DWR"] += 1
                if val is None:
                    agency_nulls["DWR"] += 1
                station_sources[stn_code].add("DWR")
                dwr_rows += 1
        print(f"    -> DWR valid period rows: {dwr_rows:,}")
    else:
        print(f"  [INFO] DWR rain file not found for basin {basin}")

    # 2. HII MOU Rain Files
    hii_mou_files = sorted(glob.glob(str(rain_dir / f"{basin}_hii_rain_mou_*.csv")))
    if hii_mou_files:
        print(f"  Reading {len(hii_mou_files)} HII MOU rain files...")
        mou_rows = 0
        for fpath in hii_mou_files:
            with open(fpath, "r", encoding="utf-8-sig") as f:
                reader = csv.reader(f)
                header = next(reader, None)
                for row in reader:
                    if len(row) < 3:
                        continue
                    raw_stn, raw_dt, raw_val = row[0].strip(), row[1].strip(), row[2].strip()
                    raw_flag = row[3].strip() if len(row) > 3 else ""
                    norm_dt = normalize_datetime(raw_dt)
                    if not norm_dt or not (start_dt <= norm_dt <= end_dt):
                        continue
                    hour_dt = norm_dt[:13] + ":00:00"
                    stn_code = raw_stn.upper()
                    val = parse_float(raw_val)

                    key = (stn_code, hour_dt)
                    # If duplicate, prefer non-null value
                    if key not in records_map or (records_map[key]["rainfall_mm"] == "" and val is not None):
                        records_map[key] = {
                            "station_code": stn_code,
                            "datetime": hour_dt,
                            "rainfall_mm": f"{val:.1f}" if val is not None else "",
                            "agency": "HII_MOU",
                            "quality_flag": raw_flag,
                        }
                    agency_counts["HII_MOU"] += 1
                    if val is None:
                        agency_nulls["HII_MOU"] += 1
                    station_sources[stn_code].add("HII_MOU")
                    mou_rows += 1
        print(f"    -> HII MOU valid period rows: {mou_rows:,}")

    # 3. HII Non-MOU Rain Files
    hii_non_mou_files = sorted(glob.glob(str(rain_dir / f"{basin}_hii_rain_non_mou_*.csv")))
    if hii_non_mou_files:
        print(f"  Reading {len(hii_non_mou_files)} HII Non-MOU rain files...")
        non_mou_rows = 0
        for fpath in hii_non_mou_files:
            with open(fpath, "r", encoding="utf-8-sig") as f:
                reader = csv.reader(f)
                header = next(reader, None)
                for row in reader:
                    if len(row) < 3:
                        continue
                    raw_stn, raw_dt, raw_val = row[0].strip(), row[1].strip(), row[2].strip()
                    raw_flag = row[3].strip() if len(row) > 3 else ""
                    norm_dt = normalize_datetime(raw_dt)
                    if not norm_dt or not (start_dt <= norm_dt <= end_dt):
                        continue
                    hour_dt = norm_dt[:13] + ":00:00"
                    stn_code = raw_stn.upper()
                    val = parse_float(raw_val)

                    key = (stn_code, hour_dt)
                    if key not in records_map or (records_map[key]["rainfall_mm"] == "" and val is not None):
                        records_map[key] = {
                            "station_code": stn_code,
                            "datetime": hour_dt,
                            "rainfall_mm": f"{val:.1f}" if val is not None else "",
                            "agency": "HII_NON_MOU",
                            "quality_flag": raw_flag,
                        }
                    agency_counts["HII_NON_MOU"] += 1
                    if val is None:
                        agency_nulls["HII_NON_MOU"] += 1
                    station_sources[stn_code].add("HII_NON_MOU")
                    non_mou_rows += 1
        print(f"    -> HII Non-MOU valid period rows: {non_mou_rows:,}")

    # Sort records chronologically by station_code, datetime
    print(f"  Sorting {len(records_map):,} unique (station, hourly) records...")
    sorted_keys = sorted(records_map.keys(), key=lambda k: (k[0], k[1]))

    out_file = out_dir / f"{basin}_hourly_rainfall.csv"
    valid_values_count = 0
    with open(out_file, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["station_code", "datetime", "rainfall_mm", "agency", "quality_flag"])
        for key in sorted_keys:
            item = records_map[key]
            if item["rainfall_mm"] != "":
                valid_values_count += 1
            writer.writerow([
                item["station_code"],
                item["datetime"],
                item["rainfall_mm"],
                item["agency"],
                item["quality_flag"],
            ])

    elapsed = time.time() - t0
    total_records = len(sorted_keys)
    unique_stations = len(station_sources)
    print(f"  -> Saved consolidated rainfall: {out_file} ({total_records:,} rows, {unique_stations} stations) in {format_duration(elapsed)}")

    return {
        "dataset_type": "rainfall",
        "output_file": str(out_file),
        "total_records": total_records,
        "valid_records": valid_values_count,
        "missing_records": total_records - valid_values_count,
        "unique_stations": unique_stations,
        "stations": sorted(list(station_sources.keys())),
        "agency_counts": dict(agency_counts),
        "agency_nulls": dict(agency_nulls),
        "date_range": [start_dt, end_dt],
        "duration_sec": round(elapsed, 2),
    }


def consolidate_waterlevel(
    basin: str,
    dataset_dir: Path,
    start_dt: str,
    end_dt: str,
    out_dir: Path,
) -> Dict[str, Any]:
    """
    Consolidates RID and HII water level data into a single continuous hourly time-series CSV.
    Harmonizes sub-hourly (10-minute) HII readings to hourly averages.
    """
    t0 = time.time()
    wl_dir = dataset_dir / basin / "waterlevel"
    print(f"\n--- [{basin.upper()}] Consolidating Hourly Water Level Data ---")
    print(f"  Input Folder : {wl_dir}")
    print(f"  Date Filter  : {start_dt} to {end_dt}")

    # Map: (station_code, datetime) -> {waterlevel_msl, agency, quality_flag}
    records_map: Dict[Tuple[str, str], Dict[str, Any]] = {}
    agency_counts: Dict[str, int] = defaultdict(int)
    agency_nulls: Dict[str, int] = defaultdict(int)
    station_sources: Dict[str, Set[str]] = defaultdict(set)

    # 1. RID Scraped Water Level File
    rid_file = wl_dir / f"{basin}_rid_hourly_waterlevel.csv"
    if rid_file.exists():
        print(f"  Reading RID water level file: {rid_file.name}")
        rid_rows = 0
        with open(rid_file, "r", encoding="utf-8-sig") as f:
            reader = csv.reader(f)
            header = next(reader, None)
            for row in reader:
                if len(row) < 3:
                    continue
                raw_stn, raw_dt, raw_val = row[0].strip(), row[1].strip(), row[2].strip()
                raw_grp = row[3].strip() if len(row) > 3 else ""
                norm_dt = normalize_datetime(raw_dt)
                if not norm_dt or not (start_dt <= norm_dt <= end_dt):
                    continue
                hour_dt = norm_dt[:13] + ":00:00"
                stn_code = raw_stn.strip()
                val = parse_float(raw_val)

                key = (stn_code, hour_dt)
                records_map[key] = {
                    "station_code": stn_code,
                    "datetime": hour_dt,
                    "waterlevel_msl": f"{val:.3f}" if val is not None else "",
                    "agency": "RID",
                    "quality_flag": raw_grp,
                }
                agency_counts["RID"] += 1
                if val is None:
                    agency_nulls["RID"] += 1
                station_sources[stn_code].add("RID")
                rid_rows += 1
        print(f"    -> RID valid period rows: {rid_rows:,}")
    else:
        print(f"  [INFO] RID water level file not found for basin {basin}")

    # 2. HII 10-minute Water Level Files
    hii_wl_files = sorted(glob.glob(str(wl_dir / f"{basin}_hii_wl_*.csv")))
    if hii_wl_files:
        print(f"  Reading {len(hii_wl_files)} HII water level files (resampling 10-min to hourly)...")
        # Temporary bucket: (stn, hour_dt) -> list of valid float values, list of flags
        hii_hourly_buckets: Dict[Tuple[str, str], Dict[str, Any]] = defaultdict(lambda: {"values": [], "flags": []})
        hii_raw_rows = 0

        for fpath in hii_wl_files:
            with open(fpath, "r", encoding="utf-8-sig") as f:
                reader = csv.reader(f)
                header = next(reader, None)
                for row in reader:
                    if len(row) < 3:
                        continue
                    raw_stn, raw_dt, raw_val = row[0].strip(), row[1].strip(), row[2].strip()
                    raw_flag = row[3].strip() if len(row) > 3 else ""
                    norm_dt = normalize_datetime(raw_dt)
                    if not norm_dt or not (start_dt <= norm_dt <= end_dt):
                        continue
                    hour_dt = norm_dt[:13] + ":00:00"
                    stn_code = raw_stn.strip()
                    val = parse_float(raw_val)

                    bucket = hii_hourly_buckets[(stn_code, hour_dt)]
                    if val is not None:
                        bucket["values"].append(val)
                    if raw_flag:
                        bucket["flags"].append(raw_flag)
                    hii_raw_rows += 1

        print(f"    -> Processed {hii_raw_rows:,} raw 10-min rows into {len(hii_hourly_buckets):,} hourly slots")

        # Aggregate each bucket to hourly mean
        for (stn_code, hour_dt), b_data in hii_hourly_buckets.items():
            vals = b_data["values"]
            flags = b_data["flags"]
            if vals:
                avg_val = sum(vals) / len(vals)
                val_str = f"{avg_val:.3f}"
            else:
                val_str = ""

            flag_str = flags[0] if flags else ""
            key = (stn_code, hour_dt)

            if key not in records_map or (records_map[key]["waterlevel_msl"] == "" and val_str != ""):
                records_map[key] = {
                    "station_code": stn_code,
                    "datetime": hour_dt,
                    "waterlevel_msl": val_str,
                    "agency": "HII",
                    "quality_flag": flag_str,
                }
            agency_counts["HII"] += 1
            if not vals:
                agency_nulls["HII"] += 1
            station_sources[stn_code].add("HII")

    # Sort chronologically
    print(f"  Sorting {len(records_map):,} unique (station, hourly) records...")
    sorted_keys = sorted(records_map.keys(), key=lambda k: (k[0], k[1]))

    out_file = out_dir / f"{basin}_hourly_waterlevel.csv"
    valid_values_count = 0
    with open(out_file, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["station_code", "datetime", "waterlevel_msl", "agency", "quality_flag"])
        for key in sorted_keys:
            item = records_map[key]
            if item["waterlevel_msl"] != "":
                valid_values_count += 1
            writer.writerow([
                item["station_code"],
                item["datetime"],
                item["waterlevel_msl"],
                item["agency"],
                item["quality_flag"],
            ])

    elapsed = time.time() - t0
    total_records = len(sorted_keys)
    unique_stations = len(station_sources)
    print(f"  -> Saved consolidated water level: {out_file} ({total_records:,} rows, {unique_stations} stations) in {format_duration(elapsed)}")

    return {
        "dataset_type": "waterlevel",
        "output_file": str(out_file),
        "total_records": total_records,
        "valid_records": valid_values_count,
        "missing_records": total_records - valid_values_count,
        "unique_stations": unique_stations,
        "stations": sorted(list(station_sources.keys())),
        "agency_counts": dict(agency_counts),
        "agency_nulls": dict(agency_nulls),
        "date_range": [start_dt, end_dt],
        "duration_sec": round(elapsed, 2),
    }


def process_basin(
    basin: str,
    dataset_dir: Path,
    start_dt: str,
    end_dt: str,
    custom_out_dir: Optional[Path] = None,
) -> Dict[str, Any]:
    """Processes a single basin: creates processed folder, consolidates rain and waterlevel."""
    out_dir = custom_out_dir or (dataset_dir / basin / "processed")
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"\n{'=' * 80}")
    print(f"  CONSOLIDATING DATASET FOR BASIN: {basin.upper()}")
    print(f"{'=' * 80}")

    rain_summary = consolidate_rainfall(basin, dataset_dir, start_dt, end_dt, out_dir)
    wl_summary = consolidate_waterlevel(basin, dataset_dir, start_dt, end_dt, out_dir)

    basin_summary = {
        "basin": basin,
        "processed_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "rainfall": rain_summary,
        "waterlevel": wl_summary,
    }

    summary_file = out_dir / f"{basin}_consolidation_summary.json"
    with open(summary_file, "w", encoding="utf-8") as f:
        json.dump(basin_summary, f, ensure_ascii=False, indent=2)

    print(f"  -> Saved basin summary metadata: {summary_file}")
    return basin_summary


def main():
    parser = argparse.ArgumentParser(
        description="Consolidate & standardize Rainfall and Water Level data into continuous time-series files."
    )
    parser.add_argument(
        "--basin",
        default="yom",
        help="Target basin: yom, nan, ping, wang, chao-phraya, or all (default: yom)",
    )
    parser.add_argument(
        "--dir",
        default="./dataset",
        help="Path to root dataset directory (default: ./dataset)",
    )
    parser.add_argument(
        "--start-date",
        default=DEFAULT_START_DATE,
        help=f"Start datetime inclusive (default: {DEFAULT_START_DATE})",
    )
    parser.add_argument(
        "--end-date",
        default=DEFAULT_END_DATE,
        help=f"End datetime inclusive (default: {DEFAULT_END_DATE})",
    )
    parser.add_argument(
        "--out-dir",
        default=None,
        help="Optional custom output directory for processed files",
    )

    args = parser.parse_args()

    base_dir = Path(__file__).resolve().parent
    dataset_dir = Path(args.dir)
    if not dataset_dir.is_absolute():
        dataset_dir = (base_dir / dataset_dir).resolve()

    target_basins = BASINS if args.basin.lower() == "all" else [args.basin.lower()]
    custom_out = Path(args.out_dir).resolve() if args.out_dir else None

    # Normalize start/end date
    start_dt = normalize_datetime(args.start_date) or DEFAULT_START_DATE
    end_dt = normalize_datetime(args.end_date) or DEFAULT_END_DATE

    print("=" * 80)
    print("  FLOOD ANALYSIS MODEL — DATASET CONSOLIDATION PIPELINE")
    print("=" * 80)
    print(f"Target Basins : {', '.join(target_basins)}")
    print(f"Dataset Root  : {dataset_dir}")
    print(f"Date Range    : {start_dt} to {end_dt}")
    print("=" * 80)

    total_t0 = time.time()
    all_summaries = {}

    for b in target_basins:
        summary = process_basin(b, dataset_dir, start_dt, end_dt, custom_out)
        all_summaries[b] = summary

    total_elapsed = time.time() - total_t0

    print("\n" + "=" * 80)
    print("  CONSOLIDATION PIPELINE SUMMARY")
    print("=" * 80)
    print(f"{'Basin':<14} | {'Rain Records':>14} | {'Rain Stns':>10} | {'WL Records':>12} | {'WL Stns':>8}")
    print("-" * 80)
    for b, s in all_summaries.items():
        r = s["rainfall"]
        w = s["waterlevel"]
        print(f"{b:<14} | {r['total_records']:>14,} | {r['unique_stations']:>10} | {w['total_records']:>12,} | {w['unique_stations']:>8}")
    print("=" * 80)
    print(f"Total Session Time: {format_duration(total_elapsed)}")
    print("=" * 80)


if __name__ == "__main__":
    main()
