"""
LINE × Notion Knowledge Bot — final clean version (2025‑06‑08)
==============================================================
• 自動從 Notion Database 擷取文字（所有 property 的 title / rich_text）。  
• `_normalize` 先移除標點、空白（含零寬）與控制字再比對，避免因為隱藏字元 miss hit。  
• 缺少必要環境變數即 raise；requests、OpenAI 都設置 timeout。  
• 專為 Railway + LINE OA 部署而寫，啟動路徑 `/webhook`。
"""

from __future__ import annotations

import os
import regex   # pip install regex
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
    """把 title / rich_text 內容串起來，其餘型別回空字串"""
    t = prop.get(prop.get("type", ""), [])
    if isinstance(t, list):
        return "".join(ch["plain_text"] for ch in t)
    return ""

def _normalize(txt: str) -> str:
    """去除所有標點、空白(含零寬)、控制字再轉小寫"""
    return regex.sub(r"[\p{P}\p{Z}\p{C}]+", "", txt).lower()

# ---------- search ----------

def search_notion(keyword: str) -> list[str]:
    kw_norm = _normalize(keyword)
    hits: list[str] = []

    for pg in fetch_all_pages():
        props = pg["properties"]
        full = "  ".join(_extract_text(v) for v in props.values())
        if kw_norm in _normalize(full):
            serial  = _extract_text(props.get("序號", {})) or "—"
            snippet = full[:120] + ("…" if len(full) > 120 else "")
            hits.append(f"{serial}: {snippet}")
    return hits

# ---------- GPT helper ----------

def gpt_answer(question: str, chunks: list[str]) -> str:
    if not chunks:
        return "資料庫沒有相關資訊"
    sys_prompt = (
        "你是鋼鐵公司內部知識助理，只能根據下列條目回答；若條目不足以回答請說『資料庫沒有相關資訊』。\n\n"
        + "\n".join(chunks)
    )
    rsp = openai.ChatCompletion.create(
        model="gpt-3.5-turbo",
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

