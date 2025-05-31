"""
customer_gpt.py
===============
外部（客戶）問題 → GPT 回答。
先嘗試 FAQ Prompt，再送 OpenAI。回傳 (answer, confidence)。
"""

import os
from typing import Tuple
import openai

openai.api_key = os.getenv("OPENAI_API_KEY")

# TODO 1: 如有固定 FAQ，可放在此列表或讀檔
FAQ_SNIPPETS = """
Q: 你們可以切 H 鋼嗎？
A: 可以，我們 H 鋼最大可切割深度 600mm，精度 ±1.5mm。

Q: 付款方式？
A: 首次合作採 30% 訂金 + 出貨前 70% 尾款。
""".strip()


def _ask_openai(prompt: str) -> str:
    rsp = openai.ChatCompletion.create(
        model="gpt-3.5-turbo",
        temperature=0.7,
        messages=[{"role": "system", "content": prompt}],
    )
    return rsp.choices[0].message.content.strip()


def answer(question: str) -> Tuple[str, float]:
    """
    :return: (answer, confidence 0–1)
    """
    system_prompt = (
        "你是鋼材公司客服，以下是常見 FAQ，若能直接回答就引用；"
        "若 FAQ 不含答案，再使用自身知識回答。請給專業、簡潔的回覆。\n\n"
        f"{FAQ_SNIPPETS}\n\n客戶問題：{question}\n\n回答："
    )
    answer_text = _ask_openai(system_prompt)

    # TODO 2: 信心評分演算法，可改為向量相似度 or GPT function_call 判斷
    confidence = 0.8  # 先固定，之後可改成更科學的方式
    return answer_text, confidence
