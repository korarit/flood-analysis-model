# Flood Analysis Model — Data Collection & Processing Pipeline

ชุดเครื่องมือและ Scripts สำหรับรวบรวมข้อมูลสถานี, ข้อมูลปริมาณน้ำฝน (Rainfall), และข้อมูลระดับน้ำ (Water Level) ย้อนหลังช่วง **01/2025 – 07/2026** (รวม 19 เดือน) แยกตาม 5 ลุ่มน้ำหลักใน Frontend (`yom`, `nan`, `ping`, `wang`, `chao-phraya`)

---

## 1. ภาพรวมชุด Scripts (Available Scripts)

| Script | หน้าที่ | แหล่งข้อมูล | ความสามารถพิเศษ |
| :--- | :--- | :--- | :--- |
| [`generate_station_dataset.py`](generate_station_dataset.py) | แยกรายการสถานีตาม 5 ลุ่มน้ำ และกลุ่มหน่วยงาน (HII, DWR, RID) | `response-api-rainstation.json`<br>`reqponse-api-waterlevel.json` | สร้าง JSON & CSV สำหรับ Metadata |
| [`fetch_hii_data.py`](fetch_hii_data.py) | ดาวน์โหลดข้อมูลฝนและระดับน้ำของ สสน. + MOU ย้อนหลัง 01/2025 – 07/2026 | HII Open Data Catalog | โหลดตรงจาก Catalog รายเดือน |
| [`scrape_dwr_rain.py`](scrape_dwr_rain.py) | Web Scraper ดึงข้อมูลฝนย้อนหลังของกรมทรัพยากรน้ำ (DWR) | `ews.dwr.go.th/ews/show-rain` | **Multi-Threading** + **Auto-Resume** + **`--dir`** |
| [`scrape_rid_waterlevel.py`](scrape_rid_waterlevel.py) | Web Scraper ดึงข้อมูลระดับน้ำย้อนหลังของกรมชลประทาน (RID) | `hyd-app-db.rid.go.th` | ดึงผ่าน WCF Service แยกรายลุ่มน้ำ + **`--dir`** |

---

## 2. วิธีการรัน Scripts (How to Run)

### 2.1 สร้างข้อมูลรายการสถานี (Station Metadata)
```bash
python generate_station_dataset.py
```

---

### 2.2 ดึงข้อมูล HII (สสน. และ MOU) — ฝน & ระดับน้ำ
```bash
# ทดสอบแบบ Smoke Test (1 เดือนแรก)
python fetch_hii_data.py --smoke-test --basin yom

# ดึงข้อมูลจริงเต็มรูปแบบครบทุก 5 ลุ่มน้ำ (202501 ถึง 202607)
python fetch_hii_data.py --start 202501 --end 202607
```

---

### 2.3 Scrape ข้อมูลฝน กรมทรัพยากรน้ำ (DWR) — แบบ Multi-Threaded เร็วขึ้น 8x–10x

#### คุณสมบัติพิเศษของ Script DWR:
- **Custom Dataset Directory**: กำหนดโฟลเดอร์ปลายทาง dataset ได้ด้วย `--dir <path>` (หากไม่ใส่จะบันทึกลงใน `dataset/` อัตโนมัติ)
- **Parallel Scraping**: ดึงข้อมูลพร้อมกันหลายสถานีด้วย `--workers 4` (ปรับเพิ่ม-ลดได้) และ date worker ภายในสถานี `--inner-workers 10`
- **Auto-Resume**: บันทึก Checkpoint แยกรายสถานีใน `dwr_station_cache/` หากหยุดกลางคัน เมื่อสั่งรันใหม่จะข้ามสถานีที่ทำเสร็จแล้วทันที
- **FilterType Options**:
  - `1D` (ค่าเริ่มต้น): ความละเอียดข้อมูลระดับ 1 ชั่วโมง (Strict 1-Hour resolution)
  - `3D`: ความละเอียดข้อมูล 2 ชั่วโมง

#### ตัวอย่างคำสั่ง:
```bash
# 1. ทดสอบ Smoke Test
python scrape_dwr_rain.py --smoke-test --basin yom

# 2. ดึงข้อมูลจริงลุ่มน้ำยม (แบบ Multi-threaded + Auto-Resume)
python scrape_dwr_rain.py --basin yom --start-date 2025-01-01 --end-date 2026-07-31 --workers 4

# 3. กำหนดโฟลเดอร์จัดเก็บ dataset ด้วย --dir
python scrape_dwr_rain.py --dir ./dataset --basin yom --workers 4

# 4. ดึงเฉพาะสถานีที่ระบุ (เช่น STN1226)
python scrape_dwr_rain.py --station STN1226 --start-date 2025-01-01 --end-date 2026-07-31
```

---

### 2.4 Scrape ข้อมูลระดับน้ำ กรมชลประทาน (RID)

#### คุณสมบัติพิเศษของ Script RID:
- **Custom Dataset Directory**: กำหนดโฟลเดอร์ปลายทาง dataset ได้ด้วย `--dir <path>` (หากไม่ใส่จะบันทึกลงใน `dataset/` อัตโนมัติ)
- **Multi-threaded Date Querying**: ดึงข้อมูลพร้อมกันแบบ Parallel ด้วย `--workers 10`

#### ตัวอย่างคำสั่ง:
```bash
# 1. ทดสอบ Smoke Test
python scrape_rid_waterlevel.py --smoke-test --basin yom

# 2. ดึงข้อมูลจริงลุ่มน้ำยม (2025-01-01 ถึง 2026-07-31)
python scrape_rid_waterlevel.py --basin yom --start-date 2025-01-01 --end-date 2026-07-31

# 3. กำหนดโฟลเดอร์จัดเก็บ dataset ด้วย --dir
python scrape_rid_waterlevel.py --dir ./dataset --basin yom
```

---

## 3. โครงสร้างโฟลเดอร์ผลลัพธ์ (Dataset Structure)

```text
flood-analysis-model/dataset/
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
