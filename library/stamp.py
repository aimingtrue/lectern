"""缓存版本戳：库文件（含 -wal）的修改时间，但同一组文件至少隔 TTL 秒才重新看一次。

后台任务（补全、评分、分类……）每几秒就写一次书目库、编目库；直接拿修改时间当版本，书城的缓存几乎每次都失效
（首页冷请求约 0.5 秒、命中约 0.07 秒）。加上最短间隔后，数据最多晚 TTL 秒出现在页面上，换来缓存真能命中。
"""
from __future__ import annotations

import threading
import time
from pathlib import Path

TTL = 30.0                 # 秒；测试里设成 0（tests/conftest.py）

_seen: dict[tuple, tuple[float, tuple]] = {}
_lock = threading.Lock()


def db_stamp(path: Path, ttl: float | None = None) -> tuple:
    """SQLite 库文件和它的 -wal 的修改时间；ttl 秒内重复问直接给上次的结果。"""
    paths = (path, path.with_name(path.name + "-wal"))
    key = tuple(str(p) for p in paths)
    now = time.monotonic()
    ttl = TTL if ttl is None else ttl
    with _lock:
        hit = _seen.get(key)
        if hit and now - hit[0] < ttl:
            return hit[1]
    st = tuple(p.stat().st_mtime if p.exists() else 0 for p in paths)
    with _lock:
        _seen[key] = (now, st)
    return st
