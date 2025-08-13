"""
LINE × Notion Knowledge Bot (expo branch)
- Any-field + blocks search in Notion
- RAG miss → general chat fallback
- Greeting short-circuit
"""

from __future__ import annotations

# ---------- stdlib ----------
import os
import logging
from typing import List, Dict
import regex                      # pip install regex
import requests

# ---------- 3rd-party ----------
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage
from openai import OpenAI

# ---------- env ----------
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
    "LINE_CHANNEL_SECRET":       LINE_CHANNEL_SECRET,
}
missing = [k for k, v in REQ.items() if not v]
if missing:
    raise RuntimeError("❌ Missing env vars: " + ", ".join(missing))

# ---------- LINE / Flask ----------
line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler      = WebhookHandler(LINE_CHANNEL_SECRET)
app          = Flask(__name__)
logging.basicConfig(level=logging.INFO)

# ---------- Notion helpers ----------
NOTION_HEADERS = {
    "Authorization": f"Bearer {NOTION_API_KEY}",
    "Notion-Version": NOTION_VERSION,
    "Content-Type": "application/json",
}

def _notion_post(path: str, payload: dict) -> dict:
    url = f"https://api.notion.com/v1/{path.lstrip('/')}"
    r = requests.post(url, headers=NOTION_HEADERS, json=payload, timeout=20)
    r.raise_for_status()
    return r.json()

def _notion_get(path: str) -> dict:
    url = f"https://api.notion.com/v1/{path.lstrip('/')}"
    r = requests.get(url, headers=NOTION_HEADERS, timeout=20)
    r.raise_for_status()
    return r.json()

def _db_query(payload: dict) -> dict:
    return _notion_post(f"databases/{NOTION_DB_ID}/query", payload)

def fetch_all_pages() -> List[dict]:
    pages: List[dict] = []
    payload: dict = {"page_size": 100}
    while True:
        data = _db_query(payload)
        pages.extend(data.get("results", []))
        if not data.get("has_more"):
            break
        payload["start_cursor"] = data.get("next_cursor")
    return pages

# ---------- text utils ----------
def _normalize(txt: str) -> str:
    """移除標點/空白/控制字符並轉小寫，便於比對"""
    return regex.sub(r"[\p{P}\p{Z}\p{C}]+", "", (txt or "")).lower()

def _rt_to_text(rt_list: list) -> str:
    return "".join(t.get("plain_text", "") for t in (rt_list or []))

def _prop_to_text(prop: dict) -> str:
    t = prop.get("type")
    if t == "title":        return _rt_to_text(prop.get("title"))
    if t == "rich_text":    return _rt_to_text(prop.get("rich_text"))
    if t == "select":       return (prop.get("select") or {}).get("name", "")
    if t == "multi_select": return " ".join(o.get("name", "") for o in prop.get("multi_select", []))
    if t == "status":       return (prop.get("status") or {}).get("name", "")
    if t == "people":       return " ".join((p.get("name") or p.get("id","")) for p in prop.get("people", []))
    if t == "number":       return "" if prop.get("number") is None else str(prop.get("number"))
    if t in ("url","email","phone_number"): return prop.get(t) or ""
    if t == "date":
        d = prop.get("date") or {}
        return " ~ ".join(x for x in [d.get("start"), d.get("end")] if x)
    if t == "checkbox":     return "true" if prop.get("checkbox") else "false"
    return ""

def _page_title(page: dict) -> str:
    for name, p in page.get("properties", {}).items():
        if p.get("type") == "title":
            return _prop_to_text(p) or "(未命名)"
    return "(未命名)"

def _blocks_text(page_id: str, limit_blocks: int = 120) -> str:
    """抓取頁面 top-level blocks 的文字（為效率限制數量）"""
    texts, cursor, seen = [], None, 0
    while True:
        path = f"blocks/{page_id}/children" + (f"?start_cursor={cursor}" if cursor else "")
        data = _notion_get(path)
        for b in data.get("results", []):
            bt = b.get("type")
            rich = b.get(bt, {}).get("rich_text")
            if isinstance(rich, list):
                texts.append(_rt_to_text(rich))
            seen += 1
            if seen >= limit_blocks:
                break
        if seen >= limit_blocks or not data.get("has_more"):
            break
        cursor = data.get("next_cursor")
    return " ".join(texts).strip()

def _page_all_text(page: dict) -> str:
    props_txt = " ".join(_prop_to_text(v) for v in page.get("properties", {}).values())
    blocks_txt = _blocks_text(page["id"], limit_blocks=120)
    return f"{props_txt} {blocks_txt}".strip()

# ---------- Notion search (any-field + blocks) ----------
def search_notion(keyword: str, max_hits: int = 3) -> List[str]:
    """回傳給 RAG 的 chunks（含標題/序號/摘要）。找不到則回空陣列。"""
    kw_norm = _normalize(keyword)
    chunks: List[str] = []

    # 1) 先用 /v1/search（快）
    try:
        s = _notion_post("search", {
            "query": keyword,
            "filter": {"property": "object", "value": "page"},
            "sort":   {"timestamp": "last_edited_time", "direction": "descending"},
            "page_size": 50,
        })
        for pg in s.get("results", []):
            parent = pg.get("parent", {})
            if parent.get("type") == "database_id" and parent.get("database_id") == NOTION_DB_ID:
                title   = _page_title(pg)
                serialP = pg.get("properties", {}).get("序號")
                serial  = _prop_to_text(serialP) if serialP else "—"
                preview = _page_all_text(pg)[:900]
                chunks.append(f"【{title}｜序號:{serial}】\n{preview}")
                if len(chunks) >= max_hits:
                    break
    except Exception as e:
        logging.warning("Notion /search failed: %s", e)

    # 2) 0 筆 → 保底全掃描（準）
    if not chunks:
        for pg in fetch_all_pages():
            hay = _normalize(_page_all_text(pg))
            if kw_norm and kw_norm in hay:
                title   = _page_title(pg)
                serialP = pg.get("properties", {}).get("序號")
                serial  = _prop_to_text(serialP) if serialP else "—"
                preview = _page_all_text(pg)[:900]
                chunks.append(f"【{title}｜序號:{serial}】\n{preview}")
                if len(chunks) >= max_hits:
                    break

    return chunks

# ---------- GPT helpers ----------
GREETINGS = ("早安","午安","晚安","嗨","你好","您好","謝謝")

def gpt_answer_from_kb(question: str, chunks: list[str]) -> str:
    rules = (
        "你是公司內部知識助理。嚴格規則：\n"
        "1) 只能使用下列【來源】逐字摘錄或極簡重述，不得加入外部知識、定義或推論。\n"
        "2) 若問題過短/模糊或來源指向多個不同主題，請先請使用者釐清，不要自行下定義。\n"
        "3) 若來源不足以回答，回：資料庫沒有足夠資訊回答此問題。\n"
        "4) 末行附【來源】列出使用到的條目（標題/序號）。\n"
    )
    src = "\n\n".join(chunks)
    rsp = client.chat.completions.create(
        model="gpt-4o",
        temperature=0,
        messages=[
            {"role": "system", "content": rules + "\n---【來源】---\n" + src},
            {"role": "user",   "content": question},
        ],
    )
    return (rsp.choices[0].message.content or "").strip()


def gpt_general_chat(text: str) -> str:
    rsp = client.chat.completions.create(
        model="gpt-4o-mini",
        temperature=0.7,
        messages=[
            {"role":"system","content":
             "你是公司內部助手。即使沒有內部資料，也要自然回覆一般問題與寒暄；"
             "不要捏造公司內規或數據。語氣親切、簡潔，語言跟著使用者。"},
            {"role":"user","content": text},
        ],
    )
    return (rsp.choices[0].message.content or "我在～需要我幫你查什麼？").strip()

# ---------- LINE webhook ----------
@app.route("/webhook", methods=["POST"])
def webhook():
    raw_body = request.get_data(as_text=True)
    logging.info("<< LINE RAW >> %s", raw_body)
    signature = request.headers.get("X-Line-Signature", "")
    try:
        handler.handle(raw_body, signature)
    except InvalidSignatureError:
        abort(400)
    return "OK"

@handler.add(MessageEvent, message=TextMessage)
if len(user_text) <= 2:
    hits = search_notion(user_text, max_hits=3)
    if hits:
        options = []
        for i, h in enumerate(hits, 1):
            head = h.splitlines()[0].strip()
            options.append(f"{i}. {head}")
        msg = "我找到以下相關條目，請告訴我要查哪一個，或補充關鍵資訊：\n" + "\n".join(options)
        line_bot_api.reply_message(event.reply_token, TextSendMessage(msg))
        return


    # 1) 先查 Notion（任何欄位＋內文）
    chunks = search_notion(user_text, max_hits=3)
    logging.info("Notion hits: %d", len(chunks))

    # 2) 命中 → RAG 回答；未命中 → 一般聊天
    reply = gpt_answer_from_kb(user_text, chunks) if chunks else gpt_general_chat(user_text)
    line_bot_api.reply_message(event.reply_token, TextSendMessage(reply))

# ---------- local run ----------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 8080)))
