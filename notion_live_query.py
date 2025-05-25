import os
import requests

def query_live_from_notion(question):
    notion_token = os.getenv("NOTION_API_KEY")
    database_id = os.getenv("NOTION_DB_ID")

    url = f"https://api.notion.com/v1/databases/{database_id}/query"
    headers = {
        "Authorization": f"Bearer {notion_token}",
        "Notion-Version": "2022-06-28",
        "Content-Type": "application/json"
    }

    response = requests.post(url, headers=headers)
    results = response.json().get("results", [])

    context = ""
    for row in results:
        try:
            content = row["properties"]["內容"]["rich_text"][0]["text"]["content"]
            if question.strip() in content:
                context += f"✅ {content}\n"
        except:
            continue

    return context if context else "查無相關資料，請再確認或補充問題。"
