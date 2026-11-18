"""ระบบจองคอร์ดแบดมินตัน — เก็บข้อมูลใน Google Sheets

แท็บ "bookings" แถวแรกเป็นหัวตาราง:
    date | court | hour | user_id | name | created_at | status
  ตัวอย่าง: 2026-11-10 | 2 | 14 | Uxxxxxxxx | สมชาย | 2026-11-10T13:05:11+07:00 |
  status ว่าง = การจองยังใช้งานอยู่, "cancelled" = ยกเลิกแล้ว (แถวยังอยู่ในชีตแต่ไม่นับว่าถูกจอง)

แท็บ "maintenance" (ไม่บังคับ) ใช้ปิดคอร์ดชั่วคราว — แถวแรกเป็นหัวตาราง:
    date | court | from_hour | to_hour | note
  date      วันที่ YYYY-MM-DD (เว้นว่าง = ทุกวัน)
  court     เลข 1-5 (เว้นว่างหรือ all = ทุกคอร์ด)
  from_hour ชั่วโมงเริ่มปิด เช่น 13 (เว้นว่าง = ตั้งแต่ 10)
  to_hour   ชั่วโมงเปิดอีกครั้ง เช่น 16 (เว้นว่าง = ถึง 20) -> ปิด 13:00-16:00
  note      หมายเหตุ (ไม่บังคับ) เช่น ซ่อมพื้น
  ช่วงที่ปิดจะขึ้น 🟡 และจองไม่ได้
"""
import json
import logging
import os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import gspread
from linebot.v3.messaging import FlexContainer, FlexMessage, TextMessage

TZ = ZoneInfo("Asia/Bangkok")
COURTS = [1, 2, 3, 4, 5]
HOURS = list(range(10, 20))  # รอบเริ่ม 10:00 ... 19:00 (รอบสุดท้ายจบ 20:00)
OPEN_HOUR, CLOSE_HOUR = HOURS[0], HOURS[-1] + 1
SHEET_NAME = os.environ.get("BOOKING_SHEET_NAME", "bookings")
MAINT_SHEET_NAME = os.environ.get("MAINTENANCE_SHEET_NAME", "maintenance")
BLOCK_PAST = os.environ.get("BLOCK_PAST_SLOTS", "1") == "1"
HISTORY_LIMIT = 10  # จำนวนรายการล่าสุดที่แสดงในประวัติการจอง
# จำนวนชั่วโมง (ช่วงละ 1 ชม.) ที่ 1 user ID จองได้สูงสุด "ต่อวัน" — ปรับได้ด้วย Environment Variable MAX_HOURS_PER_USER
MAX_PER_USER = int(os.environ.get("MAX_HOURS_PER_USER", "2"))
ADVANCE_DAYS = 1  # จองได้วันนี้ + ล่วงหน้า 1 วัน (พรุ่งนี้) = 2 วัน
STATUS_COL = 7  # คอลัมน์ G = status
CANCELLED = "cancelled"

GREEN, RED, GRAY, YELLOW = "🟢", "🔴", "⚪", "🟡"
LEGEND = f"{GREEN} ว่าง   {RED} จองแล้ว   {YELLOW} ปรับปรุง   {GRAY} ผ่านแล้ว"

_sh = None
_ws = None
_mws = None


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


def _maint_worksheet():
    global _mws
    if _mws is None:
        _mws = spreadsheet().worksheet(MAINT_SHEET_NAME)
    return _mws


# ---------------------------------------------------------------- วันที่/เวลา
def _now():
    return datetime.now(TZ)


def today_str():
    return _now().strftime("%Y-%m-%d")


def tomorrow_str():
    return (_now() + timedelta(days=1)).strftime("%Y-%m-%d")


def allowed_dates() -> list:
    """วันที่จองได้: วันนี้ + ล่วงหน้า ADVANCE_DAYS วัน"""
    n = _now()
    return [(n + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(ADVANCE_DAYS + 1)]


def norm_date(date_str) -> str:
    """ถ้าวันที่ที่ส่งมาไม่อยู่ในช่วงที่จองได้ ให้ใช้วันนี้"""
    return date_str if date_str in allowed_dates() else today_str()


def _thai_date(date_str: str) -> str:
    try:
        d = datetime.strptime(date_str, "%Y-%m-%d")
        return f"{d.day}/{d.month}/{d.year + 543}"
    except ValueError:
        return date_str


def day_label(date_str: str) -> str:
    if date_str == today_str():
        return f"วันนี้ {_thai_date(date_str)}"
    if date_str == tomorrow_str():
        return f"พรุ่งนี้ {_thai_date(date_str)}"
    return _thai_date(date_str)


def slot_label(hour: int) -> str:
    return f"{hour:02d}:00-{hour + 1:02d}:00"


def _is_past(hour: int, date_str: str | None = None) -> bool:
    """อิงเวลาจริง: ช่วงที่เริ่มไปก่อนชั่วโมงปัจจุบัน (ของวันนี้) ถือว่าผ่านแล้ว จองไม่ได้ (ช่วงปัจจุบันยังจองได้)
    วันพรุ่งนี้ไม่มีช่วงที่ผ่านแล้ว ถ้าอยากปิดการตรวจนี้ (เช่น ตอนทดสอบ) ให้ตั้ง BLOCK_PAST_SLOTS=0"""
    if not BLOCK_PAST:
        return False
    today = today_str()
    date_str = date_str or today
    return date_str < today or (date_str == today and hour < _now().hour)


# ---------------------------------------------------------------- ข้อมูลการจอง
def _is_cancelled(row) -> bool:
    return len(row) >= STATUS_COL and row[STATUS_COL - 1].strip().lower() == CANCELLED


def get_booked(date_str: str | None = None) -> dict:
    """คืน {(court, hour): แถวข้อมูล} ของวันที่กำหนด (ค่าเริ่มต้น = วันนี้) ไม่รวมแถวที่ยกเลิกแล้ว"""
    date_str = date_str or today_str()
    booked = {}
    for row in _worksheet().get_all_values()[1:]:
        if len(row) >= 3 and row[0].strip() == date_str and not _is_cancelled(row):
            try:
                booked[(int(row[1]), int(row[2]))] = row
            except ValueError:
                continue
    return booked


def _parse_date(s: str):
    """รับ YYYY-MM-DD หรือ DD/MM/YYYY (รองรับ พ.ศ.) คืน 'YYYY-MM-DD' หรือ None"""
    s = s.strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y"):
        try:
            d = datetime.strptime(s, fmt)
        except ValueError:
            continue
        if d.year > 2400:
            d = d.replace(year=d.year - 543)
        return d.strftime("%Y-%m-%d")
    return None


def _to_int(s, default):
    s = str(s).strip()
    if not s:
        return default
    try:
        return int(float(s))
    except ValueError:
        return None


def get_closed(date_str: str | None = None) -> dict:
    """คืน {(court, hour): หมายเหตุ} ของช่วงที่ปิดปรับปรุงในวันนั้น (อ่านจากแท็บ maintenance)
    ถ้าอ่านแท็บไม่ได้ (เช่น ยังไม่ได้สร้าง) ให้ถือว่าไม่มีการปิด เพื่อไม่ให้ระบบจองล่ม"""
    date_str = date_str or today_str()
    try:
        rows = _maint_worksheet().get_all_values()[1:]
    except Exception as e:  # noqa: BLE001
        logging.warning("maintenance sheet unreadable: %s", e)
        return {}

    closed = {}
    for r in rows:
        r = list(r) + [""] * 5
        d, court, h_from, h_to, note = r[0].strip(), r[1].strip(), r[2], r[3], r[4].strip()
        if not any(x.strip() for x in (d, court, str(h_from), str(h_to))):
            continue  # แถวว่าง (มีแต่หมายเหตุ) ไม่ใช้ กันปิดทั้งสนามโดยไม่ตั้งใจ
        if d and _parse_date(d) != date_str:
            continue  # date ว่าง = ทุกวัน
        if court.lower() in ("", "all", "ทั้งหมด"):
            courts = COURTS
        else:
            try:
                courts = [int(float(court))]
            except ValueError:
                logging.warning("maintenance: bad court %r", court)
                continue
        start, end = _to_int(h_from, OPEN_HOUR), _to_int(h_to, CLOSE_HOUR)
        if start is None or end is None:
            logging.warning("maintenance: bad hours %r-%r", h_from, h_to)
            continue
        for c in courts:
            for h in range(start, end):
                if h in HOURS:
                    closed[(c, h)] = note
    return closed


def free_hours(court: int, booked: dict, date_str: str | None = None, closed: dict | None = None) -> list:
    closed = closed or {}
    return [
        h
        for h in HOURS
        if (court, h) not in booked and (court, h) not in closed and not _is_past(h, date_str)
    ]


def count_user_on_date(rows: list, user_id: str, date: str) -> int:
    """จำนวนช่วงที่ผู้ใช้จองไว้ในวันนั้น (ไม่นับที่ยกเลิก) จากข้อมูลทุกแถวของชีต"""
    return sum(
        1
        for r in rows[1:]
        if len(r) >= 4 and r[0].strip() == date and r[3] == user_id and not _is_cancelled(r)
    )


def book_slot(court: int, hour: int, user_id: str, name: str, date: str | None = None) -> str:
    """จองช่วงเวลา คืนค่า: ok | taken | past | invalid | limit | closed"""
    date = date or today_str()
    if court not in COURTS or hour not in HOURS or date not in allowed_dates():
        return "invalid"
    if _is_past(hour, date):
        return "past"
    if (court, hour) in get_closed(date):
        return "closed"

    ws = _worksheet()
    rows = ws.get_all_values()
    if any(
        i > 1
        and len(r) >= 4
        and r[0].strip() == date
        and r[1].strip() == str(court)
        and r[2].strip() == str(hour)
        and not _is_cancelled(r)
        for i, r in enumerate(rows, start=1)
    ):
        return "taken"
    if count_user_on_date(rows, user_id, date) >= MAX_PER_USER:
        return "limit"

    ws.append_row(
        [date, court, hour, user_id, name, _now().isoformat(timespec="seconds")],
        value_input_option="RAW",
    )

    # ตรวจซ้ำหลังเขียน (กันกรณีกดพร้อมกัน)
    rows = ws.get_all_values()

    # 1) ถ้าอีกคนจองช่วงเดียวกันพร้อมกันและแถวของเขามาก่อน ให้ถอนการจองของเรา
    matches = [
        i
        for i, r in enumerate(rows, start=1)
        if i > 1
        and len(r) >= 4
        and r[0].strip() == date
        and r[1].strip() == str(court)
        and r[2].strip() == str(hour)
        and not _is_cancelled(r)
    ]
    mine_rows = [i for i in matches if rows[i - 1][3] == user_id]
    if len(matches) > 1 and rows[matches[0] - 1][3] != user_id:
        ws.delete_rows(mine_rows[-1])
        return "taken"

    # 2) ถ้ากดหลายช่วงพร้อมกันจนเกินโควตาต่อคน ให้ถอนช่วงที่เพิ่งจอง
    if count_user_on_date(rows, user_id, date) > MAX_PER_USER:
        ws.delete_rows(mine_rows[-1])
        return "limit"
    return "ok"


def cancel_booking(user_id: str, date: str, court: int, hour: int) -> str:
    """ยกเลิกการจองของตัวเอง คืนค่า: ok | notfound | past
    ตั้ง status = cancelled (แถวยังอยู่ในชีต) และเปิดช่วงนั้นให้คนอื่นจองได้ทันที"""
    ws = _worksheet()
    rows = ws.get_all_values()
    for i, r in enumerate(rows, start=1):
        if (
            i > 1
            and len(r) >= 4
            and r[0].strip() == date
            and r[1].strip() == str(court)
            and r[2].strip() == str(hour)
            and r[3] == user_id
            and not _is_cancelled(r)
        ):
            if date < today_str() or _is_past(hour, date):
                return "past"
            ws.update_cell(i, STATUS_COL, CANCELLED)
            return "ok"
    return "notfound"


def get_user_bookings(user_id: str) -> list:
    """คืน [(วันที่ 'YYYY-MM-DD', คอร์ด, ชั่วโมง)] ของผู้ใช้คนนี้ เฉพาะที่ยังไม่ยกเลิก เรียงใหม่สุดก่อน"""
    out = []
    for row in _worksheet().get_all_values()[1:]:
        if len(row) >= 4 and row[3] == user_id and not _is_cancelled(row):
            try:
                out.append((row[0].strip(), int(row[1]), int(row[2])))
            except ValueError:
                continue
    out.sort(key=lambda b: (b[0], b[2]), reverse=True)
    return out


def _can_cancel(date: str, hour: int) -> bool:
    return date >= today_str() and not _is_past(hour, date)


# ------------------------------------------------------------------ ข้อความตอบกลับ
def result_text(result: str, court: int, hour: int, date: str | None = None) -> str:
    date = date or today_str()
    if result == "ok":
        suffix = " (พรุ่งนี้)" if date == tomorrow_str() else ""
        return f"✅ จองสำเร็จ!\nคอร์ด {court}\nวันที่ {_thai_date(date)}{suffix}\nเวลา {slot_label(hour)}"
    if result == "taken":
        return "❌ ขออภัย ช่วงเวลานี้มีคนจองไปแล้ว เลือกช่วงอื่นได้เลยครับ"
    if result == "past":
        return "⏰ ช่วงเวลานี้ผ่านไปแล้วครับ"
    if result == "closed":
        return "🟡 ช่วงเวลานี้ปิดปรับปรุง จองไม่ได้ครับ เลือกช่วงอื่นหรือคอร์ดอื่นได้เลย"
    if result == "limit":
        return (
            f"⚠️ จองได้สูงสุด {MAX_PER_USER} ชั่วโมงต่อวันต่อคนครับ\n"
            "ถ้าอยากเปลี่ยนช่วงเวลา ให้ยกเลิกการจองเดิมก่อน (พิมพ์ \"ยกเลิกจอง\")"
        )
    return "ข้อมูลไม่ถูกต้อง ลองเลือกใหม่อีกครั้งนะครับ (จองได้เฉพาะวันนี้และพรุ่งนี้)"


def cancel_result_text(result: str, date: str, court: int, hour: int) -> str:
    if result == "ok":
        return f"✅ ยกเลิกแล้ว\nคอร์ด {court} วันที่ {_thai_date(date)}\nเวลา {slot_label(hour)}"
    if result == "past":
        return "⏰ ช่วงเวลานี้ผ่านไปแล้ว ยกเลิกไม่ได้ครับ"
    return "ไม่พบการจองนี้ (อาจยกเลิกไปแล้ว) ครับ"


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
            {"type": "text", "text": sub, "size": "xs", "color": "#D8F3DC", "margin": "xs", "wrap": True},
        ],
    }


def _legend(text: str = LEGEND) -> dict:
    return {"type": "text", "text": text, "size": "xs", "color": "#888888", "align": "center", "wrap": True}


def date_picker_flex() -> FlexMessage:
    """ขั้นที่ 1: เลือกวัน (วันนี้ / พรุ่งนี้)"""
    today = today_str()
    rows = []
    for d in allowed_dates():
        rows.append(
            {
                "type": "box",
                "layout": "horizontal",
                "spacing": "md",
                "paddingAll": "md",
                "margin": "sm",
                "cornerRadius": "md",
                "backgroundColor": "#F4F7F5",
                "action": {"type": "postback", "label": "เลือกวัน", "data": f"a=menu&d={d}"},
                "contents": [
                    {"type": "text", "text": "📅", "flex": 0, "size": "lg", "gravity": "center"},
                    {"type": "text", "text": "วันนี้" if d == today else "พรุ่งนี้", "weight": "bold", "flex": 2, "gravity": "center"},
                    {"type": "text", "text": _thai_date(d), "size": "sm", "color": "#555555", "align": "end", "flex": 3, "gravity": "center"},
                ],
            }
        )
    bubble = {
        "type": "bubble",
        "header": _title("🏸 จองคอร์ด", "เลือกวันที่ต้องการจอง (จองล่วงหน้าได้ 1 วัน)"),
        "body": {"type": "box", "layout": "vertical", "contents": rows},
    }
    return _flex("เลือกวันที่ต้องการจอง", bubble)


def court_picker_flex(date: str | None = None) -> FlexMessage:
    """ขั้นที่ 2: เลือกคอร์ด (🟢 ยังมีช่วงว่าง, 🔴 เต็มแล้ว, 🟡 ปิดปรับปรุงทั้งวัน)"""
    date = norm_date(date)
    booked = get_booked(date)
    closed = get_closed(date)
    rows = []
    for c in COURTS:
        free = free_hours(c, booked, date, closed)
        n_closed = sum(1 for h in HOURS if (c, h) in closed)
        if free:
            icon, text, color, bg = GREEN, f"ว่าง {len(free)} ช่วง", "#2E7D32", "#F4F7F5"
        elif n_closed == len(HOURS):
            icon, text, color, bg = YELLOW, "ปิดปรับปรุง", "#B26A00", "#FFF8E1"
        else:
            icon, text, color, bg = RED, "เต็มแล้ว", "#C62828", "#FBEAEA"
        row = {
            "type": "box",
            "layout": "horizontal",
            "spacing": "md",
            "paddingAll": "md",
            "margin": "sm",
            "cornerRadius": "md",
            "backgroundColor": bg,
            "contents": [
                {"type": "text", "text": icon, "flex": 0, "size": "lg", "gravity": "center"},
                {"type": "text", "text": f"คอร์ด {c}", "weight": "bold", "flex": 3, "gravity": "center"},
                {"type": "text", "text": text, "size": "sm", "color": color, "align": "end", "flex": 3, "gravity": "center"},
            ],
        }
        if free:
            row["action"] = {
                "type": "postback",
                "label": f"คอร์ด {c}",
                "data": f"a=court&c={c}&d={date}",
                "displayText": f"เลือกคอร์ด {c}",
            }
        rows.append(row)

    bubble = {
        "type": "bubble",
        "header": _title("🏸 เลือกคอร์ด", f"{day_label(date)} · เปิด 10:00-20:00"),
        "body": {"type": "box", "layout": "vertical", "contents": rows},
        "footer": {
            "type": "box",
            "layout": "vertical",
            "spacing": "sm",
            "contents": [
                _legend(f"{GREEN} ยังว่าง   {RED} เต็มแล้ว   {YELLOW} ปิดปรับปรุง"),
                {
                    "type": "button",
                    "style": "link",
                    "height": "sm",
                    "action": {"type": "postback", "label": "← เลือกวันอื่น", "data": "a=dates"},
                },
            ],
        },
    }
    return _flex("เลือกคอร์ดที่ต้องการจอง", bubble)


def slot_picker_flex(court: int, date: str | None = None) -> FlexMessage:
    """ขั้นที่ 3: เลือกช่วงเวลาของคอร์ดนั้น (แตะช่วงที่เป็น 🟢 เพื่อจอง)"""
    date = norm_date(date)
    booked = get_booked(date)
    closed = get_closed(date)
    rows = []
    for h in HOURS:
        label = slot_label(h)
        action = None
        if (court, h) in closed:
            note = closed[(court, h)]
            icon, color, bg = YELLOW, "#B26A00", "#FFF8E1"
            text = "ปรับปรุง" + (f" · {note[:24]}" if note else "")
        elif (court, h) in booked:
            icon, text, color, bg = RED, "จองแล้ว", "#C62828", "#FBEAEA"
        elif _is_past(h, date):
            icon, text, color, bg = GRAY, "ผ่านแล้ว", "#9E9E9E", "#F2F2F2"
        else:
            icon, text, color, bg = GREEN, "ว่าง · แตะเพื่อจอง", "#2E7D32", "#F4F7F5"
            action = {"type": "postback", "label": label, "data": f"a=book&c={court}&h={h}&d={date}"}
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
                {"type": "text", "text": text, "flex": 4, "size": "xs", "color": color, "align": "end", "gravity": "center", "wrap": True},
            ],
        }
        if action:
            row["action"] = action
        rows.append(row)

    bubble = {
        "type": "bubble",
        "header": _title(f"🏸 คอร์ด {court}", f"{day_label(date)} · เลือกช่วงเวลา (รอบละ 1 ชม.)"),
        "body": {"type": "box", "layout": "vertical", "contents": rows},
        "footer": {
            "type": "box",
            "layout": "vertical",
            "contents": [
                {
                    "type": "button",
                    "style": "link",
                    "height": "sm",
                    "action": {"type": "postback", "label": "← เลือกคอร์ดอื่น", "data": f"a=menu&d={date}"},
                }
            ],
        },
    }
    return _flex(f"เลือกเวลาของคอร์ด {court}", bubble)


def status_flex(date: str | None = None) -> FlexMessage:
    """ตารางสถานะทุกคอร์ด × ทุกช่วงเวลา ของวันที่เลือก (ค่าเริ่มต้น = วันนี้)"""
    date = norm_date(date)

    def cell(text: str, flex: int, **kw) -> dict:
        d = {"type": "text", "text": text, "flex": flex, "size": "sm", "align": "center", "gravity": "center"}
        d.update(kw)
        return d

    booked = get_booked(date)
    closed = get_closed(date)
    header_row = {
        "type": "box",
        "layout": "horizontal",
        "contents": [cell("เวลา", 4, weight="bold", color="#555555", align="start")]
        + [cell(str(c), 2, weight="bold", color="#555555") for c in COURTS],
    }
    rows = [header_row, {"type": "separator", "margin": "sm"}]
    for h in HOURS:
        icons = []
        for c in COURTS:
            if (c, h) in closed:
                icons.append(YELLOW)
            elif (c, h) in booked:
                icons.append(RED)
            elif _is_past(h, date):
                icons.append(GRAY)
            else:
                icons.append(GREEN)
        rows.append(
            {
                "type": "box",
                "layout": "horizontal",
                "margin": "md",
                "contents": [cell(slot_label(h), 4, align="start", color="#444444", size="xs")]
                + [cell(i, 2) for i in icons],
            }
        )

    is_today = date == today_str()
    other = tomorrow_str() if is_today else today_str()
    bubble = {
        "type": "bubble",
        "header": _title("📊 สถานะคอร์ด", f"{day_label(date)} · คอร์ด 1-5"),
        "body": {"type": "box", "layout": "vertical", "contents": rows},
        "footer": {
            "type": "box",
            "layout": "vertical",
            "spacing": "sm",
            "contents": [
                _legend(),
                {
                    "type": "button",
                    "style": "secondary",
                    "height": "sm",
                    "action": {
                        "type": "postback",
                        "label": "ดูวันพรุ่งนี้" if is_today else "ดูวันนี้",
                        "data": f"a=status&d={other}",
                    },
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
    return _flex("สถานะคอร์ด", bubble)


def my_bookings_message(user_id: str | None):
    """ประวัติการจองของผู้ใช้ (Flex) พร้อมปุ่มยกเลิก — ไม่แสดงรายการที่ยกเลิกแล้ว"""
    if not user_id:
        return TextMessage(text="ดูประวัติการจองได้เฉพาะในแชตส่วนตัวกับบอทครับ")
    bookings = get_user_bookings(user_id)
    if not bookings:
        return TextMessage(text="📭 ยังไม่มีประวัติการจองครับ พิมพ์ \"จองคอร์ด\" เพื่อเริ่มจองได้เลย")

    today, tomorrow = today_str(), tomorrow_str()

    def used(d):
        return sum(1 for b in bookings if b[0] == d)

    rows = []
    for date, court, hour in bookings[:HISTORY_LIMIT]:
        if date == today:
            bg, date_color, date_text = "#E8F5E9", "#2E7D32", f"วันนี้ {_thai_date(date)}"
        elif date == tomorrow:
            bg, date_color, date_text = "#E3F2FD", "#1565C0", f"พรุ่งนี้ {_thai_date(date)}"
        else:
            bg, date_color, date_text = "#F6F6F6", "#444444", _thai_date(date)
        contents = [
            {
                "type": "box",
                "layout": "horizontal",
                "spacing": "sm",
                "contents": [
                    {"type": "text", "text": date_text, "flex": 5, "size": "xs", "gravity": "center", "color": date_color, "wrap": True},
                    {"type": "text", "text": f"คอร์ด {court}", "flex": 3, "size": "sm", "weight": "bold", "gravity": "center"},
                    {"type": "text", "text": slot_label(hour), "flex": 4, "size": "xs", "align": "end", "gravity": "center", "color": "#444444"},
                ],
            }
        ]
        if _can_cancel(date, hour):
            contents.append(
                {
                    "type": "button",
                    "style": "secondary",
                    "height": "sm",
                    "margin": "sm",
                    "action": {
                        "type": "postback",
                        "label": "ยกเลิกการจองนี้",
                        "data": f"a=cancel_ask&d={date}&c={court}&h={hour}",
                    },
                }
            )
        rows.append(
            {
                "type": "box",
                "layout": "vertical",
                "paddingAll": "sm",
                "margin": "xs",
                "cornerRadius": "md",
                "backgroundColor": bg,
                "contents": contents,
            }
        )

    sub = f"โควตา วันนี้ {used(today)}/{MAX_PER_USER} · พรุ่งนี้ {used(tomorrow)}/{MAX_PER_USER} ชม."
    if len(bookings) > HISTORY_LIMIT:
        sub += f" · แสดง {HISTORY_LIMIT} จาก {len(bookings)} รายการ"
    bubble = {
        "type": "bubble",
        "header": _title("🗓 ประวัติการจองของคุณ", sub),
        "body": {"type": "box", "layout": "vertical", "contents": rows},
    }
    return _flex("ประวัติการจองของคุณ", bubble)


def cancel_confirm_flex(date: str, court: int, hour: int) -> FlexMessage:
    """ถามยืนยันก่อนยกเลิก กันกดพลาด"""
    bubble = {
        "type": "bubble",
        "body": {
            "type": "box",
            "layout": "vertical",
            "spacing": "sm",
            "contents": [
                {"type": "text", "text": "ยกเลิกการจองนี้ใช่ไหม?", "weight": "bold", "size": "md"},
                {"type": "text", "text": f"คอร์ด {court}", "size": "sm"},
                {"type": "text", "text": f"วันที่ {_thai_date(date)}", "size": "sm"},
                {"type": "text", "text": f"เวลา {slot_label(hour)}", "size": "sm"},
            ],
        },
        "footer": {
            "type": "box",
            "layout": "vertical",
            "spacing": "sm",
            "contents": [
                {
                    "type": "button",
                    "style": "primary",
                    "color": "#C62828",
                    "height": "sm",
                    "action": {
                        "type": "postback",
                        "label": "✅ ยืนยันยกเลิก",
                        "data": f"a=cancel_do&d={date}&c={court}&h={hour}",
                    },
                },
                {
                    "type": "button",
                    "style": "secondary",
                    "height": "sm",
                    "action": {"type": "postback", "label": "ไม่ยกเลิก", "data": "a=mybookings"},
                },
            ],
        },
    }
    return _flex("ยืนยันการยกเลิกการจอง", bubble)