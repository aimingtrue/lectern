"""数据库结构的版本化迁移（library/schema.py、library/schemas/）。"""
from __future__ import annotations

import sqlite3

import pytest

from library import schema
from library.schemas import biblio as B
from library.schemas import catalog as C


def cols(c, table):
    return [r[1] for r in c.execute(f"PRAGMA table_info({table})")]


def test_statements_split():
    s = schema.statements("""
    -- 注释行
    CREATE TABLE a(x TEXT);  -- 行尾注释
    CREATE VIEW v AS SELECT x FROM a
      WHERE x <> ';';
    """)
    assert len(s) == 2 and s[1].startswith("CREATE VIEW") and "';'" in s[1]
    assert len(schema.statements(B.V1)) > 30 and len(schema.statements(C.V1)) > 10
    with pytest.raises(ValueError):
        schema.statements("CREATE TABLE a(x")


def test_fresh_database_gets_latest_version(tmp_path):
    b = sqlite3.connect(tmp_path / "biblio.db")
    B.ensure(b)
    assert b.execute("PRAGMA user_version").fetchone()[0] == schema.latest(B.STEPS)
    assert "ext_title" in cols(b, "ratings") and b.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    c = sqlite3.connect(tmp_path / "catalog.db")
    C.ensure(c)
    assert "ol_author" in cols(c, "enrich") and {"files", "works", "shelves", "ghosts", "enrich"} <= {
        r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def test_unversioned_database_keeps_data(tmp_path):
    """版本管理之前的库：表已建好（版本号 0），有的列是临时补的，有的还没补。升级后数据不变。"""
    p = tmp_path / "biblio.db"
    c = sqlite3.connect(p)
    c.executescript("""
    CREATE TABLE ratings(fid TEXT, src TEXT, score REAL, scale INT, n INT, ext_id TEXT, url TEXT, fetched REAL,
      PRIMARY KEY(fid, src));
    ALTER TABLE ratings ADD COLUMN ext_title TEXT;
    INSERT INTO ratings VALUES('f1', 'douban', 8.5, 10, 100, 'x', 'u', 1, 'T');
    CREATE TABLE categories(fid TEXT PRIMARY KEY, top TEXT, sub TEXT, conf REAL, src TEXT, evidence TEXT, at REAL);
    INSERT INTO categories VALUES('f1', 'lit', NULL, 0.9, 'cip', '[]', 0);
    """)
    c.commit()
    assert schema.migrate(c, B.STEPS) == 0
    assert c.execute("SELECT * FROM ratings").fetchall() == [("f1", "douban", 8.5, 10, 100, "x", "u", 1, "T")]
    assert c.execute("SELECT top FROM categories").fetchone() == ("lit",)
    assert cols(c, "ratings").count("ext_title") == 1
    assert c.execute("SELECT COUNT(*) FROM same_book_final").fetchone() == (0,), "缺的表和视图补上"
    assert schema.migrate(c, B.STEPS) == schema.latest(B.STEPS), "再打开：不再升级"


def test_failed_step_rolls_back(tmp_path):
    c = sqlite3.connect(tmp_path / "x.db")

    def boom(conn):
        raise RuntimeError("坏步骤")
    with pytest.raises(RuntimeError):
        schema.migrate(c, [(1, "CREATE TABLE a(x TEXT);"), (2, boom)])
    assert c.execute("PRAGMA user_version").fetchone()[0] == 0
    assert not c.execute("SELECT name FROM sqlite_master WHERE name='a'").fetchall(), "整个回滚"
    assert schema.migrate(c, [(1, "CREATE TABLE a(x TEXT);")]) == 0
    assert schema.migrate(c, [(1, "CREATE TABLE a(x TEXT);"), (2, "CREATE TABLE b(y TEXT);")]) == 1, "只做新步骤"
    assert c.execute("PRAGMA user_version").fetchone()[0] == 2


def test_up_to_date_does_not_take_write_lock(tmp_path):
    """已是最新版本：打开时不抢写锁（biblio.db 一直有后台任务在写）。"""
    p = tmp_path / "biblio.db"
    B.ensure(sqlite3.connect(p))
    writer = sqlite3.connect(p)
    writer.execute("BEGIN IMMEDIATE")
    other = sqlite3.connect(p, timeout=0.1)
    B.ensure(other)                                  # 拿写锁的话这里会 database is locked
    writer.rollback()


def test_two_processes_upgrade_once(tmp_path):
    """两个进程同时打开旧库：后拿到写锁的那个在锁里再看一次版本号，发现已升级就不再执行步骤。"""
    import threading
    import time
    p = tmp_path / "x.db"
    sqlite3.connect(p).execute("PRAGMA journal_mode=WAL")
    runs, started = [], threading.Event()

    def slow(conn):
        runs.append(1)
        started.set()
        time.sleep(0.5)                              # 拿着写锁还没提交
    steps = [(1, slow)]
    t = threading.Thread(target=lambda: schema.migrate(sqlite3.connect(p), steps))
    t.start()
    started.wait()
    b = sqlite3.connect(p, timeout=5)
    assert b.execute("PRAGMA user_version").fetchone()[0] == 0     # 锁外读到的还是旧版本
    assert schema.migrate(b, steps) == 1                            # 等到锁后看到已是 1
    t.join()
    assert runs == [1]


def test_steps_must_increase():
    c = sqlite3.connect(":memory:")
    with pytest.raises(AssertionError):
        schema.migrate(c, [(2, "SELECT 1;"), (1, "SELECT 1;")])
