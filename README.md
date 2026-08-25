# Water Analysis Model — Data Collection & Processing Pipeline

ชุดเครื่องมือและ Scripts สำหรับรวบรวมข้อมูลสถานี, ข้อมูลปริมาณน้ำฝน (Rainfall), และข้อมูลระดับน้ำ (Water Level) ย้อนหลังช่วง **01/2025 – 07/2026** (รวม 19 เดือน) แยกตาม 5 ลุ่มน้ำหลักใน Frontend (`yom`, `nan`, `ping`, `wang`, `chao-phraya`)

---

## 1. ภาพรวมชุด Scripts (Available Scripts)

| Script | หน้าที่ | แหล่งข้อมูล | ความสามารถพิเศษ |
| :--- | :--- | :--- | :--- |
| [`generate_station_dataset.py`](file:///e:/water-analysis-project/water-analysis-model/generate_station_dataset.py) | แยกรายการสถานีตาม 5 ลุ่มน้ำ และกลุ่มหน่วยงาน (HII, DWR, RID) | `response-api-rainstation.json`<br>`reqponse-api-waterlevel.json` | สร้าง JSON & CSV สำหรับ Metadata |
| [`fetch_hii_data.py`](file:///e:/water-analysis-project/water-analysis-model/fetch_hii_data.py) | ดาวน์โหลดข้อมูลฝนและระดับน้ำของ สสน. + MOU ย้อนหลัง 01/2025 – 07/2026 | HII Open Data Catalog | โหลดตรงจาก Catalog รายเดือน |
| [`scrape_dwr_rain.py`](file:///e:/water-analysis-project/water-analysis-model/scrape_dwr_rain.py) | Web Scraper ดึงข้อมูลฝนย้อนหลังของกรมทรัพยากรน้ำ (DWR) | `ews.dwr.go.th/ews/show-rain` | **Multi-Threading (8 workers)** + **Auto-Resume** |
| [`scrape_rid_waterlevel.py`](file:///e:/water-analysis-project/water-analysis-model/scrape_rid_waterlevel.py) | Web Scraper ดึงข้อมูลระดับน้ำย้อนหลังของกรมชลประทาน (RID) | `hyd-app-db.rid.go.th` | ดึงผ่าน WCF Service แยกรายลุ่มน้ำ |

---

## 2. วิธีการรัน Scripts (How to Run)

### 2.1 สร้างข้อมูลรายการสถานี (Station Metadata)
```bash
python water-analysis-model/generate_station_dataset.py
```

---

### 2.2 ดึงข้อมูล HII (สสน. และ MOU) — ฝน & ระดับน้ำ
```bash
# ทดสอบแบบ Smoke Test (1 เดือนแรก)
python water-analysis-model/fetch_hii_data.py --smoke-test --basin yom

# ดึงข้อมูลจริงเต็มรูปแบบครบทุก 5 ลุ่มน้ำ (202501 ถึง 202607)
python water-analysis-model/fetch_hii_data.py --start 202501 --end 202607
```

---

### 2.3 Scrape ข้อมูลฝน กรมทรัพยากรน้ำ (DWR) — แบบ Multi-Threaded เร็วขึ้น 8x–10x

#### คุณสมบัติพิเศษของ Script DWR:
- **Parallel Scraping**: ดึงข้อมูลพร้อมกัน 8 สถานีด้วย `--workers 8` (ปรับเพิ่ม-ลดได้)
- **Auto-Resume**: บันทึก Checkpoint แยกรายสถานีใน `dwr_station_cache/` หากหยุดกลางคัน เมื่อสั่งรันใหม่จะข้ามสถานีที่ทำเสร็จแล้วทันที
- **FilterType Options**:
  - `3D` (ค่าเริ่มต้น): ความละเอียดข้อมูลทุก 2 ชั่วโมง (~193 requests/สถานี)
  - `7D`: ความละเอียดข้อมูลทุก 6 ชั่วโมง (~83 requests/สถานี — เร็วขึ้นกว่าเดิมอีก 2.3 เท่า)
  - `1D`: ความละเอียดข้อมูลทุก 30 นาที

#### ตัวอย่างคำสั่ง:
```bash
# 1. ทดสอบ Smoke Test
python water-analysis-model/scrape_dwr_rain.py --smoke-test --basin yom

# 2. ดึงข้อมูลจริงลุ่มน้ำยม (แบบ Multi-threaded 8 workers + Auto-Resume)
python water-analysis-model/scrape_dwr_rain.py --basin yom --start-date 2025-01-01 --end-date 2026-07-31 --workers 8

# 3. ดึงข้อมูลแบบเร็วพิเศษโดยใช้ FilterType 7D
python water-analysis-model/scrape_dwr_rain.py --basin yom --start-date 2025-01-01 --end-date 2026-07-31 --filter-type 7D --workers 8

# 4. ดึงเฉพาะสถานีที่ระบุ (เช่น STN1226)
python water-analysis-model/scrape_dwr_rain.py --station STN1226 --start-date 2025-01-01 --end-date 2026-07-31
```

---

### 2.4 Scrape ข้อมูลระดับน้ำ กรมชลประทาน (RID)
```bash
# 1. ทดสอบ Smoke Test
python water-analysis-model/scrape_rid_waterlevel.py --smoke-test --basin yom

# 2. ดึงข้อมูลจริงลุ่มน้ำยม (2025-01-01 ถึง 2026-07-31)
python water-analysis-model/scrape_rid_waterlevel.py --basin yom --start-date 2025-01-01 --end-date 2026-07-31
```

---

## 3. โครงสร้างโฟลเดอร์ผลลัพธ์ (Dataset Structure)

```text
water-analysis-model/dataset/
├── summary-list-station.json
├── yom/
│   ├── station/
│   │   ├── yom_rain_stations.json / .csv           (รวมสถานีฝน)
│   │   ├── yom_rain_stations_hii.json / .csv       (สถานีฝน HII + MOU)
│   │   ├── yom_rain_stations_dwr.json / .csv       (สถานีฝน DWR)
│   │   ├── yom_waterlevel_stations.json / .csv     (รวมสถานีระดับน้ำ)
│   │   ├── yom_waterlevel_stations_hii.json / .csv (สถานีระดับน้ำ HII + MOU)
│   │   ├── yom_waterlevel_stations_dwr.json / .csv (สถานีระดับน้ำ DWR)
│   │   └── yom_waterlevel_stations_rid.json / .csv (สถานีระดับน้ำ RID กรมชล)
│   ├── rainfall/
│   │   ├── dwr_station_cache/                      (Checkpoint รายสถานี DWR)
│   │   ├── yom_dwr_hourly_rain.csv
│   │   ├── yom_hii_rain_non_mou_202501.csv ...
│   │   └── yom_hii_rain_mou_202501.csv ...
│   └── waterlevel/
│       ├── yom_hii_wl_non_mou_202501.csv ...
│       ├── yom_hii_wl_mou_202501.csv ...
│       └── yom_rid_hourly_waterlevel.csv
├── nan/
├── ping/
├── wang/
└── chao-phraya/
```
