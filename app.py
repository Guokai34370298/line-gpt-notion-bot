# app.py
# LINE × Notion Knowledge Bot (Expo version)
# - OpenAI SDK >= 1.0  (from openai import OpenAI)
# - Strict RAG: quote-or-briefly-paraphrase from Notion only; no outside knowledge
# - Full-field Notion search with /v1/search + database query fallback
# - Clear chunk header: 【標籤｜標題｜序號:3-1】
# - Short/ambiguous queries ask user to disambiguate

from __future__ import annotations

# ---------- stdlib ----------
import os
import sys
import json
import logging

# ---------- 3rd party ----------
import regex  # pip install regex
import requests
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage
from openai import OpenAI

# ---------- config & env ----------
NOTION_VERSION = "2022-06-28"
TIMEOUT_SEC = 15

client = OpenAI(api_key=os.getenv("OPENAI_API_KEY", "").strip())

NOTION_API_KEY = os.getenv("NOTION_API_KEY", "").strip()
NOTION_DB_ID = os.getenv("NOTION_DB_ID", "").strip()

LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "").strip()
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET", "").strip()

# 依你的資料庫實際欄位名
LABEL_KEYS = ["標籤"]  # 類別/流程/標籤欄位（可加其他名稱）
SERIAL_KEY = "序號"     # 序號欄位名；若沒有可設為空字串 ""

# 檢查必要變數
REQ = {
    "OPENAI_API_KEY": client.api_key,
    "NOTION_API_KEY": NOTION_API_KEY,
    "NOTION_DB_ID": NOTION_DB_ID,
    "LINE_CHANNEL_ACCESS_TOKEN": LINE_CHANNEL_ACCESS_TOKEN,
    "LINE_CHANNEL_SECRET": LINE_CHANNEL_SECRET,
}
missing = [k for k, v in REQ.items() if not v]
if missing:
    raise RuntimeError("❌ Missing env vars: " + ", ".join(missing))

# ---------- Flask / LINE ----------
app = Flask(__name__)

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

logging.basicConfig(level=logging.INFO)


# ---------- Notion helpers ----------
# 依照「3-1」的格式做排序 key；抓不到就丟到很後面
def _serial_sort_key(serial: str):
    m = regex.search(r'(\d+)\s*-\s*(\d+)', serial or '')
    if m:
        return (int(m.group(1)), int(m.group(2)))
    m2 = regex.search(r'(\d+)', serial or '')
    return (int(m2.group(1)) if m2 else 999999, 999999)

def list_label_items_by_keyword(keyword: str, limit: int = 20):
    """
    如果 keyword 看起來是在問某個「標籤（分類）」，
    回傳：(選中的標籤, 已排序的頁面列表(前 limit 筆), 該標籤總數)
    找不到就回 (None, [], 0)
    """
    kw = _normalize(keyword)
    if not kw:
        return None, [], 0

    # 直接把整個 DB 拉回來，在記錄數量不大的情況下最穩（也避免去猜欄位型別）
    pages = fetch_all_pages()

    # 收集每個標籤底下的頁面
    groups: dict[str, list[dict]] = {}
    for pg in pages:
        label = _page_label(pg)
        if not label:
            continue
        lbl_norm = _normalize(label)
        # 關鍵詞包含或被包含都算（"客戶報價" / "3.客戶報價" 都會命中）
        if kw in lbl_norm or lbl_norm in kw:
            groups.setdefault(label, []).append(pg)

    if not groups:
        return None, [], 0

    # 選擇最貼近的那個標籤（優先完全相等，其次長度更接近的）
    best_label = sorted(
        groups.keys(),
        key=lambda l: (_normalize(l) != kw, abs(len(_normalize(l)) - len(kw)))
    )[0]

    items = groups[best_label]
    # 依序號排序（3-1、3-2、3-3 …）
    items.sort(key=lambda pg: _serial_sort_key(_page_serial(pg)))
    total = len(items)
    return best_label, items[:limit], total

def _notion_headers() -> dict:
    return {
        "Authorization": f"Bearer {NOTION_API_KEY}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }


def _notion_post(path: str, payload: dict) -> dict:
    """
    path: 'databases/<id>/query'  或 'search'
    """
    if path.startswith("http"):
        url = path
    else:
        url = f"https://api.notion.com/v1/{path}"
    resp = requests.post(url, headers=_notion_headers(), json=payload, timeout=TIMEOUT_SEC)
    resp.raise_for_status()
    return resp.json()


def fetch_all_pages() -> list[dict]:
    """把 DB 全部取回（分頁）。"""
    pages: list[dict] = []
    payload: dict = {"page_size": 100}
    while True:
        data = _notion_post(f"databases/{NOTION_DB_ID}/query", payload)
        pages.extend(data.get("results", []))
        if not data.get("has_more"):
            break
        payload["start_cursor"] = data.get("next_cursor")
    return pages


# ---------- text utils ----------
def _normalize(txt: str) -> str:
    """去除標點、空白（含零寬）、控制字元並轉小寫"""
    if not txt:
        return ""
    return regex.sub(r"[\p{P}\p{Z}\p{C}]+", "", str(txt)).lower()


def _rich_array_to_text(arr) -> str:
    # Notion rich_text/title 陣列 → plain text
    if not isinstance(arr, list):
        return ""
    return "".join((x.get("plain_text") or "") for x in arr)


def _prop_to_text(prop: dict) -> str:
    if not isinstance(prop, dict):
        return ""
    t = prop.get("type")
    if t == "title":
        return _rich_array_to_text(prop.get("title", []))
    if t == "rich_text":
        return _rich_array_to_text(prop.get("rich_text", []))
    if t == "select":
        v = prop.get("select")
        return v.get("name") if isinstance(v, dict) and v else ""
    if t == "multi_select":
        v = prop.get("multi_select", [])
        return ", ".join([x.get("name", "") for x in v if isinstance(x, dict)])
    if t == "status":
        v = prop.get("status")
        return v.get("name") if isinstance(v, dict) and v else ""
    if t in {"email", "phone_number", "url"}:
        return prop.get(t) or ""
    if t == "number":
        return str(prop.get("number") or "")
    if t == "checkbox":
        return "是" if prop.get("checkbox") else "否"
    if t == "date":
        v = prop.get("date") or {}
        return v.get("start") or ""
    if t == "people":
        v = prop.get("people", [])
        return ", ".join([(p.get("name") or "") for p in v if isinstance(p, dict)])
    # 其他類型一律嘗試內部鍵
    inner = prop.get(t)
    if isinstance(inner, list):
        return _rich_array_to_text(inner)
    if isinstance(inner, str):
        return inner
    return ""


def _page_title(page: dict) -> str:
    props = page.get("properties", {})
    for name, prop in props.items():
        if prop.get("type") == "title":
            return _prop_to_text(prop)
    return ""


def _page_label(page: dict) -> str:
    props = page.get("properties", {})
    for key in LABEL_KEYS:
        if key in props:
            return _prop_to_text(props[key]) or ""
    return ""


def _page_serial(page: dict) -> str:
    if not SERIAL_KEY:
        return ""
    props = page.get("properties", {})
    return _prop_to_text(props.get(SERIAL_KEY, {})) or ""


def _page_header(page: dict) -> str:
    # 【標籤｜標題｜序號:3-1】
    title = _page_title(page) or "(未命名)"
    label = _page_label(page)
    serial = _page_serial(page)
    parts = []
    if label:
        parts.append(label)
    parts.append(title)
    if serial:
        parts.append(f"序號:{serial}")
    return "【" + "｜".join(parts) + "】"


def _page_all_text(page: dict) -> str:
    """把 properties 內所有可讀文字併在一起（含 title / rich_text / select / status…）"""
    props = page.get("properties", {})
    vals = []
    for _, prop in props.items():
        vals.append(_prop_to_text(prop))
    # Notion block 內文（段落）若要取可以再加 /blocks children；此處以屬性為主，效率較高
    return "\n".join([v for v in vals if v])


# ---------- Notion search ----------
def search_notion(keyword: str, max_hits: int = 3) -> list[str]:
    """
    先用 /v1/search 命中同 DB 的頁面 → 用 header + 預覽作為 chunk
    不夠再用 DB 全掃描補齊；比對採 normalize 包含
    """
    kw_norm = _normalize(keyword)
    chunks: list[str] = []
    seen_ids: set[str] = set()

    # 1) /v1/search
    try:
        s = _notion_post(
            "search",
            {
                "query": keyword,
                "filter": {"property": "object", "value": "page"},
                "sort": {"timestamp": "last_edited_time", "direction": "descending"},
                "page_size": 50,
            },
        )
        for pg in s.get("results", []):
            parent = pg.get("parent", {})
            if parent.get("type") == "database_id" and parent.get("database_id") == NOTION_DB_ID:
                pid = pg.get("id")
                if pid in seen_ids:
                    continue
                header = _page_header(pg)
                preview = _page_all_text(pg)[:900]
                chunks.append(f"{header}\n{preview}")
                seen_ids.add(pid)
                if len(chunks) >= max_hits:
                    break
    except Exception as e:
        logging.warning("Notion /search failed: %s", e)

    # 2) fallback: full DB scan
    if len(chunks) < max_hits:
        try:
            for pg in fetch_all_pages():
                pid = pg.get("id")
                if pid in seen_ids:
                    continue
                text = _page_all_text(pg)
                if kw_norm and kw_norm not in _normalize(text + " " + _page_title(pg)):
                    continue
                header = _page_header(pg)
                preview = text[:900]
                chunks.append(f"{header}\n{preview}")
                seen_ids.add(pid)
                if len(chunks) >= max_hits:
                    break
        except Exception as e:
            logging.warning("Notion DB scan failed: %s", e)

    return chunks


# ---------- OpenAI helpers ----------
def gpt_general_chat(user_text: str) -> str:
    """一般聊天（查不到知識時的 fallback）"""
    sys_prompt = (
        "你是友善的助理。回答簡潔，必要時可反問 1 個問題以釐清需求。"
    )
    rsp = client.chat.completions.create(
        model="gpt-4o",
        temperature=0.7,
        messages=[
            {"role": "system", "content": sys_prompt},
            {"role": "user", "content": user_text},
        ],
        timeout=20,
    )
    return (rsp.choices[0].message.content or "").strip()


def gpt_answer_from_kb(question: str, chunks: list[str]) -> str:
    """
    嚴格引用模式：只能引用【來源】內容，不可加入外部定義/推論。
    問題過短或多主題時，請求釐清；資料不足回固定句。
    """
    if not chunks:
        return "資料庫沒有足夠資訊回答此問題"

    rules = (
        "你是公司內部知識助理。嚴格規則：\n"
        "1) 只能使用下方【來源】逐字摘錄或極簡重述，不得加入任何外部知識、定義或推論。\n"
        "2) 若問題過短/模糊或【來源】顯示多個不同主題，請先請使用者釐清，不要自行下定義。\n"
        "3) 若來源不足以回答，回：資料庫沒有足夠資訊回答此問題。\n"
        "4) 回覆最後加上【來源】列出使用到的條目（標頭第一行即可）。\n"
    )
    source = "\n\n".join(chunks)
    rsp = client.chat.completions.create(
        model="gpt-4o",
        temperature=0.0,
        messages=[
            {"role": "system", "content": rules + "\n---【來源】---\n" + source},
            {"role": "user", "content": question},
        ],
        timeout=20,
    )
    return (rsp.choices[0].message.content or "").strip()


# ---------- Routes ----------
@app.route("/", methods=["GET"])
def health():
    return "OK", 200


@app.route("/webhook", methods=["GET", "POST"])
def webhook():
    # 讓 LINE 的「Verify」(GET) 可以通過
    if request.method == "GET":
        return "OK", 200

    body = request.get_data(as_text=True)
    signature = request.headers.get("X-Line-Signature", "")
    logging.info("<< LINE RAW >> %s", body)

    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return "OK"


# ---------- LINE message handler ----------
@handler.add(MessageEvent, message=TextMessage)
def handle_message(event: MessageEvent):
    user_text = (event.message.text or "").strip()
    logging.info("User: %s", user_text)

    # A) 太短/模糊（例如只輸入 1~2 個字），先提供候選條目請求釐清
    if len(_normalize(user_text)) <= 2:
        hits = search_notion(user_text, max_hits=3)
        if hits:
            opts = []
            for i, c in enumerate(hits, 1):
                head = c.splitlines()[0].strip()  # 第一行：我們的標頭
                opts.append(f"{i}. {head}")
            ask = "我找到這些相關條目，請告訴我要看哪一個，或補充更多細節：\n" + "\n".join(opts)
            line_bot_api.reply_message(event.reply_token, TextSendMessage(ask))
            return
        # 真的沒有命中就走一般聊天
        reply = gpt_general_chat(user_text)
        line_bot_api.reply_message(event.reply_token, TextSendMessage(reply))
        return

    # B) 一般情況：先查 Notion，再決定走 RAG 或一般聊天
    chunks = search_notion(user_text, max_hits=3)
    logging.info("Notion hits: %d", len(chunks))

    if chunks:
        reply = gpt_answer_from_kb(user_text, chunks)
    else:
        reply = gpt_general_chat(user_text)

    line_bot_api.reply_message(event.reply_token, TextSendMessage(reply))


# ---------- main ----------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 8080)))

