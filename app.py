"""
LINE × Notion Knowledge Bot — gpt‑4o edition (2025‑06‑08)
========================================================
• Model 已改為 **gpt‑4o**，仍使用 openai‑python 0.28.x 介面；如要新版 SDK 再升級即可。  
• 其餘邏輯與先前一致：Notion 全欄位搜尋、文字正規化、Railway 環境變數偵錯。
"""

from __future__ import annotations

import os
import regex
import requests
import openai
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage

# ---------- configuration ----------
openai.api_key            = os.getenv("OPENAI_API_KEY", "").strip()
NOTION_API_KEY            = os.getenv("NOTION_API_KEY", "").strip()
NOTION_DB_ID              = os.getenv("NOTION_DB_ID", "").strip()
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "").strip()
LINE_CHANNEL_SECRET       = os.getenv("LINE_CHANNEL_SECRET", "").strip()
NOTION_VERSION            = "2022-06-28"

REQUIRED = {
    "OPENAI_API_KEY": openai.api_key,
    "NOTION_API_KEY": NOTION_API_KEY,
    "NOTION_DB_ID":   NOTION_DB_ID,
    "LINE_CHANNEL_ACCESS_TOKEN": LINE_CHANNEL_ACCESS_TOKEN,
    "LINE_CHANNEL_SECRET":      LINE_CHANNEL_SECRET,
}
miss = [k for k, v in REQUIRED.items() if not v]
if miss:
    raise RuntimeError("❌ 缺少環境變數: " + ", ".join(miss))

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

# ---------- text utils ----------

def _extract_text(prop: dict) -> str:
    t = prop.get(prop.get("type", ""), [])
    return "".join(r["plain_text"] for r in t) if isinstance(t, list) else ""

def _normalize(txt: str) -> str:
    return regex.sub(r"[\p{P}\p{Z}\p{C}]+", "", txt).lower()

# ---------- search ----------

def search_notion(keyword: str) -> list[str]:
    kw = _normalize(keyword)
    hits: list[str] = []
    for pg in fetch_all_pages():
        props = pg["properties"]
        full  = "  ".join(_extract_text(v) for v in props.values())
        if kw in _normalize(full):
            serial  = _extract_text(props.get("序號", {})) or "—"
            snippet = full[:120] + ("…" if len(full) > 120 else "")
            hits.append(f"{serial}: {snippet}")
    return hits

# ---------- GPT (gpt‑4o) ----------

def gpt_answer(question: str, chunks: list[str]) -> str:
    if not chunks:
        return "資料庫沒有相關資訊"
    sys_prompt = (
        "你是鋼鐵公司內部知識助理，只能根據下列條目回答；若條目不足以回答請說『資料庫沒有相關資訊』。\n\n"
        + "\n".join(chunks)
    )
    rsp = openai.ChatCompletion.create(   # openai==0.28.x 介面
        model="gpt-4o",                 # ← 已改用 GPT‑4o
        temperature=0,
        messages=[
            {"role": "system", "content": sys_prompt},
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
    reply     = gpt_answer(user_text, hits)
    line_bot_api.reply_message(event.reply_token, TextSendMessage(reply))

# ---------- local run ----------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 8080)))

