"""
Master River Basin Registry (22 Official Thai River Basins)
============================================================
Single Source of Truth for the 22 Master River Basins of Thailand
as defined by the National Water Resources Committee (ONWR / สทนช.)
and ThaiWater (Hydro-Informatics Institute / สสน. Open Data).

Provides unified metadata, mapping to RID (Utok 1-8 offices),
DWR, and ThaiWater GeoJSON schemas.
"""

from typing import Dict, List, Any, Optional, Tuple

BASIN_DEFINITIONS: List[Dict[str, Any]] = [
    {
        "slug": "salawin",
        "code_num": 1,
        "code_str": "01",
        "name_th": "ลุ่มน้ำสาละวิน",
        "name_en": "Salawin River Basin",
        "thaiwater_name": "สาละวิน",
        "rid_utok_ids": [1],
        "rid_basin_names": ["ลุ่มน้ำสาละวิน"],
        "provinces": ["แม่ฮ่องสอน", "เชียงใหม่", "ตาก"],
    },
    {
        "slug": "khong-north",
        "code_num": 2,
        "code_str": "02",
        "name_th": "ลุ่มน้ำโขงเหนือ",
        "name_en": "North Mekong River Basin",
        "thaiwater_name": "โขงเหนือ",
        "rid_utok_ids": [1],
        "rid_basin_names": ["ลุ่มน้ำโขง", "ลุ่มน้ำกก"],
        "provinces": ["เชียงราย", "พะเยา", "เชียงใหม่"],
    },
    {
        "slug": "khong-ne",
        "code_num": 3,
        "code_str": "03",
        "name_th": "ลุ่มน้ำโขงตะวันออกเฉียงเหนือ",
        "name_en": "Northeast Mekong River Basin",
        "thaiwater_name": "โขงตะวันออกเฉียงเหนือ",
        "rid_utok_ids": [3, 4],
        "rid_basin_names": ["ลุ่มน้ำโขง"],
        "provinces": ["เลย", "หนองคาย", "บึงกาฬ", "นครพนม", "มุกดาหาร", "อำนาจเจริญ", "อุบลราชธานี", "สกลนคร", "อุดรธานี"],
    },
    {
        "slug": "chi",
        "code_num": 4,
        "code_str": "04",
        "name_th": "ลุ่มน้ำชี",
        "name_en": "Chi River Basin",
        "thaiwater_name": "ชี",
        "rid_utok_ids": [3],
        "rid_basin_names": ["ลุ่มน้ำชี"],
        "provinces": ["ชัยภูมิ", "ขอนแก่น", "มหาสารคาม", "ร้อยเอ็ด", "ยโสธร", "กาฬสินธุ์", "อุบลราชธานี"],
    },
    {
        "slug": "mun",
        "code_num": 5,
        "code_str": "05",
        "name_th": "ลุ่มน้ำมูล",
        "name_en": "Mun River Basin",
        "thaiwater_name": "มูล",
        "rid_utok_ids": [4],
        "rid_basin_names": ["ลุ่มน้ำมูล"],
        "provinces": ["นครราชสีมา", "บุรีรัมย์", "สุรินทร์", "ศรีสะเกษ", "อุบลราชธานี"],
    },
    {
        "slug": "ping",
        "code_num": 6,
        "code_str": "06",
        "name_th": "ลุ่มน้ำปิง",
        "name_en": "Ping River Basin",
        "thaiwater_name": "ปิง",
        "rid_utok_ids": [1, 2],
        "rid_basin_names": ["ลุ่มน้ำปิง"],
        "provinces": ["เชียงใหม่", "ลำพูน", "ตาก", "กำแพงเพชร", "นครสวรรค์"],
    },
    {
        "slug": "wang",
        "code_num": 7,
        "code_str": "07",
        "name_th": "ลุ่มน้ำวัง",
        "name_en": "Wang River Basin",
        "thaiwater_name": "วัง",
        "rid_utok_ids": [1, 2],
        "rid_basin_names": ["ลุ่มน้ำวัง"],
        "provinces": ["ลำปาง", "ตาก"],
    },
    {
        "slug": "yom",
        "code_num": 8,
        "code_str": "08",
        "name_th": "ลุ่มน้ำยม",
        "name_en": "Yom River Basin",
        "thaiwater_name": "ยม",
        "rid_utok_ids": [1, 2],
        "rid_basin_names": ["ลุ่มน้ำยม"],
        "provinces": ["พะเยา", "แพร่", "สุโขทัย", "พิษณุโลก", "พิจิตร", "นครสวรรค์"],
    },
    {
        "slug": "nan",
        "code_num": 9,
        "code_str": "09",
        "name_th": "ลุ่มน้ำน่าน",
        "name_en": "Nan River Basin",
        "thaiwater_name": "น่าน",
        "rid_utok_ids": [1, 2],
        "rid_basin_names": ["ลุ่มน้ำน่าน"],
        "provinces": ["น่าน", "อุตรดิตถ์", "พิษณุโลก", "พิจิตร", "นครสวรรค์"],
    },
    {
        "slug": "chao-phraya",
        "code_num": 10,
        "code_str": "10",
        "name_th": "ลุ่มน้ำเจ้าพระยา",
        "name_en": "Chao Phraya River Basin",
        "thaiwater_name": "เจ้าพระยา",
        "rid_utok_ids": [5],
        "rid_basin_names": ["ลุ่มน้ำเจ้าพระยา"],
        "provinces": ["นครสวรรค์", "ชัยนาท", "สิงห์บุรี", "อ่างทอง", "พระนครศรีอยุธยา", "ปทุมธานี", "นนทบุรี", "กรุงเทพมหานคร", "สมุทรปราการ"],
    },
    {
        "slug": "sakaekrang",
        "code_num": 11,
        "code_str": "11",
        "name_th": "ลุ่มน้ำสะแกกรัง",
        "name_en": "Sakae Krang River Basin",
        "thaiwater_name": "สะแกกรัง",
        "rid_utok_ids": [5],
        "rid_basin_names": ["ลุ่มน้ำสะแกกรัง"],
        "provinces": ["อุทัยธานี", "นครสวรรค์", "กำแพงเพชร"],
    },
    {
        "slug": "pa-sak",
        "code_num": 12,
        "code_str": "12",
        "name_th": "ลุ่มน้ำป่าสัก",
        "name_en": "Pa Sak River Basin",
        "thaiwater_name": "ป่าสัก",
        "rid_utok_ids": [2, 5],
        "rid_basin_names": ["ลุ่มน้ำป่าสัก"],
        "provinces": ["เลย", "เพชรบูรณ์", "ลพบุรี", "สระบุรี", "พระนครศรีอยุธยา"],
    },
    {
        "slug": "tha-chin",
        "code_num": 13,
        "code_str": "13",
        "name_th": "ลุ่มน้ำท่าจีน",
        "name_en": "Tha Chin River Basin",
        "thaiwater_name": "ท่าจีน",
        "rid_utok_ids": [5, 7],
        "rid_basin_names": ["ลุ่มน้ำท่าจีน"],
        "provinces": ["ชัยนาท", "สุพรรณบุรี", "นครปฐม", "สมุทรสาคร"],
    },
    {
        "slug": "mae-klong",
        "code_num": 14,
        "code_str": "14",
        "name_th": "ลุ่มน้ำแม่กลอง",
        "name_en": "Mae Klong River Basin",
        "thaiwater_name": "แม่กลอง",
        "rid_utok_ids": [7],
        "rid_basin_names": ["ลุ่มน้ำแม่กลอง"],
        "provinces": ["กาญจนบุรี", "ราชบุรี", "สมุทรสงคราม"],
    },
    {
        "slug": "bang-pakong",
        "code_num": 15,
        "code_str": "15",
        "name_th": "ลุ่มน้ำบางปะกง",
        "name_en": "Bang Pakong River Basin",
        "thaiwater_name": "บางปะกง",
        "rid_utok_ids": [6],
        "rid_basin_names": ["ลุ่มน้ำบางปะกง", "ลุ่มน้ำปราจีนบุรี"],
        "provinces": ["นครนายก", "ปราจีนบุรี", "สระแก้ว", "ฉะเชิงเทรา", "ชลบุรี"],
    },
    {
        "slug": "tonle-sap",
        "code_num": 16,
        "code_str": "16",
        "name_th": "ลุ่มน้ำโตนเลสาบ",
        "name_en": "Tonle Sap River Basin",
        "thaiwater_name": "โตนเลสาป",
        "rid_utok_ids": [6],
        "rid_basin_names": ["ลุ่มน้ำโตนเลสาป", "ลุ่มน้ำโตนเลสาบ"],
        "provinces": ["สระแก้ว", "จันทบุรี", "ตราด"],
    },
    {
        "slug": "east-coast",
        "code_num": 17,
        "code_str": "17",
        "name_th": "ลุ่มน้ำชายฝั่งทะเลตะวันออก",
        "name_en": "East Coast River Basin",
        "thaiwater_name": "ชายฝั่งทะเลตะวันออก",
        "rid_utok_ids": [6],
        "rid_basin_names": ["ลุ่มน้ำชายฝั่งทะเลตะวันออก", "ลุ่มน้ำระยอง", "ลุ่มน้ำจันทบุรี"],
        "provinces": ["ชลบุรี", "ระยอง", "จันทบุรี", "ตราด"],
    },
    {
        "slug": "phetchaburi",
        "code_num": 18,
        "code_str": "18",
        "name_th": "ลุ่มน้ำเพชรบุรี-ประจวบคีรีขันธ์",
        "name_en": "Phetchaburi-Prachuap River Basin",
        "thaiwater_name": "เพชรบุรี-ประจวบคีรีขันธ์",
        "rid_utok_ids": [7],
        "rid_basin_names": ["ลุ่มน้ำเพชรบุรี", "ลุ่มน้ำปราณบุรี"],
        "provinces": ["เพชรบุรี", "ประจวบคีรีขันธ์", "ราชบุรี"],
    },
    {
        "slug": "south-east-upper",
        "code_num": 19,
        "code_str": "19",
        "name_th": "ลุ่มน้ำภาคใต้ฝั่งตะวันออกตอนบน",
        "name_en": "Upper South East Coast Basin",
        "thaiwater_name": "ภาคใต้ฝั่งตะวันออกตอนบน",
        "rid_utok_ids": [7, 8],
        "rid_basin_names": ["ลุ่มน้ำภาคใต้ฝั่งตะวันออกตอนบน", "ลุ่มน้ำตาปี", "ลุ่มน้ำชุมพร"],
        "provinces": ["ชุมพร", "สุราษฎร์ธานี", "นครศรีธรรมราช"],
    },
    {
        "slug": "songkhla-lake",
        "code_num": 20,
        "code_str": "20",
        "name_th": "ลุ่มน้ำทะเลสาบสงขลา",
        "name_en": "Songkhla Lake Basin",
        "thaiwater_name": "ทะเลสาบสงขลา",
        "rid_utok_ids": [8],
        "rid_basin_names": ["ลุ่มน้ำทะเลสาบสงขลา"],
        "provinces": ["นครศรีธรรมราช", "พัทลุง", "สงขลา"],
    },
    {
        "slug": "south-east-lower",
        "code_num": 21,
        "code_str": "21",
        "name_th": "ลุ่มน้ำภาคใต้ฝั่งตะวันออกตอนล่าง",
        "name_en": "Lower South East Coast Basin",
        "thaiwater_name": "ภาคใต้ฝั่งตะวันออกตอนล่าง",
        "rid_utok_ids": [8],
        "rid_basin_names": ["ลุ่มน้ำภาคใต้ฝั่งตะวันออกตอนล่าง", "ลุ่มน้ำปัตตานี", "ลุ่มน้ำสายบุรี"],
        "provinces": ["สงขลา", "ปัตตานี", "ยะลา", "นราธิวาส"],
    },
    {
        "slug": "south-west",
        "code_num": 22,
        "code_str": "22",
        "name_th": "ลุ่มน้ำภาคใต้ฝั่งตะวันตก",
        "name_en": "South West Coast Basin",
        "thaiwater_name": "ภาคใต้ฝั่งตะวันตก",
        "rid_utok_ids": [7, 8],
        "rid_basin_names": ["ลุ่มน้ำภาคใต้ฝั่งตะวันตก", "ลุ่มน้ำตรัง", "ลุ่มน้ำพังงา"],
        "provinces": ["ระนอง", "พังงา", "ภูเก็ต", "กระบี่", "ตรัง", "สตูล"],
    },
]

# Quick Lookups
_BY_SLUG: Dict[str, Dict[str, Any]] = {b["slug"]: b for b in BASIN_DEFINITIONS}
_BY_CODE_NUM: Dict[int, Dict[str, Any]] = {b["code_num"]: b for b in BASIN_DEFINITIONS}
_BY_CODE_STR: Dict[str, Dict[str, Any]] = {b["code_str"]: b for b in BASIN_DEFINITIONS}


def get_basin(identifier: Any) -> Optional[Dict[str, Any]]:
    """
    Resolves basin metadata from slug, code_num, code_str, or Thai name.
    """
    if identifier is None:
        return None
    if isinstance(identifier, int):
        return _BY_CODE_NUM.get(identifier)
    
    s = str(identifier).strip().lower()
    if s in _BY_SLUG:
        return _BY_SLUG[s]
    if s.zfill(2) in _BY_CODE_STR:
        return _BY_CODE_STR[s.zfill(2)]
    
    # Try matching Thai name or thaiwater_name
    for b in BASIN_DEFINITIONS:
        if s in b["name_th"].lower() or b["thaiwater_name"].lower() in s or b["name_th"].lower() in s:
            return b
        if b["name_en"].lower() in s:
            return b
    return None


def get_all_basins() -> List[Dict[str, Any]]:
    """Returns list of all 22 official basins."""
    return BASIN_DEFINITIONS


def get_all_slugs() -> List[str]:
    """Returns list of all 22 basin slugs."""
    return [b["slug"] for b in BASIN_DEFINITIONS]


def get_thaiwater_mapping() -> Dict[str, Tuple[str, str]]:
    """Returns {slug: (thaiwater_name, code_str)} for GIS boundary downloader."""
    return {b["slug"]: (b["thaiwater_name"], str(b["code_num"])) for b in BASIN_DEFINITIONS}


def get_rid_mapping() -> Dict[str, Dict[str, Any]]:
    """Returns mapping from slug to RID Utok IDs and legacy basin names."""
    return {
        b["slug"]: {
            "basin_id": b["code_num"],
            "utok_ids": b["rid_utok_ids"],
            "name_th": b["name_th"],
            "rid_basin_names": b["rid_basin_names"],
        }
        for b in BASIN_DEFINITIONS
    }
