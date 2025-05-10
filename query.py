# query.py
import pandas as pd

def query_with_context(question: str) -> str:
    """
    從 notion_knowledge.csv 找到第一筆「問題」欄位包含 question 的回答。
    若找不到，就回傳預設訊息。
    """
    try:
        df = pd.read_csv("notion_knowledge.csv")
    except Exception as e:
        return f"無法讀取知識庫：{e}"

    # 假設你 CSV 裡有兩欄：'問題' 和 '回答'
    for _, row in df.iterrows():
        if str(row.get("問題", "")).strip() and str(row["問題"]) in question:
            return str(row.get("回答", "")).strip()

    return "找不到相關資料，請稍後再試或聯絡主管。"
