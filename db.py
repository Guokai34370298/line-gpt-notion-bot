"""
db.py
=====
統一處理「國凱夥伴白名單」的存取邏輯。
預設先用 Redis；如果沒有設定 REDIS_URL，就退化成讀取本地 JSON 檔。

Env 變數
--------
REDIS_URL   Redis 連線字串 (ex: redis://default:pwd@host:6379/0)
"""

import os
import json
from pathlib import Path
from typing import Iterable

_INTERNAL_FILE = Path(__file__).with_name("internal_users.json")
_REDIS_URL = os.getenv("REDIS_URL")

if _REDIS_URL:
    import redis  # pip install redis[hiredis] (或 redis)
    _r = redis.from_url(_REDIS_URL)
else:
    _r = None


# ---------- Public APIs ---------- #
def is_internal(uid: str) -> bool:
    """判斷指定 userId 是否為國凱夥伴。"""
    if _r:
        return _r.sismember("internal_users", uid)
    elif _INTERNAL_FILE.exists():
        ids = json.loads(_INTERNAL_FILE.read_text())
        return uid in ids
    return False


def save_internal_users(ids: Iterable[str]) -> None:
    """全量覆寫目前的國凱夥伴名單。"""
    ids = list(set(ids))  # 去重
    if _r:
        # 原子性覆蓋
        pipe = _r.pipeline()
        pipe.delete("internal_users")
        if ids:
            pipe.sadd("internal_users", *ids)
        pipe.execute()
    # 同步寫本地備份
    _INTERNAL_FILE.write_text(json.dumps(ids, ensure_ascii=False, indent=2))
  
