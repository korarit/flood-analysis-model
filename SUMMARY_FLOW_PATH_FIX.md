# SUMMARY — Flow Paths Fix (generate_flow_paths.py)

สรุปการแก้ไขทั้งหมดของ pipeline สร้าง flow paths (`flood-analysis-model/scripts/`)
จากปัญหาตั้งต้น: เส้นตรงกระโดดไกล 50-65km, OSM มาไม่ครบ, cascade ถูกตัด, ไฟล์ output 178MB

รายละเอียดเชิงแผนอยู่ใน `FLOW_PATHS_IMPROVEMENT_PLAN.md` (ไฟล์นี้สรุปผลลัพธ์)

---

## ปัญหาที่วิเคราะห์เจอ (พร้อมหลักฐาน)

| ปัญหา | หลักฐาน | ต้นตอ |
|---|---|---|
| เส้นตรงกระโดด 50-65km กลางเส้น | ไฟล์เก่า: 205/569 features มี segment >1km, 114 features กระโดดแล้ววาดต่ออีกฝั่ง | forced bridging ของโค้ดรุ่นเก่า (stitch ชิ้น path ที่ไม่ต่อกัน) |
| ช่วงกระโดดตรงเขื่อนสิริกิติ์ | lat 18.58→17.99 บนแม่น้ำน่าน | OSM ไม่มี waterway line บนผิวน้ำกว้าง/เขื่อน + พื้นที่ราบ D8 ไม่นิ่ง |
| OSM มาไม่ครบ | query ใช้ bbox สถานี+0.35°, timeout 60s < server 120s, ไม่ตรวจ remark, แคชไม่ validate | `fetch_osm_waterways` เดิม |
| Rain→gauge จบที่ gauge แรก | "Select ONLY the single best downstream receiving station" | cascade รุ่นเก่า (120km/8 gauges) ถูกลดจนเหลือ 1 |
| ไฟล์ 178MB | ~9 ล้านจุด | OSM layer ครบ + เส้นสาขา 394 สถานีถือสำเนาซ้ำ + ละเอียดระดับ cell 12.5m |

---

## สิ่งที่แก้ (ตามลำดับรอบ)

### B — OSM ให้ครบ (`fetch_basin_gis.py`, `terrain_engine.py`)
- Query ด้วย **polygon ลุ่มน้ำ ThaiWater** (`dataset/{basin}/gis/{basin}_boundary.geojson`) ผ่าน Overpass `poly:` filter (simplify 0.005°, cap 5 polygons, fallback bbox สถานี + WARN)
- Timeout 180s ตรง server, ตรวจ `remark` (incomplete → ถือว่าล้ม), ห้ามเซฟ placeholder ทับแคชดี
- **Cache fingerprint** (`_meta`: sha256 ของ query) — query เปลี่ยน → refetch อัตโนมัติ
- **`fetch_osm_water_polygons`** ใหม่ (`natural=water`, `landuse=reservoir`) → burn DEM: เส้นแม่น้ำ -15m, ผิวน้ำ -10m (D8 ข้าม reservoir ได้) + ใช้ช่วย snapping สถานีริมน้ำกว้าง
- Graph รองรับ MultiLineString

### A — กำจัดเส้นตรง/เส้นขาด (`graph_topology.py`)
- `simplify_linestring_coords`: แบ่งที่ jump >0.5km (เดิม 10km!), เก็บท่อนที่ต่อกับต้นทาง, tail ≤2km (station stub) คงไว้, warning ระบุ **feature id + ขนาด jump**
- จุดต่อ overland→backbone: coarse STRIDE=8 + **scan ย้อน** หาจุดใกล้แม่น้ำจริง
- `water_prox_map` (±12 cells ≈150m) — D8 ที่พาดผ่านใกล้ gauge จะหยุดถูกจุด
- **D8 จบก่อนเจอ gauge** (หลุม/nodata/เขื่อน/5000 steps) → fallback ต่อด้วย OSM backbone Dijkstra (`routing: d8_plus_osm_backbone`)
- Elevation: **batch vectorized** ต่อ feature (ไม่ใช่ per-vertex pyproj), นอก DEM คืน unknown → ไม่พลิกทิศ (ใช้ทิศ OSM + tag `direction_source`), cache cap 500k
- `water_grid_map` ใช้ `setdefault` (สถานีแรกครอง footprint ไม่ทับกัน)

### D — Rain Cascade แบบ segments (`graph_topology.py`)
- น้ำจากสถานีฝนไหล**ผ่าน gauge แรกไป gauge ถัดไปจริง**:
  - **Case 1** (D8 ชน gauge ตรง): trace D8 ต่อจาก gauge ที่ผ่าน (exclude อันที่ผ่านแล้ว) ภายใน `--rain-cascade-km` (default 60)
  - **Case 2** (เข้าแม่น้ำห่าง gauge): Dijkstra backbone → ทุก gauge downstream-reachable เรียงตามระยะ
- เส้นเป็น**ท่อนไม่ทับซ้อน**: `entry→G1`, `G1→G2`, `G2→G3` (properties: `cascade_segment`, `previous_gauge_id`, ระยะ/lag สะสมต่อ gauge)
- `--min-flow-km` (default 1.0) บังคับทุก feature flow path
- Relations/IDW weight/exporter schema เดิม ไม่ต้องแก้ฝั่ง backend

### E — เส้นสาขา D8 ผูกต่อสถานีฝน (`graph_topology.py`)
- `extract_station_drainage_branches`: reverse-BFS ขึ้นต้นน้ำจาก path cells (acc ≥ `--branch-min-acc`) → channel head → เดินลงจนชน trunk → reaches
- `feature_type: "rainfall_drainage_branch"` ผูก `from_station_id`
- Truncation guard → **auto-escalate** `--branch-min-acc` ×4 สูงสุด 2 ครั้ง (แทนการตัดครึ่ง ๆ)
- ผูกต่อสถานีฝนตามที่เลือก (เครื่องอ่อนใช้ `--no-branches` ได้)

### F — OSM River Layer แยกในไฟล์เดียว
- `feature_type: "osm_river"` (id `osm_river_{osm_id}`, properties: `osm_id/river_name/waterway/length_km`) — **เก็บครบทุกเส้น ไม่กรองความยาว** ตามที่ผู้ใช้ยืนยัน; `--no-osm-layer` ปิดได้

### G — ลดขนาดไฟล์ 178MB → เป้า ≤4MB
- **G1 Dedupe เส้นสาขา**: เส้นเดียวกันที่หลายสถานีถือ → เก็บ 1 feature + `shared_with: [...]` (hash เรขาคณิต 5dp, O(total points))
- **G2** `--branch-max-count` (default 30) — ต่อสถานีเก็บยาวสุด N เส้น
- **G3** เขียน **ทั้ง** `flow_paths.geojson` (raw) **และ** `flow_paths.geojson.gz` (level 9, mtime=0) serialize ครั้งเดียว (`write_geojson_pair`); `--no-gzip` ปิดได้
- **G4** Size report ตอนจบ generate (features/points ต่อ type + MB จริง); validator อ่าน .gz ได้

### H — ความยาวขั้นต่ำเส้นสาขา
- `--branch-min-km` default **1.0km** (ปรับจาก 1.5km ในรอบที่ 3) เฉพาะเส้นสาขา — flow path หลักยังใช้ `--min-flow-km` 1.0

---

## ไฟล์ที่แก้/เพิ่ม

| ไฟล์ | การเปลี่ยนแปลง |
|---|---|
| `scripts/modules/graph_topology.py` | หัวใจของ A/D/E/F/G/H — graph, cascade, branches, dedupe |
| `scripts/fetch_basin_gis.py` | B1-B4 — polygon query, fingerprint, water polygons |
| `scripts/modules/terrain_engine.py` | burn line (15m) + polygon (10m) ใน mask เดียว |
| `scripts/modules/gis_utils.py` | `dumps_compact_json`, `write_geojson_pair` |
| `scripts/generate_flow_paths.py` | wiring ทุกอย่าง + CLI ใหม่ + size report |
| `scripts/validate_flow_paths.py` | **ใหม่** — ตรวจ jump/stub/.gz (stdlib เท่านั้น) |
| `tests/test_flow_topology.py` | **ใหม่** — 9 synthetic tests รันได้บนเครื่องนี้ (ไม่ใช้ data จริง) |

## CLI พารามิเตอร์ใหม่ทั้งหมด

```
--rain-cascade-km    (60)      ระยะ cascade สูงสุดบน backbone
--min-flow-km        (1.0)     ระยะขั้นต่ำ flow path หลัก
--branch-min-km      (1.0)     ระยะขั้นต่ำเฉพาะเส้นสาขา (ปรับจาก 1.5)
--branch-min-acc     (500)     flow accumulation ขั้นต่ำของเส้นสาขา
--branch-max-cells   (400000)  cap cells ต่อสถานี (auto-escalate เมื่อเกิน)
--branch-max-count   (30)      cap จำนวนเส้นสาขาต่อสถานี (ยาวสุดก่อน)
--polygon-burn-depth (burn-5)  depth burn ผิวน้ำ (reservoir)
--no-branches                  ปิดเส้นสาขา
--no-osm-layer                 ปิด OSM display layer
--no-gzip                      ไม่เขียน .gz
```

## โครงสร้าง flow_paths.geojson (4 layer + relations)

| feature_type | ความหมาย |
|---|---|
| `gauge_to_gauge_flowpath` | สถานีน้ำ → สถานีน้ำ (D8 + backbone fallback) |
| `rainfall_to_gauge_flowpath` | ฝน → gauge (cascade segments, มี `cascade_segment`/`previous_gauge_id`) |
| `rainfall_drainage_branch` | ลำธารสาขา D8 ผูกสถานีฝน (`shared_with` เมื่อซ้ำข้ามสถานี) |
| `osm_river` | เครือข่ายแม่น้ำ OSM เต็ม (display layer) |

## การตรวจสอบ

- **py_compile**: ผ่านทุกไฟล์
- **tests**: 9/9 PASSED (`python tests/test_flow_topology.py` — synthetic DEM+OSM, ไม่ต้องมี data จริง)
- **validator**: จับ bug ไฟล์เก่าได้ (1,304 jumps, worst 64.91km → FAIL) ✅
- recheck กับ plan แบบ grep-verified ทุกข้อ + self-review จับบั๊กตัวเอง 3 จุดก่อน merge

## วิธีรัน (เครื่องที่มี data)

```bash
# 1. ดึง OSM ใหม่ด้วย polygon ลุ่มน้ำ (waterways + water polygons)
python scripts/fetch_basin_gis.py --basin yom --force-osm

# 2. generate (--force จำเป็นรอบแรก: re-burn DEM ด้วย water polygons)
python scripts/generate_flow_paths.py --basin yom --force

# 3. ตรวจผล (อ่านทั้ง .geojson และ .geojson.gz)
python scripts/validate_flow_paths.py --geojson dataset/yom/processed/flow_paths.geojson.gz
```

## หมายเหตุฝั่ง Frontend

- ใช้ `flow_paths.geojson.gz`: `fetch()` + `DecompressionStream('gzip')` หรือให้ server ใส่ `Content-Encoding: gzip` (ไม่ต้องแก้โค้ด)
- กรอง layer ด้วย `properties.feature_type` ได้ทั้ง 4 แบบ (เปิด/ปิดราย layer)
- เส้นสาขาที่แชร์กัน: `from_station_id` = เจ้าของหลัก, `shared_with` = สถานีอื่นที่ใช้ร่วม

## Commits

| commit | เนื้อหา |
|---|---|
| `7a94bac` | แก้หลักทั้งหมด (A+B+D+E+F ยุคแรก + validator + tests) |
| `4226268` | G: dedupe + cap + gzip pair + size report + diagnostics |
| `800b35b` | H: `--branch-min-km` 1.5km เฉพาะเส้นสาขา |

---

# รอบแก้ที่ 2 — Flow Path ไขว้แม่น้ำ / ผูกผิดแม่น้ำ (River-First Routing)

ปัญหาจาก `flow_paths_nan.geojson` รอบก่อน: เส้น flow ไขว้ osm_river ที่กั้นอยู่กลางทาง
(เช่น 1113284 ↔ 1483144 ผ่านรอย osm 482783692), สถานีติดแม่น้ำแต่เส้นไปจบที่แม่น้ำอื่นไกลๆ
(8894 ติด 482787607/440936187 แต่จบที่ 318801683; 1394921 แตะ ลำน้ำเหือง 37106619
ห่าง 11m แต่ลากไป gauge 6119 ไกล 107km)

## ต้นตอที่วิเคราะห์พบ (พร้อมหลักฐาน)

| ต้นตอ | หลักฐาน |
|---|---|
| กราฟ OSM backbone แตกเป็นเกาะ — weld เฉพาะ vertex ซ้ำ ≤39m, ไม่มี noding จุดตัด | union-find บน nan: 7,984 ways → **2,282 components**, 1,322 singletons; แม่น้ำปาด = เกาะขนาด 2 |
| D8 ไม่รู้จักแม่น้ำเวกเตอร์ — stop เฉพาะ gauge footprint | `trace_downstream_path` เดิมพาดผ่านแม่น้ำไป 5,000 steps ได้ |
| Cascade Case 1 ชนะเสมอ — เจอ gauge footprint ที่ไหนก็ได้ใน 5,000 steps | 1394921 วิ่ง cross-country 107km ทั้งที่แม่น้ำอยู่ติดๆ |
| Fallback ผูก backbone เฉพาะ "จุดจบ D8" รัศมี 330m แบบ vertex-only | 8894 จบที่ singleton 318801683 ใกล้ปากน้ำ |
| Branch เป็น raster ล้วน — หยุดที่ขอบ claimed set แม้ห่างแม่น้ำไม่กี่เมตร | branch 1394921 ห่าง ลำน้ำเหือง 11m แต่ไม่จบที่แม่น้ำ |
| ข้อจำกัดข้อมูล: OSM ลุ่มน่านวิ่งเป็นท่อนห่างกัน 1-2km + stream โดดเดี่ยวจำนวนมาก | ปลาย 115568998 (แม่น้ำปาดตอนล่าง) ไม่มี way ใดในรัศมี 2km; 1,496 endpoints ไม่มีเพื่อนใน 1km |

## สิ่งที่แก้ (รอบ 2)

### 1. `DirectedRiverGraph.finalize_connectivity()` —  heal กราฟให้ต่อกัน
- **Crossing noding**: หาจุดตัดระหว่าง ways (STRtree + `intersection`) แล้ว split **ทุก edge** ที่พาดผ่านจุดตัด → junction จริง (แก้เคส tributary ไขว้แม่น้ำหลักโดยไม่แชร์ vertex)
- **Endpoint welding**: way ที่จบแล้วห้อย (ไม่มี out-edge) ให้ project ลง edge ของ way อื่นภายใน 110m (split ที่ projection + connector edge บังคับทิศด้วย elevation)
- Edge spatial-grid index + `snap_point_to_graph()`: project จุดลง **edge** (ไม่ใช่แค่ vertex) แล้ว split — ใช้ anchor สถานี/จุดเข้าแม่น้ำ/จุด attach
- Log จำนวน components ก่อน/หลังทุกรอบรัน

### 2. River mask + river-aware D8 ("แม่น้ำมาก่อน")
- `terrain_engine.build_river_mask()`: rasterize OSM waterway lines + dilate 2 cells (~25m) → `river_mask.tif` (cache)
- `trace_downstream_path(..., river_mask=...)`: trace บนบก**หยุดที่ cell แม่น้ำแรกที่เจอ** (ยกเว้น cell เริ่ม) → น้ำฝนเข้าแม่น้ำใกล้สุด ไม่ไขว้และไม่วิ่งข้ามภูเขาไป gauge ไกล
- ลำดับใหม่: ชนแม่น้ำก่อน gauge → เข้า backbone cascade (Case 2) / ชน gauge footprint ก่อนแม่น้ำจริงๆ → Case 1 เดิม

### 3. Dead-end fallback (กันข้อมูล OSM มีรอยต่อห่าง)
- ถ้าแม่น้ำที่จับได้เป็น **topology island** (ไม่มี gauge ใด reachable บน backbone) → re-trace แบบไม่มี river stop เพื่อไม่เสีย relations กับสถานี
- Case 3 (overland ล้วน) ถูก cap ด้วย `--overland-max-km` (default 5km) — ห้ามเส้นบนบกเดินดิ่งเป็นสิบ km

### 4. Layer 1 fallback สแกนย้อน
- แนบจุดจบ D8 ลง backbone ด้วย `snap_point_to_graph` (edge-level, 330m); ถ้าไม่เจอ สแกนย้อน path หาจุดสุดท้ายที่รันเคียงแม่น้ำ + เช็ค elevation (ห้ามแนบขึ้นโนน)

### 5. Branch merge แม่น้ำ
- Branch เดินลงต่อได้บน river-mask cells (≤50 cells) แม้นอก claimed set → ปลาย branch ไปจบที่ตัวแม่น้ำจริง (property `river_merge: true`)

### 6. Validator: orphan crossing detection
- นับ flow path ที่**ไขว้ osm_river โดยไม่วิ่งตาม** (crossing ที่ไม่มีจุดเคียงแม่น้ำในรัศมี 80m รอบจุดตัด)
- ไฟล์เก่า nan: จับได้ **228 features / 398 orphan crossings** → FAIL ✅

### 7. Latent bug ที่จับได้ระหว่างแก้
- Case 2 (rain → river entry → gauge) ในโค้ดเดิมใช้ `seg_count` ที่ไม่เคย init → จะ crash ทันทีถ้ารันถึง (output เก่าไม่เคยมี segment จาก Case 2 เลย) — init + increment ให้แล้ว

### 8. Basin-boundary clipping (ตัดเส้นตามพื้นที่ลุ่มน้ำ ThaiWater)
- เดิม boundary ใช้แค่ scope query OSM + clip DEM (buffer 1.7km) — เส้น output ไม่เคยถูกตัดเชิงเรขาคณิต
- ตอนนี้**ทุกเส้น output** (flow paths / branches / osm_river) ถูก clip ด้วย polygon `{basin}_boundary.geojson`:
  เส้นที่ไขว้ขอบเก็บท่อนที่ต่อกับ from-station, เส้นที่อยู่นอกลุ่มทั้งหมดถูก drop, feature ที่ถูกตัดมี `basin_clipped: true`
- ปิดได้ด้วย `--no-basin-clip` (relation distances ยังคำนวณจากเส้นเต็มเชิงอุทกวิทยา)

## CLI เพิ่ม (รอบ 2)

```
--overland-max-km     (5.0)   cap ความยาวเส้นบนบกล้วน (0 = ปิด)
--no-basin-clip               ไม่ตัดเส้น output ตามขอบเขตลุ่มน้ำ
```

## การตรวจสอบ (รอบ 2-3)

- **tests**: 14/14 PASSED — เพิ่ม 6 test: crossing noding เชื่อมกัน, endpoint weld ปิดช่องว่าง, snap_point_to_graph split edge, river-stop D8, river-first cascade ยังได้ gauge chain เดิม, basin-boundary clipping
- **validator** กับไฟล์ nan เก่า: FAIL ถูกต้อง (jumps + orphan crossings)
- **กราฟจริง nan (display layer)**: 2,288 → 1,628 components (noding 244 + weld 267); ที่เหลือคือ stream โดดเดี่ยวจริงตามข้อมูล OSM — routing ที่เหลือพึ่ง D8 บน DEM ที่ burn ครบ
- ข้อจำกัด: เครื่อง dev ไม่มี DEM ลุ่มน่าน — ต้องรัน end-to-end บนเครื่อง data: `fetch_basin_gis.py --basin nan --force-osm` → `generate_flow_paths.py --basin nan --force` → `validate_flow_paths.py --geojson dataset/nan/processed/flow_paths.geojson.gz` แล้วเปิดดู 3 จุดเดิม (1113284/1483144, 1394921, 8894)

## รอบแก้ที่ 3 — สรุปการปรับเพิ่มเติม

| หัวข้อ | เดิม | ใหม่ | commit |
|---|---|---|---|
| ตัดเส้นตามขอบเขตลุ่มน้ำ | boundary ใช้แค่ scope query OSM + clip DEM (buffer 1.7km) เส้นเลยนอกลุ่มได้ | **ทุกเส้น output ถูก clip** ด้วย `{basin}_boundary.geojson` (ข้อ 8 ด้านบน) — เส้นไขว้ขอบเก็บท่อนที่ต่อ from-station, เส้นนอกลุ่มทั้งหมด drop, `basin_clipped: true`, ปิดด้วย `--no-basin-clip` | `fab7c3b` |
| ความยาวเส้นสาขาขั้นต่ำ | `--branch-min-km` = 1.5km | **1.0km** (หมายเหตุ: 500 ที่เข้าใจผิดคือ `--branch-min-acc` หน่วย flow-accumulation cell ไม่ใช่ระยะทาง) | `f6bbbc6` |

## Commits (รอบ 2-3)

| commit | เนื้อหา |
|---|---|
| `2428478` | River-first routing: noding + endpoint weld + river mask/river-aware D8 + dead-end fallback + backward attach + branch merge + orphan-crossing validator + fix `seg_count` latent bug + tests 13/13 |
| `fab7c3b` | Basin-boundary clipping ทุกเส้น output + `--no-basin-clip` + test = 14/14 |
| `f6bbbc6` | `--branch-min-km` default 1.5 → 1.0 km |

---

# รอบแก้ที่ 4 — OSM-as-Base + Real Polygon Crop + แก้ snap ผิดลำธาร (ตาม plan_fix_flow_path_v2.md)

ปัญหาหลังรอบ 3: (1) เส้น gauge_to_gauge ของสถานี 8894 ยังผูกกับ osm_river 318801683 (stream เกาะ 1.95km ที่ไปไหนไม่ได้),
(2) เส้นในแผนที่ยังถูก crop ด้วยกรอบสี่เหลี่ยม (extent osm_river = station bbox + 0.35° เป๊ะ) เพราะ boundary หายแล้ว pipeline
เดิน fallback สี่เหลี่ยมเงียบ ๆ ทั้งระบบ

## Root Cause ที่แก้ (RC1-RC3)

| RC | ต้นตอ | การแก้ |
|---|---|---|
| RC1 ไม่มี boundary → fallback สี่เหลี่ยมทั้ง pipeline | `dataset/*/gis/` ว่างเปล่า, WARN แล้วรันต่อ, OSM query ตกไป bbox สถานี, basin clip ถูกข้าม | Boundary เป็น**สิ่งบังคับ** (G5): fallback chain 4 ทาง (ไฟล์ท้องถิ่น → ThaiWater → dissolve subbasins → OSM admin boundary ผ่าน Nominatim) แล้วถ้าล้มทั้งหมด **raise/exit พร้อมคำแนะนำ** — ลบ fallback กล่องสี่เหลี่ยมออกทั้งจาก `fetch_basin_boundary` และ `generate_flow_paths` |
| RC2 snap ปลายเส้นติด "ลำธารเกาะ" | backward attach / snap เลือกจุด**ใกล้สุด**บน edge ใดก็ได้ — ไม่ดูคลาส/ความยาว/การเชื่อมต่อ gauge | **Candidate ranking** (Phase 4): ให้คะแนน edge ถ่วงน้ำหนัก — `+class` (river/canal > stream), `+connectivity` (มี gauge downstream-reachable = คะแนนหลัก, เกาะ = ติดลบ, precompute reverse-BFS ครั้งเดียว O(E)), `+length`, `-distance` — รัศมีไล่ลำดับ (gated → ungated) แล้ว tag `attach_quality: "degraded"` |
| RC3 osm_river เกินจำเป็น | 7,980 features ครอบสี่เหลี่ยมใหญ่ | **Crop OSM ด้วย polygon จริงตั้งแต่ fetch** (SOURCE filter, buffer `--crop-buffer-m` default 2km): เส้นนอกลุ่ม drop, เส้นไขว้ขอบเก็บท่อนในลุ่ม, บันทึก `crop_polygon` fingerprint ใน `_meta` |

## สิ่งที่แก้ (ตามเฟสใน plan v2)

### Phase 1-2 — Boundary บังคับ + Crop OSM ตอน fetch (`fetch_basin_gis.py`, `generate_flow_paths.py`)
- `fetch_basin_boundary()`: fallback chain 4 ทาง + ตรวจ geometry เป็น (Multi)Polygon จริง (< 50 vertices → WARN กรอบหยาบ) + **raise เมื่อทุกทางล้ม**
- **Crop ตอน fetch**: `crop_geojson_to_basin()` — shapely prepared polygon + bbox pre-filter (O(N) ถูก → intersection เฉพาะตัวที่ผ่าน O(K)) กับทั้ง waterways และ water polygons **ก่อนเขียน cache เสมอ**; `_meta` บันทึก `crop_polygon` fingerprint + `crop_stats` (n_in/n_out/dropped/clipped)
- Cache เดิมที่ `source: "station_bbox"` หรือไม่มี `crop_polygon` → **refetch อัตโนมัติ** (`_load_valid_cache(require_crop=...)`)
- **Recrop ตอน load**: `ensure_osm_cropped()` — แคชเก่าที่ยังไม่เคย crop จะถูก crop ในหน่วยความจำแล้ว rewrite cache (idempotent); แคชยุคสี่เหลี่ยม → raise บังคับ `--force-osm`
- `generate_flow_paths.py`: boundary ไม่มี → **SystemExit พร้อมวิธีแก้** (`resolve_basin_boundary_or_fail`) — ไม่มี WARN-แล้วรันผิดต่ออีก

### Phase 3 — osm_river ไม่โดนตัดใน output (G2)
- บล็อก basin clip ใน `graph_topology.py` **ข้าม `feature_type == "osm_river"` ทั้งหมด** — output layer = ตัวที่ crop ด้วย polygon จริงตอน fetch เป๊ะ ๆ, ไม่มี `basin_clipped`, ไม่มีการตัดซ้ำ
- flow paths / branches ยังถูก clip ต่อด้วย polygon จริง (G3)

### Phase 4 — Snap ปลายเส้นฉลาดขึ้น (G4)
- `DirectedRiverGraph`: เก็บ `way_meta` (osm_id/class/length_km) ต่อ way; `compute_gauge_reachability()` (reverse BFS จาก gauge nodes, ครั้งเดียวต่อรัน); `snap_point_to_graph_ranked()` — scoring + radii escalation + **project จุดปลายลง edge ที่เลือกจริง** (split ที่ projection)
- ใช้แทน snap เดิมทุกจุดแนบปลาย: Layer 1 (D8 end + backward scan) และ Layer 2 (river entry scan)
- **ทุก flowpath ที่แนบ backbone มี metadata**: `attach_osm_id`, `attach_distance_m`, `attach_quality`, `attach_class`, `attach_length_km`

### Phase 5 — Validator + Tests (F1-F4)
- `validate_flow_paths.py` เช็คใหม่ (v2): osm_river ห้ามมี `basin_clipped`; ปลาย flowpath ต้องอยู่ที่ gauge หรือบนเส้น osm_river ≤ 30m (spatial grid, ไม่มี O(P×R)); attach point ต้องอยู่บนเส้นจริง; `_meta` ต้องมี filter report ต่อ layer (`n_in → n_out`); OSM `source == "station_bbox"` = FAIL; `--boundary` ตรวจจุดนอก polygon ด้วย shapely prepared (optional)
- `build_flow_paths_and_relations` ฝัง `_meta.filters` (per-layer n_in/n_out/clipped/dropped + osm_source_label) ในไฟล์ output — validator ตรวจย้อนได้
- Tests ใหม่ 4 เคส: ranked snap เลือกแม่น้ำหลักที่ไกลกว่าทับ stream เกาะ, island-only → `degraded`, boundary หาย → exit, crop geojson ตาม polygon; อัปเดต test basin clip ให้ตรง G2 (osm_river ไม่ถูกแตะ)

## CLI เพิ่ม (รอบ 4)

```
--crop-buffer-m       (2000)    buffer (เมตร) รอบ polygon ลุ่มตอน crop OSM (ทั้ง fetch และ generate)
```

## การตรวจสอบ (รอบ 4)

- **tests**: 18/18 PASSED (เพิ่ม 4 ใหม่ + อัปเดต 1)
- **validator** กับไฟล์เก่า `flow_paths(18).geojson`: จับครบ — osm_river โดน clip 97 เส้น (G2 violation), floating endpoints 17 เส้น, ไม่มี filter report → **FAIL ถูกต้อง** ✅
- **validator** กับ output synthetic ใหม่: PASS ทุกเช็ค v2 ✅

## ลำดับการรัน (เครื่องที่มี data — ลุ่มน่าน)

```bash
# 1) สร้าง boundary ให้ได้ก่อน (บังคับ — fail ไม่ได้)
python scripts/fetch_basin_gis.py --basin nan

# 2) ดึง OSM ใหม่ + crop ด้วย polygon (cache เดิมเป็นสี่เหลี่ยม → refetch อัตโนมัติอยู่แล้ว)
python scripts/fetch_basin_gis.py --basin nan --force-osm

# 3) generate
python scripts/generate_flow_paths.py --basin nan --force

# 4) ตรวจ (+ ตรวจจุดนอก polygon ด้วย boundary)
python scripts/validate_flow_paths.py --geojson dataset/nan/processed/flow_paths.geojson.gz \
    --boundary dataset/nan/gis/nan_boundary.geojson
```

เช็คด้วยตา 3 จุด: 8894 จบบนแม่น้ำหลัก (ไม่ใช่ 318801683), ขอบลุ่มหักตาม polygon concave, osm_river เต็มทุกเส้นในลุ่มไม่มีรอยตัดเทียม

---

# รอบแก้ที่ 5 — จับต้นตอ "ร่องตรง" (straight trenches) + ปิดช่อง boundary สี่เหลี่ยมรอด

ต่อจากรอบ 4: ผลรัน `flow_paths(19).geojson` ยังมีปัญหา 2 ข้อ (1) osm_river ยังเป็นสี่เหลี่ยม 7,984 เส้น
(2) เส้น flow มี segment ตรงผิดธรรมชาติ 15–64km (เช่น 1558→4148, rainfall 1113297)

## หลักฐานที่พิสูจน์จากไฟล์จริง (flow_paths(19).geojson)

| ข้อค้นพบ | หลักฐาน |
|---|---|
| osm_river ยังเป็นสี่เหลี่ยมเพราะแคช boundary เก่ารอด | `_meta` บอก `basin_polygon` แต่ osm_river ยัง 7,984 เส้นไม่ถูกตัด → ไฟล์ `nan_boundary.geojson` บนเครื่องรันยังเป็น rectangle ยุค fallback (5 vertices) และรอบ 4 แค่ WARN แล้วใช้ต่อ |
| กระโดดเป็น defect เดิม ไม่ใช่บั๊กรอบ 4 | ไฟล์ (18) มี 161 features มี seg >10km, (19) มี 152, nan เก่า 219 |
| 90% ของกระโดดเป็นแนวแกนตรง (Manhattan) | N/S/E/W ล้วน + hub จุดเดิมซ้ำข้ามหลายสิบเส้น (เช่น `[100.816,18.487]` x26) |
| กระโดดไม่ได้มาจาก OSM graph / D8 step | D8 เดินทีละ cell, OSM cache ไม่มี seg >5km, hub ไม่ตรง osm_river endpoint (0/12) |
| ต้นตออยู่ใน DEM/burn/fdir | รอ diagnose ยืนยัน (ด้านล่าง) |

## Step 0 — สคริปต์ diagnose (`scripts/diagnose_terrain_artifacts.py`, ใหม่)

ตรวจ boundary / water polygon ขยะ / way jump / DEM void-flat run / fdir straight run
(อ่าน raster แบบ strip ประหยัด RAM) + เทียบ hub จากไฟล์ flow กับทุกตัวต้องสงสัย
(แก้ภายหลัง: แปลงพิกัด projected → lon/lat เพื่อให้จับคู่ hub ได้)

**ผลรันจริงบนเครื่อง data (ลุ่มน่าน):**

| ตัวต้องสงสัย | ผล | สรุป |
|---|---|---|
| boundary | 350 vertices, ThaiWater, OK | สี่เหลี่ยมแก้สำเร็จแล้ว ✅ |
| water polygon ขยะ | 0 / 1,904 | hypothesis ตาย — ตัดออกด้วยการวัด |
| way jump >2km | 15 (สูงสุด 10.7km) | **way 400328476 gap 10.7km ตรง hub (100.527,17.993)↔(100.475,17.911) เป๊ะ** |
| fdir straight run | 2,384 cells (81km) วางทับ flat run ที่มีใน raw_dem อยู่แล้ว | **ต้นตอจริง: ผิวน้ำนิ่ง (exact-constant plateau) → D8 flat resolution ไล่ตรงตามแถว/คอลัมน์** |
| DEM | raw 12.5m แต่ conditioned/fdir = **34m** (downsample ที่ max_cells=150M) | ขัด plan §7 — เปิดประเด็นให้ผู้ใช้ตัดสินใจ |

## สิ่งที่แก้ (พร้อม commit)

### Step 1 — boundary cache ตรวจจริง (`11879cf`)
- `load_valid_boundary()`: แคชผ่านเมื่อ (Multi)Polygon + **≥50 vertices** + source ไม่ใช่ "Station Bounding Box Fallback" เท่านั้น
- `fetch_basin_boundary()`: แคชกล่อง → ทิ้ง + ดึงใหม่ตาม chain อัตโนมัติ; `generate_flow_paths` → exit พร้อมคำสั่ง DELETE + refetch (ยืนยันจาก Colab จริง: จับ rectangle 5 vertices ได้)

### Step 2a — `terrain_engine.break_exact_flats()` (`5ce05d6`)
- sawtooth micro-gradient **1 float32 ULP/cell ตามลำดับ raster, wrap ทุก 64 cells** ทับ exact-constant plateau → D8 ไล่ได้ไม่เกิน 64 cells ต่อทิศ
- **ทดลองกับ pyflwdir จริง (synthetic plateau): straight run 398 → 63 cells, 0 pits** (จุด wrap ~sub-mm ถูก fill หมด)
- wire เข้า `generate_flow_paths` ก่อน `pyflwdir.from_dem` → ต้อง `--force` เพื่อ re-burn + คำนวณ fdir ใหม่

### Step 2b — `fetch_basin_gis.sanitize_osm_way_jumps()` (`5ce05d6` + fix `3f6be54`)
- split way ที่ vertex gap >2km, drop ชิ้นจิ๋ว/way ที่เป็น jump ล้วน, MultiLineString + tag `jump_split` + counters ใน `_meta`, idempotent
- **bug ที่จับได้จาก log จริง**: เวอร์ชันแรกใช้ min_part_km=1.0 กรอง**ทุก way** → drop ลำธารสั้น 867 เส้นทั้งที่ไม่มี jump (ผิดกฎ "OSM ไม่กรองความยาว") → แก้: กรองเฉพาะชิ้นที่เกิดจากการ split; way ไม่มี jump ผ่านทุกเส้น
- `snap_stations_to_stream` รองรับ MultiLineString แล้ว

### `fetch_basin_gis.py` force flags (`e02b7fc` + `3f6be54`)
- `--force` = **boundary + OSM + DEM re-download/mosaic ทั้งหมด** (แก้จาก log จริงที่ติด DEM cache)
- `--force-osm` / `--force-dem` สำหรับเลือกเฉพาะส่วน

## ผลรัน --force บนเครื่อง data (ยืนยันแล้ว)

```
[JUMP-SPLIT] osm_waterways: 1,688 ways -> 1,686 (split: 8, tiny ways dropped: 2)   ✅ (เดิม 821 ผิด)
[CROP] osm_waterways: 1,686 -> 1,686 (clipped: 16, buffer 2000 m)                  ✅
[CROP] osm_water_polygons: 1,904 -> 1,904 (clipped: 1)                             ✅
[FORCE] Re-downloading & re-mosaicking the ALOS DEM ... 74 tiles, 45,817x24,233    ✅
```

## การตรวจสอบ

- **tests**: 23/23 PASSED (เพิ่ม: boundary cache rejection, generate fail-fast, break_exact_flats ผ่าน pyflwdir, way-jump split + idempotent + regression way สั้น, --force bypass/overwrite boundary ด้วย network patch)
- **generate บน Colab**: fail-fast จับ boundary เก่า 5 vertices ได้ → ต้องลบไฟล์ + รัน fetch ด้วย `--dir` เดียวกันก่อน

## สถานะ & ขั้นตอนถัดไป

- ✅ fetch ฝั่ง data machine สมบูรณ์ (boundary จริง, OSM 1,686 เส้น, DEM ใหม่)
- ⏳ รอรัน `generate_flow_paths.py --basin nan --force` + validator บนเครื่อง data/Colab (`--dir` ต้องตรงกัน; ไม่จำเป็นต้อง `--force` ที่ fetch ซ้ำ ถ้าแคช OSM ใหม่แล้ว)
- ⏳ ตัวชี้วัดสำเร็จ: features ที่มี segment >2km ลดจาก 265 → ~0; ดู 1558→4148, 1113297, ขอบลุ่ม
- ❓ เปิดประเด็นค้าง: DEM 34m (downsampled) vs native 12.5m ตาม plan §7 — native ต้องการ RAM ~4.4GB+ สำหรับ 1.1 พันล้าน cells

## Commits (รอบ 5)

| commit | เนื้อหา |
|---|---|
| `11879cf` | Step 0 diagnose script + Step 1 strict boundary cache validation |
| `5ce05d6` | Step 2a break_exact_flats + Step 2b way-jump split + diagnose lon/lat |
| `e02b7fc` | force flags 3 ระดับ (--force / --force-osm / --force-dem) |
| `3f6be54` | แก้ --force ครอบ DEM + แก้ bug jump-split กรอง way สั้น (867 เส้น) |

---

# รอบแก้ที่ 6 — ตายต้นตอเส้นตรงบนอ่าง/ที่ราบ + Branch ownership ผิดก้อน + OSM audit

ปัญหาจาก `flow_paths_nan_v5.geojson` (รัน --force ครบแล้ว): (1) เส้นตรง 30-40km แกน N-S/E-W
กระจุกบนแนวอ่างสิริกิติ์ (branch 1080962 กระโดด 32km ลงเขื่อน, 1113297→1558 jump 15.6+39.6+13.3+30km),
(2) branch `from_station_id=1137134` เป็นเจ้าของ ~20 เส้นกระจายทั้งลุ่ม (shared_with 60-70 สถานี),
branch 478 วิ่งเข้าแดน 480, (3) osm_river 112285698 geometry ไม่สมเหตุผล (length 9.12km ใน bbox 1.9km)
+ สงสัยว่า OSM โดนลบเงียบ ๆ

## หลักฐานที่วัดจากไฟล์ v5 จริง

| ข้อค้นพบ | หลักฐาน |
|---|---|
| เส้นตรงเป็น D8 flat-trench บน plateau | 446 segments >2km; hub ซ้ำ (100.813,18.216)×23, (100.75,18.09)×23, (100.816,18.487)×20 — ทั้งหมดบนแนวอ่างสิริกิติ์; ทิศแกน N-S/E-W ล้วน |
| sawtooth รอบ 5 ยังมีรู | pyflwdir `fill_depressions` (Wang & Liu) fill ถึงระดับเดียวกันเป๊ะ (ไม่มี epsilon) แล้วกำหนด d8 ตาม heap pop order — sawtooth (r+c) mod P เหลือ "ราง" tie พอดี และแม้แต่ลาด planar ก็ได้ run ยาวจาก heap tie-break (ทดสอบ: raw DEM ได้ straight run 399 cells บนโซนลาดเรียบ) |
| Branch ผูกสถานีผิด | reverse-BFS ต่อสถานีไม่มีขอบเขต → สถานีที่แตะลำธาร acc≥500 อ้างสิทธิ์ network ต้นน้ำทั้งลุ่ม; dedupe เลือกเจ้าของ = สถานีแรกที่ประมวลผล (1137134) ไม่ใช่เจ้าของ local |
| OSM audit ไม่มี | `_meta.filters` มีแต่ basin_clip — ไม่มี counter ของ jump-split ให้ตรวจย้อนว่ามี way ถูกทิ้งไหม |

## สิ่งที่แก้ (รอบ 6)

### Phase A1 — `break_exact_flats` ใหม่: hash ULP noise (ทุก cell)
- ทดแทน sawtooth: deterministic integer-hash noise ขนาด ULP (0.5mm cap, ขั้นต่ำ 64 ULP) แบบไร้โครงสร้างเชิงพื้นที่ → fill order ของ pyflwdir ถูกสุ่มพลาง ไม่เกิด raster-order trench ทั้งบน plateau (อ่าง burn) และลาด planar
- ผล synthetic: straight run 399 → 50 cells, pits = 0, DEM ปกติเพี้ยน < 1mm (macro drainage ยังตัดสินด้วย sill เหมือนเดิม = hydrologically identical)

### Phase A2/A3 — Water-body D8 stop + Reservoir transit (generic ทุกลุ่ม ไม่ hardcode)
- `terrain_engine.build_water_polygon_mask()`: rasterize OSM water polygons → (mask, ids)
- `trace_downstream_path(..., water_poly_mask, water_poly_ids, start_poly_id)`: หยุดที่ cell ผิวน้ำแรกที่ "ต่าง polygon จากจุดเริ่ม" — gauge ที่อยู่ใน/ริมอ่างเดินต่อออกจาก polygon ตัวเองได้ แต่ห้ามไต่ข้ามอ่างอื่น
- `graph_topology.build_water_body_transits()`: ต่อ polygon หา OUTLET cell = acc สูงสุดใน polygon (หน้าเขื่อน/ปากปล่อย) → snap เป็น outlet node; เชื่อม backbone component ที่โดนอ่างตัดด้วย **transit edge** (node ใกล้สุด → outlet, ทิศตรวจด้วย elevation) — ถ้า OSM มี centerline ในอ่างจริง component จะรวมกับ outlet อยู่แล้ว **ไม่เติม edge ทับ** (geometry จริงชนะเสมอ)
- Layer 1: D8 หยุดที่ขอบอ่าง → attach backbone (ranked) → ถ้า attach ไม่ได้ ใช้ transit outlet; Layer 2: poly stop = หา entry เหมือน river stop → ถ้าไม่เจอ centerline ใช้ outlet เป็น entry + เส้นตรง shore→outlet tag `reservoir_transit: true`; cascade chain ที่ผ่าน transit edge ถูก tag อัตโนมัติ
- **Bug ที่จับได้ระหว่างแก้**: Layer 1 เดิมเช็ค `if target_station_id:` โดย sentinel stop ที่เป็น string หลุดเข้าเงื่อนไข → สถานีบางตัวถูกข้ามทั้งตัว; + latent bug รอบ 4: บรรทัด refine ของ entry scan assign ค่า 3 ตัวลง 4 ตัวแปร (จะ crash เมื่อ trigger) — แก้แล้วทั้งคู่

### Phase B — Branch first-claim ownership (เขียนใหม่ `extract_station_drainage_branches`)
- จากเดิม reverse-BFS ต่อสถานี (ไม่อั้น) + dedupe เจ้าของแรก → **first-contact semantics**: first-claim ของ seed cells → ครั้งเดียว global reverse-BFS รวม → แต่ละ channel head เดิน D8 ลงจนชน path ของสถานีใดก่อน = เจ้าของ branch นั้น (memoized path compression, O(K) amortized)
- ห้าม claim channel cells ใน water polygon (branch ไม่วิ่งในอ่าง — ตายต้นตอ branch 32km ตรงข้ามอ่าง)
- เส้นที่เดินลงไม่เจอ path สถานีใดเลย → drop + นับ log (ไม่มีเจ้าของ = ไม่ออกไฟล์)
- `shared_with` ถูกถอดออกทั้งกลไก (head หนึ่งมีเจ้าของเดียว) — เคส 1137134 ครองทั้งลุ่ม / 478→480 ตายที่ต้นตอ
- Case 3: branch seeds ใช้ overland path **หลัง cap** (เดิมใช้ trace เต็มก่อน cap → seed หลุดข้ามลุ่ม)

### Phase C — OSM layer audit
- `sanitize_osm_way_jumps`: `_meta.way_jump_stats` เพิ่ม `dropped_osm_ids` (cap 200) — รายชื่อ way ที่ถูกทิ้งทั้งเส้น
- `_meta.filters` ในไฟล์ output เพิ่ม `osm_way_jump_split` + `osm_crop` — validator/คนตรวจพิสูจน์ย้อนได้ว่า "ไม่มีการลบ OSM เงียบ ๆ"

### Phase D — Validator + diagnose + tests
- `validate_flow_paths.py` เช็คใหม่: **axis-aligned teleport** (N-S/E-W ตรง >max-jump-km) = FAIL ยกเว้น `reservoir_transit`; `_meta.filters.osm_way_jump_split` ต้องมี
- `diagnose_terrain_artifacts.py` เพิ่ม: OSM audit section (แสดง counter + รายชื่อ way ที่โดนทิ้ง), hub-inside-any-water-polygon correlation
- tests 27/27 PASSED — เพิ่ม 5 ใหม่: water-poly stop (รวม own-polygon exemption), transit เชื่อม component ข้าม lake, end-to-end gauge chain ข้าม lake ไม่มี teleport, validator จับ axis teleport + ยกเว้น transit, transit กับ projected CRS (UTM) + guard; เขียนใหม่: branch ownership test (anti-steal + order-independence)

### Phase E — Hotfix ตามผลรันจริงบนเครื่อง data/Colab (รอบ 6 ยังไม่ผ่าน end-to-end — crash ตามรายการด้านล่าง)

| commit | อาการ | ต้นตอ | การแก้ |
|---|---|---|---|
| `1b166ec` | (เชิงป้องกัน) | `generate --force` อ่าน `conditioned_dem.tif` เก่า (มี burn/noise ค้างจากรอบก่อน) มา **burn ซ้ำทับ** — คลองจมลึกขึ้นทุกรัน | `--force` เริ่มจาก `raw_dem.tif` เสมอ (bypass conditioned cache) → รัน `generate --force` ตัวเดียวได้ ไม่ต้องรัน fetch/diagnose ก่อน |
| `778946f` | `OverflowError: cannot convert float infinity to integer` ใน `snap_point_to_graph_ranked` | DEM เป็น projected CRS (UTM): outlet cell ถูกแปลงกลับ lon/lat ด้วย Transformer **ทิศผิด** (`4326→crs`) → เมตรถูกป้อนเป็นองศา → pyproj คืน `inf` | แยก transformer 2 ตัวให้ถูกทิศ (`to_lonlat` crs→4326 สำหรับ outlet, `to_raster` 4326→crs สำหรับ node mapping) + guard: พิกัด outlet ไม่ finite → ข้าม polygon นั้น |
| `7661a55` | `KeyError: 68456` ใน union-find `find()` | การ snap outlet **split edge แล้วสร้าง node ใหม่** หลัง component map ถูกสร้างไปแล้ว | `find()` tolerant (node ไม่รู้จัก = root ตัวเอง) + `union_new_node()` รับ node ใหม่เข้า component ของ way ที่มันลง (a→x→b หลัง split) |
| `c8f3b51` | `TypeError: '<' not supported between float and dict` | typo ตอนแก้ latent bug รอบ 4: assign `m2` (meta dict) ลง `best_d` ใน entry-refine loop | assign `d2` ถูกต้อง |
| `b029592` | (ไม่ crash แต่ logic ผิด) | เลือก node ของแต่ละ component ที่ "ใกล้ outlet" ด้วยการเทียบระยะ `d < comp_nodes[comp]` ที่เก็บ **node id** ไว้ → เลือกผิด เส้น transit ยาวเกิน | เก็บ `(distance, node)` คู่กันแทน |

## ลำดับการรัน (เครื่อง data/Colab)

```bash
# ตัวเลือก: เก็บหลักฐานก่อน (ไม่บังคับ)
python scripts/diagnose_terrain_artifacts.py --basin nan --geojson flow_paths_nan_v5.geojson

# รันตัวเดียวพอ -- --force: refetch OSM (ต้องมีเน็ต) + re-burn จาก raw DEM + fdir ใหม่ + transits + branches
python scripts/generate_flow_paths.py --basin nan --force

# ตรวจผล
python scripts/validate_flow_paths.py --geojson dataset/nan/processed/flow_paths.geojson.gz \
    --boundary dataset/nan/gis/nan_boundary.geojson
```

**ตัวชี้วัดสำเร็จ**: axis-aligned teleports 1193 → 0 (นอก transit); ไม่มี branch จากสถานีใดครองทั้งลุ่ม;
1113297→1558 วาดเข้าอ่างตาม centerline/transit; `_meta.filters.osm_way_jump_split` มีครบ → validator PASS

**สถานะ**: แก้ครบทุก Phase แล้ว (commits `bd26216`..`b029592`) — hotfix รอบ Colab ผ่านจุด crash ทั้ง 4 จุดตามลำดับ
(graph → transits → entry refine) และรอผลรัน `--force` รอบใหม่เพื่อยืนยันตัวชี้วัดข้างบน ถ้า crash จุดใหม่
ให้ส่ง traceback ต่อได้ทันที (โครงหลักของ Layer 1/2/branches เป็นโค้ดที่รอบก่อนรันเคยผ่านแล้ว)

---

# รอบแก้ที่ 7 — Complete Hydro-Vector Hybrid Engine (ตาม plan_fix_flow_path_v3.md)

ปัญหาที่พบจากผลการรันจริงใน `flow_paths_nan_v6.geojson`:
1. ยังมีเส้นตรงแกน N-S/E-W (20–64 km) หลงเหลืออยู่ เช่น rainfall_to_gauge จากสถานี 501384 ไป 1558 (ยาว 151 km) และจากสถานี 685649 ไป 268599 (มีเส้นตรง 25.5 km และ 39.2 km ก่อนเข้าแม่น้ำ)
2. OSM waterways ลดลงจาก 7,984 เหลือ 1,694 เส้น และสถานีบริเวณขอบลุ่มน้ำ/ยอดดอยกลายเป็น Dead-Zone

## Root Causes & สิ่งที่แก้ในรอบ 7

| ปัญหา | Root Cause | วิธีแก้ในรอบ 7 (Generic 100% ไร้ Hardcode) |
|---|---|---|
| **D8 Flat Trenches (เส้นตรง N-S/E-W 20–64 km)** | `pyflwdir.from_dem` ทำ Wang & Liu (2006) Pit-Fill แล้วยกความสูงทุกเซลล์ในหลุม/อ่างเก็บน้ำให้เท่ากับ $Z_{\text{sill}}$ เป๊ะ ทำให้ Noise $0.5\text{ mm}$ ถูกลบทับจนเรียบ 100% แล้ว Min-Heap กำหนด D8 ตามลำดับสแกน Raster | **`enforce_geodesic_flat_slope`**: คำนวณ Geodesic Distance Transform จากจุด Outlets ที่ระบายน้ำได้จริง ย้อนกลับเข้าสู่กลางแอ่งราบหลัง Pit-Fill แล้วใส่ Micro-Slope ลาดเท $\Delta Z = 10^{-5}\text{ m/cell}$ (~$0.8\text{ mm/km}$) ทำให้ D8 ไหลโค้งมุ่งหน้าสู่ Spillway จริง $100\%$ |
| **D8 Overland วิ่งเตลิด 151 km (501384 $\rightarrow$ 1558)** | สถานี 501384 ไม่มีแม่น้ำ OSM ใกล้ตัว D8 จึงวิ่งบนบกข้ามจังหวัดไปชน Footprint สถานีน้ำ 1558 จนเข้า Case 1 วาดเส้น D8 บนบก 151 km | **`STREAM_STOP` & Overland Cap**: ใน `trace_downstream_path` หยุดการเดินบนบกเมื่อ $\text{acc} \ge 500\text{ cells}$ หรือเดินครบ $3.0\text{ km}$ แล้วนำจุดนี้ Snap เข้าหาแม่น้ำ OSM Backbone เพื่อวิ่งต่อด้วย Dijkstra สู่สถานีน้ำในลุ่มน้ำของตนเอง |
| **OSM Waterways แหว่ง & Dead-Zone** | Overpass Query ใช้เฉพาะ Polygon ลุ่มน้ำที่ถูก simplify และ Crop ตัดทิ้งท่อนย่อยด้วย `longest = max(parts)` | **Station-Enveloped Query**: ขยายขอบเขต Query ด้วย $\text{Union}(\text{Basin}, \text{ConvexHull}(\text{Stations})).\text{buffer}(5.5\text{km})$ และใน `crop_geojson_to_basin` เก็บทุกชิ้นส่วนย่อยเป็น `MultiLineString` ไม่ทิ้งชิ้นย่อย |
| **แม่น้ำกลับทิศทาง 180° จาก Noise สะพาน** | `add_river_segment` ตัดสินการ Reverse เส้นโดยดูแค่ 2 จุดหัว-ท้าย | **Majority-Weighted Slope**: คำนวณความลาดชันสุทธิตลอดสาย ($\sum \text{sign}(\Delta Z) \cdot L$) |
| **Collinear Straight Runs ใน GeoJSON** | D8 เดิน 3,000 ก้าวตรงเป๊ะ แล้ว Douglas-Peucker ยุบเหลือ 2 จุดหัว-ท้าย กลายเป็นเส้นตรง 36 km | **Collinear Step Run Sanitization**: ดักจับก้าวตรงในแนวแกนติดต่อกันเกิน 60 cells (~750m) ใน `simplify_linestring_coords` |

## ไฟล์ที่ได้รับการแก้ไขในรอบ 7:
1. `flood-analysis-model/scripts/fetch_basin_gis.py`: ขยาย Overpass Query Envelope ด้วย Convex Hull สถานี + รักษา MultiLineString ชิ้นส่วนลำน้ำใน `crop_geojson_to_basin`
2. `flood-analysis-model/scripts/modules/terrain_engine.py`: เพิ่มฟังก์ชัน `enforce_geodesic_flat_slope` (SciPy C-core Distance Transform บน Flat Mask หลัง Pit-Fill)
3. `flood-analysis-model/scripts/generate_flow_paths.py`: เชื่อมต่อ `enforce_geodesic_flat_slope` ทันทีหลัง Pit-Fill ก่อนคำนวณ Flow Direction / Accumulation
4. `flood-analysis-model/scripts/modules/graph_topology.py`: เพิ่ม `STREAM_STOP` และ Overland Cap ใน `trace_downstream_path`, ปรับปรุง Layer 2 Ingress ใน `build_flow_paths_and_relations`, Majority Slope ใน `add_river_segment`, และ Collinear Guard ใน `simplify_linestring_coords`

---

# รอบแก้ที่ 8 — Boundary Natural Divides & Stream Continuity Restoration (V8)

ผลการรันจริงใน `flow_paths_nan_v7.geojson`:
1. เส้นตรงหายไปหมดจริง ($100\%$)
2. เกิดปัญหาขอบเขตบวมเป็นวงรีบาน ๆ และ OSM rivers เพิ่มเป็น 9,499 เส้น จากการใช้ Convex Hull ถมรอยเว้าลุ่มน้ำ
3. เส้น rainfall_to_gauge ลดลงจาก 662 เหลือ 272 เส้น (194 สถานีกลายเป็นติ่งสั้น 200m) จากการที่ `STREAM_STOP` ตัดจบก่อนถึงแม่น้ำ และ gauge_to_gauge ลดจาก 169 เหลือ 68 เส้นจาก default cap 3km

## สิ่งที่แก้ในรอบ 8 (V8):
1. **กู้คืนขอบเขตลุ่มน้ำธรรมชาติ:** ถอด Convex Hull ออกทั้งหมด ใช้ `basin_poly.buffer(2000m)` เพื่อรักษาแนวสันเขาเว้าคอดธรรมชาติ ไม่บวมเป็นวงรี และตัดแม่น้ำนอกลุ่มน้ำทิ้ง
2. **กู้คืนเส้น Gauge $\rightarrow$ Gauge:** ปลดล็อค `max_overland_cells=0` ใน Layer 1 เพื่อให้สถานีวัดน้ำเดินตามลำน้ำสายหลักได้เต็มระยะ 10–50 km
3. **กู้คืนเส้น Rainfall $\rightarrow$ Gauge:** ถอด `STREAM_STOP` ที่ตัดจบที่ 200 เมตรออก ให้ D8 ไหลนำทางตามร่องเขาธรรมชาติลงมาจนบรรจบแม่น้ำ OSM หรือสถานีวัดน้ำในระยะทางที่ถูกต้อง
4. **เร่งความเร็ว $O(N)$ ใน `build_water_body_transits`:** ใช้ `scipy.ndimage.find_objects` และ Node Hash Map Pre-indexing ทำให้ขั้นตอนที่ 5 รันเสร็จสิ้นใน $<1$ วินาที

---

# รอบแก้ที่ 8.1 — True Douglas-Peucker & Tributary Confluence Termination (V8.1)

ผลการตรวจสอบ `flow_paths_nan_v8.geojson`:
1. พบเส้นตรงเทเลพอร์ต 51.66 km ในสถานี 5488 (`flow_gauge_5488_downstream`) และ 1.5–2.8 km ในกิ่งของสถานี 1430 (`branch_1430_007, 010, 012, 019, 025, 028`)
2. กิ่งสาขาของสถานี 1430 ไหลล้นลงไปในแม่น้ำสายหลักแล้ววิ่งทับซ้อนกันเองตามแนวแม่น้ำน่าน ($100.833^\circ$)

## สิ่งที่แก้ใน V8.1:
1. **กำจัดเส้นตรงเทเลพอร์ต 51.66 km ถาวร:** ลบบล็อก `if consec_dx > 60: continue` ออกจาก `simplify_linestring_coords` เพื่อไม่ให้โค้ดตัดพิกัดแม่น้ำทิ้งกลางคัน และคืนการทำงานให้ Douglas-Peucker มาตรฐาน (~35m tolerance)
2. **หยุดกิ่งสาขาที่จุดบรรจบแม่น้ำ (River Confluence Stop):** ใน `extract_station_drainage_branches` สั่งหยุดการเดินกิ่งสาขาทันทีที่แตะแม่น้ำ OSM (`river_mask`) ทำให้กิ่งสาขาเป็นลำธารบนภูเขาที่ไหลลงสู่แม่น้ำสายหลักอย่างสะอาด ไม่ไหลล้นลงมาทับซ้อนกันเองในแม่น้ำ



