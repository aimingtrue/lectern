"""数据库结构的版本化迁移（catalog.db、biblio.db 共用；library.db 的见 library/db.py）。

每个库的结构写在 library/schemas/<库>.py 的 STEPS 里：[(版本, SQL 脚本或函数), ...]，只往后追加，不改已有的步骤。
打开库时，版本号（PRAGMA user_version）低于最新版本就在一个 BEGIN IMMEDIATE 事务里执行还没做过的步骤：
拿到写锁后再看一次版本（另一个进程可能刚升级完），全部成功才写入新版本号，中途出错整个回滚。
步骤要能在「表已经有了」的旧库上重复执行（CREATE … IF NOT EXISTS，加列前先看列在不在）：
版本管理是后来加的，已有的库版本号是 0，但表早就建好了。
"""
from __future__ import annotations

import sqlite3
from typing import Callable

Step = tuple[int, "str | Callable[[sqlite3.Connection], None]"]


def statements(script: str) -> list[str]:
    """把 SQL 脚本切成一条条语句（executescript 会先提交事务，不能用在迁移事务里）。"""
    out, cur = [], ""
    for line in script.splitlines(keepends=True):
        if not cur and line.lstrip().startswith("--"):
            continue
        cur += line
        if sqlite3.complete_statement(cur):
            if cur.strip():
                out.append(cur.strip())
            cur = ""
    if cur.strip():
        raise ValueError(f"不完整的 SQL：{cur.strip()[:80]}")
    return out


def add_column(table: str, column: str, decl: str) -> Callable[[sqlite3.Connection], None]:
    """加列（列已经有了就跳过）。"""
    def step(c: sqlite3.Connection) -> None:
        if column not in {r[1] for r in c.execute(f"PRAGMA table_info({table})")}:
            c.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
    return step


def latest(steps: list[Step]) -> int:
    return max(v for v, _ in steps)


def migrate(c: sqlite3.Connection, steps: list[Step]) -> int:
    """把库升到 steps 的最新版本，返回升级前的版本号。已是最新时不拿写锁。"""
    vs = [v for v, _ in steps]
    assert vs == sorted(vs) and len(set(vs)) == len(vs), "版本号要递增且不重复"
    top = vs[-1]
    v = c.execute("PRAGMA user_version").fetchone()[0]
    if v >= top:
        return v
    if c.in_transaction:
        c.commit()
    c.execute("BEGIN IMMEDIATE")
    try:
        v = c.execute("PRAGMA user_version").fetchone()[0]
        for ver, step in steps:
            if ver <= v:
                continue
            if callable(step):
                step(c)
            else:
                for s in statements(step):
                    c.execute(s)
        if top > v:
            c.execute(f"PRAGMA user_version={top}")
        c.execute("COMMIT")
    except BaseException:
        c.execute("ROLLBACK")
        raise
    return v


def ensure(c: sqlite3.Connection, steps: list[Step]) -> None:
    """打开库时调用：WAL 日志模式（不能在事务里设）+ 升到最新版本。"""
    if c.in_transaction:
        c.commit()
    c.execute("PRAGMA journal_mode=WAL")
    migrate(c, steps)
