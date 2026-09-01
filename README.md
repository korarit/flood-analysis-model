# Flood Analysis Model — Hydrological & Physics-Informed ML Engine

ระบบประมวลผลข้อมูลอุทกวิทยา, โครงข่ายทิศทางการไหลของน้ำ (Hydrological River Routing), และแบบจำลอง Machine Learning สำหรับคำนวณ **เวลาน้ำหลากเดินทาง (Flood Wave Travel Time)** และ **เกณฑ์ฝนกระตุ้นเตือนภัยน้ำท่วม 4 กรอบเวลา (Multi-Window Rainfall Trigger Thresholds)** ครอบคลุม 5 ลุ่มน้ำหลักในประเทศไทย (`yom`, `nan`, `ping`, `wang`, `chao-phraya`)

---

## 📖 เอกสารทางเทคนิคเชิงลึก (Technical Architecture Docs)

สำหรับรายละเอียดเชิงลึกเกี่ยวกับทฤษฎีคณิตศาสตร์ ฟิสิกส์ชลศาสตร์ อัลกอริทึม ML และ Mermaid Flowcharts สามารถอ่านเพิ่มเติมได้ที่:

* 🤖 **[docs/Technical-of-ML.md](docs/Technical-of-ML.md)** : สถาปัตยกรรม **Machine Learning & Hydrological Response Model**
  * ทฤษฎี Kleitz-Seddon Law, Hydrograph Centroid/Plateau Matching, Unsupervised K-Means Soil Moisture Clustering (AMC), Physics-Informed Ridge Regression และเกณฑ์ฝน 4 ช่วงเวลา (`3h`, `24h`, `72h`, `168h`)
* 🌊 **[docs/Technical-of-WaterFlow.md](docs/Technical-of-WaterFlow.md)** : สถาปัตยกรรม **2-Layer Hybrid Water Flow & Terrain Engine**
  * การขุดร่องน้ำชลศาสตร์ (Hydro-Enforced Stream Burning 15m), D8 Flow Direction, Flow Accumulation, Directed River Graph (OSM Vector), การ Snap สถานี, และการแบ่งพื้นที่รับน้ำ (Catchment Delineation)

---

## 1. ภาพรวมสถาปัตยกรรมระบบ (System Architecture)

ระบบทำงานเป็นแบบจำลองแบบ **Physics-Informed Hydrological & Machine Learning Pipeline** ที่เชื่อมโยงข้อมูลตั้งแต่ระดับภูมิประเทศ ดาวเทียม ไปจนถึงการส่งออกฐานข้อมูลและแสดงผลบนแผนที่:

```text
┌────────────────────────────────────────────────────────────────────────────────────────┐
│                        FLOOD ANALYSIS MODEL & ML PIPELINE                              │
└────────────────────────────────────────────────────────────────────────────────────────┘
                                            │
  ┌─────────────────────────────────────────┼─────────────────────────────────────────┐
  ▼                                         ▼                                         ▼
[1. DATA INGESTION]                 [2. TERRAIN & GIS FLOW]               [3. PIML HYDROLOGY ENGINE]
• Web Scraping (DWR / RID)          • FABDEM 30m Mosaic                   • Tri-Feature Event Matching
• HII Open Data Catalog             • Hydro Stream Burning (15m)          • Empirical Observed Travel Time
• Station Metadata (ZG, Bank MSL)   • D8 Flow Direction & Acc             • Ridge Regression (Unobserved)
• Data Harmonization (10m -> 1h)    • Directed River Graph (OSM)          • K-Means Soil Clustering (AMC)
• Deduplication & Cleaning          • 2-Layer Hybrid Flow Paths           • 4-Window Rain Thresholds
                                            │
                                            ▼
                             [4. BACKEND & FRONTEND EXPORT]
                             • station_relations_db.json (PostgreSQL)
                             • relations_frontend.json (UI Flow)
                             • flow_paths.geojson (Leaflet Vector Map)
                             • catchments.geojson (Catchment Boundaries)
```

---

## 2. โครงสร้างชุด Scripts ทั้งหมด (Scripts Directory Structure)

### 2.1 ไปป์ไลน์หลัก 6 ขั้นตอน (Core 6-Step Model Pipeline)

| สคริปต์ | หน้าที่หลัก | เอกสารอ้างอิง |
| :--- | :--- | :--- |
| [`scripts/run_model_pipeline.py`](scripts/run_model_pipeline.py) | **Master Script**: สั่งรัน Pipeline ครบทั้ง 6 ขั้นตอนอัตโนมัติจบในคำสั่งเดียว | - |
| [`scripts/fetch_basin_gis.py`](scripts/fetch_basin_gis.py) | **Step 1:** ดาวน์โหลดขอบเขตลุ่มน้ำ, OSM Waterways, และ FABDEM 30m จาก AWS Open Data | [`Technical-of-WaterFlow.md`](docs/Technical-of-WaterFlow.md) |
| [`scripts/build_river_network.py`](scripts/build_river_network.py) | **Step 2:** ทำ Hydro-Enforcement, คำนวณ D8 Flow Direction & Flow Accumulation | [`Technical-of-WaterFlow.md`](docs/Technical-of-WaterFlow.md) |
| [`scripts/build_station_chain.py`](scripts/build_station_chain.py) | **Step 3:** สร้าง Directed River Graph, Snap สถานี, ลาก Hybrid Flow Paths, สกัด Catchments | [`Technical-of-WaterFlow.md`](docs/Technical-of-WaterFlow.md) |
| [`scripts/train_response_model.py`](scripts/train_response_model.py) | **Step 4:** ตรวจจับน้ำหลาก 4h Rise, สกัด Tri-Feature Lags, เทรน ML Travel Time Model | [`Technical-of-ML.md`](docs/Technical-of-ML.md) |
| [`scripts/calculate_rainfall_thresholds.py`](scripts/calculate_rainfall_thresholds.py) | **Step 5:** ทำ K-Means AMC ดิน 3 สภาวะ และคำนวณเกณฑ์ฝนเตือนภัย 4 กรอบเวลา (`3h`, `24h`, `72h`, `168h`) | [`Technical-of-ML.md`](docs/Technical-of-ML.md) |
| [`scripts/export_backend_dataset.py`](scripts/export_backend_dataset.py) | **Step 6:** ส่งออก Database Payloads (`station_relations`) และ `relations_frontend.json` | - |

---

### 2.2 สคริปต์สำหรับรวบรวมข้อมูลและเตรียมข้อมูล (Data Collection & Scraping)

| สคริปต์ | หน้าที่ | แหล่งข้อมูล |
| :--- | :--- | :--- |
| [`generate_station_dataset.py`](generate_station_dataset.py) | แยกรายการสถานีตาม 5 ลุ่มน้ำ และกลุ่มหน่วยงาน (HII, DWR, RID) | API Station JSON |
| [`scrape_rid_station_metadata.py`](scrape_rid_station_metadata.py) | ดึงค่าศูนย์เสา (ZG), ตลิ่ง (Bank MSL), ความจุลำน้ำ (QMax) ชลประทาน สำนัก 1-8 | `hyd-app-db.rid.go.th` |
| [`fetch_hii_data.py`](fetch_hii_data.py) | ดาวน์โหลดข้อมูลฝนและระดับน้ำย้อนหลังของ สสน. + MOU | HII Open Data Catalog |
| [`scrape_dwr_rain.py`](scrape_dwr_rain.py) | Web Scraper ดึงข้อมูลฝนย้อนหลังของกรมทรัพยากรน้ำ (DWR) | `ews.dwr.go.th` |
| [`scrape_rid_waterlevel.py`](scrape_rid_waterlevel.py) | Web Scraper ดึงข้อมูลระดับน้ำย้อนหลังของกรมชลประทาน (RID) | `hyd-app-db.rid.go.th` |
| [`consolidate_basin_data.py`](consolidate_basin_data.py) | รวมและ Clean ข้อมูลฝน/ระดับน้ำจากทุกแหล่ง แปลงเป็น Time-series 1 ชั่วโมง | DWR, HII, RID Files |

---

## 3. วิธีการรันระบบ (Quickstart & Execution Guide)

### 3.1 การรันโมเดลหลัก (Master Pipeline)

สามารถรันครบทุกขั้นตอน (Step 1 ถึง Step 6) จบในคำสั่งเดียว:

```bash
# 1. รันลุ่มน้ำยม (ค่าเริ่มต้นจะดาวน์โหลดข้อมูล DEM & GIS อัตโนมัติ)
./venv/bin/python scripts/run_model_pipeline.py --basin yom

# 2. กำหนดโฟลเดอร์ dataset และ terrain อิสระ
./venv/bin/python scripts/run_model_pipeline.py --basin yom --dir ./dataset --terrain-dir ./terrain

# 3. รันครบทุก 5 ลุ่มน้ำหลัก (yom, nan, ping, wang, chao-phraya)
./venv/bin/python scripts/run_model_pipeline.py --basin all
```

---

### 3.2 สคริปต์ด่วนเฉพาะจุด (Standalone Fast Utilities)

หากต้องการปรับปรุงเฉพาะส่วนโดยไม่ต้องรันทั้งไปป์ไลน์ใหม่:

```bash
# 🌊 สร้างและอัปเดตเส้นทางน้ำ Flow Paths (Hybrid OSM + D8) จบในไม่กี่วินาที
./venv/bin/python scripts/generate_flow_paths.py --basin yom --force

# 🗺️ สร้างความสัมพันธ์น้ำ-น้ำแบบเวกเตอร์แม่นยำสูง (OSM Pure Routing)
./venv/bin/python scripts/generate_osm_waterlevel_relations.py --basin yom

# 🤖 เทรนโมเดล Travel Time และวิเคราะห์ Flood Hydrograph
./venv/bin/python scripts/train_response_model.py --basin yom

# 🌧️ คำนวณและอัปเดตเกณฑ์ฝนเตือนภัย 4 กรอบเวลา (In-Place Update)
./venv/bin/python scripts/calculate_rainfall_thresholds.py --basin yom --update-existing

# 📦 ส่งออกไฟล์สำหรับ Frontend และ Database
./venv/bin/python scripts/export_backend_dataset.py --basin yom
```

---

### 3.3 การรวบรวมและ Clean ข้อมูล Time-Series

```bash
# รวมและ Harmonize ข้อมูลฝนและระดับน้ำเป็นไฟล์ 1 ชั่วโมงต่อเนื่อง (2025-01 ถึง 2026-07)
./venv/bin/python consolidate_basin_data.py --basin yom --dir ./dataset

# รันทุก 5 ลุ่มน้ำ
./venv/bin/python consolidate_basin_data.py --basin all
```

---

## 4. โครงสร้างโฟลเดอร์ผลลัพธ์ (Dataset & Terrain Structure)

```text
flood-analysis-model/
├── docs/                                           <-- [เอกสารทางเทคนิคและทฤษฎี]
│   ├── Technical-of-ML.md                          (สถาปัตยกรรม ML & อุทกวิทยา)
│   └── Technical-of-WaterFlow.md                   (สถาปัตยกรรมเส้นทางน้ำ & DEM)
├── ai-memory/                                      <-- [บันทึกประวัติการพัฒนาและ Plan]
├── dataset/                                        <-- [Station & Time-Series Data]
│   ├── summary-list-station.json
│   └── yom/
│       ├── station/
│       │   ├── yom_waterlevel_stations.json        (Metadata สถานีวัดน้ำพร้อม Bank MSL)
│       │   ├── yom_rainfall_stations.json          (Metadata สถานีวัดน้ำฝน)
│       │   ├── station-relations.json              (ความสัมพันธ์ต้นน้ำ-ท้ายน้ำ)
│       │   └── rainfall-relations.json             (ความสัมพันธ์ฝน-ระดับน้ำ)
│       ├── response/
│       │   ├── observed-response.json              (เวลาเดินทางจากการตรวจวัดจริง)
│       │   ├── estimated-response.json             (เวลาเดินทางจากการทำนายด้วย ML)
│       │   └── rainfall-thresholds.json            (เกณฑ์ฝนเตือนภัย 4 กรอบเวลา & สภาวะดิน)
│       ├── gis/
│       │   ├── yom_boundary.geojson                (ขอบเขตลุ่มน้ำ)
│       │   └── osm_waterways.geojson               (โครงข่ายแม่น้ำ OpenStreetMap)
│       ├── catchment/
│       │   └── catchments.geojson                  (รูปปิดพื้นที่รับน้ำย่อยรายสถานี)
│       └── processed/
│           ├── yom_hourly_waterlevel.csv           (Time-Series ระดับน้ำรายชั่วโมง)
│           ├── yom_hourly_rainfall.csv             (Time-Series น้ำฝนรายชั่วโมง)
│           ├── flow_paths.geojson                  (เวกเตอร์เส้นทางน้ำ 2 ระดับสำหรับแผนที่)
│           ├── relations_frontend.json             (Payload สำหรับ Frontend UI)
│           └── station_relations_db.json           (Payload สำหรับตาราง Database)
└── terrain/                                        <-- [Terrain & DEM Raster Grid]
    └── yom/
        ├── raw_dem.tif                             (โมเสก FABDEM 30m ดั้งเดิม)
        ├── conditioned_dem.tif                     (DEM หลังขุดร่อง 15m และแก้ Flats)
        ├── flow_direction.tif                      (D8 Flow Direction Raster)
        └── flow_accumulation.tif                   (Flow Accumulation Raster)
```

---

## 5. ความสัมพันธ์กับระบบส่วนอื่น (Integration with Backend & Frontend)

1. **Frontend (`flood-analysis-frontend`):**
   * ใช้ `dataset/{basin}/processed/flow_paths.geojson` แสดงผลเส้นทางน้ำบนแผนที่ Leaflet
   * ใช้ `dataset/{basin}/catchment/catchments.geojson` วาดขอบเขตพื้นที่รับน้ำ
   * ใช้ `dataset/{basin}/processed/relations_frontend.json` แสดงความสัมพันธ์ของสถานีต้นน้ำ-ท้ายน้ำ และเกณฑ์ฝนเตือนภัย
2. **Backend Database (PostgreSQL / PostGIS):**
   * นำเข้า `station_relations_db.json` ลงตาราง `station_relations` เพื่อให้ระบบแจ้งเตือน Real-time Query เวลาที่น้ำจะเดินทางมาถึง
