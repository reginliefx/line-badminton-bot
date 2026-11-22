# 🏸 AI Badminton Court Chatbot (กรณีศึกษา: ศูนย์เยาวชนกรุงเทพมหานคร ไทย-ญี่ปุ่น)

ระบบแชทบอทปัญญาประดิษฐ์บนแพลตฟอร์ม **LINE Official Account** สำหรับให้บริการข้อมูลสนามแบดมินตัน ตรวจสอบตารางคอร์ตว่าง จองคิวออนไลน์ และส่งต่อการสนทนาให้ผู้ดูแลระบบ (Human Handoff) พัฒนาด้วยภาษา Python (Flask) เชื่อมต่อการทำงานร่วมกับ **Google Gemini API** และฐานข้อมูล **Google Sheets** โฮสต์บนคลาวด์แบบ Serverless ผ่าน **Vercel**[cite: 8]

---

## ✨ ความสามารถหลักของระบบ (Features)

1. **ระบบตอบกลับอัตโนมัติตามคำสำคัญ (Rule-based Wake Words)**
   - ตรวจจับคีย์เวิร์ดจากไฟล์ `keywords.json` เพื่อตอบกลับข้อมูลพื้นฐานอย่างรวดเร็วและแม่นยำโดยไม่เสียโควตา AI (เช่น ที่ตั้งสนาม, เวลาเปิด-ปิด, กฎระเบียบ, กิจกรรม, อัตราค่าบริการ)[cite: 8]
   - รองรับการตอบกลับทั้งข้อความ (Text), รูปภาพ (Image) และการ์ดข้อมูลแบบโต้ตอบได้ (LINE Flex Message)[cite: 8]

2. **ระบบปัญญาประดิษฐ์ (AI Conversational Fallback)**
   - หากข้อความไม่ตรงกับชุดคำสั่งหลัก ระบบจะส่งคำถามไปยัง **Google Gemini API** (`gemini-3.5-flash-lite`) พร้อมคำสั่งควบคุมบทบาท (`system_prompt.txt`) เพื่อสร้างคำตอบที่เป็นธรรมชาติและตรงบริบทของสนามกีฬา[cite: 8]

3. **ระบบจองและจัดการคอร์ตออนไลน์ (Court Booking System)**
   - แสดงตารางคอร์ตว่างรายวันและรายชั่วโมงผ่าน Flex Message
   - บันทึกข้อมูลการจองคิว ตรวจสอบประวัติการจอง และยกเลิกการจองของตนเองได้แบบเรียลไทม์ผ่านฐานข้อมูล **Google Sheets**[cite: 8]

4. **ระบบส่งต่อผู้ดูแลระบบ (Human Handoff & Admin Controls)**
   - เมื่อผู้ใช้ต้องการติดต่อเจ้าหน้าที่ บอทจะหยุดตอบกลับอัตโนมัติชั่วคราว (`pause`) และส่งการ์ดแจ้งเตือนไปยัง LINE ส่วนตัวของแอดมินทันที
   - แอดมินสามารถกดปุ่ม **"คืนให้บอท"** หรือพิมพ์คำสั่ง **`ปิดเคส`** เพื่อคืนสิทธิ์การตอบกลับให้ระบบอัตโนมัติได้ทันที

---

## 🏗️ สถาปัตยกรรมและเทคโนโลยีที่ใช้ (Tech Stack)

- **Backend Framework:** Python 3, Flask
- **Messaging Platform:** LINE Messaging API (`line-bot-sdk` v3)[cite: 8]
- **Generative AI:** Google GenAI SDK (`gemini-3.5-flash-lite`)[cite: 8]
- **Database & Auth:** Google Sheets API (`gspread`) เชื่อมต่อผ่าน Google Cloud Service Account[cite: 8]
- **Cloud Hosting:** Vercel (Serverless Functions)[cite: 8]

```text
[ผู้ใช้ใน LINE] 
      │
      ▼ (Webhook POST /callback)
[Vercel - Flask Server (app.py)]
      ├──► ตรงกับ Wake Word (keywords.json) ──► ตอบกลับ Text / Image / Flex Message / Booking Menu
      ├──► ขอติดต่อแอดมิน (Handoff)         ──► หยุดบอทชั่วคราว & แจ้งเตือน LINE แอดมิน
      └──► คำถามทั่วไป (Fallback)          ──► ประมวลผลผ่าน Google Gemini API & ตอบกลับ
