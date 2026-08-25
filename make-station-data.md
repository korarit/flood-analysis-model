เขียน script python จัดการ 2 เรื่องนี้

### หา station list ของ สถานีน้ำฝน ในแต่ละลุ่มน้ำตาม frontend มี
สิ่งนายต้องทำคือ เอา response-api-rainstation.json มาแยกเป็น
ไฟล์ของแต่ละลุ่มน้ำตามที่ frontend มีอยู่ โดยที่แยกแค่ ของ hii คือ "สถาบันสารสนเทศทรัพยากรน้ำ (องค์การมหาชน)" และ tele_station_oldcode ขึ้นต้นด้วย
MOU ตามด้วยตัวเลข และไฟล์ของ dwr ซึ่งก็คือ "กรมทรัพยากรน้ำ"

### หา station list ของ สถานีวัดระดับน้ำ ในแต่ละลุ่มน้ำตาม frontend มี
ให้ใช้ไฟล์ reqponse-api-waterlevel.json เป็นหลัก แต่ให้แยกเป็นของแต่ละลุ่มน้ำตามที่ frontend มีอยู่ โดยให้ format เหมือนกับ station list ของสถานีน้ำฝน

### ไฟล์เก็บใน folder dataset

### station detail ยังไม่ต้องมีข้อมูลน้ำฝนนะ