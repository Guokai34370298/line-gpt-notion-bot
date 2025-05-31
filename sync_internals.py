"""
sync_internals.py
=================
排程腳本：同步「國凱夥伴」userId 清單到 Redis / JSON。

> Railway 可在 Settings → Cron / Jobs 建立：
    python sync_internals.py
"""

import os
from db import save_internal_users

# TODO 1: 掛到你的 HR/Notion，撈員工對應的 LINE userId
# 這裡先示範寫死兩個 id，之後只要回傳 list[str] 即可
def fetch_staff_user_ids():
    # 範例: 呼叫 Notion Database 或讀 CSV...
    return [
        "Uxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",  # Andy
        "Uyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyy",  # Cathy
    ]


def main():
    ids = fetch_staff_user_ids()
    save_internal_users(ids)
    print(f"[sync_internals] 更新員工名單，共 {len(ids)} 人")

if __name__ == "__main__":
    main()
