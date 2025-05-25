def query_with_context(question):
    try:
        # 嘗試從 Notion API 抓資料（未來啟用）
        from notion_live_query import query_live_from_notion
        return query_live_from_notion(question)
    except ImportError:
        # 若尚未啟用 API，則 fallback 使用 CSV
        import csv
        result = ""
        with open("notion_knowledge.csv", newline='', encoding='utf-8') as csvfile:
            reader = csv.DictReader(csvfile)
            for row in reader:
                if question.strip() in row["內容"]:
                    result += f"✅ {row['內容']}\n"
        return result if result else "查無相關資料，請再確認或補充問題。"

