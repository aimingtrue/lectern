"""轻入库：扫描只存书目和摘要，打开时才存全文；摘要和按全文现算的结果一致；精简阅读库的一次性迁移。"""
from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

from test_library import lib, make_pdf  # noqa: F401  （lib 是 fixture）

ROOT = Path(__file__).resolve().parent.parent


def scanned(lib):
    from library.config import load
    from library.db import DB
    from library.scan import Scanner
    cfg = load()
    db = DB(cfg.db_path)
    sc = Scanner(db, cfg, set())
    assert sc.run([str(lib / "books")]).added == 1
    return cfg, db, sc, db.one("SELECT id FROM books")["id"]


def test_scan_stores_digest_not_text_and_open_stores_text(lib):
    from fastapi.testclient import TestClient
    from library.web import create_app
    cfg, db, sc, bid = scanned(lib)
    b = db.book(bid)
    assert b["stored"] == 0 and b["n_paras"] > 5 and b["toc"]
    assert db.one("SELECT COUNT(*) n FROM paras")["n"] == 0, "扫描不存全文"
    d = db.one("SELECT * FROM digest WHERE book=?", (bid,))
    assert d["head"].startswith("The hyphen") and json.loads(d["gate"])["total"] > 1000
    assert sc.run([str(lib / "books")]).skipped == 1, "没存全文的书再扫描也跳过"
    c = TestClient(create_app(cfg))
    assert c.get(f"/book/{bid}").status_code == 200
    j = c.get(f"/api/books/{bid}/text", params={"from": 0, "count": 3}).json()
    assert j["total"] == b["n_paras"] and len(j["paras"]) == 3, "打开时解析并存全文"
    assert db.book(bid)["stored"] == 1 and db.one("SELECT COUNT(*) n FROM paras")["n"] == b["n_paras"]
    db.run("UPDATE books SET parser_ver=0")
    assert sc.run([str(lib / "books")]).added == 1
    assert db.book(bid)["stored"] == 1, "存过全文的书重新解析时照旧存全文"


def test_light_book_with_missing_file_is_gone(lib):
    from fastapi.testclient import TestClient
    from library.web import create_app
    cfg, db, sc, bid = scanned(lib)
    (lib / "books" / "novel.pdf").rename(lib / "gone.pdf")
    c = TestClient(create_app(cfg))
    assert c.get(f"/api/books/{bid}/text").status_code == 410
    assert c.get(f"/book/{bid}").status_code == 200


def test_store_add_stores_text(lib):
    from library.config import load
    from library.db import DB
    from library.scan import Scanner
    cfg = load()
    db = DB(cfg.db_path)
    sc = Scanner(db, cfg, set())
    p = lib / "books" / "novel.pdf"
    assert sc.add_file(p) == "added" and db.one("SELECT stored FROM books")["stored"] == 0
    assert sc.add_file(p, full=True) == "added", "要全文时不能因为没改动就跳过"
    assert db.one("SELECT stored FROM books")["stored"] == 1 and db.one("SELECT COUNT(*) n FROM paras")["n"] > 0
    assert sc.add_file(p, full=True) == "skipped"


def test_digest_matches_full_text(lib):
    """摘要里的开头、指纹、体检指标和按全文现算的一样（下游没有摘要时退回现算）。"""
    sys.path.insert(0, str(ROOT / "tools"))
    import quality_gate
    from library import digest
    from library import match as M
    cfg, db, sc, bid = scanned(lib)
    sc.add_file(lib / "books" / "novel.pdf", force=True, full=True)
    ro = sqlite3.connect(f"file:{cfg.db_path}?mode=ro", uri=True)
    with_digest = (M.snippet(ro, bid), M.snippet(ro, bid, 2000), quality_gate.metrics(ro, bid, digest.common_han()),
                   digest.read(ro, bid)["sketch"], digest.lengths(ro))
    db.run("DELETE FROM digest")
    t = M.body(ro, bid)
    without = (M.snippet(ro, bid), M.snippet(ro, bid, 2000), quality_gate.metrics(ro, bid, digest.common_han()),
               digest.pack(M.sketch(t)) if t else None, digest.lengths(ro))
    assert with_digest == without
    assert with_digest[0].endswith("…") and len(with_digest[0]) <= 121
    assert with_digest[4][bid] == db.one("SELECT SUM(length(text)) n FROM paras")["n"]
    ro.close()


def test_completeness_of_light_book_uses_digest(lib, monkeypatch):
    """没存全文的书：体检的完整度用摘要里的有效字数，和存了全文时按全文现算的一样（不再误报「只有 0%」）。"""
    sys.path.insert(0, str(ROOT / "tools"))
    import quality_gate
    monkeypatch.setattr(quality_gate, "source", lambda path: (1000.0, 0))
    cfg, db, sc, bid = scanned(lib)
    path = db.book(bid)["path"]
    _, light = quality_gate.completeness((str(cfg.db_path), bid, path))
    assert light[0] > 500
    sc.add_file(Path(path), force=True, full=True)
    _, full = quality_gate.completeness((str(cfg.db_path), bid, path))
    assert light == full
    db.run("UPDATE digest SET gate=json_remove(gate, '$.w')")
    db.run("DELETE FROM paras")
    assert quality_gate.completeness((str(cfg.db_path), bid, path)) == (bid, None), "旧摘要没有有效字数：不判完整度"


def test_sketch_is_stable_across_processes():
    from library import match as M
    t = "It was a bright cold day in April and the clocks were striking thirteen " * 20
    code = ("import sys; sys.path.insert(0, %r); from library import match as M; "
            "print(M.sketch(M.norm_text(%r))[:5])" % (str(ROOT), t))
    outs = {subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True).stdout
            for _ in range(2)}
    assert outs == {f"{M.sketch(M.norm_text(t))[:5]}\n"}


def test_migration_from_v2_adds_columns_without_backup(tmp_path):
    from library.db import DB, VERSION
    p = tmp_path / "library.db"
    c = sqlite3.connect(p)
    c.executescript("""
    CREATE TABLE books(id TEXT PRIMARY KEY, path TEXT NOT NULL, size INT, mtime REAL, title TEXT, author TEXT, pages INT,
      has_text INT, lang TEXT, typo_cover INT DEFAULT 0, toc TEXT, added REAL, status TEXT DEFAULT 'ok', error TEXT,
      last_read REAL, progress_page INT DEFAULT 0, progress_frac REAL DEFAULT 0, mode TEXT, parser_ver INT DEFAULT 0,
      n_paras INT DEFAULT 0, progress_para INT DEFAULT 0, progress_sent INT DEFAULT 0, quality TEXT);
    CREATE TABLE paras(book TEXT, n INT, page INT, kind TEXT, text TEXT, sents TEXT, PRIMARY KEY(book, n)) WITHOUT ROWID;
    INSERT INTO books(id,path,title,parser_ver,n_paras) VALUES('b1','/x/a.pdf','A',9,1);
    INSERT INTO paras VALUES('b1',0,0,'p','hello','[]');
    PRAGMA user_version=2;
    """)
    c.commit()
    c.close()
    db = DB(p)
    assert db.backup is None and not list(tmp_path.glob("*.bak-*")), "只加列、加表：不备份"
    b = db.book("b1")
    assert (b["stored"], b["parser_ver"]) == (1, 9), "已有全文的书标为已存，不重新解析"
    assert db.one("PRAGMA user_version")[0] == VERSION and db.one("SELECT COUNT(*) n FROM digest")["n"] == 0


def test_slim_keeps_text_only_for_read_books(lib, tmp_path):
    sys.path.insert(0, str(ROOT / "tools"))
    import slim_library
    from library.config import load
    from library.db import DB
    from library.scan import Scanner
    import shutil
    shutil.copy(lib / "books" / "novel.pdf", lib / "books" / "other.pdf")
    import pymupdf
    with pymupdf.open(lib / "books" / "other.pdf") as d:          # 内容不同才是另一本（书的 ID 是内容指纹）
        d.set_metadata({"title": "Other", "author": "B"})
        d.save(lib / "books" / "other2.pdf")
    (lib / "books" / "other.pdf").unlink()
    cfg = load()
    db = DB(cfg.db_path)
    sc = Scanner(db, cfg, set())
    for f in ("novel.pdf", "other2.pdf"):
        assert sc.add_file(lib / "books" / f, full=True) == "added"
    ids = [r["id"] for r in db.q("SELECT id FROM books ORDER BY title")]
    db.run("UPDATE books SET last_read=1 WHERE id=?", (ids[1],))      # 读过 The Test Novel
    db.run("INSERT INTO ai_cache VALUES('k','v',1)")
    db.run("DELETE FROM digest WHERE book=?", (ids[0],))              # 旧库：摘要要补算
    db.conn.close()
    assert slim_library.plan(cfg.db_path)["missing"] == 1
    assert slim_library.build_digests(cfg.db_path, log=lambda *a: None) == 1
    p = slim_library.plan(cfg.db_path)
    assert (p["books"], p["missing"], p["keep"]) == (2, 0, 1)
    trash = slim_library.slim(cfg.db_path, tmp_path / "trash", log=lambda *a: None)
    assert (trash / "library.db").exists()
    new = DB(cfg.db_path)
    assert {r["id"]: r["stored"] for r in new.q("SELECT id, stored FROM books")} == {ids[0]: 0, ids[1]: 1}
    assert {r["book"] for r in new.q("SELECT DISTINCT book FROM paras")} == {ids[1]}
    assert new.one("SELECT COUNT(*) n FROM digest")["n"] == 2 and new.one("SELECT value FROM ai_cache")["value"] == "v"
    assert new.book(ids[1])["last_read"] == 1


def test_digest_backfill_does_not_hold_the_write_lock(lib, monkeypatch):
    """补摘要跑的时候，网站要能随时写进度：每本算完就提交，不能攒一批才提交（一批几十秒，别的进程等写锁超时
    报 database is locked——2026-10-11 夜里网站记进度失败 41 次、体检把 3 本书误标成读不出）。"""
    import threading
    import time
    sys.path.insert(0, str(ROOT / "tools"))
    import slim_library
    from library import digest
    from library.config import load
    from library.db import DB
    from library.scan import Scanner
    cfg = load()
    db = DB(cfg.db_path)
    Scanner(db, cfg, set()).run([str(lib / "books")])
    bid = db.one("SELECT id FROM books")["id"]
    for i in range(3):                                   # 多造几本没有摘要的书
        db.run("INSERT INTO books(id, path, title, status) VALUES(?, '/x.pdf', 'X', 'ok')", (f"b{i}",))
    db.run("DELETE FROM digest")
    db.conn.close()
    real = digest.compute
    monkeypatch.setattr(digest, "compute", lambda paras: (time.sleep(0.4), real(paras))[1])   # 每本算得慢一点
    t = threading.Thread(target=lambda: slim_library.build_digests(cfg.db_path, log=lambda *a: None))
    t.start()
    time.sleep(0.6)                                      # 补摘要已经在跑、写过至少一本
    other = sqlite3.connect(cfg.db_path, timeout=0.3)   # 「网站」只愿意等 0.3 秒
    other.execute("UPDATE books SET progress_para=5 WHERE id=?", (bid,))
    other.commit()
    t.join(10)
    assert sqlite3.connect(cfg.db_path.with_name("digest.db")).execute("SELECT COUNT(*) FROM digest").fetchone()[0] == 4
