## ข้อมูล ปริมาณน้ำฝน

### จาก HII ใช้ CSV จาก
ให้เริ่มที่ เดือน 01/2025 - 07/2026
- สำหรับไม่ใช่ MOU https://tiservice.hii.or.th/opendata/data_catalog/hourly_rain/
- สำหรับ MOU ให้ใช้ข้อมูลจาก https://tiservice.hii.or.th/opendata/data_catalog_mou/hourly_rain_mou/


### จาก DWR ใช้จาก
ให้ทำ web scrapper script ดึงย้อนหลัง เริ่มที่ 01/2025 - 07/2026
https://ews.dwr.go.th/ews/show-rain?FilterSTN=STN0001&FilterType=3D&FilterDate=2026-08-22&FilterTime=00%3A00&FilterNumData=48 แล้วทดสอบ แบบ smoke พร้อมเขียนวิธีการรัน script ไว้ ใน readme


## ข้อมูล ระดับน้ำ
### จาก HII ใช้ CSV จาก
ให้เริ่มที่ เดือน 01/2025 - 07/2026
- สำหรับไม่ใช่ MOU https://tiservice.hii.or.th/opendata/data_catalog/water_level/
- สำหรับ MOU ให้ใช้ข้อมูลจาก https://tiservice.hii.or.th/opendata/data_catalog_mou/water_level_mou/

### จากกรมชลประทาน
ให้ทำ web scrapper script ด้วย python ดึงย้อนหลัง เริ่มที่ 01/2025 - 07/2026
https://hyd-app-db.rid.go.th/hydro2h.html โดยที่เลขคือ สำนักงาน มี 1-8 ให้นายหาลุ่มน้ำที่ต้องใช้งานก็พอ แล้วทดสอบ แบบ smoke พร้อมเขียนวิธีการรัน script ไว้ ใน readme