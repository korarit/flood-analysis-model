# Flow Paths Improvement Plan

แผนแก้ไข `flood-analysis-model/scripts/generate_flow_paths.py` + modules
(สถานะ: **✅ ดำเนินการเสร็จสิ้น** — ทุกข้อผ่าน py_compile + synthetic tests + recheck แล้ว)

## ปัญหาที่ยืนยันแล้ว (จาก flow_paths(15).geojson + อ่านโค้ด)

1. **เส้นตรงกระโดดไกล** — 205/569 features มี segment >1km, สูงสุด 64.9km,
   114 features กระโดด "กลางเส้นแล้ววาดต่ออีกฝั่ง" (stitching ของชิ้น path ที่ไม่ต่อกัน
   จาก forced bridging รุ่นเก่า, commit `60bb5df`)
2. **ช่วงกระโดดตรงกับแม่น้ำน่านช่วงผ่านเขื่อนสิริกิติ์ (lat 18.58→17.99)** —
   OSM ไม่มี waterway line บนผิวน้ำกว้าง/เขื่อน + พื้นที่ราบ D8 ไม่นิ่ง
3. **OSM มาไม่ครบ** — query ใช้ bbox ของสถานี+0.35° ไม่ใช่ขอบเขตลุ่มน้ำ,
   client timeout 60s < server 120s, ไม่ตรวจ `remark`, ไม่ดึง water polygons,
   แคชไม่ validate
4. **Cascade ถูกตัด** — รุ่นเก่าเคย cascade 120km/8 gauges → ถูกลดเหลือ gauge เดียว
   (water จากสถานีฝนไหลผ่าน gauge แรกไป gauge ถัดไปจริงทางอุทกวิทยา)

## การตัดสินใจที่ผู้ใช้เลือก

| หัวข้อ | การตัดสินใจ |
|---|---|
| พื้นที่ query OSM | ใช้ polygon ลุ่มน้ำ ThaiWater จาก `dataset/{basin}/gis/{basin}_boundary.geojson` |
| รูปแบบเส้น cascade | ทางเลือก C — เส้นเป็นท่อน `entry→G1`, `G1→G2`, `G2→G3` ไม่ทับซ้อน |
| เส้นสาขา (ลำธารย่อย) | จาก D8 terrain (ครบกว่า OSM), **ผูกต่อสถานีฝน**, อยู่ไฟล์ `flow_paths.geojson` เดิม |
| ระยะขั้นต่ำ | `--min-flow-km` default **1.0 km** |
| ข้อจำกัด | **ห้ามลดความละเอียด DEM** (`max_cells=150M` ใน `read_dem_geotiff` ต้องคงเดิม) — optimize ที่ logic/RAM แทน |

---

## B — OSM ให้ครบ (fetch_basin_gis.py, generate_flow_paths.py)

- **B1** `fetch_osm_waterways(..., basin_boundary_geojson=None)`:
  - โหลด boundary จาก `{basin}_boundary.geojson` (ThaiWater basin.json) → shapely union →
    `simplify(0.005)` (~550m) → แปลงเป็น Overpass `poly:"lat lon ..."` filter
    (MultiPolygon → union หลาย statement, cap 5 polygons ใหญ่สุด)
  - fallback: ไม่มี boundary → bbox สถานี +0.35° + `[WARN]`
- **B2** Reliability: client timeout 180s ตรง `[timeout:180]`, ตรวจ `remark`
  (incomplete/timed out → ถือว่าล้มเหลว → mirror ถัดไป), ทุก mirror ล้ม →
  **ห้ามเซฟ placeholder ทับแคชเดิม** (return แคชเก่า + warn ถ้ามี, ไม่งั้นเซฟ `.failed`)
- **B3** แคช fingerprint: `_meta = {fingerprint(sha256 query), fetched_at, n_features, source}` —
  fingerprint ไม่ตรง → refetch อัตโนมัติ
- **B4** Water polygons: ฟังก์ชันใหม่ `fetch_osm_water_polygons` →
  `way["natural"="water"]` (closed ways → Polygon) ลง `osm_water_polygons.geojson`;
  `burn_stream_network_into_dem(..., water_polygons_geojson, polygon_burn_depth_m)`:
  burn เส้นแม่น้ำ `-burn_depth` (15m), burn polygon `-polygon_burn_depth` (default burn-5=10m)
  → D8 ข้าม reservoir ได้; ใช้ polygon boundary ช่วย snapping ด้วย
- **B5** graph รองรับ `MultiLineString` (build_flow_paths_and_relations)

## A — กำจัดเส้นตรง/เส้นขาด (graph_topology.py)

- **A1** `simplify_linestring_coords`: `max_step_km` 10.0 → **0.5**, เปลี่ยน `break` →
  แบ่ง chunk ที่จุดกระโดด + เก็บ chunk แรก (มีจุดเริ่ม/สถานี) + warn เมื่อทิ้งจุด
- **A2** จุดต่อ overland→backbone: coarse STRIDE=8 hit ที่ `i0` → scan ย้อน `[i0-7, i0]`
  หา index ที่ใกล้แม่น้ำสุด (cKDTree query ≤8 ครั้ง/สถานี)
- **A3** สถานีเป้าหมาย: เพิ่ม `water_prox_map` (±12 cells ≈ 150m รอบพิกัดสถานี) เป็น
  fallback stop condition ต่อจาก footprint 11×11 (footprint ตรวจก่อน, O(1) dict lookup)
- **A4** Layer 1 D8 จบก่อนเจอ gauge (หลุม/nodata/ขั้นสูงสุด) → fallback: หา river node
  ใกล้เซลล์สุดท้าย (≤300m) → Dijkstra บน backbone (≤ `cascade_max_km`) → ต่อเส้น
  จนถึง gauge ที่ downstream-reachable ตัวแรก, ไม่งั้นคง "Basin Outlet" เดิม
- **A5** Elevation: `sample_elevation_strict` คืน **None นอก DEM/invalid** (ไม่ clamp);
  `add_river_segment` รับ `elevs` แบบ batch (vectorized affine+pyproj ต่อ feature ไม่ใช่
  ต่อ vertex — ลด pyproj overhead ~100x) — ปลาย None → **ไม่ flip** ใช้ทิศ OSM + tag
  `direction_source="osm"`; cache cap 500k entries กัน RAM บาน
- **A6** `water_grid_map` ใช้ `setdefault` (สถานีแรกครอง footprint)

## D — Rain Cascade รูปแบบ C (graph_topology.py)

- **D1** ✅ cascade **2 ชั้น** (ขยายจากแผนเดิมระหว่างทดสอบ):
  - **Case 1 (D8 ชน gauge ตรง)**: หลังถึง gauge แรก → trace D8 ต่อจาก gauge นั้น
    (stop แบบ exclude gauge ที่ผ่านแล้ว) ไล่ต่อจนครบ `cascade_max_km` —
    เดิม Case 1 จบที่ gauge แรกและบล็อก cascade ทั้งหมด (ตรวจพบตอนรัน e2e test)
  - **Case 2 (เข้าแม่น้ำห่าง gauge)**: เก็บ **ทุก** gauge downstream-reachable บน backbone
    ภายใน `--rain-cascade-km` (directed edges กรองสาขาย้อนให้เอง), เรียงตาม channel distance
- **D2** ✅ `--min-flow-km` (1.0): Case 3: 0.5→1.0, Case 1/2: เพิ่ม min; cascade ทั้งสองชั้น
  กรอง segment < 1km; Case 2 กรอง gauge ห่างจาก entry < 1km
- **D3** ✅ เส้นแบบท่อนไม่ทับซ้อน: Case 1 seg0 = overland+D8→G1, seg k = D8 G(k-1)→Gk;
  Case 2 seg0 = overland+channel entry→G1, seg k = channel G(k-1)→Gk
  (ใช้ `reconstruct_node_path` + `stitch_coords_from_prev`; node gauge ก่อนหน้าไม่อยู่ใน
  chain → ใช้ full chain เป็น fallback) — relation schema/IDW/exporter ไม่แก้
- **D4** ✅ Lag ต่อ gauge ใช้ cumulative distance (hydrologically correct)

## E — เส้นสาขา D8 ผูกต่อสถานีฝน (graph_topology.py)

- **E1** `trace_downstream_path` คืน `path_rc` เพิ่ม (แก้ caller ให้ครบ)
- **E2** Layer 2 เก็บ `overland_cells` ต่อสถานีฝน (Case1/3: ทั้ง path, Case2: `[:entry_idx+1]`)
- **E3** `extract_station_drainage_branches(...)`: ต่อสถานี — reverse-BFS ขึ้นต้นน้ำจาก
  path cells (acc ≥ `--branch-min-acc` default 500 cells) → หา channel head → เดิน
  downstream จนชน trunk/เส้นที่เดินแล้ว → reaches → กรองยาว ≥ 1km →
  feature_type `rainfall_drainage_branch` ผูก `from_station_id`
- **E4** Guards: cap cells/เครือข่าย (เกิน → skip + warn), visited mask รีเซ็ต O(K),
  `--no-branches` ปิดได้ (BFS ต่อสถานีเครื่องอ่อนช้าขึ้น)

## C — ตรวจสอบ

- **C1** ✅ `scripts/validate_flow_paths.py` (stdlib เท่านั้น): สแกน jump >1km,
  เส้นขาดกลาง path, stub, นับ feature/relation ต่อ type —
  ทดสอบกับไฟล์เก่า `flow_paths(15).geojson`: จับ jump 1,304 จุด (worst 64.91km) → FAIL ถูกต้อง
- **C2** ✅ `tests/test_flow_topology.py` (สั่งรัน: `python tests/test_flow_topology.py`,
  venv เดิม ไม่ต้องมี pytest): 6 test — graph ทิศ/flip/unknown-elev, node path + stitch,
  simplify split, merge dedup, burn line vs polygon depth, e2e synthetic basin
  (gauge chain, cascade 2 segments, drainage branch, weight IDW = 100%)

## สิ่งที่ค้นพบเพิ่มระหว่างแก้ (self-review จับบั๊กตัวเองได้ 3 จุด)

1. **simplify tail re-append**: คืนจุดปลายทางกลับเมื่อช่องว่างท้ายเส้น ≤ 2km (station access
   stub) เท่านั้น — ถ้า re-append ไม่มีเงื่อนไข จะกลับไปวาดเส้นตรง 65km ดังเดิม
2. **Branch BFS ลืมเช็ค `fdir`**: ครั้งแรกคลมทุก cell ที่ acc ≥ threshold โดยไม่เช็คว่า
   ไหลเข้าหา cell ปัจจุบัน → ฟลัดทั้งหลุมย่อม แก้ด้วย `int(fdir[nr,nc]) == code`
3. **Branch in-degree นับกลับด้าน**: นับ out-degree ของ neighbor ทำให้หา channel head
   ไม่เจอเลย (heads=0) — แก้เป็นนับ in-degree ของ cell ปัจจุบัน
4. **D8 ลัดวง meander อ่อน**: D8 บน DEM 12.5m ตัดโค้งแม่น้ำที่โค้งน้อยกว่า ~1 cell/row ได้
   → Douglas-Peucker รวมเป็น segment ตรงยาว (ทุก cell ต่อเนื่องจริง ไม่ใช่ jump) —
   ยอมรับได้ทางอุทกวิทยา; validator ตั้ง `--max-jump-km` ปรับได้ตามงาน

## ผลการ recheck เทียบแผน (grep-verified ทุกข้อ)

| กลุ่ม | รายการ | สถานะ |
|---|---|---|
| B1–B5 | polygon query / 180s+remark / fingerprint / burn+snap polygons / MultiLineString | ✅ ครบ |
| A1–A6 | simplify 0.5+split / entry refine / prox map / backbone fallback / batch elev+cap / setdefault | ✅ ครบ |
| D1–D4 | cascade Case1+Case2 / min 1km / segments / lag สะสม | ✅ ครบ |
| E1–E4 | path_rc / seeds / branch BFS+walk+filter / guards+`--no-branches` | ✅ ครบ |
| C1–C2 | validator / synthetic tests (ALL PASSED) | ✅ ครบ |
| F1–F3 | OSM river layer แยก feature_type / คุมขนาด / test | ✅ ครบ |
| ข้อจำกัด | `max_cells=150M` + ความละเอียด DEM 12.5m คงเดิมทุกจุด | ✅ ไม่ลด |

**หมายเหตุ**: `run_model_pipeline.py` และ `build_river_network.py` ยังเรียก
`fetch_osm_waterways` แบบไม่ส่ง boundary (fallback เป็น station bbox อัตโนมัติ) —
ถ้าต้องการให้สอง script นี้ใช้ polygon ลุ่มน้ำด้วย ให้โหลด boundary แล้วส่ง
`basin_boundary_geojson=` เพิ่ม (API รองรับแล้ว)

## F — OSM River Layer แยกใน flow_paths.geojson (เพิ่มเติมหลังแผนแรก)

- **F1** ✅ `build_flow_paths_and_relations(..., include_osm_layer=True)`:
  คัดลอก network จาก `osm_waterways.geojson` เข้า `flow_paths.geojson` เป็น
  `feature_type="osm_river"` (id `osm_river_{osm_id}`) พร้อม properties
  `osm_id / river_name / waterway / length_km` → frontend เปิด/ปิด layer ได้เหมือน
  `rainfall_drainage_branch`
- **F2** ✅ คุมขนาดไฟล์: simplify 35m (DP เดียวกับเส้นอื่น), **ไม่กรองความยาว**
  (เก็บ network OSM ครบทุกเส้นตามที่ผู้ใช้แก้ทีหลัง); ปิด layer ได้ด้วย `--no-osm-layer`
- **F3** ✅ test e2e เพิ่ม assertion: layer ครบ 4 feature_type
  (gauge / rainfall / branch / osm_river)

## CLI พารามิเตอร์ใหม่ (generate_flow_paths.py)

```
--rain-cascade-km   (default 60)   ระยะ cascade สูงสุดบน backbone
--min-flow-km       (default 1.0)  ระยะขั้นต่ำของ feature (OSM layer ไม่กรอง)
--branch-min-acc    (default 500)  flow accumulation ขั้นต่ำของเส้นสาขา
--branch-min-km     (default 1.5)  ความยาวขั้นต่ำของเส้นสาขาเท่านั้น
                                   (flow paths หลักยังใช้ --min-flow-km 1.0)
--polygon-burn-depth(default burn-5) depth burn water polygon
--no-branches                      ปิดการสร้างเส้นสาขา
--no-osm-layer                     ปิด OSM river display layer
```

## Big-O / RAM สรุป (ข้อจำกัด: คง max_cells=150M, คง 12.5m)

| จุด | เดิม | ใหม่ |
|---|---|---|
| Elevation ต่อ OSM vertex | pyproj per-vertex (ช้า) + cache ไม่จำกัด | batch vectorized ต่อ feature, cache cap 500k |
| Branch extraction | — | O(K) BFS+walk ต่อสถานี, visited mask reuse, reset O(K) |
| Dijkstra cache | โตไม่จำกัดตามจำนวน entry node | cap 256 entries (clear เมื่อเกิน) |
| Entry detection | STRIDE=8 ครั้งเดียว | +≤8 cKDTree query (O(log N)) ต่อสถานี |
| GeoJSON | — | features เพิ่มจาก segments + branches (คาด ~2-4x), compact JSON เดิม |

## G — ลดขนาด output 178MB → ≤4MB (gzip) (เพิ่มเติมรอบที่สาม)

สาเหตุไฟล์โต: OSM layer ครบ (~12-30MB) + เส้นสาขา 394 สถานีถือสำเนาซ้ำ + ละเอียดระดับ cell
(การตัดสินใจของผู้ใช้: OSM เก็บครบในไฟล์เดียว, simplify คง 35m, ยอมใช้ gzip)

- **G1** ✅ Dedupe เส้นสาขา: hash เรขาคณิต (round 5dp) — เส้นเดียวกันที่หลายสถานีถือ
  เก็บ 1 feature + `shared_with: [...]`, จัด id ใหม่หลัง dedupe — O(total points)
- **G2** ✅ `--branch-max-count` (default 30): ต่อสถานีเก็บ branch ยาวสุด N เส้นก่อน dedupe
- **G3** ✅ เขียนทั้งสองไฟล์ (serialize ครั้งเดียว): `flow_paths.geojson` (raw) **และ**
  `flow_paths.geojson.gz` (level 9, mtime=0) — `--no-gzip` ปิดได้; helper
  `write_geojson_pair` ใน gis_utils
- **G4** ✅ Size report ตอนจบ generate (features/points ต่อ type + MB จริงทั้ง 2 ไฟล์);
  `validate_flow_paths.py` อ่าน .gz ได้ (sniff magic bytes) + รายงาน points ต่อ type
- **G5** ✅ tests: R1+R2 ใน catchment เดียวกัน → branch เดียวกันถูก dedupe เป็น 1 feature
  พร้อม `shared_with`, ไม่มี geometry ซ้ำเหลือ; cap=1 ทำงาน (test_branch_cap_and_dedupe)

ประมาณการผล: raw ~16-30MB, `.gz` ~2.5-4MB เลเวอร์สำรองถ้ายังเกิน:
`--branch-max-count` ลดลง → `--branch-min-acc` เพิ่ม → (สุดท้าย) แยก OSM layer ออกไฟล์

## H — ความยาวขั้นต่ำเส้นสาขา 1.5km (รอบที่สี่)

- **H1** ✅ `--branch-min-km` (default **1.5**) แยกจาก `--min-flow-km` (1.0):
  กรองเฉพาะ `extract_station_drainage_branches` ผ่าน param `branch_min_km` —
  Case 1/2/3 ของ flow path หลักยังกรอง 1.0km เหมือนเดิมทุกจุด
- **H2** ✅ tests: e2e ทดสอบ wiring ด้วย branch_min_km=1.0 (synthetic basin มี
  branch fragment ยาวสุด ~1.5km พอดี) + `test_branch_min_km_default` ตรวจ
  default 1.5/1.0 ผ่าน signature introspection

## ลำดับดำเนินการ (รวมทุกรอบ)

B1–B3 → B4–B5 → A1–A6 → D1–D4 → E1–E4 → CLI wiring → C1 → C2 → F1–F3 →
diagnostics/escalation → G1–G5 → recheck เทียบไฟล์นี้
