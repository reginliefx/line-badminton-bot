import json
import logging
import os

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
    ReplyMessageRequest,
    TextMessage,
)
from linebot.v3.webhooks import MessageEvent, TextMessageContent

load_dotenv()
logging.basicConfig(level=logging.INFO)

app = Flask(__name__)

handler = WebhookHandler(os.environ["LINE_CHANNEL_SECRET"])
line_config = Configuration(access_token=os.environ["LINE_CHANNEL_ACCESS_TOKEN"])

gemini = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# โดเมนของเว็บที่ deploy เช่น https://line-badminton-bot.vercel.app (ใช้ประกอบ URL รูปในโฟลเดอร์ public/)
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")

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


def build_messages(rule: dict):
    """สร้างรายการข้อความตอบกลับจากกฎ: ข้อความ (reply) และ/หรือ Flex (flex_file)
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
        url = img if img.startswith("http") else f"{PUBLIC_BASE_URL}/{img}"
        messages.append(ImageMessage(original_content_url=url, preview_image_url=url))
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
    rule = match_wake_word(text)
    if rule:
        try:
            messages = build_messages(rule)
        except Exception:
            logging.exception("build_messages error")
            messages = [TextMessage(text=rule.get("reply", "ขออภัย เกิดข้อผิดพลาด"))]
    else:
        messages = [TextMessage(text=ask_gemini(text))]

    with ApiClient(line_config) as client:
        MessagingApi(client).reply_message(
            ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=messages,
            )
        )


@app.get("/")
def health():
    return "ok"


if __name__ == "__main__":
    app.run(port=int(os.environ.get("PORT", 5000)))
