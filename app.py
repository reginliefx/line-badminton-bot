import json
import logging
import os
from urllib.parse import parse_qsl, quote

from dotenv import load_dotenv
from flask import Flask, abort, request
from google import genai
from google.genai import types
from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    ApiClient,
    Configuration,
    FlexContainer,
    FlexMessage,
    ImageMessage,
    MessagingApi,
    PushMessageRequest,
    ReplyMessageRequest,
    TextMessage,
)
from linebot.v3.webhooks import MessageEvent, PostbackEvent, TextMessageContent

import booking
import handoff

load_dotenv()
logging.basicConfig(level=logging.INFO)

app = Flask(__name__)

handler = WebhookHandler(os.environ["LINE_CHANNEL_SECRET"])
line_config = Configuration(access_token=os.environ["LINE_CHANNEL_ACCESS_TOKEN"])

gemini = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.5-flash-lite")
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# โดเมนของเว็บที่ deploy เช่น https://line-badminton-bot.vercel.app (ใช้ประกอบ URL รูปในโฟลเดอร์ public/)
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")
# user ID ของแอดมิน (คั่นด้วยคอมมา) ใช้รับแจ้งเตือนเมื่อลูกค้าขอคุยกับผู้ดูแล — พิมพ์ myid หาบอทเพื่อดู ID ของตัวเอง
ADMIN_USER_IDS = [x.strip() for x in os.environ.get("ADMIN_USER_IDS", "").split(",") if x.strip()]

with open(os.path.join(BASE_DIR, "system_prompt.txt"), encoding="utf-8") as f:
    SYSTEM_PROMPT = f.read().strip()


def load_rules():
    """โหลด wake word จาก keywords.json ทุกครั้งที่มีข้อความเข้า"""
    with open(os.path.join(BASE_DIR, "keywords.json"), encoding="utf-8") as f:
        return json.load(f)["rules"]


def match_wake_word(text: str):
    t = text.strip().lower()
    for rule in load_rules():
        for kw in rule["keywords"]:
            if kw.lower() in t:
                return rule
    return None


def build_messages(rule: dict, user_id: str | None = None):
    """สร้างรายการข้อความตอบกลับจากกฎ: ข้อความ (reply), Flex (flex_file),
    รูป (images) และระบบจองคอร์ด (action)
    LINE ส่งได้สูงสุด 5 ข้อความต่อการตอบหนึ่งครั้ง"""
    messages = []
    if rule.get("reply"):
        messages.append(TextMessage(text=rule["reply"]))
    if rule.get("flex_file"):
        with open(os.path.join(BASE_DIR, rule["flex_file"]), encoding="utf-8") as f:
            flex_json = f.read()
        messages.append(
            FlexMessage(
                alt_text=rule.get("alt_text", "ข้อความใหม่"),
                contents=FlexContainer.from_json(flex_json),
            )
        )
    # รูปภาพ: ใส่เป็นชื่อไฟล์ในโฟลเดอร์ public/ (เช่น "event1.jpg") หรือ URL https เต็ม ๆ ก็ได้
    for img in rule.get("images", []):
        url = img if img.startswith("http") else f"{PUBLIC_BASE_URL}/{quote(img)}"
        messages.append(ImageMessage(original_content_url=url, preview_image_url=url))
    # ระบบจองคอร์ด
    action = rule.get("action")
    if action == "book_menu":
        messages.append(booking.court_picker_flex())
    elif action == "status":
        messages.append(booking.status_flex())
    elif action == "my_bookings":
        messages.append(booking.my_bookings_message(user_id))
    return messages[:5]


def ask_gemini(text: str) -> str:
    try:
        resp = gemini.models.generate_content(
            model=GEMINI_MODEL,
            contents=text,
            config=types.GenerateContentConfig(
                system_instruction=SYSTEM_PROMPT,
                max_output_tokens=800,
            ),
        )
        # LINE จำกัดข้อความละ 5000 ตัวอักษร
        return (resp.text or "ขอโทษครับ ตอบไม่ได้ตอนนี้")[:4900]
    except Exception:
        logging.exception("Gemini error")
        return "ขอโทษครับ ระบบ AI มีปัญหาชั่วคราว ลองใหม่อีกครั้งนะ"


def get_display_name(user_id: str) -> str:
    try:
        with ApiClient(line_config) as client:
            return MessagingApi(client).get_profile(user_id).display_name
    except Exception:
        return ""


def push_to_admins(messages):
    """ส่งข้อความหาแอดมินทุกคน (นับเป็นโควตา push ของ LINE OA)"""
    if not ADMIN_USER_IDS:
        logging.warning("ADMIN_USER_IDS ว่าง — ไม่ได้แจ้งแอดมิน")
        return
    with ApiClient(line_config) as client:
        api = MessagingApi(client)
        for admin_id in ADMIN_USER_IDS:
            try:
                api.push_message(PushMessageRequest(to=admin_id, messages=messages))
            except Exception:
                logging.exception("push to admin failed: %s", admin_id)


NOTIFY_ON_RESUME = os.environ.get("NOTIFY_CUSTOMER_ON_RESUME", "1") == "1"
RESUME_TEXT = "🤖 บอทกลับมาตอบแล้วครับ ถามได้เลย"


def push_to_user(user_id: str, messages):
    try:
        with ApiClient(line_config) as client:
            MessagingApi(client).push_message(PushMessageRequest(to=user_id, messages=messages))
    except Exception:
        logging.exception("push to user failed: %s", user_id)


def notify_resumed(user_id: str):
    """บอกลูกค้าว่าบอทกลับมาตอบแล้ว (นับเป็นโควตา push — ปิดได้ด้วย NOTIFY_CUSTOMER_ON_RESUME=0)"""
    if NOTIFY_ON_RESUME:
        push_to_user(user_id, [TextMessage(text=RESUME_TEXT)])


def close_case_messages():
    """แอดมินสั่ง 'ปิดเคส': ถ้ามีลูกค้ารอคนเดียวคืนให้บอทเลย ถ้ามีหลายคนส่งการ์ดให้เลือก"""
    items = handoff.list_paused()
    if not items:
        return [TextMessage(text="ไม่มีลูกค้าที่รอแอดมินตอบอยู่ครับ")]
    if len(items) == 1:
        user_id, name, _ = items[0]
        handoff.resume(user_id)
        notify_resumed(user_id)
        return [TextMessage(text=f"✅ ปิดเคสแล้ว คืนให้บอท: {name or 'ลูกค้า'}\nบอทกลับมาตอบลูกค้ารายนี้แล้วครับ")]
    return [handoff.paused_list_flex(items)]


def send(event, messages):
    with ApiClient(line_config) as client:
        MessagingApi(client).reply_message(
            ReplyMessageRequest(reply_token=event.reply_token, messages=messages)
        )


@app.post("/callback")
def callback():
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return "OK"


@handler.add(MessageEvent, message=TextMessageContent)
def on_text(event: MessageEvent):
    text = event.message.text
    user_id = getattr(event.source, "user_id", None)

    # ดู user ID ของตัวเอง (ใช้ตั้งค่า ADMIN_USER_IDS)
    if text.strip().lower() == "myid":
        send(event, [TextMessage(text=f"user ID ของคุณ:\n{user_id}")])
        return

    rule = match_wake_word(text)
    action = rule.get("action") if rule else None

    # แอดมินพิมพ์ "ปิดเคส" หาบอทใน LINE ส่วนตัว → คืนลูกค้าให้บอท (คนทั่วไปพิมพ์คำนี้ = ข้อความปกติ)
    if action == "admin_close":
        if user_id and user_id in ADMIN_USER_IDS:
            try:
                send(event, close_case_messages())
            except Exception:
                logging.exception("close case error")
                send(event, [TextMessage(text="ขอโทษครับ ระบบมีปัญหาชั่วคราว ลองใหม่อีกครั้งนะ")])
            return
        rule, action = None, None

    # ลูกค้าอยู่ในช่วงให้แอดมินตอบ: บอทเงียบ (ยกเว้นคำสั่งคืนให้บอท)
    if user_id and action != "resume_bot" and handoff.is_paused(user_id):
        return

    try:
        if user_id and action == "handoff":
            name = get_display_name(user_id)
            try:
                handoff.pause(user_id, name)
            except Exception:
                logging.exception("handoff.pause error")
            push_to_admins([handoff.admin_notice_flex(user_id, name, text)])
        elif user_id and action == "resume_bot":
            handoff.resume(user_id)
    except Exception:
        logging.exception("handoff side-effect error")

    if rule:
        try:
            messages = build_messages(rule, user_id)
        except Exception:
            logging.exception("build_messages error")
            messages = [
                TextMessage(
                    text=rule.get("reply") or "ขอโทษครับ ระบบมีปัญหาชั่วคราว ลองใหม่อีกครั้งนะ"
                )
            ]
    else:
        messages = [TextMessage(text=ask_gemini(text))]
    send(event, messages)


@handler.add(PostbackEvent)
def on_postback(event: PostbackEvent):
    """ปุ่มใน Flex การจอง: a=menu | a=court&c=2 | a=book&c=2&h=14"""
    data = dict(parse_qsl(event.postback.data))
    action = data.get("a")
    try:
        if action == "menu":
            messages = [booking.court_picker_flex()]
        elif action == "resume":
            # ปุ่ม "คืนให้บอท" จากแจ้งเตือน — เฉพาะแอดมินเท่านั้น
            if event.source.user_id not in ADMIN_USER_IDS:
                return
            target = data.get("u", "")
            n = handoff.resume(target)
            if n:
                notify_resumed(target)
            messages = [
                TextMessage(
                    text="✅ คืนให้บอทแล้ว บอทกลับมาตอบลูกค้ารายนี้" if n else "ลูกค้ารายนี้ไม่ได้อยู่ในสถานะหยุดบอทแล้วครับ"
                )
            ]
        elif action == "resume_all":
            if event.source.user_id not in ADMIN_USER_IDS:
                return
            waiting = [uid for uid, _name, _until in handoff.list_paused()]
            n = handoff.resume_all()
            for uid in waiting:
                notify_resumed(uid)
            messages = [TextMessage(text=f"✅ คืนให้บอทแล้ว {n} คน" if n else "ไม่มีลูกค้าที่รอแอดมินตอบอยู่ครับ")]
        elif action == "court":
            messages = [booking.slot_picker_flex(int(data["c"]))]
        elif action == "book":
            court, hour = int(data["c"]), int(data["h"])
            user_id = event.source.user_id
            result = booking.book_slot(court, hour, user_id, get_display_name(user_id))
            messages = [
                TextMessage(text=booking.result_text(result, court, hour)),
                booking.slot_picker_flex(court),
            ]
        else:
            return
    except Exception:
        logging.exception("postback error")
        messages = [TextMessage(text="ขอโทษครับ ระบบจองมีปัญหาชั่วคราว ลองใหม่อีกครั้งนะ")]
    send(event, messages)


@app.get("/")
def health():
    return "ok"


if __name__ == "__main__":
    app.run(port=int(os.environ.get("PORT", 5000)))
