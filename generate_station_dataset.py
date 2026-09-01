"""
Script to extract and organize Rainfall and Water Level station lists
by River Basin (matching frontend basins: yom, nan, ping, wang, chao-phraya)
and by Agency (HII + MOU, DWR, and RID for water level).

Outputs clean JSON and CSV files into the dataset/{basin}/station/ directories.
"""

import os
import sys
import json
import csv
import re
from pathlib import Path
from typing import Dict, List, Any, Optional

# Ensure UTF-8 stdout for Windows terminals
if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from scripts.modules.basin_registry import get_all_basins, get_basin

BASINS = get_all_basins()


# Yom River Sub-basins (08xx)
YOM_SUB_BASINS: Dict[str, Dict[str, str]] = {
    "0801": {"th": "แม่น้ำยมตอนบน", "en": "Upper Yom River"},
    "0802": {"th": "น้ำแม่ควร", "en": "Nam Mae Khuan"},
    "0803": {"th": "น้ำแม่ลาว", "en": "Nam Mae Lao"},
    "0804": {"th": "น้ำแม่งาว", "en": "Nam Mae Ngao"},
    "0805": {"th": "น้ำแม่สอง", "en": "Nam Mae Song"},
    "0806": {"th": "น้ำแม่คำมี", "en": "Nam Mae Kham Mi"},
    "0807": {"th": "น้ำแม่ต้า", "en": "Nam Mae Ta"},
    "0808": {"th": "แม่น้ำยมส่วนที่ 2", "en": "Yom River Part 2"},
    "0809": {"th": "น้ำแม่พุง", "en": "Nam Mae Phung"},
    "0810": {"th": "น้ำแม่มอก", "en": "Nam Mae Mok"},
    "0811": {"th": "น้ำแม่ปาน", "en": "Nam Mae Pan"},
    "0812": {"th": "น้ำแม่รำพัน", "en": "Nam Mae Ramphan"},
    "0813": {"th": "แม่น้ำยมส่วนที่ 3", "en": "Yom River Part 3"},
    "0814": {"th": "แม่น้ำยมตอนกลาง", "en": "Middle Yom River"},
    "0815": {"th": "คลองหกบาท", "en": "Khlong Hok Bat"},
    "0816": {"th": "น้ำแม่ท่าแพ", "en": "Nam Mae Tha Phae"},
    "0817": {"th": "แม่น้ำยมส่วนที่ 4", "en": "Yom River Part 4"},
    "0818": {"th": "คลองยาง", "en": "Khlong Yang"},
    "0819": {"th": "แม่น้ำยมตอนล่าง", "en": "Lower Yom River"},
}

AGENCY_TRANSLATIONS: Dict[str, Dict[str, Any]] = {
    "สถาบันสารสนเทศทรัพยากรน้ำ (องค์การมหาชน)": {
        "name": {
            "th": "สถาบันสารสนเทศทรัพยากรน้ำ (องค์การมหาชน)",
            "en": "Hydro-Informatics Institute (Public Organization)",
        },
        "shortname": {"th": "สสน.", "en": "HII"},
    },
    "กรมทรัพยากรน้ำ": {
        "name": {
            "th": "กรมทรัพยากรน้ำ",
            "en": "Department of Water Resources",
        },
        "shortname": {"th": "ทน.", "en": "DWR"},
    },
    "กรมชลประทาน": {
        "name": {
            "th": "กรมชลประทาน",
            "en": "Royal Irrigation Department",
        },
        "shortname": {"th": "ชป.", "en": "RID"},
    },
    "กรมชลประทาน ": {
        "name": {
            "th": "กรมชลประทาน",
            "en": "Royal Irrigation Department",
        },
        "shortname": {"th": "ชป.", "en": "RID"},
    },
    "การไฟฟ้าฝ่ายผลิตแห่งประเทศไทย": {
        "name": {
            "th": "การไฟฟ้าฝ่ายผลิตแห่งประเทศไทย",
            "en": "Electricity Generating Authority of Thailand",
        },
        "shortname": {"th": "กฟผ.", "en": "EGAT"},
    },
    "มูลนิธิอาสาเพื่อนพึ่ง (ภาฯ) ยามยาก สภากาชาดไทย": {
        "name": {
            "th": "มูลนิธิอาสาเพื่อนพึ่ง (ภาฯ) ยามยาก สภากาชาดไทย",
            "en": "Friend in Need (of 'Pa') Volunteers Foundation",
        },
        "shortname": {"th": "พพภ", "en": "FOP"},
    },
    "กรมป้องกันและบรรเทาสาธารณภัย": {
        "name": {
            "th": "กรมป้องกันและบรรเทาสาธารณภัย",
            "en": "Department of Disaster Prevention and Mitigation",
        },
        "shortname": {"th": "ปภ.", "en": "DDPM"},
    },
    "กรมอุตุนิยมวิทยา": {
        "name": {
            "th": "กรมอุตุนิยมวิทยา",
            "en": "Thai Meteorological Department",
        },
        "shortname": {"th": "อต.", "en": "TMD"},
    },
    "กรมอุตุนิยมวิทยา ": {
        "name": {
            "th": "กรมอุตุนิยมวิทยา",
            "en": "Thai Meteorological Department",
        },
        "shortname": {"th": "อต.", "en": "TMD"},
    },
    "สำนักการระบายน้ำ กรุงเทพมหานคร": {
        "name": {
            "th": "สำนักการระบายน้ำ กรุงเทพมหานคร",
            "en": "Department of Drainage and Sewerage BMA",
        },
        "shortname": {"th": "สนน กทม.", "en": "BMA"},
    },
}


# ----------------------------------------------------------------------
# Agency Classification Helpers
# ----------------------------------------------------------------------

def is_hii_or_mou(agency_name_th: str, oldcode: str) -> bool:
    """Check if station belongs to HII or is an MOU station."""
    if "สถาบันสารสนเทศ" in agency_name_th or "HII" in agency_name_th:
        return True
    if re.match(r"^MOU\d+", str(oldcode or "").strip(), re.IGNORECASE):
        return True
    return False


def is_dwr(agency_name_th: str, agency_short_th: str) -> bool:
    """Check if station belongs to DWR (กรมทรัพยากรน้ำ)."""
    return "กรมทรัพยากรน้ำ" in agency_name_th or agency_short_th == "ทน." or agency_short_th == "DWR"


def is_rid(agency_name_th: str, agency_short_th: str) -> bool:
    """Check if station belongs to RID (กรมชลประทาน)."""
    return "กรมชลประทาน" in agency_name_th or agency_short_th == "ชป." or agency_short_th == "RID"


# ----------------------------------------------------------------------
# Extract & Clean Data Schemas
# ----------------------------------------------------------------------

def clean_rain_station(item: Dict[str, Any], basin_info: Dict[str, Any]) -> Dict[str, Any]:
    """
    Extracts metadata from a rainfall station response item.
    Omits telemetry/measurement fields like rain_24h, rainfall_datetime.
    """
    raw_st = item.get("station") or {}
    raw_agency = item.get("agency") or {}
    raw_geocode = item.get("geocode") or {}

    sub_basin_id = str(raw_st.get("sub_basin_id") or "").strip()
    sub_basin_name = raw_st.get("sub_basin_name")
    if not sub_basin_name and sub_basin_id in YOM_SUB_BASINS:
        sub_basin_name = YOM_SUB_BASINS[sub_basin_id]

    station = {
        "id": raw_st.get("id"),
        "tele_station_name": raw_st.get("tele_station_name") or {},
        "tele_station_lat": float(raw_st.get("tele_station_lat")) if raw_st.get("tele_station_lat") is not None else None,
        "tele_station_long": float(raw_st.get("tele_station_long")) if raw_st.get("tele_station_long") is not None else None,
        "tele_station_oldcode": raw_st.get("tele_station_oldcode") or "",
        "tele_station_type": raw_st.get("tele_station_type") or "rainfall_24h",
        "sub_basin_id": sub_basin_id or None,
        "sub_basin_name": sub_basin_name or None,
        "sponsor_by": raw_st.get("sponsor_by") or None,
    }

    agency_th = (raw_agency.get("agency_name") or {}).get("th", "").strip()
    if agency_th in AGENCY_TRANSLATIONS:
        agency = {
            "agency_name": AGENCY_TRANSLATIONS[agency_th]["name"],
            "agency_shortname": AGENCY_TRANSLATIONS[agency_th]["shortname"],
        }
    else:
        agency = {
            "agency_name": raw_agency.get("agency_name") or {"th": agency_th, "en": ""},
            "agency_shortname": raw_agency.get("agency_shortname") or {},
        }

    geocode = {
        "warning_zone": raw_geocode.get("warning_zone") or "",
        "area_code": raw_geocode.get("area_code") or "",
        "area_name": raw_geocode.get("area_name") or {},
        "amphoe_name": raw_geocode.get("amphoe_name") or {},
        "tumbon_name": raw_geocode.get("tumbon_name") or {},
        "province_code": raw_geocode.get("province_code") or "",
        "province_name": raw_geocode.get("province_name") or {},
    }

    basin = {
        "id": basin_info["code_num"],
        "basin_code": basin_info["code_num"],
        "basin_name": {
            "th": basin_info["name_th"],
            "en": basin_info["name_en"],
        },
    }

    return {
        "station": station,
        "agency": agency,
        "geocode": geocode,
        "basin": basin,
    }


def clean_waterlevel_station(feat: Dict[str, Any], basin_info: Dict[str, Any]) -> Dict[str, Any]:
    """
    Extracts metadata from a water level feature item and matches the rainfall station schema.
    Omits telemetry/measurement fields like waterlevelMsl, storagePercent, etc.
    """
    props = feat.get("properties") or {}
    raw_st = props.get("station") or {}
    raw_geo = props.get("geoCode") or {}
    raw_agency = props.get("agency") or {}

    lat_val = raw_st.get("latitude")
    lon_val = raw_st.get("longitude")
    if lat_val is None or lon_val is None:
        coords = feat.get("geometry", {}).get("coordinates", [None, None])
        lon_val = coords[0] if len(coords) > 0 else None
        lat_val = coords[1] if len(coords) > 1 else None

    station_name_th = raw_st.get("station") or ""
    oldcode = raw_st.get("stationCode") or ""

    station = {
        "id": raw_st.get("id"),
        "tele_station_name": {"th": station_name_th, "en": ""},
        "tele_station_lat": float(lat_val) if lat_val is not None else None,
        "tele_station_long": float(lon_val) if lon_val is not None else None,
        "tele_station_oldcode": oldcode,
        "tele_station_type": props.get("type") or "waterlevel",
        "ground_level": raw_st.get("groundLevel"),
        "qmax": raw_st.get("qmax"),
        "min_bank": props.get("minBank"),
        "river_name": props.get("riverName"),
        "sponsor_by": raw_st.get("sponsorBy"),
    }

    agency_th = raw_agency.get("agency", "").strip()
    agency_short_th = raw_agency.get("agencyShort", "").strip()
    agency_code = raw_agency.get("agencyCode")

    if agency_th in AGENCY_TRANSLATIONS:
        agency = {
            "agency_name": AGENCY_TRANSLATIONS[agency_th]["name"],
            "agency_shortname": AGENCY_TRANSLATIONS[agency_th]["shortname"],
            "agency_code": agency_code,
        }
    else:
        agency = {
            "agency_name": {"th": agency_th, "en": ""},
            "agency_shortname": {"th": agency_short_th, "en": ""},
            "agency_code": agency_code,
        }

    geocode = {
        "area_code": raw_geo.get("areaCode") or "",
        "area_name": {"th": raw_geo.get("area") or "", "en": ""},
        "amphoe_name": {"th": raw_geo.get("district") or "", "en": ""},
        "tumbon_name": {"th": raw_geo.get("subdistrict") or "", "en": ""},
        "province_code": raw_geo.get("provinceCode") or "",
        "province_name": {"th": raw_geo.get("province") or "", "en": ""},
        "geo_code": raw_geo.get("geoCode") or "",
        "rid_code": raw_geo.get("ridCode") or "",
        "tmd_code": raw_geo.get("tmdCode") or "",
    }

    basin = {
        "id": basin_info["code_num"],
        "basin_code": basin_info["code_num"],
        "basin_name": {
            "th": basin_info["name_th"],
            "en": basin_info["name_en"],
        },
    }

    return {
        "station": station,
        "agency": agency,
        "geocode": geocode,
        "basin": basin,
    }


def flatten_station_for_csv(st_data: Dict[str, Any]) -> Dict[str, Any]:
    """Flattens station nested structure into a single-level dictionary for CSV output."""
    station = st_data.get("station") or {}
    agency = st_data.get("agency") or {}
    geocode = st_data.get("geocode") or {}
    basin = st_data.get("basin") or {}

    st_name = station.get("tele_station_name") or {}
    sub_basin_name = station.get("sub_basin_name") or {}
    agency_name = agency.get("agency_name") or {}
    agency_short = agency.get("agency_shortname") or {}

    row = {
        "station_id": station.get("id"),
        "station_oldcode": station.get("tele_station_oldcode", ""),
        "station_name_th": st_name.get("th", "") if isinstance(st_name, dict) else str(st_name),
        "station_name_en": st_name.get("en", "") if isinstance(st_name, dict) else "",
        "latitude": station.get("tele_station_lat"),
        "longitude": station.get("tele_station_long"),
        "station_type": station.get("tele_station_type", ""),
        "sub_basin_id": station.get("sub_basin_id", ""),
        "sub_basin_name_th": sub_basin_name.get("th", "") if isinstance(sub_basin_name, dict) else "",
        "sub_basin_name_en": sub_basin_name.get("en", "") if isinstance(sub_basin_name, dict) else "",
        "sponsor_by": station.get("sponsor_by", ""),
        "river_name": station.get("river_name", ""),
        "ground_level": station.get("ground_level", ""),
        "min_bank": station.get("min_bank", ""),
        "qmax": station.get("qmax", ""),
        "province_code": geocode.get("province_code", ""),
        "province_name_th": (geocode.get("province_name") or {}).get("th", ""),
        "province_name_en": (geocode.get("province_name") or {}).get("en", ""),
        "amphoe_name_th": (geocode.get("amphoe_name") or {}).get("th", ""),
        "amphoe_name_en": (geocode.get("amphoe_name") or {}).get("en", ""),
        "tumbon_name_th": (geocode.get("tumbon_name") or {}).get("th", ""),
        "tumbon_name_en": (geocode.get("tumbon_name") or {}).get("en", ""),
        "area_name_th": (geocode.get("area_name") or {}).get("th", ""),
        "warning_zone": geocode.get("warning_zone", ""),
        "agency_shortname_th": agency_short.get("th", "") if isinstance(agency_short, dict) else "",
        "agency_shortname_en": agency_short.get("en", "") if isinstance(agency_short, dict) else "",
        "agency_name_th": agency_name.get("th", "") if isinstance(agency_name, dict) else "",
        "agency_name_en": agency_name.get("en", "") if isinstance(agency_name, dict) else "",
        "basin_id": basin.get("id", ""),
        "basin_name_th": (basin.get("basin_name") or {}).get("th", ""),
        "basin_name_en": (basin.get("basin_name") or {}).get("en", ""),
    }
    return row


def save_dataset_files(stations: List[Dict[str, Any]], json_path: Path, csv_path: Path):
    """Saves formatted station data into JSON and CSV files."""
    json_path.parent.mkdir(parents=True, exist_ok=True)
    csv_path.parent.mkdir(parents=True, exist_ok=True)

    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(stations, f, ensure_ascii=False, indent=2)

    flat_rows = [flatten_station_for_csv(st) for st in stations]
    if flat_rows:
        with open(csv_path, "w", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=list(flat_rows[0].keys()))
            writer.writeheader()
            writer.writerows(flat_rows)
    else:
        with open(csv_path, "w", encoding="utf-8-sig", newline="") as f:
            dummy_row = flatten_station_for_csv({})
            writer = csv.DictWriter(f, fieldnames=list(dummy_row.keys()))
            writer.writeheader()


# ----------------------------------------------------------------------
# Main Processing Pipeline
# ----------------------------------------------------------------------

def process_all_basins(
    rain_input_path: Path,
    waterlevel_input_path: Path,
    output_dir: Path,
    target_basins: Optional[List[str]] = None,
):
    print("=" * 80)
    print("  WATER ANALYSIS MODEL - BASIN STATION DATASET GENERATOR (22 BASINS)")
    print("=" * 80)

    # 1. Load raw data
    print(f"\n[1/3] กำลังอ่านไฟล์ข้อมูลดิบ...")
    print(f"  - Rain Station API Response: {rain_input_path}")
    with open(rain_input_path, "r", encoding="utf-8") as f:
        rain_raw = json.load(f)
    rain_items = rain_raw.get("data", []) if isinstance(rain_raw, dict) else rain_raw
    print(f"    -> โหลดข้อมูลสถานีน้ำฝนดิบสำเร็จ: {len(rain_items):,} รายการ")

    print(f"  - Water Level API Response: {waterlevel_input_path}")
    with open(waterlevel_input_path, "r", encoding="utf-8") as f:
        wl_raw = json.load(f)
    wl_prov_dict = wl_raw.get("data", {}) if isinstance(wl_raw, dict) else {}
    wl_all_features = []
    for prov_k, fc in wl_prov_dict.items():
        if isinstance(fc, dict) and "features" in fc:
            wl_all_features.extend(fc.get("features", []))
    print(f"    -> โหลดข้อมูลสถานีระดับน้ำดิบสำเร็จ: {len(wl_all_features):,} รายการ")

    # Filter basins if specified
    active_basins = BASINS
    if target_basins and "all" not in target_basins:
        target_slugs = set(s.lower() for s in target_basins)
        active_basins = [b for b in BASINS if b["slug"] in target_slugs]

    # 2. Process each basin
    print(f"\n[2/3] กำลังประมวลผลแยกข้อมูลตาม {len(active_basins)} ลุ่มน้ำ...")

    summary_report = []

    for basin_info in active_basins:
        slug = basin_info["slug"]
        code_num = basin_info["code_num"]
        code_str = basin_info["code_str"]
        name_th = basin_info["name_th"]
        tw_name = basin_info.get("thaiwater_name", "")

        # Ensure directory structure: dataset/{basin}/station, rainfall, waterlevel
        basin_station_dir = output_dir / slug / "station"
        basin_station_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / slug / "rainfall").mkdir(parents=True, exist_ok=True)
        (output_dir / slug / "waterlevel").mkdir(parents=True, exist_ok=True)

        # --------------------------------------------------------------
        # Process Rainfall Stations for this basin
        # (Filtering only HII+MOU and DWR as requested, no RID for rain)
        # --------------------------------------------------------------
        rain_stations = []
        seen_rain_ids = set()

        for item in rain_items:
            st = item.get("station") or {}
            sub_id = str(st.get("sub_basin_id") or "").strip()
            basin_obj = item.get("basin") or {}
            b_name = (basin_obj.get("basin_name") or {}).get("th", "")

            # Matching criteria
            match = False
            if sub_id.startswith(code_str):
                match = True
            elif name_th in b_name or (tw_name and tw_name in b_name):
                match = True
            elif basin_info["name_en"].lower() in str(basin_obj.get("basin_name")).lower():
                match = True

            if match:
                st_id = st.get("id")
                if st_id and st_id in seen_rain_ids:
                    continue
                if st_id:
                    seen_rain_ids.add(st_id)
                clean_st = clean_rain_station(item, basin_info)
                rain_stations.append(clean_st)

        # Sort stations by province, amphoe, name
        rain_stations.sort(
            key=lambda x: (
                (x.get("geocode") or {}).get("province_name", {}).get("th", ""),
                (x.get("geocode") or {}).get("amphoe_name", {}).get("th", ""),
                (x.get("station") or {}).get("tele_station_name", {}).get("th", ""),
            )
        )

        # Filter agency subsets for Rain (HII + MOU and DWR)
        rain_hii_mou = [
            st for st in rain_stations
            if is_hii_or_mou(
                st.get("agency", {}).get("agency_name", {}).get("th", ""),
                st.get("station", {}).get("tele_station_oldcode", "")
            )
        ]
        rain_dwr = [
            st for st in rain_stations
            if is_dwr(
                st.get("agency", {}).get("agency_name", {}).get("th", ""),
                st.get("agency", {}).get("agency_shortname", {}).get("th", "")
            )
        ]

        # Save Rain Files in {basin}/station/
        save_dataset_files(
            rain_stations,
            basin_station_dir / f"{slug}_rain_stations.json",
            basin_station_dir / f"{slug}_rain_stations.csv"
        )
        save_dataset_files(
            rain_hii_mou,
            basin_station_dir / f"{slug}_rain_stations_hii.json",
            basin_station_dir / f"{slug}_rain_stations_hii.csv"
        )
        save_dataset_files(
            rain_dwr,
            basin_station_dir / f"{slug}_rain_stations_dwr.json",
            basin_station_dir / f"{slug}_rain_stations_dwr.csv"
        )

        # --------------------------------------------------------------
        # Process Water Level Stations for this basin
        # (Filtering HII+MOU, DWR, and RID for waterlevel)
        # --------------------------------------------------------------
        wl_stations = []
        seen_wl_ids = set()

        for feat in wl_all_features:
            props = feat.get("properties") or {}
            basin_obj = props.get("basin") or {}
            b_code = str(basin_obj.get("basinCode") or "").strip().zfill(2)
            b_name = str(basin_obj.get("basin") or "").strip()

            match = False
            if b_code == code_str:
                match = True
            elif name_th in b_name or (tw_name and tw_name in b_name):
                match = True

            if match:
                st = props.get("station") or {}
                st_id = st.get("id")
                if st_id and st_id in seen_wl_ids:
                    continue
                if st_id:
                    seen_wl_ids.add(st_id)
                clean_st = clean_waterlevel_station(feat, basin_info)
                wl_stations.append(clean_st)

        # Sort stations by province, amphoe, name
        wl_stations.sort(
            key=lambda x: (
                (x.get("geocode") or {}).get("province_name", {}).get("th", ""),
                (x.get("geocode") or {}).get("amphoe_name", {}).get("th", ""),
                (x.get("station") or {}).get("tele_station_name", {}).get("th", ""),
            )
        )

        # Filter agency subsets for Water Level (HII + MOU, DWR, and RID)
        wl_hii_mou = [
            st for st in wl_stations
            if is_hii_or_mou(
                st.get("agency", {}).get("agency_name", {}).get("th", ""),
                st.get("station", {}).get("tele_station_oldcode", "")
            )
        ]
        wl_dwr = [
            st for st in wl_stations
            if is_dwr(
                st.get("agency", {}).get("agency_name", {}).get("th", ""),
                st.get("agency", {}).get("agency_shortname", {}).get("th", "")
            )
        ]
        wl_rid = [
            st for st in wl_stations
            if is_rid(
                st.get("agency", {}).get("agency_name", {}).get("th", ""),
                st.get("agency", {}).get("agency_shortname", {}).get("th", "")
            )
        ]

        # Save Water Level Files in {basin}/station/
        save_dataset_files(
            wl_stations,
            basin_station_dir / f"{slug}_waterlevel_stations.json",
            basin_station_dir / f"{slug}_waterlevel_stations.csv"
        )
        save_dataset_files(
            wl_hii_mou,
            basin_station_dir / f"{slug}_waterlevel_stations_hii.json",
            basin_station_dir / f"{slug}_waterlevel_stations_hii.csv"
        )
        save_dataset_files(
            wl_dwr,
            basin_station_dir / f"{slug}_waterlevel_stations_dwr.json",
            basin_station_dir / f"{slug}_waterlevel_stations_dwr.csv"
        )
        save_dataset_files(
            wl_rid,
            basin_station_dir / f"{slug}_waterlevel_stations_rid.json",
            basin_station_dir / f"{slug}_waterlevel_stations_rid.csv"
        )

        summary_report.append({
            "slug": slug,
            "basin_name_th": name_th,
            "basin_code": code_str,
            "rain_total": len(rain_stations),
            "rain_hii_mou": len(rain_hii_mou),
            "rain_dwr": len(rain_dwr),
            "wl_total": len(wl_stations),
            "wl_hii_mou": len(wl_hii_mou),
            "wl_dwr": len(wl_dwr),
            "wl_rid": len(wl_rid),
        })

    # Save summary metadata in dataset root (both summary-list-station.json and summary.json)
    with open(output_dir / "summary-list-station.json", "w", encoding="utf-8") as f:
        json.dump(summary_report, f, ensure_ascii=False, indent=2)
    with open(output_dir / "summary.json", "w", encoding="utf-8") as f:
        json.dump(summary_report, f, ensure_ascii=False, indent=2)

    # 3. Print Output Summary
    print("\n[3/3] สรุปผลการสร้างชุดข้อมูลสถานี (Dataset Summary):")
    print("-" * 88)
    print(f"{'ลุ่มน้ำ (Basin)':<20} | {'ฝน (Rain) HII+MOU / DWR / รวม':<25} | {'น้ำ (WL) HII / DWR / RID / รวม':<32}")
    print("-" * 88)
    total_rain_all = 0
    total_wl_all = 0
    total_wl_rid = 0
    for r in summary_report:
        rain_str = f"{r['rain_hii_mou']:>3} / {r['rain_dwr']:>3} / {r['rain_total']:>3}"
        wl_str = f"{r['wl_hii_mou']:>3} / {r['wl_dwr']:>3} / {r['wl_rid']:>3} / {r['wl_total']:>3}"
        print(f"{r['slug'] + ' (' + r['basin_name_th'] + ')':<20} | {rain_str:<25} | {wl_str:<32}")
        total_rain_all += r['rain_total']
        total_wl_all += r['wl_total']
        total_wl_rid += r['wl_rid']
    print("-" * 88)
    print(f"รวมสถานีฝนทั้งหมด: {total_rain_all:,} สถานี")
    print(f"รวมสถานีระดับน้ำทั้งหมด: {total_wl_all:,} สถานี (เป็นของกรมชลประทาน RID: {total_wl_rid:,} สถานี)")
    print(f"จัดเก็บไฟล์เรียบร้อยที่โฟลเดอร์: {output_dir.resolve()}")
    print("=" * 80)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Extract station lists by river basin (22 basins)")
    parser.add_argument("--basin", type=str, default="all", help="Target basin slug (or 'all')")
    parser.add_argument("--dir", type=str, default="./dataset", help="Output dataset directory")
    args = parser.parse_args()

    base_dir = Path(__file__).parent
    rain_input = base_dir / "response-api-rainstation.json"
    wl_input = base_dir / "reqponse-api-waterlevel.json"
    out_dir = Path(args.dir)

    target_basins = None if args.basin == "all" else [args.basin]

    process_all_basins(
        rain_input_path=rain_input,
        waterlevel_input_path=wl_input,
        output_dir=out_dir,
        target_basins=target_basins,
    )

