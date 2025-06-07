"""
LINE × Notion Knowledge Bot  ─ 2025‑06 rev.
================================================
• 使用 Notion Database 作為單一可靠知識來源。
• 若文字命中『內容』欄，整理條目後交由 GPT 回覆；
  無命中則回「資料庫沒有相關資訊」。
• 支援 >100 筆資料—自動分頁抓取。
• 自動去除環境變數尾端換行／空白；缺少必填參數會在啟動時直接 raise。

必要環境變數（Railway Service Variables）
------------------------------------------
OPENAI_API_KEY             = sk‑…
NOTION_API_KEY             = ntn_… / secret_…
NOTION_DB_ID               = 32 字元 db id
LINE_CHANNEL_ACCESS_TOKEN  = xxxxxxxxxx
LINE_CHANNEL_SECRET        = xxxxxxxxxx

可選：
STAFF_GROUP_ID  → 低信心時推播到固定群組（本版僅保留欄位，未實作）。
"""

from __future__ import annotations

import os
import requests
import openai
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage

# ---------- config ----------
openai.api_key              = os.getenv("OPENAI_API_KEY", "").strip()
NOTION_API_KEY              = os.getenv("NOTION_API_KEY", "").strip()
NOTION_DB_ID                = os.getenv("NOTION_DB_ID", "").strip()
LINE_CHANNEL_ACCESS_TOKEN   = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "").strip()
LINE_CHANNEL_SECRET         = os.getenv("LINE_CHANNEL_SECRET", "").strip()
STAFF_GROUP_ID              = os.getenv("STAFF_GROUP_ID")  # optional
NOTION_VERSION              = "2022-06-28"

REQUIRED_VARS = {
    "OPENAI_API_KEY": openai.api_key,
    "NOTION_API_KEY": NOTION_API_KEY,
    "NOTION_DB_ID":   NOTION_DB_ID,
    "LINE_CHANNEL_ACCESS_TOKEN": LINE_CHANNEL_ACCESS_TOKEN,
    "LINE_CHANNEL_SECRET":      LINE_CHANNEL_SECRET,
}
missing = [k for k, v in REQUIRED_VARS.items() if not v]
if missing:
    raise RuntimeError(f"❌ 缺少必填環境變數: {', '.join(missing)}")

# ---------- LINE client ----------
line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler      = WebhookHandler(LINE_CHANNEL_SECRET)
app          = Flask(__name__)

# ---------- Notion helpers ----------

def _post_notion(json_payload: dict):
    url = f"https://api.notion.com/v1/databases/{NOTION_DB_ID}/query"
    headers = {
        "Authorization": f"Bearer {NOTION_API_KEY}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }
    res = requests.post(url, headers=headers, json=json_payload, timeout=15)
    res.raise_for_status()
    return res.json()

def fetch_all_pages() -> list[dict]:
    pages: list[dict] = []
    payload: dict = {"page_size": 100}
    while True:
        data = _post_notion(payload)
        pages.extend(data["results"])
        if not data.get("has_more"):
            break
        payload["start_cursor"] = data["next_cursor"]
    return pages

def search_notion(keyword: str) -> list[str]:
    """簡易 substring 比對；回傳整理後的條目清單。"""
    kw_lower = keyword.lower()
    hits: list[str] = []
    for page in fetch_all_pages():
        props = page["properties"]
        rich   = props.get("內容", {}).get("rich_text", [])
        serial = props.get("序號", {}).get("title", [])
        content = "".join(t["plain_text"] for t in rich) if rich else ""
        order   = serial[0]["plain_text"] if serial else ""
        if kw_lower in content.lower():
            hits.append(f"{order}: {content}")
    return hits

# ---------- GPT helper ----------

def gpt_answer(question: str, chunks: list[str]) -> str:
    if not chunks:
        return "資料庫沒有相關資訊"
    sys_prompt = (
        "你是鋼鐵公司內部助理，僅能用下列條目回答，"
        "不得加入任何推測或額外資訊。\n\n" + "\n".join(chunks)
    )
    rsp = openai.ChatCompletion.create(
        model="gpt-3.5-turbo",
        temperature=0,
        messages=[
            {"role": "system", "content": sys_prompt},
            {"role": "user", "content": question},
        ],
        timeout=20,
    )
    return rsp.choices[0].message.content.strip()

# ---------- webhook ----------

@app.route("/webhook", methods=["POST"])
def webhook():
    signature = request.headers.get("X-Line-Signature", "")
    body      = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return "OK"

@handler.add(MessageEvent, message=TextMessage)
def handle_message(event: MessageEvent):
    user_text = event.message.text.strip()
    notion_hits = search_notion(user_text)
    reply_text  = gpt_answer(user_text, notion_hits)
    line_bot_api.reply_message(event.reply_token, TextSendMessage(reply_text))

# ---------- local runner ----------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 8080)))
