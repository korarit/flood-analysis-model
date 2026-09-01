# ข้อกำหนดข้อมูลระหว่าง Model และ Backend (Backend Interface Requirements)
> **เอกสารอ้างอิงหลัก**: [`water-analysis-backend/api-req.md`](file:///e:/water-analysis-project/water-analysis-backend/api-req.md) และ [`water-analysis-frontend/src/components/station/StationRelations.tsx`](file:///e:/water-analysis-project/water-analysis-frontend/src/components/station/StationRelations.tsx)  
> **Target Subsystem**: `water-analysis-model`  
> **Consumer Subsystem**: `water-analysis-backend` (ส่งต่อไปยัง Cloudflare R2 และ Frontend)

เอกสารนี้ระบุ **โครงสร้างข้อมูลจริงที่ระบบ Backend ต้องการจากโมเดลอุทกวิทยา (`water-analysis-model`)** เพื่อนำไปบันทึกลงฐานข้อมูล `station_relations` และสร้างไฟล์ `relations.json` ตามที่ Frontend ต้องการแสดงผล

---

## 1. ภาพรวมการเชื่อมโยงข้อมูล (Architecture & Data Flow)

```text
┌─────────────────────────────────────────────────────────────┐
│                    water-analysis-model                     │
│    (คำนวณระยะทาง, เวลาน้ำหลากเดินทาง, และน้ำหนักอิทธิพลฝน)    │
└──────────────────────────────┬──────────────────────────────┘
                               │
            1. List สถานีฝนต้นน้ำที่มี Effect ต่อสถานีน้ำ
            2. List สถานีน้ำท้ายน้ำที่รับผลกระทบ
            3. ค่าเวลาเดินทาง (travelTimeHours / lagTimeHours)
                               ▼
┌─────────────────────────────────────────────────────────────┐
│                   water-analysis-backend                    │
│                                                             │
│  • บันทึกลงตาราง Database `station_relations`               │
│  • ผสานกับค่าโทรมาตรสด (`telemetry_latest`)                │
│  • นำ `travelTimeHours` ไปสร้าง Alert Reason อัตโนมัติ       │
│  • Publish ขึ้น Cloudflare R2:                              │
│    `/basin/{slug}/stations/{id}/relations.json`             │
└──────────────────────────────┬──────────────────────────────┘
                               │
                               ▼
┌─────────────────────────────────────────────────────────────┐
│                   water-analysis-frontend                   │
│                                                             │
│  • แสดงใน Component `StationRelations.tsx`                   │
│    - การ์ดสถานีฝนต้นน้ำ (`influencingStations`)              │
│    - การ์ดสถานีน้ำท้ายน้ำ (`downstreamStations`)             │
│    - ป้ายแสดงเวลาน้ำหลากเดินทาง `~X ชม.`                     │
└─────────────────────────────────────────────────────────────┘
```

---

## 2. โครงสร้างตารางใน Database Backend (`station_relations`)

Backend ได้สร้างตาราง PostgreSQL รองรับผลลัพธ์จาก Model ไว้ดังนี้:

```typescript
// water-analysis-backend/src/db/schema/stationRelations.ts
export const stationRelations = pgTable("station_relations", {
  id: bigserial("id", { mode: "number" }).primaryKey(),
  stationId: varchar("station_id", { length: 64 }).notNull(),          // รหัสสถานีหลัก (เช่น "8892" หรือ "Y-0014")
  targetStationId: varchar("target_station_id", { length: 64 }).notNull(), // รหัสสถานีที่ผูกโยง (เช่น "133528", "Y-0020")
  relationType: varchar("relation_type", { length: 32 }).notNull(),    // 'rainfall_influence' | 'downstream_gauge' | 'tributary'
  distanceKm: doublePrecision("distance_km"),                          // ระยะทางตามแนวลำน้ำ (กม.)
  travelTimeHours: doublePrecision("travel_time_hours"),               // เวลาน้ำหลากเดินทาง (ชม.) จาก Model
  travelTimeHoursMin: doublePrecision("travel_time_hours_min"),       // เวลาเดินทางขั้นต่ำ (ชม.)
  travelTimeHoursMax: doublePrecision("travel_time_hours_max"),       // เวลาเดินทางสูงสุด (ชม.)
  influenceWeightPercent: doublePrecision("influence_weight_percent"), // % น้ำหนักอิทธิพล เช่น 45.0%
  isUpstream: boolean("is_upstream").default(true).notNull(),          // true = ต้นน้ำ, false = ท้ายน้ำ
  notes: text("notes"),
  createdAt: timestamp("created_at").defaultNow().notNull(),
  updatedAt: timestamp("updated_at").defaultNow().notNull(),
});
```

---

## 3. ข้อมูลที่ Model ต้องจัดเตรียมให้ Backend (Data Contracts)

### 3.1 ข้อมูลเครือข่ายความสัมพันธ์สถานี (Station Relations List)
> **ตรงตาม `api-req.md` §4.8 (`/basin/{basin}/stations/{station_id}/relations.json`)**

โมเดลต้องระบุ **List รายชื่อสถานีวัดน้ำฝนต้นน้ำที่มีผลต่อสถานีวัดระดับน้ำแต่ละแห่ง** และ **สถานีระดับน้ำท้ายน้ำ**

#### Schema ที่ต้องการ:
```typescript
interface StationRelationPayload {
  stationId: string;                 // รหัสสถานีหลัก (เช่น "Y-0014")
  targetStationId: string;           // รหัสสถานีที่สัมพันธ์ (เช่น "P-0004", "Y-0003A")
  relationType: "rainfall_influence" | "downstream_gauge" | "tributary";
  distanceKm: number;                // ระยะทางตามแนวลำน้ำ (กิโลเมตร)
  travelTimeHours: number;           // เวลาที่มวลน้ำเดินทางระหว่าง 2 สถานี (ชั่วโมง)
  travelTimeHoursMin?: number;       // เวลาเดินทางเร็วสุดกรณีน้ำหลากแรง (ชั่วโมง)
  travelTimeHoursMax?: number;       // เวลาเดินทางช้าสุด (ชั่วโมง)
  influenceWeightPercent: number;    // % น้ำหนักอิทธิพลต่อสถานีปลายทาง (เช่น 45%)
  isUpstream: boolean;               // อยู่เหนือน้ำหรือไม่ (true / false)
}
```

#### ตัวอย่างชุดข้อมูลจริง (JSON Example):
```json
[
  {
    "stationId": "8892",
    "targetStationId": "133528",
    "relationType": "rainfall_influence",
    "distanceKm": 32.0,
    "travelTimeHours": 4.5,
    "travelTimeHoursMin": 3.5,
    "travelTimeHoursMax": 6.0,
    "influenceWeightPercent": 40.0,
    "isUpstream": true
  },
  {
    "stationId": "8892",
    "targetStationId": "Y-0020",
    "relationType": "downstream_gauge",
    "distanceKm": 54.0,
    "travelTimeHours": 8.0,
    "travelTimeHoursMin": 6.5,
    "travelTimeHoursMax": 10.0,
    "influenceWeightPercent": 85.0,
    "isUpstream": false
  },
  {
    "stationId": "Y-0020",
    "targetStationId": "133528",
    "relationType": "rainfall_influence",
    "distanceKm": 86.0,
    "travelTimeHours": 12.5,
    "travelTimeHoursMin": 10.0,
    "travelTimeHoursMax": 16.0,
    "influenceWeightPercent": 30.0,
    "isUpstream": true
  }
]
```

---

### 3.2 ข้อมูลเกณฑ์ระดับตลิ่งและเวลาน้ำเดินทางมาตรฐานประจำสถานี (Station Thresholds & Lag Time)
> **ตรงตาม `api-req.md` §4.5 (`/basin/{basin}/stations/{station_id}/detail.json`)**

ค่าคงที่ทางชลศาสตร์ที่ผ่านการ Calibrate จากโมเดล เพื่อบันทึกลงในตาราง `stations`:

#### Schema ที่ต้องการ:
```typescript
interface StationCalibratedThresholds {
  stationId: string;                 // รหัสสถานี (เช่น "8892", "Y-0020")
  bankLevelMsl?: number;             // ระดับตลิ่ง (ม.รทก.)
  warningLevelMsl?: number;          // ระดับเตือนภัย (ม.รทก.)
  criticalLevelMsl?: number;         // ระดับวิกฤต (ม.รทก.)
  warningRain24h?: number;           // เกณฑ์ฝนเตือนภัย 24 ชม. (มม.) ปกติ 90.0
  criticalRain24h?: number;          // เกณฑ์ฝนวิกฤต 24 ชม. (มม.) ปกติ 150.0
  lagTimeHoursMin?: number;          // เวลาเดินทางของยอดน้ำขั้นต่ำจากต้นน้ำถึงสถานีนี้ (ชม.)
  lagTimeHoursMax?: number;          // เวลาเดินทางของยอดน้ำสูงสุดจากต้นน้ำถึงสถานีนี้ (ชม.)
}
```

#### ตัวอย่างชุดข้อมูลจริง (JSON Example):
```json
[
  {
    "stationId": "8892",
    "bankLevelMsl": 6.50,
    "warningLevelMsl": 5.80,
    "criticalLevelMsl": 6.20,
    "lagTimeHoursMin": 6.0,
    "lagTimeHoursMax": 12.0
  },
  {
    "stationId": "Y-0020",
    "bankLevelMsl": 7.45,
    "warningLevelMsl": 6.80,
    "criticalLevelMsl": 7.20,
    "lagTimeHoursMin": 14.0,
    "lagTimeHoursMax": 20.0
  }
]
```

---

## 4. ผลลัพธ์ที่ Backend และ Frontend จะนำไปสร้าง

เมื่อ Backend ได้รับข้อมูลความสัมพันธ์ข้างต้น ระบบจะรวมกับข้อมูลโทรมาตรล่าสุด (`telemetry_latest`) และ Publish เป็น JSON ให้ Frontend อัตโนมัติ:

```json
{
  "schemaVersion": "1.0",
  "stationId": "8892",
  "generatedAt": "2026-08-23T04:50:00Z",
  "relations": [
    {
      "type": "rainfall_influence",
      "stationId": "133528",
      "targetStationId": "133528",
      "name": { "th": "สถานีวัดน้ำฝนศรีสัชนาลัย", "en": "Si Satchanalai Rain Station" },
      "targetStationName": { "th": "สถานีวัดน้ำฝนศรีสัชนาลัย", "en": "Si Satchanalai Rain Station" },
      "stationType": "rainfall",
      "distanceKm": 32.0,
      "travelTimeHours": 4.5,
      "influenceWeightPercent": 40,
      "latestValue": "124.0 มม.",
      "status": "critical",
      "isUpstream": true
    },
    {
      "type": "downstream_gauge",
      "stationId": "Y-0020",
      "targetStationId": "Y-0020",
      "name": { "th": "สะพานพระแม่ย่า (เมืองสุโขทัย)", "en": "Phra Mae Ya Bridge (Sukhothai City)" },
      "targetStationName": { "th": "สะพานพระแม่ย่า (เมืองสุโขทัย)", "en": "Phra Mae Ya Bridge (Sukhothai City)" },
      "stationType": "water_level",
      "distanceKm": 54.0,
      "travelTimeHours": 8.0,
      "influenceWeightPercent": 85,
      "latestValue": "5.82 ม.รทก.",
      "status": "warning",
      "isUpstream": false
    }
  ]
}
```

---

## 5. สิ่งที่ Model **ไม่ต้องทำ** (Backend จัดการเอง)

1. ❌ **ไม่ต้องสร้างข้อความบทวิเคราะห์ (No Text Synopsis / Key Drivers)**:
   * Backend มี `LLMBulletinService` (OpenAI SDK Function Calling) สังเคราะห์ข้อความรายงานภาษาทางการเอง
2. ❌ **ไม่มี Endpoint `/forecasts/*.json`**:
   * ตาม `api-req.md` ระบบแสดงเฉพาะ Near Real-time Telemetry และ Time-series ย้อนหลัง (`history/*.json`)
3. ❌ **ไม่ต้องเขียนข้อความเตือนภัย**:
   * Backend นำค่า `travelTimeHours` หรือ `lagTimeHoursMin/Max` จาก Model ไปประกอบเป็นข้อความเตือนภัยอัตโนมัติ เช่น:
     > *"ขณะนี้มีฝนตกหนักที่ต้นน้ำ (สถานี ... ฝน 24 ชม. ... มม.) โปรดเฝ้าระวังระดับน้ำและมวลน้ำหลากที่จะไหลลงสู่พื้นที่นี้ ในอีกประมาณ {lagTimeHoursMin} - {lagTimeHoursMax} ชั่วโมง"*

---

## 6. ช่องทางการส่งข้อมูลเข้าสู่ Backend

Model สามารถบันทึก/ส่งข้อมูลชุดนี้เข้าสู่ Backend ได้ 2 ช่องทาง:
1. **ผ่าน Database Seed / Migration**: บันทึกใส่ `src/db/seed.ts` ในตัวแปร `initialStationRelations`
2. **ผ่าน REST API**:
   - **Endpoint**: `POST /api/admin/relations/sync`
   - **Header**: `x-cron-secret: <CRON_SECRET>`
