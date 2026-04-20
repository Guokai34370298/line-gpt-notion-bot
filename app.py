"""
LINE × Notion Knowledge Bot  
Upgraded to **openai‑python ≥ 1.0.0** & GPT‑4o (2025‑06‑08)
=========================================================
• 使用新版 SDK：`from openai import OpenAI`, `client.chat.completions.create(...)`  
• 仍保留 Notion 全欄位搜尋＋文字正規化＋LINE Webhook 流程  
• 如需本機測試：`pip install -r requirements.txt`（需含 regex, requests, openai>=1.3.8, flask, line-bot-sdk）
"""

from __future__ import annotations

# stdlib
import os
import sys
import json

# 3rd‑party
import regex          # pip install regex
import requests
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage
from openai import OpenAI      # ← 新 SDK

# ---------------------------------------------------------------------------
#  environment & config
# ---------------------------------------------------------------------------
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY", "").strip())
NOTION_API_KEY            = os.getenv("NOTION_API_KEY", "").strip()
NOTION_DB_ID              = os.getenv("NOTION_DB_ID", "").strip()
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "").strip()
LINE_CHANNEL_SECRET       = os.getenv("LINE_CHANNEL_SECRET", "").strip()
NOTION_VERSION            = "2022-06-28"

REQ = {
    "OPENAI_API_KEY": client.api_key,
    "NOTION_API_KEY": NOTION_API_KEY,
    "NOTION_DB_ID":   NOTION_DB_ID,
    "LINE_CHANNEL_ACCESS_TOKEN": LINE_CHANNEL_ACCESS_TOKEN,
    "LINE_CHANNEL_SECRET":      LINE_CHANNEL_SECRET,
}
missing = [k for k, v in REQ.items() if not v]
if missing:
    raise RuntimeError("❌ Missing env vars: " + ", ".join(missing))

# ---------------------------------------------------------------------------
#  LINE client / Flask
# ---------------------------------------------------------------------------
line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler      = WebhookHandler(LINE_CHANNEL_SECRET)
app          = Flask(__name__)

# ---------------------------------------------------------------------------
#  Notion helpers
# ---------------------------------------------------------------------------
def _serial_sort_key(serial: str):
    m = regex.search(r'(\d+)\s*-\s*(\d+)', serial or '')
    if m:
        return (int(m.group(1)), int(m.group(2)))
    m2 = regex.search(r'(\d+)', serial or '')
    return (int(m2.group(1)) if m2 else 999999, 999999)

def list_label_items_by_keyword(keyword: str, limit: int = 20):
    kw = _normalize(keyword)
    if not kw:
        return None, [], 0

    pages = fetch_all_pages()

    groups: dict[str, list[dict]] = {}
    for pg in pages:
        label = _page_label(pg)
        if not label:
            continue
        lbl_norm = _normalize(label)
        if kw in lbl_norm or lbl_norm in kw:
            groups.setdefault(label, []).append(pg)

    if not groups:
        return None, [], 0

    best_label = sorted(
        groups.keys(),
        key=lambda l: (_normalize(l) != kw, abs(len(_normalize(l)) - len(kw)))
    )[0]

    items = groups[best_label]
    items.sort(key=lambda pg: _serial_sort_key(_page_serial(pg)))
    total = len(items)
    return best_label, items[:limit], total

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

# ---------------------------------------------------------------------------
#  text utils
# ---------------------------------------------------------------------------

def _extract_text(prop: dict) -> str:
    t = prop.get(prop.get("type", ""), [])
    return "".join(r["plain_text"] for r in t) if isinstance(t, list) else ""

def _normalize(txt: str) -> str:
    return regex.sub(r"[\p{P}\p{Z}\p{C}]+", "", txt).lower()

# ---------------------------------------------------------------------------
#  Notion search
# ---------------------------------------------------------------------------

def search_notion(keyword: str) -> list[str]:
    kw_norm = _normalize(keyword)
    hits: list[str] = []

    for pg in fetch_all_pages():
        props = pg["properties"]
        full  = "  ".join(_extract_text(v) for v in props.values())
        if kw_norm in _normalize(full):
            serial  = _extract_text(props.get("序號", {})) or "—"
            snippet = full[:120] + ("…" if len(full) > 120 else "")
            hits.append(f"{serial}: {snippet}")
    return hits

# ---------------------------------------------------------------------------
#  GPT‑4o helper
# ---------------------------------------------------------------------------

def gpt_answer(question: str, chunks: list[str]) -> str:
    if not chunks:
        return "資料庫沒有相關資訊"

    sys_prompt = (
        "你是鋼鐵公司內部知識助理，只能根據下列 Notion 條目回答；"
        "若條目不足以回答，請回答『資料庫沒有相關資訊』。\n\n"
        + "\n".join(chunks)
    )

    rsp = client.chat.completions.create(
        model="gpt-4o",
        temperature=0,
        messages=[
            {"role": "system", "content": sys_prompt},
            {"role": "user",   "content": question},
        ],
        timeout=20,
    )
    return rsp.choices[0].message.content.strip()

# ---------------------------------------------------------------------------
#  LINE webhook
# ---------------------------------------------------------------------------

import logging
logging.basicConfig(level=logging.INFO)

@app.route("/webhook", methods=["POST"])
def webhook():
    raw_body  = request.get_data(as_text=True)
    logging.info(f"<< LINE RAW >> %s", raw_body)
    signature = request.headers.get("X-Line-Signature", "")
    body      = raw_body

    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return "OK"


@handler.add(MessageEvent, message=TextMessage)
def handle_message(event: MessageEvent):
    user_text = (event.message.text or "").strip()
    logging.info("User: %s", user_text)

    # 員工查詢自己的 LINE User ID
    if "我的" in user_text and "id" in user_text.lower():
        user_id = event.source.user_id
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(f"你的 LINE User ID 是：\n{user_id}")
        )
        return

    hits  = search_notion(user_text)
    reply = gpt_answer(user_text, hits)
    line_bot_api.reply_message(event.reply_token, TextSendMessage(reply))

# ---------------------------------------------------------------------------
#  local run (for dev)
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 8080)))
