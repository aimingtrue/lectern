"""线程本地的 SQLite 连接：线程退出时关掉。

网站的接口跑在 anyio 的工作线程里，线程空闲 10 秒就退出、下次请求再起新的，每个新线程都开自己的连接。
连接只靠垃圾回收释放的话，SQLite 的文件句柄不会跟着关（每个连接漏 2 个），服务跑一两天就顶到
launchd 给的 256 个句柄上限，接不了新请求、库也打不开（500）。显式 close() 才真正释放句柄。
"""
from __future__ import annotations

import sqlite3


class _Closer:
    """放在线程本地里：线程退出时线程本地被清掉，它随即释放（没有循环引用，不等垃圾回收）并关掉连接。"""
    __slots__ = ("c",)

    def __init__(self, c: sqlite3.Connection):
        self.c = c

    def __del__(self):
        try:
            self.c.close()
        except Exception:  # noqa: BLE001
            pass


def keep(local, c: sqlite3.Connection) -> sqlite3.Connection:
    """把连接记到线程本地 local.c，线程退出时关掉。"""
    local.c = c
    local.closer = _Closer(c)
    return c
