# Flood Analysis Model — Data Collection & Processing Pipeline

ชุดเครื่องมือและ Scripts สำหรับรวบรวมข้อมูลสถานี, ข้อมูลปริมาณน้ำฝน (Rainfall), และข้อมูลระดับน้ำ (Water Level) ย้อนหลังช่วง **01/2025 – 07/2026** (รวม 19 เดือน) แยกตาม 5 ลุ่มน้ำหลักใน Frontend (`yom`, `nan`, `ping`, `wang`, `chao-phraya`)

---

## 1. ภาพรวมชุด Scripts (Available Scripts)

| Script | หน้าที่ | แหล่งข้อมูล | ความสามารถพิเศษ |
| :--- | :--- | :--- | :--- |
| [`generate_station_dataset.py`](generate_station_dataset.py) | แยกรายการสถานีตาม 5 ลุ่มน้ำ และกลุ่มหน่วยงาน (HII, DWR, RID) | `response-api-rainstation.json`<br>`reqponse-api-waterlevel.json` | สร้าง JSON & CSV สำหรับ Metadata |
| [`scrape_rid_station_metadata.py`](scrape_rid_station_metadata.py) | ดึงค่าระดับศูนย์เสา (ZG), ตลิ่ง (braelevel), ความจุลำน้ำ (QMax) จากชลประทาน สำนัก 1-8 | `hyd-app-db.rid.go.th` | คำนวณ `bank_level_msl`, `warning_level_msl` และอัปเดตสถานีทันที |
| [`fetch_hii_data.py`](fetch_hii_data.py) | ดาวน์โหลดข้อมูลฝนและระดับน้ำของ สสน. + MOU ย้อนหลัง 01/2025 – 07/2026 | HII Open Data Catalog | โหลดตรงจาก Catalog รายเดือน |
| [`scrape_dwr_rain.py`](scrape_dwr_rain.py) | Web Scraper ดึงข้อมูลฝนย้อนหลังของกรมทรัพยากรน้ำ (DWR) | `ews.dwr.go.th/ews/show-rain` | **Multi-Threading** + **Auto-Resume** + **`--dir`** |
| [`scrape_rid_waterlevel.py`](scrape_rid_waterlevel.py) | Web Scraper ดึงข้อมูลระดับน้ำย้อนหลังของกรมชลประทาน (RID) | `hyd-app-db.rid.go.th` | ดึงผ่าน WCF Service แยกรายลุ่มน้ำ + **`--dir`** |
| [`consolidate_basin_data.py`](consolidate_basin_data.py) | รวมและ Clean ข้อมูลฝน/ระดับน้ำจากทุกแหล่งเป็นไฟล์เดี่ยวต่อเนื่อง | DWR, HII, RID Files ใน `dataset/` | **Harmonize 10m -> 1h**, **Deduplicate**, **Sort**, **Summary JSON** |

---

## 2. วิธีการรัน Scripts (How to Run)

### 2.1 สร้างข้อมูลรายการสถานี (Station Metadata)
```bash
python generate_station_dataset.py
```

### 2.2 ดึงและคำนวณค่าระดับตลิ่ง (Bank MSL), ศูนย์เสา (ZG), QMax ของกรมชลประทาน (สำนัก 1-8)
```bash
python scrape_rid_station_metadata.py --basin all
```

---

### 2.3 ดึงข้อมูล HII (สสน. และ MOU) — ฝน & ระดับน้ำ
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

### 2.5 รวบรวมและ Clean ข้อมูลให้เป็นไฟล์ต่อเนื่อง (Data Consolidation Pipeline)

สร้างไฟล์ Time-series รวมต่อเนื่องระดับ 1 ชั่วโมงสำหรับเตรียมเข้า Model ([`req-make-model.md`](req-make-model.md)):
- รวมข้อมูลฝนจาก **DWR + HII MOU + HII Non-MOU**
- รวมข้อมูลระดับน้ำจาก **RID + HII** (แปลง 10 นาที เป็น 1 ชั่วโมงเฉลี่ย)
- กรองช่วงเวลา (01/2025 – 07/2026), Deduplicate, เรียงลำดับเวลา และสร้าง Summary JSON

#### ตัวอย่างคำสั่ง:
```bash
# 1. รวบรวมข้อมูลสำหรับลุ่มน้ำยม
python consolidate_basin_data.py --basin yom

# 2. รวบรวมข้อมูลทุก 5 ลุ่มน้ำ
python consolidate_basin_data.py --basin all

# 3. กำหนดโฟลเดอร์ dataset และช่วงวันที่ต้องการ
python consolidate_basin_data.py --basin yom --dir ./dataset --start-date 2025-01-01 --end-date 2026-07-31
```

---

## 3. โครงสร้างโฟลเดอร์ผลลัพธ์ (Dataset Structure)

```text
```text
flood-analysis-model/
├── dataset/                                        <-- [Station & Time-Series Data (--dir)]
│   ├── summary-list-station.json
│   ├── yom/
│   │   ├── station/                                (สถานีฝนและระดับน้ำ)
│   │   ├── rainfall/                               (ข้อมูลฝนดิบ)
│   │   ├── waterlevel/                             (ข้อมูลระดับน้ำดิบ)
│   │   ├── catchment/                              (catchments.geojson ขอบเขตลุ่มน้ำย่อย)
│   │   └── processed/                              (ไฟล์โมเดลพร้อมใช้งาน และ GeoJSON แผนที่)
├── terrain/                                        <-- [Terrain & DEM Raster Data (--terrain-dir)]
│   ├── yom/
│   │   ├── alos_tiles/                             (Zip tiles & extracted .dem.tif)
│   │   ├── raw_dem.tif                             (Mosaic DEM 12.5m รวม)
│   │   ├── conditioned_dem.tif                     (DEM หลังทำ Pit Filling)
│   │   ├── flow_direction.tif                      (D8 Flow Direction Raster)
│   │   └── flow_accumulation.tif                   (Flow Accumulation Raster)
│   ├── nan/
│   ├── ping/
│   ├── wang/
│   └── chao-phraya/
```

---

## 4. โมเดลการไหลของน้ำและเวลาน้ำเดินทาง (Water Flow Chain & Travel Time Model)

ชุดสคริปต์ในโฟลเดอร์ `scripts/` สำหรับสร้างโมเดลโครงข่ายแม่น้ำ (Chain Water) และคำนวณเวลาน้ำหลากเดินทาง (Travel Time) ตามข้อกำหนดใน [`req-make-model.md`](req-make-model.md) และ [`backend-req.md`](backend-req.md)

### 4.1 รายการสคริปต์ใน `scripts/` (6-Step Pipeline)

| สคริปต์ | หน้าที่หลัก |
| :--- | :--- |
| [`scripts/run_model_pipeline.py`](scripts/run_model_pipeline.py) | **Master Script**: สั่งรัน Pipeline ครบทั้ง 6 ขั้นตอนอัตโนมัติจบในคำสั่งเดียว |
| [`scripts/generate_flow_paths.py`](scripts/generate_flow_paths.py) | **Standalone Flow Path Generator**: รันสร้าง/อัปเดตเฉพาะ `flow_paths.geojson` และ Station Relations ด้วยโมเดล Hybrid (OSM + D8) จบในไม่กี่วินาที |
| [`scripts/fetch_basin_gis.py`](scripts/fetch_basin_gis.py) | Step 1: ดาวน์โหลดขอบเขตลุ่มน้ำ, OpenStreetMap Waterways, HydroRIVERS, และ ALOS PALSAR 12.5m DEM จาก NASA Earthdata ลงใน `terrain/` |
| [`scripts/build_river_network.py`](scripts/build_river_network.py) | Step 2: ทำ Pit Filling, คำนวณ D8 Flow Direction, Flow Accumulation, และสกัด `river_network.geojson` |
| [`scripts/build_station_chain.py`](scripts/build_station_chain.py) | Step 3: Snap สถานีเข้าแนวแม่น้ำ OSM, ลากเส้นทางน้ำไหล Hybrid Flow Paths, และตัดขอบเขตลุ่มน้ำย่อย `catchments.geojson` |
| [`scripts/train_response_model.py`](scripts/train_response_model.py) | Step 4: ตรวจจับน้ำขึ้นต่อเนื่อง $\ge 4$ ชม., วิเคราะห์ช่วงน้ำนิ่งแช่, คำนวณ Observed Travel Time, และเทรน ML Model |
| [`scripts/calculate_rainfall_thresholds.py`](scripts/calculate_rainfall_thresholds.py) | Step 5: ใช้ ML เรียนรู้สภาวะดิน (Wet/Normal/Dry) และคำนวณปริมาณฝนสะสมกระตุ้นน้ำหลาก & เตือนภัย 4 ช่วงเวลา (`3h`, `24h`, `72h`, `168h`) |
| [`scripts/export_backend_dataset.py`](scripts/export_backend_dataset.py) | Step 6: Export ข้อมูลลงตาราง Database `station_relations` และสร้าง `relations_frontend.json` |

---

### 4.2 สคริปต์ด่วนเฉพาะจุด (Standalone Fast Utilities)

ระบบถูกออกแบบให้สามารถรันแยกเฉพาะส่วนที่ต้องการอัปเดตได้ โดยไม่ต้องรันไปป์ไลน์ใหม่ตั้งแต่ต้น (ช่วยประหยัดเวลาประมวลผล)

#### 🛰️ (1) ดาวน์โหลด GIS & DEM (`fetch_basin_gis.py`)
ดาวน์โหลดขอบเขตลุ่มน้ำ, OSM Waterways, และ ALOS PALSAR 12.5m DEM จาก NASA Earthdata
* **Arguments:** `--basin` (yom, nan, all), `--dir`, `--terrain-dir`, `--username`, `--password`, `--force-osm`
```bash
./venv/bin/python scripts/fetch_basin_gis.py --basin yom
```

#### 🌊 (2) สร้าง Flow Paths พื้นฐาน (`generate_flow_paths.py`)
ลากเส้นทางการไหลของน้ำแบบ Hybrid (Rain-to-Gauge ใช้ DEM, Gauge-to-Gauge ใช้ DEM D8)
* **Arguments:** `--basin`, `--dir`, `--terrain-dir`, `--force`
```bash
./venv/bin/python scripts/generate_flow_paths.py --basin yom --force
```

#### 📍 (3) สร้างขอบเขตพื้นที่รับน้ำย่อย (`generate_catchments.py`)
สกัด Polygon พื้นที่รับน้ำ (Catchment Area) เฉพาะของแต่ละสถานีจาก Flow Accumulation
* **Arguments:** `--basin`, `--dir`, `--terrain-dir`
```bash
./venv/bin/python scripts/generate_catchments.py --basin yom
```

#### 🗺️ (4) ลากเส้นความสัมพันธ์น้ำ-น้ำแบบแม่นยำสูง (`generate_osm_waterlevel_relations.py`)
สร้างเส้นทางน้ำไหล (Gauge-to-Gauge) โดยใช้โครงข่าย Vector จาก OSM เพียวๆ (ไม่พึ่งพารูปแบบ Grid ของ DEM) ช่วยเพิ่มความแม่นยำของระยะทางและเวลา (Kinematic Wave) ให้ตรงกับแม่น้ำจริง
* **Arguments:** `--basin`, `--dir`, `--terrain-dir`, `--force`
```bash
./venv/bin/python scripts/generate_osm_waterlevel_relations.py --basin yom
```

#### ⚡ (5) ซ่อมแซม Travel Time ด่วน (`patch_travel_times.py`)
สคริปต์ฉุกเฉินสำหรับแก้ปัญหาเวลาเป็น 0 โดยมันจะคำนวณ Kinematic Wave Travel Time แทรกเข้าไปในไฟล์ `station-relations.json` เดิมทันทีโดยไม่ต้องรันเส้นทาง DEM ใหม่ (ประหยัดเวลาไปได้ 5 นาที)
* **Arguments:** `--basin`, `--dir`
```bash
./venv/bin/python scripts/patch_travel_times.py --basin yom
```

#### 🤖 (6) เทรนโมเดลเวลาการเดินทางน้ำ (`train_response_model.py`)
ดึงข้อมูลประวัติระดับน้ำย้อนหลัง (Water Level Dataset) มาวิเคราะห์ Peak Matching หาค่า Empirical Travel Time (เวลาที่น้ำเดินทางจริงๆ) และสร้าง Model พยากรณ์เวลา
* **Arguments:** `--basin`, `--dir`
```bash
./venv/bin/python scripts/train_response_model.py --basin yom
```

#### 🌧️ (7) อัปเดตเกณฑ์ฝนเตือนภัย (`calculate_rainfall_thresholds.py`)
ใช้ AI (K-Means Clustering) เรียนรู้สภาวะดิน (Wet/Normal/Dry) ของแต่ละลุ่มน้ำ และคำนวณเกณฑ์ฝนสะสมกระตุ้นน้ำหลากใหม่ (3h, 24h, 72h, 168h) แยกรายสถานี
* **Arguments:** `--basin`, `--dir`, `--update-existing`
```bash
./venv/bin/python scripts/calculate_rainfall_thresholds.py --basin yom --update-existing
```

#### 📦 (8) ส่งออกข้อมูลไปใช้งาน (`export_backend_dataset.py`)
รวบรวมไฟล์ JSON ย่อยๆ ทั้งหมด สังเคราะห์และประกอบร่างเป็น `relations_frontend.json` (สำหรับ UI) และ Database Payload
* **Arguments:** `--basin`, `--dir`
```bash
./venv/bin/python scripts/export_backend_dataset.py --basin yom
```

---

### 4.3 ตัวอย่างคำสั่งการรัน Master Script (Full 6-Step Pipeline)

```bash
# 1. รันลุ่มน้ำยม (ระบุ NASA Earthdata Login สำหรับโหลด ALOS PALSAR 12.5m DEM)
./venv/bin/python scripts/run_model_pipeline.py --basin yom --username <earthdata_user> --password <earthdata_pass>

# 2. หรือตั้งค่าใน Environment Variables
export EARTHDATA_USER="your_username"
export EARTHDATA_PASS="your_password"
./venv/bin/python scripts/run_model_pipeline.py --basin yom

# 3. กำหนดโฟลเดอร์ terrain อิสระจาก dataset ด้วย --terrain-dir
./venv/bin/python scripts/run_model_pipeline.py --basin yom --dir ./dataset --terrain-dir ./terrain

# 4. รันครบทุก 5 ลุ่มน้ำหลัก
./venv/bin/python scripts/run_model_pipeline.py --basin all
```

---

### 4.4 ไฟล์ผลลัพธ์โมเดลสำหรับ Backend & แผนที่ Frontend

* `dataset/{basin}/gis/{basin}_boundary.geojson` — รูปปิด Polygon ขอบเขตลุ่มน้ำ
* `dataset/{basin}/gis/{basin}_subbasins.geojson` — รูปปิด Polygon ขอบเขตลุ่มน้ำย่อยแบบเรียงลำดับต้นน้ำสู่ท้ายน้ำ (Topological)
* `dataset/{basin}/gis/osm_waterways.geojson` — โครงข่ายแม่น้ำ ลำห้วย คลอง จาก OpenStreetMap
* `terrain/{basin}/raw_dem.tif` — โมเสกภาพแบบจำลองความสูงภูมิประเทศ ALOS PALSAR 12.5m DEM
* `dataset/{basin}/response/rainfall-thresholds.json` — เกณฑ์ฝนสะสมกระตุ้นน้ำหลากและเตือนภัยแยกรายคู่สถานีฝน-น้ำ
* `dataset/{basin}/processed/station_relations_db.json` — Payload สำหรับตาราง `station_relations` ใน PostgreSQL
* `dataset/{basin}/processed/relations_frontend.json` — โครงสร้างข้อมูลสำหรับหน้า `StationRelations.tsx` บน Frontend
* `dataset/{basin}/processed/flow_paths.geojson` — เส้นทางเวกเตอร์การไหลของน้ำ (Rain-to-Gauge และ Gauge-to-Gauge) สำหรับแสดงผลบน `LeafletWaterMap.tsx`
* `dataset/{basin}/processed/river_network.geojson` — เส้นโครงข่ายลำน้ำสายหลักและสายรองทั้งหมด พร้อมความชัน
* `dataset/{basin}/catchment/catchments.geojson` — รูปปิด Polygon ขอบเขตพื้นที่รับน้ำย่อยของแต่ละสถานี


