"""
LINE × Notion Knowledge Bot  ─ 2025‑06 rev.2
================================================
• 讀取 Notion Database 作為唯一知識來源，命中就用 GPT 重組回答。  
• 不再硬寫欄位名；自動從 **所有 property** 的 title / rich_text 擷取文字搜尋。  
• 支援分頁 (`has_more`)；自動 `.strip()` 去除環境變數末端換行。  
• 缺少必填環境變數即 raise，避免隱性 500。
"""

from __future__ import annotations

import os
import re
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
NOTION_VERSION              = "2022-06-28"

REQUIRED = {
    "OPENAI_API_KEY": openai.api_key,
    "NOTION_API_KEY": NOTION_API_KEY,
    "NOTION_DB_ID":   NOTION_DB_ID,
    "LINE_CHANNEL_ACCESS_TOKEN": LINE_CHANNEL_ACCESS_TOKEN,
    "LINE_CHANNEL_SECRET":      LINE_CHANNEL_SECRET,
}
missing = [k for k, v in REQUIRED.items() if not v]
if missing:
    raise RuntimeError("❌ 缺少環境變數: " + ", ".join(missing))

# ---------- LINE client ----------
line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler      = WebhookHandler(LINE_CHANNEL_SECRET)
app          = Flask(__name__)

# ---------- Notion helpers ----------

def _post_notion(payload: dict) -> dict:
    url = f"https://api.notion.com/v1/databases/{NOTION_DB_ID}/query"
    headers = {
        "Authorization": f"Bearer {NOTION_API_KEY}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }
    res = requests.post(url, headers=headers, json=payload, timeout=15)
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

def _extract_text(prop: dict) -> str:
    """安全擷取 title / rich_text 為純文字，其他類型回空字串"""
    t = prop.get(prop.get("type", ""), [])
    return "".join(chunk["plain_text"] for chunk in t) if isinstance(t, list) else ""

def search_notion(keyword: str) -> list[str]:
    pat = re.compile(re.escape(keyword.lower()))
    hits: list[str] = []
    for page in fetch_all_pages():
        props = page["properties"]
        # 聚合所有欄位文字
        full_text = "  ".join(_extract_text(v) for v in props.values()).lower()
        if pat.search(full_text):
            serial = _extract_text(props.get("序號", {})) or "—"
            snippet = full_text[:120] + ("…" if len(full_text) > 120 else "")
            hits.append(f"{serial}: {snippet}")
    return hits

# ---------- GPT helper ----------

def gpt_answer(question: str, chunks: list[str]) -> str:
    if not chunks:
        return "資料庫沒有相關資訊"
    system = (
        "你是鋼鐵公司內部知識助理，只能根據下列條目回答；若條目不足以回答，說『資料庫沒有相關資訊』。\n\n"+
        "\n".join(chunks)
    )
    rsp = openai.ChatCompletion.create(
        model="gpt-3.5-turbo",
        temperature=0,
        messages=[
            {"role": "system", "content": system},
            {"role": "user",   "content": question},
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
    hits      = search_notion(user_text)
    reply_txt = gpt_answer(user_text, hits)
    line_bot_api.reply_message(event.reply_token, TextSendMessage(reply_txt))

# ---------- local runner ----------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 8080)))

