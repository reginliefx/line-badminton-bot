"""ระบบจองคอร์ดแบดมินตัน — เก็บข้อมูลการจองใน Google Sheets

ชีต "bookings" แถวแรกเป็นหัวตาราง:
    date | court | hour | user_id | name | created_at
ตัวอย่างแถวข้อมูล:
    2026-11-10 | 2 | 14 | Uxxxxxxxx | สมชาย | 2026-11-10T13:05:11+07:00
"""
import json
import os
from datetime import datetime
from zoneinfo import ZoneInfo

import gspread
from linebot.v3.messaging import FlexContainer, FlexMessage, TextMessage

TZ = ZoneInfo("Asia/Bangkok")
COURTS = [1, 2, 3, 4, 5]
HOURS = list(range(10, 20))  # รอบเริ่ม 10:00 ... 19:00 (รอบสุดท้ายจบ 20:00)
SHEET_NAME = os.environ.get("BOOKING_SHEET_NAME", "bookings")
BLOCK_PAST = os.environ.get("BLOCK_PAST_SLOTS", "1") == "1"
HISTORY_LIMIT = 10  # จำนวนรายการล่าสุดที่แสดงในประวัติการจอง

GREEN, RED, GRAY = "🟢", "🔴", "⚪"

_sh = None
_ws = None


# ---------------------------------------------------------------- Google Sheets
def spreadsheet():
    """เปิดไฟล์ Google Sheets (แคชไว้ใช้ซ้ำ) — handoff.py ใช้ร่วมกัน"""
    global _sh
    if _sh is None:
        info = json.loads(os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"])
        gc = gspread.service_account_from_dict(info)
        _sh = gc.open_by_key(os.environ["GOOGLE_SHEET_ID"])
    return _sh


def _worksheet():
    global _ws
    if _ws is None:
        _ws = spreadsheet().worksheet(SHEET_NAME)
    return _ws


def _now():
    return datetime.now(TZ)


def today_str():
    return _now().strftime("%Y-%m-%d")


def today_thai():
    n = _now()
    return f"{n.day}/{n.month}/{n.year + 543}"


def slot_label(hour: int) -> str:
    return f"{hour:02d}:00 – {hour + 1:02d}:00"


def _is_past(hour: int) -> bool:
    """อิงเวลาจริง: ช่วงที่เริ่มไปก่อนชั่วโมงปัจจุบันถือว่าผ่านแล้ว จองไม่ได้ (ช่วงปัจจุบันยังจองได้)
    ถ้าอยากปิดการตรวจนี้ (เช่น ตอนทดสอบ) ให้ตั้ง Environment Variable BLOCK_PAST_SLOTS=0"""
    return BLOCK_PAST and hour < _now().hour


def get_booked(date_str: str | None = None) -> dict:
    """คืน {(court, hour): แถวข้อมูล} ของวันที่กำหนด (ค่าเริ่มต้น = วันนี้)"""
    date_str = date_str or today_str()
    booked = {}
    for row in _worksheet().get_all_values()[1:]:
        if len(row) >= 3 and row[0].strip() == date_str:
            try:
                booked[(int(row[1]), int(row[2]))] = row
            except ValueError:
                continue
    return booked


def free_hours(court: int, booked: dict) -> list:
    return [h for h in HOURS if (court, h) not in booked and not _is_past(h)]


def book_slot(court: int, hour: int, user_id: str, name: str) -> str:
    """จองช่วงเวลา คืนค่า: ok | taken | past | invalid"""
    if court not in COURTS or hour not in HOURS:
        return "invalid"
    if _is_past(hour):
        return "past"

    date = today_str()
    if (court, hour) in get_booked(date):
        return "taken"

    ws = _worksheet()
    ws.append_row(
        [date, court, hour, user_id, name, _now().isoformat(timespec="seconds")],
        value_input_option="RAW",
    )

    # ตรวจซ้ำ: ถ้าอีกคนจองช่วงเดียวกันพร้อมกันและแถวของเขามาก่อน ให้ถอนการจองของเรา
    rows = ws.get_all_values()
    matches = [
        i
        for i, r in enumerate(rows, start=1)
        if len(r) >= 4
        and r[0].strip() == date
        and r[1].strip() == str(court)
        and r[2].strip() == str(hour)
    ]
    if len(matches) > 1 and rows[matches[0] - 1][3] != user_id:
        mine = [i for i in matches if rows[i - 1][3] == user_id][-1]
        ws.delete_rows(mine)
        return "taken"
    return "ok"


def _thai_date(date_str: str) -> str:
    try:
        d = datetime.strptime(date_str, "%Y-%m-%d")
        return f"{d.day}/{d.month}/{d.year + 543}"
    except ValueError:
        return date_str


def get_user_bookings(user_id: str) -> list:
    """คืน [(วันที่ 'YYYY-MM-DD', คอร์ด, ชั่วโมง)] ของผู้ใช้คนนี้ เรียงใหม่สุดก่อน"""
    out = []
    for row in _worksheet().get_all_values()[1:]:
        if len(row) >= 4 and row[3] == user_id:
            try:
                out.append((row[0].strip(), int(row[1]), int(row[2])))
            except ValueError:
                continue
    out.sort(key=lambda b: (b[0], b[2]), reverse=True)
    return out


def my_bookings_message(user_id: str | None):
    """ประวัติการจองของผู้ใช้ (Flex) หรือข้อความถ้ายังไม่มี"""
    if not user_id:
        return TextMessage(text="ดูประวัติการจองได้เฉพาะในแชตส่วนตัวกับบอทครับ")
    bookings = get_user_bookings(user_id)
    if not bookings:
        return TextMessage(text="📭 ยังไม่มีประวัติการจองครับ พิมพ์ \"จองคอร์ด\" เพื่อเริ่มจองได้เลย")

    today = today_str()
    rows = []
    for date, court, hour in bookings[:HISTORY_LIMIT]:
        is_today = date == today
        rows.append(
            {
                "type": "box",
                "layout": "horizontal",
                "spacing": "sm",
                "paddingAll": "sm",
                "margin": "xs",
                "cornerRadius": "md",
                "backgroundColor": "#E8F5E9" if is_today else "#F6F6F6",
                "contents": [
                    {"type": "text", "text": _thai_date(date), "flex": 4, "size": "sm", "gravity": "center"},
                    {"type": "text", "text": f"คอร์ด {court}", "flex": 3, "size": "sm", "weight": "bold", "gravity": "center"},
                    {"type": "text", "text": slot_label(hour), "flex": 5, "size": "xs", "align": "end", "gravity": "center"},
                ],
            }
        )
        if is_today:
            rows[-1]["contents"][0]["text"] = f"วันนี้ {_thai_date(date)}"
            rows[-1]["contents"][0]["color"] = "#2E7D32"

    sub = f"แสดง {min(len(bookings), HISTORY_LIMIT)} รายการล่าสุด จากทั้งหมด {len(bookings)}"
    bubble = {
        "type": "bubble",
        "header": _title("🗓 ประวัติการจองของคุณ", sub),
        "body": {"type": "box", "layout": "vertical", "contents": rows},
    }
    return _flex("ประวัติการจองของคุณ", bubble)


def result_text(result: str, court: int, hour: int) -> str:
    if result == "ok":
        return (
            f"✅ จองสำเร็จ!\nคอร์ด {court}\nวันที่ {today_thai()}\nเวลา {slot_label(hour)}"
        )
    if result == "taken":
        return "❌ ขออภัย ช่วงเวลานี้มีคนจองไปแล้ว เลือกช่วงอื่นได้เลยครับ"
    if result == "past":
        return "⏰ ช่วงเวลานี้ผ่านไปแล้วครับ"
    return "ข้อมูลไม่ถูกต้อง ลองเลือกใหม่อีกครั้งนะครับ"


# ------------------------------------------------------------------ Flex builders
def _flex(alt_text: str, bubble: dict) -> FlexMessage:
    return FlexMessage(
        alt_text=alt_text, contents=FlexContainer.from_json(json.dumps(bubble))
    )


def _title(text: str, sub: str) -> dict:
    return {
        "type": "box",
        "layout": "vertical",
        "backgroundColor": "#0B6E4F",
        "paddingAll": "lg",
        "contents": [
            {"type": "text", "text": text, "weight": "bold", "size": "lg", "color": "#FFFFFF"},
            {"type": "text", "text": sub, "size": "xs", "color": "#D8F3DC", "margin": "xs"},
        ],
    }


def court_picker_flex() -> FlexMessage:
    """ขั้นที่ 1: เลือกคอร์ด (🟢 = ยังมีช่วงว่าง, 🔴 = เต็มแล้ว)"""
    booked = get_booked()
    rows = []
    for c in COURTS:
        free = free_hours(c, booked)
        ok = len(free) > 0
        row = {
            "type": "box",
            "layout": "horizontal",
            "spacing": "md",
            "paddingAll": "md",
            "margin": "sm",
            "cornerRadius": "md",
            "backgroundColor": "#F4F7F5" if ok else "#FBEAEA",
            "contents": [
                {"type": "text", "text": GREEN if ok else RED, "flex": 0, "size": "lg", "gravity": "center"},
                {"type": "text", "text": f"คอร์ด {c}", "weight": "bold", "flex": 3, "gravity": "center"},
                {
                    "type": "text",
                    "text": f"ว่าง {len(free)} ช่วง" if ok else "เต็มแล้ว",
                    "size": "sm",
                    "color": "#2E7D32" if ok else "#C62828",
                    "align": "end",
                    "flex": 3,
                    "gravity": "center",
                },
            ],
        }
        if ok:
            row["action"] = {
                "type": "postback",
                "label": f"คอร์ด {c}",
                "data": f"a=court&c={c}",
                "displayText": f"เลือกคอร์ด {c}",
            }
        rows.append(row)

    bubble = {
        "type": "bubble",
        "header": _title("🏸 เลือกคอร์ด", f"วันที่ {today_thai()} · เปิด 10:00 – 20:00"),
        "body": {"type": "box", "layout": "vertical", "contents": rows},
        "footer": {
            "type": "box",
            "layout": "vertical",
            "contents": [
                {
                    "type": "text",
                    "text": f"{GREEN} ยังว่าง   {RED} เต็มแล้ว",
                    "size": "xs",
                    "color": "#888888",
                    "align": "center",
                }
            ],
        },
    }
    return _flex("เลือกคอร์ดที่ต้องการจอง", bubble)


def slot_picker_flex(court: int) -> FlexMessage:
    """ขั้นที่ 2: เลือกช่วงเวลาของคอร์ดนั้น (แตะช่วงที่เป็น 🟢 เพื่อจอง)"""
    booked = get_booked()
    rows = []
    for h in HOURS:
        label = slot_label(h)
        if (court, h) in booked:
            icon, note, color, bg, action = RED, "จองแล้ว", "#C62828", "#FBEAEA", None
        elif _is_past(h):
            icon, note, color, bg, action = GRAY, "ผ่านแล้ว", "#9E9E9E", "#F2F2F2", None
        else:
            icon, note, color, bg = GREEN, "ว่าง · แตะเพื่อจอง", "#2E7D32", "#F4F7F5"
            action = {
                "type": "postback",
                "label": label,
                "data": f"a=book&c={court}&h={h}",
            }
        row = {
            "type": "box",
            "layout": "horizontal",
            "spacing": "md",
            "paddingAll": "sm",
            "margin": "xs",
            "cornerRadius": "md",
            "backgroundColor": bg,
            "contents": [
                {"type": "text", "text": icon, "flex": 0, "gravity": "center"},
                {"type": "text", "text": label, "flex": 3, "size": "sm", "weight": "bold", "gravity": "center"},
                {"type": "text", "text": note, "flex": 4, "size": "xs", "color": color, "align": "end", "gravity": "center"},
            ],
        }
        if action:
            row["action"] = action
        rows.append(row)

    bubble = {
        "type": "bubble",
        "header": _title(f"🏸 คอร์ด {court}", f"วันที่ {today_thai()} · เลือกช่วงเวลา (รอบละ 1 ชม.)"),
        "body": {"type": "box", "layout": "vertical", "contents": rows},
        "footer": {
            "type": "box",
            "layout": "vertical",
            "contents": [
                {
                    "type": "button",
                    "style": "link",
                    "height": "sm",
                    "action": {"type": "postback", "label": "← เลือกคอร์ดอื่น", "data": "a=menu"},
                }
            ],
        },
    }
    return _flex(f"เลือกเวลาของคอร์ด {court}", bubble)


def status_flex() -> FlexMessage:
    """ตารางสถานะทุกคอร์ด × ทุกช่วงเวลา ของวันนี้"""

    def cell(text: str, flex: int, **kw) -> dict:
        d = {"type": "text", "text": text, "flex": flex, "size": "sm", "align": "center", "gravity": "center"}
        d.update(kw)
        return d

    booked = get_booked()
    header_row = {
        "type": "box",
        "layout": "horizontal",
        "contents": [cell("เวลา", 3, weight="bold", color="#555555", align="start")]
        + [cell(str(c), 2, weight="bold", color="#555555") for c in COURTS],
    }
    rows = [header_row, {"type": "separator", "margin": "sm"}]
    for h in HOURS:
        icons = []
        for c in COURTS:
            if (c, h) in booked:
                icons.append(RED)
            elif _is_past(h):
                icons.append(GRAY)
            else:
                icons.append(GREEN)
        rows.append(
            {
                "type": "box",
                "layout": "horizontal",
                "margin": "md",
                "contents": [cell(f"{h:02d}-{h + 1:02d}", 3, align="start", color="#444444")]
                + [cell(i, 2) for i in icons],
            }
        )

    bubble = {
        "type": "bubble",
        "header": _title("📊 สถานะคอร์ดวันนี้", f"วันที่ {today_thai()} · คอร์ด 1-5"),
        "body": {"type": "box", "layout": "vertical", "contents": rows},
        "footer": {
            "type": "box",
            "layout": "vertical",
            "spacing": "sm",
            "contents": [
                {
                    "type": "text",
                    "text": f"{GREEN} ว่าง   {RED} จองแล้ว   {GRAY} ผ่านแล้ว",
                    "size": "xs",
                    "color": "#888888",
                    "align": "center",
                },
                {
                    "type": "button",
                    "style": "primary",
                    "color": "#0B6E4F",
                    "height": "sm",
                    "action": {"type": "message", "label": "🏸 จองคอร์ด", "text": "จองคอร์ด"},
                },
            ],
        },
    }
    return _flex("สถานะคอร์ดวันนี้", bubble)
