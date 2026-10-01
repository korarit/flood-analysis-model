"""
Script to resolve and map Rainfall Station IDs in flood-analysis-model
from legacy/internal IDs to official ThaiWater platform IDs (hiiId).

Features:
- Queries ThaiWater API /location/all-search with rate-limiting and persistent disk caching
- Matches by station name, subdistrict, and GPS coordinates distance threshold
- Updates JSON and CSV station datasets in dataset/{basin}/station/
- Updates relationship files in dataset/{basin}/processed/ (relations_frontend.json & station_relations_db.json)
"""

import os
import sys
import json
import csv
import time
import math
import argparse
import urllib.request
import urllib.parse
from pathlib import Path
from typing import Dict, List, Any, Optional, Tuple

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

# ThaiWater API Configuration
API_BASE_URL = "https://twa-api-public.thaiwater.net"
API_KEY = "TPSXrHRvTHeVT2Lygq6YeTqqAm4xZ72x"
HEADERS = {
    "x-api-key": API_KEY,
    "origin": "https://twa.thaiwater.net",
    "referer": "https://twa.thaiwater.net/",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "accept": "application/json, text/plain, */*",
}

CACHE_FILE = Path(__file__).resolve().parent / "station_id_cache.json"


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Calculates great-circle distance between two points in kilometers."""
    R = 6371.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2.0) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2.0) ** 2
    c = 2.0 * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))
    return R * c


class StationIdResolver:
    def __init__(self, cache_path: Path = CACHE_FILE):
        self.cache_path = cache_path
        self.cache: Dict[str, Any] = self._load_cache()
        self.cache_modified = False

    def _load_cache(self) -> Dict[str, Any]:
        if self.cache_path.exists():
            try:
                with open(self.cache_path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                return {}
        return {}

    def save_cache(self):
        if self.cache_modified:
            with open(self.cache_path, "w", encoding="utf-8") as f:
                json.dump(self.cache, f, ensure_ascii=False, indent=2)
            self.cache_modified = False

    def query_search_api(self, query_text: str) -> List[Dict[str, Any]]:
        clean_q = query_text.strip()
        if not clean_q:
            return []
        encoded = urllib.parse.quote(clean_q)
        url = f"{API_BASE_URL}/location/all-search?q={encoded}&limit=8"
        req = urllib.request.Request(url, headers=HEADERS)

        for attempt in range(3):
            try:
                with urllib.request.urlopen(req, timeout=10) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                    return data.get("data", [])
            except Exception as e:
                time.sleep(0.3 * (attempt + 1))
        return []

    def resolve_station(
        self,
        current_id: Any,
        name_th: str,
        lat: Optional[float],
        lon: Optional[float],
        tambon: str = "",
        amphoe: str = "",
        oldcode: str = ""
    ) -> Tuple[str, str, float]:
        """
        Resolves station to official hiiId.
        Returns: (resolved_id, match_source, distance_km)
        """
        cache_key = f"{name_th}|{lat:.4f}|{lon:.4f}" if lat and lon else f"{name_th}|{current_id}"
        if cache_key in self.cache:
            entry = self.cache[cache_key]
            return str(entry["resolved_id"]), entry["match_source"], entry.get("distance_km", 0.0)

        # 1. Search with full station name
        search_results = self.query_search_api(name_th)

        # 2. If no results or no station matches, search with stripped prefix
        station_matches = [
            x for x in search_results
            if x.get("type") == "station" and x.get("meta", {}).get("station", {}).get("type") in ["rainfall", "water_level", "weather"]
        ]

        if not station_matches:
            # Strip prefixes like อบต., ทต., บ้าน, สถานี
            cleaned = name_th
            for prefix in ["อบต.", "ทต.", "บ้าน", "สถานีวัดน้ำฝน", "สถานีฝนสะสม", "สถานี"]:
                if cleaned.startswith(prefix):
                    cleaned = cleaned[len(prefix):].strip()
                    break
            if cleaned and cleaned != name_th:
                time.sleep(0.1)
                search_results = self.query_search_api(cleaned)
                station_matches = [
                    x for x in search_results
                    if x.get("type") == "station" and x.get("meta", {}).get("station", {}).get("type") in ["rainfall", "water_level", "weather"]
                ]

        # 3. If still no station match, try searching by tambon or amphoe
        if not station_matches and tambon:
            time.sleep(0.1)
            search_results = self.query_search_api(tambon)
            station_matches = [
                x for x in search_results
                if x.get("type") == "station" and x.get("meta", {}).get("station", {}).get("type") in ["rainfall", "water_level", "weather"]
            ]

        # 4. Score and pick best match
        best_id = None
        best_dist = 999999.0
        best_source = "not_found"

        for item in station_matches:
            cand_id = item.get("hiiId") or item.get("id")
            if not cand_id:
                continue
            
            st_meta = item.get("meta", {}).get("station", {})
            c_lat = st_meta.get("lat")
            c_lon = st_meta.get("long")

            if lat is not None and lon is not None and c_lat is not None and c_lon is not None:
                dist = haversine_km(lat, lon, float(c_lat), float(c_lon))
                # Accept if within 15 km or closer than existing best
                if dist < best_dist and dist <= 15.0:
                    best_dist = dist
                    best_id = str(cand_id)
                    best_source = f"name_and_coord({dist:.2f}km)"
            elif not best_id:
                best_id = str(cand_id)
                best_source = "name_only"

        # If still not found, keep existing current_id as fallback
        if not best_id:
            best_id = str(current_id)
            best_source = "fallback_original"
            best_dist = 0.0

        self.cache[cache_key] = {
            "resolved_id": best_id,
            "match_source": best_source,
            "distance_km": round(best_dist, 2),
            "original_id": str(current_id),
            "name_th": name_th
        }
        self.cache_modified = True

        return best_id, best_source, best_dist


def update_csv_file(csv_path: Path, id_map: Dict[str, str]):
    """Updates station_id in CSV files."""
    if not csv_path.exists():
        return
    rows = []
    with open(csv_path, "r", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames
        for r in reader:
            old_id = str(r.get("station_id") or "")
            if old_id in id_map:
                r["station_id"] = id_map[old_id]
            rows.append(r)

    with open(csv_path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def process_basin(basin_slug: str, base_dataset_dir: Path, resolver: StationIdResolver):
    stn_dir = base_dataset_dir / basin_slug / "station"
    proc_dir = base_dataset_dir / basin_slug / "processed"

    main_json = stn_dir / f"{basin_slug}_rain_stations.json"
    if not main_json.exists():
        print(f"  [SKIP] {main_json} does not exist.")
        return

    print(f"\n========================================================")
    print(f"  Processing Basin: {basin_slug.upper()}")
    print(f"========================================================")

    with open(main_json, "r", encoding="utf-8") as f:
        stations = json.load(f)

    print(f"  Loaded {len(stations)} rainfall stations from {main_json.name}")

    id_map: Dict[str, str] = {}
    resolved_count = 0
    fallback_count = 0

    for idx, item in enumerate(stations):
        st = item.get("station") or {}
        geo = item.get("geocode") or {}
        current_id = st.get("id")
        name_th = (st.get("tele_station_name") or {}).get("th", "")
        lat = st.get("tele_station_lat")
        lon = st.get("tele_station_long")
        tambon = (geo.get("tumbon_name") or {}).get("th", "")
        amphoe = (geo.get("amphoe_name") or {}).get("th", "")
        oldcode = st.get("tele_station_oldcode", "")

        resolved_id, source, dist = resolver.resolve_station(
            current_id=current_id,
            name_th=name_th,
            lat=lat,
            lon=lon,
            tambon=tambon,
            amphoe=amphoe,
            oldcode=oldcode
        )

        old_id_str = str(current_id)
        id_map[old_id_str] = resolved_id

        if resolved_id != old_id_str:
            resolved_count += 1
            st["id"] = int(resolved_id) if resolved_id.isdigit() else resolved_id
        else:
            fallback_count += 1

        if (idx + 1) % 50 == 0 or (idx + 1) == len(stations):
            print(f"  Progress: {idx + 1}/{len(stations)} stations processed (Mapped: {resolved_count}, Fallback: {fallback_count})...")
            resolver.save_cache()

    resolver.save_cache()

    # Save updated main JSON
    with open(main_json, "w", encoding="utf-8") as f:
        json.dump(stations, f, ensure_ascii=False, indent=2)
    print(f"  -> Saved {main_json.name}")

    # Update CSV
    main_csv = stn_dir / f"{basin_slug}_rain_stations.csv"
    update_csv_file(main_csv, id_map)

    # Update subset JSONs (hii, dwr)
    for subset in ["hii", "dwr"]:
        sub_json = stn_dir / f"{basin_slug}_rain_stations_{subset}.json"
        if sub_json.exists():
            with open(sub_json, "r", encoding="utf-8") as f:
                sub_stations = json.load(f)
            for it in sub_stations:
                s = it.get("station") or {}
                s_old = str(s.get("id") or "")
                if s_old in id_map:
                    new_val = id_map[s_old]
                    s["id"] = int(new_val) if new_val.isdigit() else new_val
            with open(sub_json, "w", encoding="utf-8") as f:
                json.dump(sub_stations, f, ensure_ascii=False, indent=2)
            sub_csv = stn_dir / f"{basin_slug}_rain_stations_{subset}.csv"
            update_csv_file(sub_csv, id_map)
            print(f"  -> Saved {sub_json.name}")

    # Update relations in processed/
    rel_frontend = proc_dir / "relations_frontend.json"
    if rel_frontend.exists():
        with open(rel_frontend, "r", encoding="utf-8") as f:
            relations = json.load(f)
        rel_updated = 0
        for r in relations:
            for inf in r.get("influencingStations", []):
                old_inf_id = str(inf.get("stationId") or "")
                if old_inf_id in id_map:
                    inf["stationId"] = id_map[old_inf_id]
                    rel_updated += 1
        with open(rel_frontend, "w", encoding="utf-8") as f:
            json.dump(relations, f, ensure_ascii=False, indent=2)
        print(f"  -> Updated {rel_updated} influencing station references in {rel_frontend.name}")

    rel_db = proc_dir / "station_relations_db.json"
    if rel_db.exists():
        with open(rel_db, "r", encoding="utf-8") as f:
            relations_db = json.load(f)
        db_rel_updated = 0
        for r in relations_db:
            rel_type = r.get("relationType") or r.get("relation_type")
            if rel_type == "rainfall_influence":
                target_key = "targetStationId" if "targetStationId" in r else "target_station_id"
                old_target = str(r.get(target_key) or "")
                if old_target in id_map:
                    r[target_key] = id_map[old_target]
                    db_rel_updated += 1
        with open(rel_db, "w", encoding="utf-8") as f:
            json.dump(relations_db, f, ensure_ascii=False, indent=2)
        print(f"  -> Updated {db_rel_updated} targetStationId references in {rel_db.name}")

    print(f"  [DONE {basin_slug}] Remapped: {resolved_count}, Fallback unchanged: {fallback_count}")


def main():
    parser = argparse.ArgumentParser(description="Resolve Rainfall Station IDs to ThaiWater hiiId")
    parser.add_argument("--basin", type=str, default="all", help="Target basin slug (e.g. yom, ping, nan) or 'all'")
    parser.add_argument("--dataset-dir", type=str, default="dataset", help="Base dataset directory path")
    args = parser.parse_args()

    base_dir = Path(__file__).resolve().parent.parent / args.dataset_dir
    resolver = StationIdResolver()

    # Discover all basins present in the dataset directory
    all_basins = sorted([d.name for d in base_dir.iterdir() if d.is_dir() and not d.name.startswith(".")])
    target_basins = all_basins if args.basin == "all" else [b.strip() for b in args.basin.split(",")]

    print("=" * 80)
    print("  RESOLVING RAINFALL STATION IDs TO THAIWATER HII IDs")
    print("=" * 80)
    print(f"Target Basins: {', '.join(target_basins)}")
    print(f"Dataset Directory: {base_dir}")

    for b in target_basins:
        process_basin(b, base_dir, resolver)

    resolver.save_cache()
    print("\n[COMPLETE] All station IDs resolved and datasets updated.")


if __name__ == "__main__":
    main()
