# Requirements — Water Flow & Response Time Model

## 1. วัตถุประสงค์

ระบบต้องสร้าง Model สำหรับอธิบายการไหลและการตอบสนองของน้ำภายในลุ่มน้ำ โดยใช้ข้อมูล:

* DEM
* River Network
* Water Level Station
* Historical Water Level เฉพาะสถานีที่มีข้อมูล
* Rainfall Station
* Historical Rainfall
* Basin / Catchment

Model ต้องสามารถหา:

1. เส้นทางการไหลของน้ำ
2. Upstream / Downstream Relationship
3. Catchment ของสถานี
4. River Distance
5. River Slope
6. Rainfall → Water Level Relationship
7. Rainfall Accumulation ก่อนเกิด Water Level Response
8. Observed Response Time ระหว่างสถานีที่มี Historical Water Level
9. Estimated Response Time สำหรับสถานีที่ไม่มี Historical Water Level
10. Confidence / Quality ของค่าที่ได้

> Model นี้เป็น **Hydrological / Empirical Response Model** ไม่ใช่ Hydraulic Simulation Model

---

# 2. Input Data

## 2.1 DEM

ใช้สำหรับสร้างข้อมูลภูมิประเทศและ Network

```text
DEM
├── CRS
├── Resolution
├── Elevation
├── NoData
└── Extent
```

ใช้สร้าง:

* Flow Direction
* Flow Accumulation
* Drainage Network
* Catchment
* Elevation Profile
* River Slope

---

## 2.2 Basin Boundary

```text
basin_id
geometry
```

Model ต้องรองรับหลาย Basin

```text
Basin A
Basin B
Basin C
...
```

---

## 2.3 River Network

```text
river_id
geometry
river_name
```

ต้องสร้างเป็น Directed River Network

```text
Upstream
   ↓
River Segment
   ↓
River Segment
   ↓
Downstream
```

---

## 2.4 Water Level Station

ข้อมูลสถานี:

```text
station_id
latitude
longitude
basin_id
type = water_level
```

Historical Water Level:

```text
station_id
timestamp
water_level
```

**ไม่กำหนดว่า Water Level Station ทุกสถานีต้องมี Historical Data**

ตัวอย่าง:

```text
Y001 → มี History
Y002 → มี History
Y003 → ไม่มี History
Y004 → มี History
Y005 → ไม่มี History
```

Model ต้องรองรับสถานีทั้งสองประเภท

---

## 2.5 Rainfall Station

```text
station_id
latitude
longitude
basin_id
type = rainfall
```

Historical Rainfall:

```text
station_id
timestamp
rainfall
```

ข้อมูลเป็นรายชั่วโมง

---

# 3. DEM Processing

Pipeline:

```text
DEM
 ↓
DEM Validation
 ↓
Hydrological Conditioning
 ↓
Flow Direction
 ↓
Flow Accumulation
 ↓
Drainage Network
 ↓
Catchment
```

---

# 4. Flow Direction

สร้าง Flow Direction จาก DEM

Algorithm เริ่มต้น:

```text
D8
```

Output:

```text
flow-direction.tif
```

ต้องเก็บ:

```text
algorithm
parameters
input_dem
```

---

# 5. Flow Accumulation

สร้างจาก Flow Direction

```text
Flow Direction
      ↓
Flow Accumulation
```

Output:

```text
flow-accumulation.tif
```

---

# 6. Drainage Network

ระบบต้องสามารถสร้าง Drainage Network จาก DEM ได้

```text
Flow Accumulation
        ↓
Stream Threshold
        ↓
Drainage Network
```

ต้องกำหนด:

```text
stream_threshold
```

และเก็บ Parameter ไว้ใน Model Metadata

Output:

```text
drainage-network.geojson
```

---

# 7. River Network Integration

ระบบต้องสามารถเปรียบเทียบ:

```text
DEM-derived Drainage Network
```

กับ:

```text
Existing River Network
```

เพื่อ:

* ตรวจสอบ Flow Direction
* ตรวจสอบ Connectivity
* ตรวจสอบ River Segment
* ตรวจสอบความผิดปกติของ Network

---

# 8. River Slope

ระบบต้องคำนวณ River Slope จาก DEM

Pipeline:

```text
River Network
      ↓
Sample DEM
      ↓
Elevation Profile
      ↓
River Slope
```

สูตร:

[
S = \frac{\Delta Z}{L}
]

ตัวอย่าง:

```text
Upstream Elevation   = 120 m
Downstream Elevation = 100 m
Distance             = 20 km

Slope = 0.001
```

หรือ:

```text
0.1%
```

---

# 9. River Segment Features

ทุก River Segment ต้องมีข้อมูล:

```text
segment_id
length_km
elevation_upstream
elevation_downstream
elevation_difference
slope
```

ข้อมูลเหล่านี้จะเป็น Spatial Features สำหรับ Response Time Model

---

# 10. Water Level Station Mapping

นำ Water Level Station ไป Snap กับ River Network

```text
Water Level Station
        ↓
Nearest River Segment
        ↓
Station → Segment
```

ต้องเก็บ:

```text
station_id
river_segment_id
snap_distance
mapping_quality
```

---

# 11. Catchment

ระบบต้องสร้าง Catchment จาก DEM

สำหรับ Water Level Station:

```text
Station
   ↓
Outlet
   ↓
Upstream Catchment
```

Output:

```text
catchment_id
station_id
catchment_area
geometry
```

---

# 12. Rainfall Station → Catchment

ต้องหา Rainfall Station ที่อยู่ในหรือเกี่ยวข้องกับ Catchment

```text
Rainfall Station
       ↓
Catchment
       ↓
Water Level Station
```

รองรับ:

```text
หลาย Rainfall Station
        ↓
หนึ่ง Catchment
```

---

# 13. Rainfall Aggregation

หาก Catchment มีหลาย Rainfall Station ต้องสามารถสร้าง Catchment Rainfall ได้

ตัวอย่าง:

```text
R001 ─┐
R002 ─┼── Catchment
R003 ─┘
```

สามารถใช้วิธี Spatial Weighting เช่น:

* Thiessen Polygon
* Area Weighting
* วิธีอื่นที่กำหนดใน Model Configuration

Output:

```text
catchment_rainfall
timestamp
rainfall
```

---

# 14. Rainfall Accumulation

จาก Historical Rainfall รายชั่วโมง ต้องคำนวณ Rainfall Accumulation

อย่างน้อยต้องรองรับ:

```text
1 hour
3 hours
6 hours
12 hours
24 hours
48 hours
72 hours
```

ตัวอย่าง:

```text
08:00 = 10 mm
09:00 = 20 mm
10:00 = 15 mm
11:00 = 10 mm
12:00 =  5 mm
```

6-hour accumulation:

```text
60 mm
```

---

# 15. Rainfall Event Detection

ระบบต้องสามารถแบ่ง Historical Rainfall เป็น Events

Event ต้องมี:

```text
event_id
start_time
end_time
duration
total_rainfall
peak_intensity
```

ต้องรองรับ Parameter:

```text
minimum_rainfall
dry_period
event_separation
```

---

# 16. Water Level Event Detection

Historical Water Level ที่มีต้องถูกวิเคราะห์เป็น Events

ระบบต้องหา:

* Start of Rise
* Peak
* End of Rise
* Rate of Change
* Event Duration

ต้องมี Threshold สำหรับกำหนดว่าเมื่อใดถือว่าเป็น **Significant Water Level Rise**

เพื่อป้องกัน noise ของข้อมูลรายชั่วโมง

---

# 17. Rainfall → Water Level Response

สำหรับคู่:

```text
Rainfall Station
        ↓
Water Level Station
```

ระบบต้องวิเคราะห์ Historical Data เพื่อหา:

```text
Rainfall Accumulation
Accumulation Duration
Response Lag
Correlation
Water Level Response
```

---

# 18. Rainfall Response Time

ตัวอย่าง:

```text
Rainfall
08:00
   │
   │ accumulation
   │ 5 hr
   ▼
13:00
   │
   │ response lag
   │ 2 hr
   ▼
Water Level starts rising
15:00
```

ต้องแยก:

```text
Accumulation Duration = 5 hr
Response Lag           = 2 hr
Total Rain-to-Stage    = 7 hr
```

---

# 19. Empirical Rainfall Threshold

ระบบต้องวิเคราะห์ว่า ก่อนระดับน้ำจะเริ่มเพิ่ม มีฝนสะสมประมาณเท่าใด

ตัวอย่าง Historical Events:

```text
Event 01 → 48 mm
Event 02 → 55 mm
Event 03 → 62 mm
Event 04 → 51 mm
Event 05 → 70 mm
```

สามารถสรุปเป็น:

```text
typical_accumulated_rainfall
```

พร้อม:

```text
min
median
max
percentile
event_count
```

ต้องเรียกเป็น **Empirical Rainfall Response Threshold** ไม่ใช่ Physical Threshold

---

# 20. Water Level → Water Level Relationship

สำหรับสถานีที่มี Historical Water Level ทั้งคู่:

```text
Y001 ─────────→ Y002
History         History
```

ระบบต้องวิเคราะห์:

```text
Y001 Water Level
       ↓
Time Lag
       ↓
Y002 Water Level
```

เพื่อหา **Observed Response Time**

---

# 21. Observed Response Time

ใช้ Historical Water Level โดยตรง เช่น:

```text
Event 01 → 4.1 hr
Event 02 → 4.5 hr
Event 03 → 3.9 hr
Event 04 → 4.3 hr
```

สรุป:

```text
min     = 3.9 hr
typical = 4.2 hr
max     = 4.5 hr
```

สามารถใช้:

* Cross-Correlation
* Event Matching
* Lag Analysis

---

# 22. Station Pair Classification

ทุกคู่ Station ต้องถูกแบ่งเป็น:

### Observed Pair

มี Historical Water Level ทั้งสองสถานี

```text
Y001 → Y002
History    History
```

สามารถหา:

```text
Observed Response Time
```

### Partially Observed Pair

มี Historical Water Level เพียงสถานีเดียว

```text
Y001 → Y003
History    No History
```

ไม่สามารถหา Observed Response Time โดยตรง

ต้องใช้ Estimated Model

### Unobserved Pair

ไม่มี Historical Water Level ทั้งสองสถานี

```text
Y003 → Y005
No History    No History
```

ต้องใช้ Estimated Model เช่นกัน

---

# 23. Estimated Response Time Model

สำหรับคู่ที่ไม่มี Historical Water Level ครบทั้งสองสถานี ต้องประมาณ Response Time จากคู่ที่มี Observed Response Time

Input Features:

```text
river_distance
river_slope
elevation_difference
catchment_area
river_position
upstream_distance
downstream_distance
```

Target:

```text
Observed Response Time
```

ตัวอย่าง:

```text
Observed Pairs
       │
       ▼
┌──────────────────────────┐
│ Response Time Dataset    │
│                          │
│ Distance                │
│ Slope                   │
│ Elevation Difference    │
│ Catchment Area          │
│ Observed Response Time  │
└────────────┬─────────────┘
             │
             ▼
      Estimated Model
             │
             ▼
       Missing Station Pair
```

---

# 24. Slope ใน Estimated Model

Slope ต้องเป็นหนึ่งใน Feature ของ Estimated Response Time

ตัวอย่าง:

```text
Distance = 30 km
Slope = 0.0012
       ↓
Observed Response
```

เทียบกับ:

```text
Distance = 30 km
Slope = 0.0004
       ↓
Observed Response
```

เพื่อให้ Model เรียนรู้ความสัมพันธ์ระหว่าง:

```text
Distance
+
Slope
+
Terrain
+
Catchment
→
Observed Response Time
```

**ไม่ใช้ Slope เพียงตัวเดียวในการคำนวณ Travel Time**

---

# 25. Response Time Model ต้องแยก Observed / Estimated

ทุก Relationship ต้องมี:

```text
response_type
```

ค่า:

```text
OBSERVED
ESTIMATED
```

ตัวอย่าง:

```json
{
  "from": "Y001",
  "to": "Y002",
  "response_time_hours": 4.2,
  "response_type": "OBSERVED"
}
```

หรือ:

```json
{
  "from": "Y003",
  "to": "Y004",
  "response_time_hours": 5.1,
  "response_type": "ESTIMATED"
}
```

---

# 26. Estimated Response Confidence

Estimated Response ต้องมี Confidence

เช่น:

```text
HIGH
MEDIUM
LOW
```

โดยพิจารณาจาก:

* จำนวน Observed Station Pairs
* ระยะห่างจากพื้นที่ที่มี Observed Data
* Similarity ของ Slope
* Similarity ของ Catchment
* Model Error
* Training Data Coverage

---

# 27. Response Time Range

ไม่ควรเก็บเพียงค่าเดียว

ต้องรองรับ:

```text
min_hours
typical_hours
max_hours
```

สำหรับ Observed Data สามารถคำนวณจาก Historical Events

สำหรับ Estimated Data สามารถสร้าง Prediction Interval จาก Model

---

# 28. River Network Response

Model ต้องสามารถสร้าง Relationship:

```text
Y001
 ↓
Y002
 ↓
Y003
 ↓
Y004
```

แต่ละ Edge ต้องมี:

```text
from_station
to_station
river_distance
river_slope
elevation_difference
response_time
response_type
confidence
```

---

# 29. Multi-Segment Travel

หากไม่มีสถานีระดับน้ำระหว่างต้นน้ำและปลายน้ำ:

```text
Y001
 │
 ├── Segment A
 ├── Segment B
 ├── Segment C
 │
 ▼
Y005
```

ต้องสามารถคำนวณ/ประมาณ Response Time ของทั้งเส้นทางจาก Segment-Level Information

```text
Total Response
=
Segment A
+
Segment B
+
Segment C
```

โดยค่าที่เป็น Observed และ Estimated ต้องยังคงถูกแยกประเภท

---

# 30. Historical Validation

คู่สถานีที่มี Historical Data ต้องใช้เป็น Ground Truth

ตัวอย่าง:

```text
Observed:
Y001 → Y002 = 4.2 hr

Estimated Model:
Y001 → Y002 = 4.5 hr

Error:
0.3 hr
```

ต้องคำนวณ Model Performance เช่น:

```text
MAE
RMSE
Bias
```

และใช้เพื่อประเมินว่า Estimated Model ใช้งานได้ดีเพียงใด

---

# 31. Model Dataset

โครงสร้าง Output:

```text
water-flow/
└── {basin-name}/
    └── {model-version}/

        metadata.json

        terrain/
        ├── conditioned-dem.tif
        ├── flow-direction.tif
        ├── flow-accumulation.tif
        └── drainage-network.geojson

        river/
        ├── river-network.geojson
        ├── river-segments.json
        └── river-slope.json

        catchment/
        └── catchments.geojson

        station/
        ├── station-mapping.json
        ├── station-relations.json
        └── rainfall-relations.json

        response/
        ├── rainfall-response.json
        ├── observed-response.json
        ├── estimated-response.json
        └── validation.json
```

---

# 32. Model Metadata

ต้องบันทึก:

```json
{
  "model_version": "v1",
  "basin_id": "yom",

  "dem": {
    "source": "...",
    "resolution": "...",
    "crs": "..."
  },

  "flow_direction": {
    "algorithm": "D8"
  },

  "drainage_network": {
    "threshold": 1000
  },

  "response_model": {
    "method": "...",
    "training_station_pairs": 24
  }
}
```

---

# 33. Model Processing Pipeline

```text
                         DEM
                          │
                          ▼
                Hydrological Processing
                          │
          ┌───────────────┼────────────────┐
          ▼               ▼                ▼
    Flow Network       Catchment       Elevation
          │                                │
          │                                ▼
          │                           River Slope
          │                                │
          └───────────────┬────────────────┘
                          ▼
                   Station Mapping
                          │
            ┌─────────────┴─────────────┐
            ▼                           ▼
      Rainfall Station            Water Station
            │                           │
            ▼                           ▼
    Historical Rainfall        Historical Water Level
            │                           │
            ▼                           ▼
    Rainfall Events             Water Level Events
            │                           │
            └─────────────┬─────────────┘
                          ▼
                   Response Analysis
                          │
              ┌───────────┴───────────┐
              ▼                       ▼
        Rain → Stage            Stage → Stage
              │                       │
              ▼                       ▼
      Rainfall Threshold       Observed Response
      Response Time                   │
                                      ▼
                             Observed Dataset
                                      │
                                      ▼
                             Estimated Model
                                      │
                                      ▼
                          Missing Station Pairs
```

---

# 34. สิ่งที่ Model นี้ไม่ใช้

เนื่องจากไม่มีข้อมูล Hydraulic Model จึง **ไม่ต้องมี**:

* Discharge `m³/s`
* Velocity `m/s`
* Cross-section
* Cross-sectional Area
* Hydraulic Radius
* Manning's Roughness
* Hydraulic Simulation

แต่ **Slope จาก DEM ใช้ได้และควรใช้** เป็น Spatial Feature ในการวิเคราะห์/ประมาณ Response Time

---

# 35. ผลลัพธ์ที่ต้องการ

สุดท้าย Model ต้องสามารถตอบได้สองระดับ

### ระดับที่ 1 — Historical Observed

> **สถานี Y001 → Y002 จากข้อมูลย้อนหลัง น้ำมี Response Time ประมาณ 4.2 ชั่วโมง**

พร้อมหลักฐาน:

```text
42 Historical Events
Correlation = 0.87
Typical = 4.2 hr
```

### ระดับที่ 2 — Estimated

> **สถานี Y003 → Y004 ไม่มี Historical Water Level ครบทั้งคู่ จึงประมาณ Response Time จาก Spatial Features และคู่สถานีที่มีข้อมูลจริง ได้ประมาณ 5.1 ชั่วโมง**

พร้อม:

```text
response_type = ESTIMATED
confidence = MEDIUM
```

แบบนี้จะเหมาะกับสถานการณ์ของแม่น้ำยมมากกว่า เพราะ **ไม่ทิ้งสถานีที่ไม่มี Historical Water Level** แต่ก็ไม่ทำเหมือนว่าค่าที่ประมาณขึ้นมามีความน่าเชื่อถือเท่ากับค่าที่วัดจาก Historical Data จริง ๆ.
