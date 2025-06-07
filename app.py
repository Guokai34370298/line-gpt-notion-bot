import os
import requests
import openai
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage

"""
Minimal but **Notion‑driven** LINE↔︎GPT Bot
=========================================
特色
----
1. **先全文掃出 Notion Database** ➜ 只回傳「有命中關鍵字的條目」。  
2. 若無命中，直接回「資料庫沒有相關資訊」，不給通用建議。  
3. 不用向量檢索，單純 *substring* 比對；若要更進階可改成 Embedding + FAISS。
"""

# ---------- config ----------
openai.api_key           = os.getenv("OPENAI_API_KEY")
NOTION_API_KEY             = os.getenv("NOTION_API_KEY")
NOTION_DB_ID       = os.getenv("NOTION_DB_ID")
LINE_CHANNEL_ACCESS_TOKEN= os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
LINE_CHANNEL_SECRET      = os.getenv("LINE_CHANNEL_SECRET")
NOTION_VERSION           = "2022-06-28"

# ---------- client ----------
line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler      = WebhookHandler(LINE_CHANNEL_SECRET)
app          = Flask(__name__)

# ---------- Notion helper ----------

def fetch_all_pages():
    """一次抓滿 100 筆（簡化示範）。"""
    url = f"https://api.notion.com/v1/databases/{NOTION_DATABASE_ID}/query"
    headers = {
        "Authorization": f"Bearer {NOTION_TOKEN}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }
    res = requests.post(url, headers=headers, json={"page_size": 100})
    res.raise_for_status()
    return res.json()["results"]


def search_notion(user_text: str):
    """回傳所有包含使用者文字的條目 list[str]。"""
    user_text_lower = user_text.lower()
    matches = []
    for page in fetch_all_pages():
        props   = page["properties"]
        content = props["內容"]["rich_text"][0]["text"]["content"] if props["內容"]["rich_text"] else ""
        serial  = props["序號"]["title"][0]["plain_text"] if props["序號"]["title"] else ""
        if user_text_lower in content.lower():
            matches.append(f"{serial}: {content}")
    return matches

# ---------- GPT helper ----------

def gpt_answer(user_text: str, chunks: list[str]) -> str:
    if not chunks:
        return "資料庫沒有相關資訊"

    system_prompt = (
        "你是鋼鐵公司內部助理，僅能根據下列 Notion 條目回答使用者問題，"
        "不得添加未在條目中出現的資訊；若無法回答請說『資料庫沒有相關資訊』。\n\n"
        + "\n".join(chunks)
    )
    completion = openai.ChatCompletion.create(
        model="gpt-3.5-turbo",
        temperature=0,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user",   "content": user_text},
        ],
    )
    return completion.choices[0].message.content.strip()

# ---------- LINE webhook ----------

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
def handle_message(event):
    user_text = event.message.text.strip()
    notion_hits = search_notion(user_text)
    reply = gpt_answer(user_text, notion_hits)
    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)))
