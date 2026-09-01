  # PLAN — Flow Path Fix รอบ 4 (v2): OSM-as-Base + Real Polygon Clip + แก้ snap ผิดลำธาร

> ไฟล์นี้เป็นแผนแก้ไขรอบถัดไป ต่อจาก `SUMMARY_FLOW_PATH_FIX.md` (รอบ 1-3)
> ปัญหาที่ผู้ใช้รายงานหลังรอบ 3: (1) เส้น gauge_to_gauge จากสถานี 8894 ยังผูกกับ osm_river 318801683
> (2) เส้นในแผนที่ยังไม่ถูก crop ตามขอบเขตจริง — ยังเป็นกรอบ 4 เหลี่ยมอยู่
> หลักการที่ผู้ใช้กำหนด: **OSM เป็น base, ต้อง crop OSM ด้วยขอบเขตจริงก่อนเอาเข้า process,
> ตัว river layer ห้ามถูกตัดใด ๆ ใน output, เลเยอร์อื่นตัดได้แต่ต้องใช้ filter แบบจริง (polygon) ไม่ใช่สี่เหลี่ยม**

---

## จุดประสงค์ (Objectives)

1. **ความถูกต้องเชิงพื้นที่** — ทุกชั้นข้อมูล (OSM, DEM, flow paths) ต้องอยู่ในกรอบลุ่มน้ำจริง (polygon ลุ่ม ThaiWater) ตั้งแต่ต้น pipeline ไม่ใช่กรอบสี่เหลี่ยมกั้นหยาบ ๆ
2. **OSM เป็น base ของการเดินเส้น** — backbone แม่น้ำเป็นเป้าหมายการแนบปลายเส้นเสมอ โดยเลือก "สายหลักที่ไปถึง gauge ได้" ไม่ใช่ลำธารเกาะใกล้สุด
3. **ความซื่อสัตย์ของเลเยอร์ (layer integrity)** — `osm_river` ส่งออกเหมือนที่ crop ไว้ทุกจุด (ไม่ถูกตัดซ้ำ), เลเยอร์อื่นถูก clip ด้วย polygon จริง, ทุก filter ตรวจสอบย้อนได้ (log + validator)
4. **หายเป็นระบบ ไม่แก้จุด** — ปิด fallback ที่เงียบ ๆ ทำให้ผลผิด (station-bbox, WARN-แล้วรันต่อ) และกันด้วย validator/test ไม่ให้กลับมาซ้ำ
5. **คงคุณภาพข้อมูลตั้งต้น** — ทำทั้งหมดโดย**ไม่ลดความละเอียดของ DEM** (ไม่ downsample/coarsen raster ใด ๆ) และควบคุม Big-O / เวลารัน / RAM ให้อยู่ในงบประมาณเดิมหรือดีกว่าเดิม (ดู Section 7)

---

## 1. Root Cause ที่วิเคราะห์เจอ (พร้อมหลักฐานจากของจริง)

### RC1 — ไม่มี basin boundary เลย → ระบบ fallback เป็นกรอบสี่เหลี่ยมทั้ง pipeline
- `dataset/nan/gis/` และ `dataset/yom/gis/` **ว่างเปล่า** — ไม่มี `nan_boundary.geojson` อยู่บนดิสก์เลย
- `generate_flow_paths.py:100-110` — ถ้าไฟล์ boundary ไม่มีและ `fetch_basin_boundary()` โหลดไม่สำเร็จ
  → แค่ `[WARN]` แล้วตั้ง `boundary_geojson = None` **แล้วรันต่อเฉย ๆ**
- ผลที่ตามมา 2 ทางพร้อมกัน:
  - Query OSM ตกไปใช้ fallback **station bbox + buffer (สี่เหลี่ยม)** (`_build_overpass_query`, `station_bbox_buffer_deg=0.35`)
  - Basin clip ถูกข้ามทั้งหมด (`basin_poly = None` → `graph_topology.py:2575` ไม่ทำอะไรเลย)
- **หลักฐานยืนยัน**: extent ของ `osm_river` ใน `flow_paths(18).geojson` = `[99.5028, 15.5431, 101.5238, 19.8966]`
  — ตรงกับ station bbox ของสถานีลุ่มน่าน + buffer เป๊ะ (กว้าง ~2° x ยาว ~4.3°) = สี่เหลี่ยมที่เห็นในแผนที่
- ตัว `fetch_basin_boundary` ไปโหลด `https://www.thaiwater.net/json/boundary/basin.json` — ไม่มี fallback ท้องถิ่น,
  เน็ตล่ม/ถูกบล็อก = pipeline ทั้งระบบเดินสี่เหลี่ยมโดยไม่มีใครรู้ตัว (มีแค่ WARN บรรทัดเดียว)

### RC2 — Fallback snap ปลายเส้นติด "ลำธารเกาะเล็ก" โดยไม่ดูความสำคัญของ way
- เคส 8894: flowpath จบที่ `[101.36376, 17.42201]` และ osm_river `318801683` (stream, ยาว 1.95km,
  **topology island / singleton**) เริ่มที่ `[101.36073, 17.42226]` — ห่างกัน ~300m บนแผนที่ดูเหมือนเส้นไขว้/ต่อกัน
- กลไกที่ทำให้เกิด (จากรอบ 2): backward attach / `snap_point_to_graph` เลือก **จุดใกล้สุด** บน edge ใดก็ได้
  — ไม่เรียงลำดับความสำคัญ (river/waterway class, ความยาว, มี gauge downstream-reachable หรือไม่)
  ลำธารเล็กที่โดดเดี่ยวจึงชนะได้ ทั้งที่ต่อไปไหนไม่ได้
- props ของ flowpath 8894 ไม่มี `routing` / `attach` metadata ใด ๆ → debug ยาก, validator จับไม่ได้
- `downstream_elev_m = -45.0` — ปลายเส้นจบใน nodata/burn artifact ไม่ใช่น้ำจริง (สัญญาณว่าจบผิดที่)

### RC3 — osm_river ในไฟล์เก่ามาเกินจำเป็น
- 7,980 features osm_river ครอบสี่เหลี่ยมใหญ่ (RC1) — หลายเส้นอยู่นอกลุ่มน่านไกล
  ทั้งที่ถ้า crop ด้วย polygon จริงตั้งแต่ต้นจะเหลือเฉพาะที่อยู่ในลุ่ม → ไฟล์เล็กลงมากและ process เร็วขึ้น

---

## 2. เป้าหมาย (ตามโจทย์ผู้ใช้)

| # | เป้าหมาย | เกณฑ์ผ่าน |
|---|---|---|
| G1 | **Crop OSM ด้วย polygon ลุ่มจริง ก่อนเข้า process** | OSM ทั้ง lines และ polygons ถูกตัดด้วย `{basin}_boundary.geojson` ตั้งแต่ fetch/load; ห้ามมี fallback สี่เหลี่ยมแบบเงียบ ๆ |
| G2 | **osm_river layer ไม่ถูกตัดใด ๆ ใน output** | เส้น osm_river ในไฟล์ output = ตรงกับ OSM ที่ crop ไว้ตอนต้นทุกเส้นทุกจุด (`basin_clipped` ห้ามขึ้นบน osm_river) |
| G3 | **เลเยอร์อื่นตัดได้ แต่ต้องใช้ filter แบบจริง** | flow paths / branches ถูก clip ด้วย polygon (concave) ของลุ่ม — ไม่มีการใช้ bbox สี่เหลี่ยมในการตัดอีกต่อไป |
| G4 | **แก้เคส 8894 ↔ 318801683** | ปลาย flowpath ต้องแนบ "แม่น้ำสายหลักที่ไปถึง gauge ได้" เท่านั้น — ห้ามแนบ topology island stream เมื่อมีตัวเลือกที่ดีกว่า; จุดแนบต้องอยู่บนเส้นแม่น้ำจริง (≤ tolerance) |
| G5 | Boundary เป็นสิ่งบังคับ | ถ้าไม่มี boundary → pipeline **fail fast** พร้อมวิธีแก้ใน message (ไม่ใช่ WARN แล้วรันผิด ๆ ต่อ) |
| G6 | **มี Flow Layer Filter ชัดเจนต่อ layer** | ทุก feature_type มี filter chain ของตัวเอง กำหนดเป็นเอกสาร + บังคับในโค้ด + ตรวจใน validator (ดู Section 2A) |

---

## 2A. Flow Layer & Filter Matrix (ของใหม่ใน v2)

หลักการ: filter กระจายเป็น **3 จังหวะ** — (1) `SOURCE` = ตอน fetch/crop OSM ก่อนเข้า process,
(2) `PROCESS` = ตอนสร้างเส้น (routing/branch), (3) `OUTPUT` = ก่อนเขียนไฟล์ (clip/ตรวจสภาพเส้น)
ปัจจุบัน filter กระจัดกระจายฝังใน code path และ osm_river แทบไม่มี filter เลย (มาทุกเส้นในสี่เหลี่ยม) — v2 จะกำหนดเป็นตารางเดียวและบังคับจริง

| layer (`feature_type`) | SOURCE filter | PROCESS filter | OUTPUT filter |
|---|---|---|---|
| `osm_river` | **crop ด้วย basin polygon + `--crop-buffer-m` (2km)** — ทั้ง lines/polygons ตั้งแต่ fetch (Phase 2) | ไม่กรองความยาว/คลาส (เก็บครบตามที่ผู้ใช้ยืนยัน) + สร้าง backbone graph จากชุดที่ crop แล้วเท่านั้น | **ไม่ clip ไม่แก้ geometry ใด ๆ** (Phase 3) — เขียนตรงจาก crop-set; ห้ามมี `basin_clipped` |
| `gauge_to_gauge_flowpath` | — | `--min-flow-km` (1.0) · `--overland-max-km` (5.0) เฉพาะท่อนบนบกล้วน · ปลายเส้นต้องแนบ backbone ที่ **ผ่าน candidate ranking** (Phase 4) | clip ด้วย basin polygon (เก็บท่อนต่อจาก from-station) + จุดปลายต้องอยู่บน osm_river/gauge (≤30m) ไม่งั้น drop/WARN |
| `rainfall_to_gauge_flowpath` (cascade segments) | — | `--rain-cascade-km` (60) cap ระยะบน backbone · ทุก segment ≥ `--min-flow-km` · ลำดับ entry→G1→G2… ไม่ทับซ้อน | clip ด้วย basin polygon ต่อ segment |
| `rainfall_drainage_branch` | — | `--branch-min-acc` (500, auto-escalate ×4) · `--branch-min-km` (1.0) · `--branch-max-cells` (400k) · `--branch-max-count` (30, ยาวสุดก่อน) · dedupe `shared_with` | clip ด้วย basin polygon (ยกเว้นท่อน river_merge ที่จบบนแม่น้ำ) |

**กติกาที่บังคับทุก layer** (ใหม่):
- **F1** ทุก filter ที่ apply แล้วต้องเกิด log สรุปต่อ layer: `n_in → n_out` + เหตุผลที่ drop (min_km / clip / dedupe / outside-basin)
- **F2** ลำดับการทำงานตายตัว: `crop OSM (SOURCE)` → `routing/branches (PROCESS)` → `per-layer filter` → `basin-polygon clip (OUTPUT, ยกเว้น osm_river)` → `write`
- **F3** ห้ามมี filter ใดใช้ **station-bbox สี่เหลี่ยม** ทั้งใน SOURCE และ OUTPUT — ถ้า boundary ไม่มีให้ fail ตาม G5 ไม่ใช่ fallback
- **F4** validator ต้องตรวจว่าตารางนี้ถูก apply จริง (ดู Phase 5 ข้อ 16)

**สิ่งที่ไม่ใช่ filter แต่ต้องไม่หลงลืม**: `--no-osm-layer` / `--no-branches` / `--no-gzip` / `--no-basin-clip` ยังเป็น switch ปิดทั้ง layer ได้เหมือนเดิม (จะถูกถือเป็น "filter ระดับ layer" ใน log F1 ด้วย)

---

## 3. แผนแก้ไข

> ทุกเฟสด้านล่างต้องเป็นไปตามข้อจำกัดใน **Section 7 (Big-O / Performance / RAM)** —
> โดยเฉพาะ: ห้ามลดความละเอียด DEM, งานค้นหาต้องผ่าน index, งานหนัก ๆ ต้อง amortize ลง cache

### Phase 1 — Boundary บังคับ + ไม่มีสี่เหลี่ยมอีกแล้ว (แก้ RC1)

**ไฟล์: `scripts/fetch_basin_gis.py`**
1. `fetch_basin_boundary()`: เพิ่ม fallback chain
   - 1) ไฟล์ท้องถิ่น `{basin}_boundary.geojson` (มีอยู่แล้วใช้เลย)
   - 2) โหลด thaiwater.net (เดิม)
   - 3) โหลดจาก **subbasins** ที่เคยดาวน์โหลดไว้ → dissolve เป็น polygon เดียว
   - 4) โหลดจาก OSM boundary relation (Nominatim/Overpass `boundary=administrative` ของจังหวัดในลุ่ม) → union
   - ทุกทางล้มเหลว → raise exception (ไม่ return None เงียบ ๆ)
2. ตรวจ geometry ที่ได้ต้องเป็น (Multi)Polygon จริง + มีจุดมากพอที่จะ concave (เช่น ≥ 50 vertices;
   ถ้าน้อยกว่านั้น = น่าจะเป็นกรอบหยาบ → WARN)

**ไฟล์: `scripts/generate_flow_paths.py`**
3. ถ้า boundary ไม่มี/โหลดไม่ได้ → **exit พร้อมคำแนะนำ** ("รัน `fetch_basin_gis.py --basin X` ก่อน")
   — ลบพฤติกรรม WARN-แล้วรันต่อ (เกณฑ์ G5)
4. ถ้า OSM cache เดิมถูกสร้างด้วย `source_label == "station_bbox"` (เช็คจาก cache `_meta`) →
   บังคับ refetch อัตโนมัติเมื่อมี polygon แล้ว (cache fingerprint ครอบอยู่แล้ว — แค่ทำให้ meta บันทึก source และเทียบ)

### Phase 2 — Crop OSM ด้วย polygon ก่อนเข้า process (แก้ G1/RC3)

**ไฟล์: `scripts/fetch_basin_gis.py`**
5. หลัง Overpass ตอบกลับ (ไม่ว่า query จะใช้ poly: หรือ bbox) → **crop ทุก way ด้วย shapely กับ basin polygon
   (+ buffer ~2km กันขอบหลุดไฮโดร)** ก่อนเขียน cache เสมอ: เส้นที่อยู่นอกลุ่มทั้งหมด drop,
   เส้นที่ไขว้ขอบเก็บท่อนในลุ่ม (ใช้ helper เดียวกับ `_clip_line_to_basin`)
6. ทำแบบเดียวกันกับ `fetch_osm_water_polygons`
7. บันทึกลง cache `_meta`: `crop_polygon: "<fingerprint ของ boundary>"` — ถ้าเปลี่ยน boundary → refetch/recrop

**ไฟล์: `scripts/generate_flow_paths.py` / `modules/terrain_engine.py`**
8. ตอนโหลด OSM cache: ถ้า `_meta` ไม่มี `crop_polygon` (แคชเก่า) → crop ตอน load แล้ว rewrite cache
   (idempotent, ไม่ต้องรอ user รัน force-osm)
9. DEM clip (`clip_dem_to_polygon`) ใช้ polygon จริงอยู่แล้ว — ตรวจว่า buffer สอดคล้องกับตัว crop OSM

### Phase 3 — osm_river ไม่โดนตัดใน output (แก้ G2)

**ไฟล์: `scripts/modules/graph_topology.py`** (บล็อก basin clip ~บรรทัด 2571-2600)
10. ข้าม feature ที่ `feature_type == "osm_river"` ทั้งหมด — เพราะ OSM ถูก crop ด้วย polygon จริงตั้งแต่ Phase 2 แล้ว
    (output layer = ตัวที่ crop แล้วเป๊ะ ๆ, ไม่มีการตัดซ้ำ ไม่มี `basin_clipped: true`)
11. flow paths / branches ยังถูก clip ต่อด้วย `_clip_line_to_basin` (polygon จริง — เกณฑ์ G3)

### Phase 4 — Snap ปลายเส้นให้ฉลาดขึ้น (แก้ RC2 / G4)

**ไฟล์: `scripts/modules/graph_topology.py`**
12. **Candidate ranking** ตอนแนบปลาย D8/fallback ลง backbone (`snap_point_to_graph` + backward attach):
    ให้คะแนน edge ผู้สมัครแบบถ่วงน้ำหนัก แทน "ใกล้สุดชนะ":
    - `+class`: river/canal ได้คะแนนสูงกว่า stream มาก (อ่านจาก tag `waterway` ของ way)
    - `+connectivity`: way ที่มี gauge อยู่ downstream-reachable บน backbone = คะแนนหลัก
      (island ที่ไม่มี gauge ใดถึง = คะแนนติดลบ → ใช้ได้เมื่อไม่มีตัวเลือกอื่นในรัศมีเลย)
    - `+length`: way ยาวได้คะแนนมากกว่า (stream 1.95km ตัดคะแนน)
    - `-distance`: ระยะยังใช้เป็นตัวถ่วง แต่ไม่ใช่ตัวตัดสินเดี่ยว
13. **ปรับ radii ให้ไล่ลำดับ**: ค้นรัศมีใกล้ก่อนเฉพาะ edge ที่ "ผ่านเกณฑ์คุณภาพ" (มี gauge downstream / class สูง);
    ถ้าไม่เจอค่อยขยายรัศมีรับ edge รอง แล้ว tag `attach_quality: "degraded"` ไว้ให้ validator ตรวจ
14. **จุดแนบต้องอยู่บนเส้นจริง**: project จุดปลายลง edge ที่เลือกเสมอ (split ที่ projection —
    `snap_point_to_graph` ทำอยู่แล้ว) และต่อ connector สั้นสุด — ปลาย flowpath กับเส้น osm_river
    ต้องชนกันจริง (≤ ~30m) ไม่ใช่ลอยห่าง 300m
15. **ทุก flowpath ใส่ attach metadata**: `attach_osm_id`, `attach_distance_m`, `attach_quality`,
    `attach_class` — debug ง่าย + validator อ่านได้

### Phase 5 — Validator + Tests (กันเหลื่อมซ้ำ)

**ไฟล์: `scripts/validate_flow_paths.py`**
16. เช็คใหม่:
    - `osm_river` ต้องไม่มี `basin_clipped` และ geometry ตรงกับ crop-set (สุ่มเทียบ hash)
    - ปลาย flowpath ทุกเส้น: อยู่ที่ gauge/outlet หรือแนบบนเส้น osm_river (≤ 30m) — ลอยห่าง = FAIL
    - แนบ topology island ที่ไม่มี gauge downstream โดยไม่มี `attach_quality: "degraded"` = FAIL
    - ตรวจว่า `flow_paths` ไม่มีจุดอยู่นอก basin polygon (เมื่อมี boundary ให้อ่าน)
    - **ตรวจ Filter Matrix (Section 2A/F4)**: อ่าน log/meta สรุป `n_in → n_out` ต่อ layer —
      layer ไหนไม่มีรายงาน filter หรือ osm_river มี `basin_clipped` = FAIL;
      ตรวจว่า osm_river ทุกเส้นมาจาก crop-set (hash ตรงกับ meta) และ flow/branch ทุกจุดอยู่ใน polygon
17. พิมพ์สรุป `source_label` ของ OSM query ที่ใช้ — ถ้าเป็น `station_bbox` ให้ FAIL ทันที (ห้ามสี่เหลี่ยม)

**ไฟล์: `tests/test_flow_topology.py`**
18. เพิ่ม synthetic tests:
    - clip branch/flowpath ด้วย polygon concave แต่ **osm_river ไม่ถูกแตะ**
    - candidate ranking: ปลายเส้นใกล้ stream เกาะเล็ก 50m และใกล้แม่น้ำหลัก 120m → ต้องเลือกแม่น้ำหลัก
    - ปลายเส้นถูก project ลงบนเส้นแม่น้ำที่เลือกจริง
    - boundary หาย → generate ต้อง exit ด้วย error (ไม่รันต่อ)

---

## 4. ลำดับการรัน (เครื่องที่มี data — ลุ่มน่าน)

```bash
# 1) สร้าง boundary ให้ได้ก่อน (ต้องผ่าน — ถ้า fail ให้แก้ fallback chain)
python scripts/fetch_basin_gis.py --basin nan

# 2) ดึง OSM ใหม่ + crop ด้วย polygon (cache เดิมเป็นสี่เหลี่ยม → ต้อง refetch)
python scripts/fetch_basin_gis.py --basin nan --force-osm

# 3) generate
python scripts/generate_flow_paths.py --basin nan --force

# 4) ตรวจ
python scripts/validate_flow_paths.py --geojson dataset/nan/processed/flow_paths.geojson.gz
```

**เช็คด้วยตาด้วย 3 จุด:**
- 8894: ปลายเส้นต้องจบบนแม่น้ำหลักที่ไปต่อ gauge ได้ — ไม่ใช่ stream 318801683
- ขอบลุ่ม: เส้น flow/branch หักตามขอบ polygon (concave) ไม่ใช่ตัดเป็นแนวตรงสี่เหลี่ยม
- osm_river: เต็มทุกเส้นในลุ่ม ไม่มีรอยตัดเทียม

---

## 5. ความเสี่ยง / หมายเหตุ

| ความเสี่ยง | การรับมือ |
|---|---|
| thaiwater.net โหลดไม่ได้อีก | fallback chain 3 ทาง (Phase 1.1) + fail fast ชัดเจน |
| Buffer ตอน crop OSM ตัดหัวท้ายลุ่มจน routing ขาด | buffer 2km ที่ขอบ + ตั้งเป็น CLI `--crop-buffer-m` ปรับได้ |
| Candidate ranking ทำให้เคสที่เคยถูกต้องเปลี่ยนไป | ใส่ `attach_quality` + รัน validator เทียบเคสเดิม (1394921, 1113284/1483144) ต้องยังผ่าน |
| แคช OSM เก่า (สี่เหลี่ยม) ถูกใช้ต่อโดยไม่รู้ตัว | `_meta` fingerprint + auto refetch (Phase 1.4, Phase 2.8) |

## 6. ไฟล์ที่จะแก้ทั้งหมด

| ไฟล์ | เนื้อหา |
|---|---|
| `scripts/fetch_basin_gis.py` | Phase 1 (fallback chain, fail fast) + Phase 2 (crop ตอน fetch, meta) |
| `scripts/generate_flow_paths.py` | Phase 1 (boundary mandatory) + Phase 2 (crop ตอน load) |
| `scripts/modules/graph_topology.py` | Phase 3 (skip osm_river ใน clip) + Phase 4 (candidate ranking, attach metadata) |
| `scripts/validate_flow_paths.py` | Phase 5 (เช็คใหม่ 4 ข้อ) |
| `tests/test_flow_topology.py` | Phase 5 (test ใหม่ 4 เคส) |
| `SUMMARY_FLOW_PATH_FIX.md` | อัปเดตสรุปรอบ 4 หลังทำเสร็จ |

---

## 7. Big-O / Performance / RAM (ข้อจำกัดบังคับ)

**ข้อห้ามเด็ดขาด**: ทุกเฟสต้องคงความละเอียด DEM เดิม (native 12.5m) — ห้าม downsample, ห้าม aggregate cell,
ห้าม coarsen grid เพื่อเร่งงาน ทุกการเร่งทำได้เฉพาะทาง **อัลกอริทึม/โครงสร้างข้อมูล** เท่านั้น
(ส่วนที่เป็น vector เช่น STRIDE=8 ในการ scan หาจุดต่อ เป็นพารามิเตอร์การค้นหา ไม่เกี่ยวกับ resample raster — คงเดิม)

### 7.1 ความซับซ้อนของงานใหม่ต่อเฟส

| เฟส | งาน | กลยุทธ์ | Big-O | รันเมื่อไหร่ |
|---|---|---|---|---|
| P1 | ตรวจ boundary + fallback chain | อ่านไฟล์/โหลดครั้งเดียว | O(V) ต่อ polygon | 1 ครั้งต่อรัน |
| P2 | Crop OSM ด้วย polygon ตอน fetch | `shapely.prepared` polygon + คัดเฉพาะ way ที่ bbox ตัดกับลุ่มก่อน (STRtree / bbox pre-filter) แล้วค่อย `intersection` | คัดเบื้องต้น O(N) ที่ถูกมาก (เทียบ bbox) → intersection เฉพาะตัวที่ผ่าน O(K · log V), K ≪ N | **ครั้งเดียวต่อ cache** (fetch/recrop) — ต้นทุนถูก amortize เพราะเขียนลง cache พร้อม fingerprint |
| P2 | Recrop แคชเก่าตอน load | ทำเหมือนข้างบน แล้ว rewrite cache + เขียน meta | เดียวกัน | ครั้งเดียวจนกว่า boundary เปลี่ยน |
| P3 | ข้าม osm_river ใน clip | เป็น **การลบงาน** ไม่ใช่เพิ่ม — osm_river ~8k features เดิมเข้า `LineString.intersection` ทุกเส้น | ลดลง O(N_osm · clip) → 0 | ทุกรัน (ได้เวลาคืน) |
| P4 | Candidate ranking ปลายเส้น | **precompute ครั้งเดียวต่อ graph**: gauge-downstream-reachable ต่อ component ด้วย union-find/DFS ที่มีอยู่แล้วใน `finalize_connectivity` → เก็บ flag ต่อ edge; ตอน attach ใช้ spatial grid index เดิมหา candidate ในรัศมีแล้ว score O(K) ต่อจุด | precompute O(E α(V)) ครั้งเดียว + attach query O(K) ต่อ flowpath (K = candidate ในรัศมี, สิบ ๆ ตัว) | precompute 1 ครั้ง/รัน, query ต่อ flowpath ~176-900 เส้น |
| P4 | Project ปลายเส้นลง edge | split ที่ projection ผ่าน `snap_point_to_graph` เดิม (มี grid index แล้ว) | O(K) ต่อจุด | ต่อ attach |
| P5 | Validator จุดนอก polygon + ปลายเส้นบนเส้น | prepared polygon (vectorized `contains_xy`/`intersects` ถ้ามี shapely 2) + **bucket พิกัดเส้น osm_river ลง spatial grid** แบบ `water_grid_map` เดิม → ค้นจุดใกล้ O(1) เฉลี่ย | O(P) จุดทั้งหมด, ไม่มีการเทียบ cross-product P×R | 1 ครั้งต่อ validate |
| P5 | hash เทียบ osm_river ↔ crop-set | hash เรขาคณิต 5dp แบบเดียวกับ dedupe (G1) | O(total points) | 1 ครั้ง |

**สรุปงบเวลา**: ต้นทุนใหม่จริง ๆ คือ crop (ครั้งเดียวต่อ cache) + precompute reachability (ครั้งเดียวต่อรัน) —
ส่วนที่เหลือเป็นการแทนที่งานเดิมที่แพงกว่า (clip osm_river ทิ้ง = เร็วขึ้น) และค้นหาแบบ index ที่ O(1)/O(K) เฉลี่ย

### 7.2 งบ RAM (ไม่ลดความละเอียด DEM)

| ตัวแปร | ปัจจุบัน | ทางแก้/ข้อกำหนด v2 |
|---|---|---|
| DEM grids (fdir, acc, slope, river_mask) | เต็มความละเอียด ทุกตัว 1 array เต็ม raster | **คงเดิมทั้งขนาดและ dtype**: fdir/acc int32, slope float32, river_mask → **bool/uint8** (ปัจจุบันถ้าเป็น float ให้เปลี่ยน — ลด 4-8 เท่าทันทีโดยไม่แตะ resolution) |
| สำเนา raster ชั่วคราว | burn DEM อาจ clone array | ทำ **in-place** ที่ทำได้ + `del` ทุก temp หลังใช้, หลีกเลี่ยง `np.gradient` ซ้ำ (slope cache ครั้งเดียว) |
| Overpass JSON ดิบ | เก็บ dict ทั้งก้อนระหว่าง crop | crop แบบ **สร้าง list ใหม่แล้วปล่อยอันเก่าทันที** (`elements` เดิม del หลังแปลง) — peak = เก่า+ใหม่ช่วงสั้น ๆ ครั้งเดียวต่อ cache |
| OSM graph (edges + geometry) | ~8k ways | คงเดิม; flag reachable เก็บเป็น `np.bool_`/int8 ต่อ edge ไม่ใช่ dict ต่อ node ถ้าเลี่ยงได้ |
| Elevation cache ต่อ feature | cap 500k จุด (มีอยู่แล้ว) | คงเดิม |
| Peak คาดการณ์ | — | ต้อง **ไม่เกินเดิม** (งาน raster เป็นตัวกำหนด peak อยู่แล้ว); ถ้า crop/recrop ทำให้ peak ขยับ ให้ crop ทีละ chunk (คัดจาก bbox ก่อนแล้วค่อยแปลง) เพื่อไม่แปลงพร้อมกันทั้งก้อน |

### 7.3 เกณฑ์ตรวจรับด้าน performance

- เวลารัน `generate_flow_paths.py` รอบใหม่ ≤ รอบเดิม +10% (ยกเว้นรันแรกที่ต้อง crop/recrop cache — อนุญาตช้ากว่าได้ครั้งเดียว)
- Peak RAM วัดได้ (เช่น `time -v` / `resource.ru_maxrss`) ≤ รอบเดิม
- DEM ที่ใช้ทุกขั้นตอนมี shape/dtype เดิม — ใส่ assert/log ขนาด raster ตอนต้น pipeline เพื่อพิสูจน์ว่าไม่ถูก resample
