"""ส่งต่อให้แอดมินตอบแทน (human handoff)

ลูกค้าพิมพ์ "ติดต่อผู้ดูแล" → บอท/AI หยุดตอบลูกค้าคนนั้นชั่วคราว → แอดมินตอบในแชตของ LINE OA
→ แอดมินกดปุ่ม "คืนให้บอท" (หรือลูกค้าพิมพ์ "คุยกับบอท" หรือรอจนหมดเวลา) บอทก็กลับมาตอบ

เก็บสถานะในแท็บ "handoff" ของ Google Sheets (แถวแรกเป็นหัวตาราง):
    user_id | name | paused_until | updated_at
"""
import json
import logging
import os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from linebot.v3.messaging import FlexContainer, FlexMessage

import booking

TZ = ZoneInfo("Asia/Bangkok")
SHEET_NAME = os.environ.get("HANDOFF_SHEET_NAME", "handoff")
PAUSE_MINUTES = int(os.environ.get("HANDOFF_MINUTES", "120"))

_ws = None


def _worksheet():
    global _ws
    if _ws is None:
        _ws = booking.spreadsheet().worksheet(SHEET_NAME)
    return _ws


def _now():
    return datetime.now(TZ)


def is_paused(user_id: str) -> bool:
    """True ถ้าลูกค้าคนนี้อยู่ในช่วงให้แอดมินตอบ (ยังไม่หมดเวลา)
    ถ้าอ่านชีตไม่ได้ ให้ถือว่าไม่ได้หยุด เพื่อให้บอทยังทำงานต่อ"""
    try:
        for row in _worksheet().get_all_values()[1:]:
            if len(row) >= 3 and row[0] == user_id:
                try:
                    if datetime.fromisoformat(row[2]) > _now():
                        return True
                except ValueError:
                    continue
        return False
    except Exception:
        logging.exception("handoff.is_paused error")
        return False


def resume(user_id: str) -> int:
    """คืนให้บอท (ลบสถานะหยุด) คืนจำนวนแถวที่ลบ"""
    ws = _worksheet()
    rows = ws.get_all_values()
    targets = [i for i, r in enumerate(rows, start=1) if i > 1 and r and r[0] == user_id]
    for i in reversed(targets):  # ลบจากล่างขึ้นบน เลขแถวจะไม่เพี้ยน
        ws.delete_rows(i)
    return len(targets)


def list_paused() -> list:
    """คืน [(user_id, name, หมดเวลา)] ของลูกค้าที่บอทกำลังหยุดตอบอยู่ (ยังไม่หมดเวลา)"""
    out = []
    for row in _worksheet().get_all_values()[1:]:
        if len(row) >= 3 and row[0]:
            try:
                until = datetime.fromisoformat(row[2])
            except ValueError:
                continue
            if until > _now():
                out.append((row[0], row[1], until))
    return out


def resume_all() -> int:
    """คืนให้บอททุกคน (ลบทุกแถวยกเว้นหัวตาราง) คืนจำนวนแถวที่ลบ"""
    ws = _worksheet()
    rows = ws.get_all_values()
    targets = [i for i, r in enumerate(rows, start=1) if i > 1 and r and r[0]]
    for i in reversed(targets):
        ws.delete_rows(i)
    return len(targets)


def paused_list_flex(items: list) -> FlexMessage:
    """การ์ดรายชื่อลูกค้าที่รอแอดมิน พร้อมปุ่มคืนให้บอทรายคน/ทั้งหมด"""
    rows = []
    for user_id, name, _until in items[:10]:
        rows.append(
            {
                "type": "box",
                "layout": "horizontal",
                "spacing": "md",
                "margin": "sm",
                "contents": [
                    {"type": "text", "text": name or "(ไม่ทราบชื่อ)", "flex": 3, "gravity": "center", "wrap": True},
                    {
                        "type": "button",
                        "style": "secondary",
                        "height": "sm",
                        "flex": 2,
                        "action": {"type": "postback", "label": "คืนให้บอท", "data": f"a=resume&u={user_id}"},
                    },
                ],
            }
        )
    if len(items) > 10:
        rows.append({"type": "text", "text": f"และอีก {len(items) - 10} คน", "size": "xs", "color": "#888888", "margin": "md"})
    bubble = {
        "type": "bubble",
        "header": {
            "type": "box",
            "layout": "vertical",
            "contents": [{"type": "text", "text": f"🙋 ลูกค้าที่รอแอดมิน ({len(items)})", "weight": "bold"}],
        },
        "body": {"type": "box", "layout": "vertical", "contents": rows},
        "footer": {
            "type": "box",
            "layout": "vertical",
            "contents": [
                {
                    "type": "button",
                    "style": "primary",
                    "color": "#0B6E4F",
                    "height": "sm",
                    "action": {"type": "postback", "label": "✅ คืนให้บอททั้งหมด", "data": "a=resume_all"},
                }
            ],
        },
    }
    return FlexMessage(alt_text="ลูกค้าที่รอแอดมิน", contents=FlexContainer.from_json(json.dumps(bubble)))


def pause(user_id: str, name: str) -> datetime:
    """ให้บอทหยุดตอบลูกค้าคนนี้ไปอีก PAUSE_MINUTES นาที"""
    resume(user_id)  # ลบแถวเก่าก่อน กันซ้ำ
    until = _now() + timedelta(minutes=PAUSE_MINUTES)
    _worksheet().append_row(
        [user_id, name, until.isoformat(timespec="seconds"), _now().isoformat(timespec="seconds")],
        value_input_option="RAW",
    )
    return until


def admin_notice_flex(user_id: str, name: str, text: str) -> FlexMessage:
    """การ์ดแจ้งเตือนแอดมิน มีปุ่ม 'คืนให้บอท'"""
    bubble = {
        "type": "bubble",
        "body": {
            "type": "box",
            "layout": "vertical",
            "spacing": "sm",
            "contents": [
                {"type": "text", "text": "🙋 ผู้ใช้งานขอคุยกับผู้ดูแล", "weight": "bold", "size": "md"},
                {"type": "text", "text": f"ชื่อ: {name or '(ไม่ทราบชื่อ)'}", "size": "sm", "wrap": True},
                {"type": "text", "text": f"ข้อความ: {text[:200]}", "size": "sm", "color": "#666666", "wrap": True},
                {
                    "type": "text",
                    "text": "เรียกผู้ดูแลแล้ว กรุณารอสักครู่ "
                    f"(บอทหยุดตอบผู้ใช้งานเป็นเวลา {PAUSE_MINUTES} นาที) "
                    "เมื่อเสร็จแล้วกดปุ่มด้านล่าง ผู้ใช้งานกดคืนให้บอทเองได้ครับ",
                    "size": "xs",
                    "color": "#888888",
                    "wrap": True,
                    "margin": "md",
                },
            ],
        },
        "footer": {
            "type": "box",
            "layout": "vertical",
            "contents": [
                {
                    "type": "button",
                    "style": "primary",
                    "color": "#0B6E4F",
                    "height": "sm",
                    "action": {
                        "type": "postback",
                        "label": "✅ คืนให้บอท",
                        "data": f"a=resume&u={user_id}",
                    },
                }
            ],
        },
    }
    return FlexMessage(
        alt_text=f"ลูกค้า {name or ''} ขอคุยกับผู้ดูแล",
        contents=FlexContainer.from_json(json.dumps(bubble)),
    )
