from __future__ import annotations

import os
import time
import json
import logging
from typing import Dict, List, Tuple

import regex
import requests
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage
from openai import OpenAI

# -------------------- logging --------------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")

# -------------------- env ------------------------
OPENAI_API_KEY            = os.getenv("OPENAI_API_KEY", "").strip()
NOTION_API_KEY            = os.getenv("NOTION_API_KEY", "").strip()
NOTION_DB_ID              = os.getenv("NOTION_DB_ID", "").strip()
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "").strip()
LINE_CHANNEL_SECRET       = os.getenv("LINE_CHANNEL_SECRET", "").strip()
if not all([OPENAI_API_KEY, NOTION_API_KEY, NOTION_DB_ID, LINE_CHANNEL_ACCESS_TOKEN, LINE_CHANNEL_SECRET]):
    raise RuntimeError("Missing env vars")

client       = OpenAI(api_key=OPENAI_API_KEY)
NOTION_VER   = "2022-06-28"
NOTION_HDRS  = {
    "Authorization": f"Bearer {NOTION_API_KEY}",
    "Notion-Version": NOTION_VER,
    "Content-Type": "application/json",
}

# -------------------- LINE/Flask -----------------
line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler      = WebhookHandler(LINE_CHANNEL_SECRET)
app          = Flask(__name__)

# -------------------- Notion helpers --------------
def _post_notion(payload: dict) -> dict:
    url = f"https://api.notion.com/v1/databases/{NOTION_DB_ID}/query"
    r = requests.post(url, headers=NOTION_HDRS, json=payload, timeout=20)
    r.raise_for_status()
    return r.json()

_CACHE: Dict[str, object] = {"ts": 0.0, "pages": []}
_CACHE_TTL = 60  # seconds

def fetch_all_pages() -> List[dict]:
    now = time.time()
    if _CACHE["pages"] and now - _CACHE["ts"] < _CACHE_TTL:
        return _CACHE["pages"]  # type: ignore

    pages: List[dict] = []
    payload: dict = {"page_size": 100}
    while True:
        data = _post_notion(payload)
        pages.extend(data.get("results", []))
        if not data.get("has_more"):
            break
        payload["start_cursor"] = data["next_cursor"]
    _CACHE.update(ts=now, pages=pages)
    return pages

def _extract_text(prop: dict) -> str:
    t = prop.get("type")
    v = prop.get(t)
    if t in ("title", "rich_text") and isinstance(v, list):
        return "".join([x.get("plain_text", "") for x in v])
    if t == "select" and isinstance(v, dict):
        return v.get("name", "") or ""
    if t == "multi_select" and isinstance(v, list):
        return "、".join([x.get("name", "") for x in v])
    if t == "number":
        return str(v) if v is not None else ""
    if t == "checkbox":
        return "是" if v else "否"
    if t in ("url", "email", "phone_number"):
        return v or ""
    if isinstance(v, list):
        return "".join([str(x) for x in v])
    if isinstance(v, dict):
        return json.dumps(v, ensure_ascii=False)
    return str(v) if v is not None else ""

def _page_title(pg: dict) -> str:
    props = pg.get("properties", {})
    for name, prop in props.items():
        if prop.get("type") == "title":
            return _extract_text(prop).strip()
    return "(未命名)"

def _page_serial(pg: dict) -> str:
    # 依你的 DB，序號欄叫「序號」。若有別名請加到 candidates。
    candidates = ["序號", "編號", "Serial", "No", "序列"]
    props = pg.get("properties", {})
    for key in candidates:
        if key in props:
            s = _extract_text(props[key]).strip()
            if s:
                return s
    # 有些把序號塞到 title 前綴（像 3-1 XXX）
    t = _page_title(pg)
    return t.split()[0] if t else "-"

def _page_label(pg: dict) -> str:
    # 你的欄位是「標籤」。若有別名請加到 candidates。
    candidates = ["標籤", "分類", "類別", "Label", "Category"]
    props = pg.get("properties", {})
    for key in candidates:
        if key in props:
            s = _extract_text(props[key]).strip()
            if s:
                return s
    return ""

def _page_content(pg: dict) -> str:
    # 你的欄位叫「內容」。若有別名請加到 candidates。
    candidates = ["內容", "Content", "說明", "Text", "備註"]
    props = pg.get("properties", {})
    for key in candidates:
        if key in props:
            s = _extract_text(props[key]).strip()
            if s:
                return s
    # fallback: 把所有欄位串起來
    texts = []
    for v in props.values():
        texts.append(_extract_text(v))
    return "  ".join(texts).strip()

def _normalize(s: str) -> str:
    return regex.sub(r"[\p{P}\p{S}\p{Z}\p{C}]+", "", s).lower()

# -------------------- 搜尋：分類優先 ----------------
def list_label_items_by_keyword(keyword: str) -> Tuple[str, List[dict], int]:
    kw = _normalize(keyword)
    if not kw:
        return "", [], 0

    pages = fetch_all_pages()
    buckets: Dict[str, List[dict]] = {}
    for pg in pages:
        lb = _page_label(pg)
        if not lb:
            continue
        buckets.setdefault(lb, []).append(pg)

    # 允許子字串，如「報價」命中「客戶報價」
    best = ""
    for lb in buckets.keys():
        if _normalize(lb).find(kw) != -1:
            # 盡量選字數較長的（更專一）
            if len(lb) > len(best):
                best = lb

    if not best:
        return "", [], 0

    items = buckets[best]
    total = len(items)

    # 讓序號 3-1, 3-2…有序
    def _key(pg: dict):
        s = _page_serial(pg)
        if "-" in s:
            a, b = s.split("-", 1)
            import re
            return (int(re.sub(r"\D", "", a) or 0), int(re.sub(r"\D", "", b) or 0))
        import re
        return (int(re.sub(r"\D", "", s) or 0), 0)

    try:
        items = sorted(items, key=_key)
    except Exception:
        pass

    return best, items, total

def search_pages_by_fulltext(keyword: str) -> List[dict]:
    """依『內容』欄（找不到則用全文）做包含搜尋，回傳所有命中頁。"""
    kw = _normalize(keyword)
    if not kw:
        return []
    hits: List[dict] = []
    for pg in fetch_all_pages():
        text = _page_content(pg)
        if kw in _normalize(text):
            hits.append(pg)
    # 依序號排序
    try:
        hits = sorted(hits, key=lambda p: _page_serial(p))
    except Exception:
        pass
    return hits

# -------------------- LINE reply utils -------------
def split_long_text(s: str, max_len: int = 900) -> List[str]:
    out: List[str] = []
    cur = ""
    for line in s.splitlines(True):
        if len(cur) + len(line) > max_len:
            out.append(cur)
            cur = ""
        cur += line
    if cur:
        out.append(cur)
    return out

def reply_texts(reply_token: str, blocks: List[str]):
    """
    一次 reply 最多 5 則訊息，避免超長。
    """
    # 把超長 block 先切小塊
    msgs: List[TextSendMessage] = []
    for b in blocks:
        for chunk in split_long_text(b, 900):
            msgs.append(TextSendMessage(chunk))
            if len(msgs) >= 5:
                break
        if len(msgs) >= 5:
            break
    if not msgs:
        msgs = [TextSendMessage("（沒有可顯示的內容）")]
    line_bot_api.reply_message(reply_token, msgs)

# -------------------- GPT（只用於小聊/一般聊） --------
def gpt_general_chat(user_text: str) -> str:
    rsp = client.chat.completions.create(
        model="gpt-4o",
        temperature=0.7,
        messages=[
            {"role": "system", "content": "你是親切的助理，使用繁體中文，簡潔回答。"},
            {"role": "user",   "content": user_text},
        ],
        timeout=20,
    )
    return (rsp.choices[0].message.content or "").strip()

SMALLTALK_RE = regex.compile(r"^(早安|午安|晚安|你好|您好|嗨+|哈囉|謝謝|感謝|掰掰|再見|辛苦了|天氣很好|天氣不錯)$")
def is_smalltalk(s: str) -> bool:
    return bool(SMALLTALK_RE.match(_normalize(s)))

# -------------------- Webhook ----------------------
@app.route("/webhook", methods=["POST"])
def webhook():
    raw = request.get_data(as_text=True)
    sig = request.headers.get("X-Line-Signature", "")
    try:
        handler.handle(raw, sig)
    except InvalidSignatureError:
        abort(400)
    return "OK"

# -------------------- Handler ----------------------
@handler.add(MessageEvent, message=TextMessage)
def handle_message(event: MessageEvent):
    user_text = (event.message.text or "").strip()
    logging.info("User: %s", user_text)

    # 1) 先判斷是否命中「標籤/分類」
    label, items, total = list_label_items_by_keyword(user_text)
    if items:
        # 依你的需求：列出該分類全部清單（序號+標題）
        lines = [f"{_page_serial(pg)}　{_page_title(pg)}" for pg in items]
        head  = f"「{label}」共有 {total} 筆：\n"
        body  = "\n".join(lines)
        tail  = "\n\n要看其中一條，直接輸入序號（例如：3-7），或再補關鍵字。"
        reply_texts(event.reply_token, [head + body + tail])
        return

    # 2) 若不是分類，就用「內容」做全文搜尋
    hits = search_pages_by_fulltext(user_text)
    if hits:
        blocks: List[str] = []
        for pg in hits:
            serial  = _page_serial(pg) or "-"
            title   = _page_title(pg)
            content = _page_content(pg)  # 直接取「內容」欄全文
            blocks.append(f"【{serial}　{title}】\n{content}")
        # 太長自動切段，超過 5 段會截斷（LINE 限制一次最多 5 則訊息）
        reply_texts(event.reply_token, blocks)
        return

    # 3) 沒有命中資料庫：若是小聊 → 小聊
    if is_smalltalk(user_text):
        reply_texts(event.reply_token, [gpt_general_chat(user_text)])
        return

    # 4) 其他 → 一般聊天
    reply_texts(event.reply_token, [gpt_general_chat(user_text)])

# -------------------- local run --------------------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 8080)))
