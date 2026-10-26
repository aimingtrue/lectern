"""数据正确性（架构评审 P1）：封面表唯一写入口、作品编号变化时迁移「查过」记录、重扫不盖掉别的进程刚写的进度、
解析失败的书不反复重解析、豆瓣请求如实标明身份、不可再生数据的备份与恢复。"""
from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

import pytest

from test_library import lib, make_pdf  # noqa: F401  （lib 是 fixture）


def test_work_id_change_moves_tried_marks(tmp_path):
    from library import dedupe
    from library.biblio import open_db
    from library.catalog import Catalog
    cat = Catalog(tmp_path / "catalog.db")
    cat.conn.execute("INSERT INTO files(path, fid, work) VALUES('/b/kim.epub', 'f9', 'new')")
    cat.conn.commit()
    b = open_db(tmp_path / "biblio.db")
    b.executemany("INSERT INTO tries(work, src, status, at) VALUES(?,?,?,0)",
                  [("old", "douban", "ok"), ("old", "openlibrary", "none"), ("new", "openlibrary", "ok")])
    b.execute("INSERT INTO pub_tries(work, status, at) VALUES('old', 'ok', 0)")
    b.commit()
    b.close()
    n = dedupe.migrate_work_marks(cat, [("old", "/b/kim.epub"), ("same", "/b/none.epub")], tmp_path / "biblio.db")
    b = sqlite3.connect(tmp_path / "biblio.db")
    assert n == 2
    assert sorted(b.execute("SELECT work, src, status FROM tries")) == [("new", "douban", "ok"), ("new", "openlibrary", "ok")], \
        "新编号已有的保留新的"
    assert b.execute("SELECT work FROM pub_tries").fetchall() == [("new",)]


def test_rescan_does_not_overwrite_progress_written_meanwhile(lib):
    """命令行重扫一本书时，网站正好记了进度：重扫要等进度写完、读到新进度再写，不能用读到的旧值盖掉。"""
    from library.config import load
    from library.db import DB
    from library.extract import parse
    from library.scan import Scanner
    cfg = load()
    db = DB(cfg.db_path)
    Scanner(db, cfg, set()).run([str(lib / "books")])
    bid = db.one("SELECT id FROM books")["id"]
    path = lib / "books" / "novel.pdf"
    parsed = parse(path, set())
    parsed.parser_ver = db.book(bid)["parser_ver"]
    other = sqlite3.connect(cfg.db_path, timeout=30)       # 「网站」的连接：先拿到写锁，过一会儿才提交进度
    other.execute("BEGIN IMMEDIATE")
    other.execute("UPDATE books SET progress_para=7, progress_sent=2, last_read=1 WHERE id=?", (bid,))
    st = path.stat()
    t = threading.Thread(target=lambda: db.save_book(bid, str(path), st.st_size, st.st_mtime, parsed))
    t.start()
    time.sleep(0.4)
    other.commit()
    t.join(10)
    assert not t.is_alive()
    b = db.book(bid)
    assert (b["progress_para"], b["progress_sent"], b["last_read"]) == (7, 2, 1)


def test_failed_light_book_is_not_reparsed_on_every_open(lib, monkeypatch):
    from fastapi.testclient import TestClient
    from library.config import load
    from library.db import DB
    from library.extract import PARSER_VER
    from library.scan import Scanner
    from library.web import create_app
    cfg = load()
    db = DB(cfg.db_path)
    Scanner(db, cfg, set()).run([str(lib / "books")])
    b = db.one("SELECT * FROM books")
    db.mark_error(b["id"], b["path"], b["size"], b["mtime"], "RuntimeError: 坏文件", PARSER_VER)
    app = create_app(cfg)
    calls = []
    monkeypatch.setattr(app.state.lib.scanner, "_parse", lambda p: calls.append(p) or (_ for _ in ()).throw(RuntimeError("坏")))
    c = TestClient(app)
    r = c.get(f"/api/books/{b['id']}/text")
    assert r.status_code == 422 and "Could not extract text" in r.json()["detail"]
    assert c.get(f"/api/books/{b['id']}/text").status_code == 422
    assert calls == [], "这一版解析器已经失败过、文件没变：不再解析"
    assert c.get(f"/read/{b['id']}").status_code == 422
    assert c.get(f"/book/{b['id']}").status_code == 200, "书页照常显示书的信息"


def test_backup_snapshot_and_restore(tmp_path):
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
    import backup
    from library.biblio import open_db as biblio_db
    from library.catalog import Catalog
    from library.db import DB
    data = tmp_path / "data"
    db = DB(data / "library.db")
    db.run("INSERT INTO books(id, path, title, last_read, progress_para, progress_sent) VALUES('b1', '/x.pdf', 'X', 5, 12, 3)")
    db.run("INSERT INTO paras(book, n, page, kind, text, sents) VALUES('b1', 0, 0, 'p', 'long text', '[]')")
    db.run("INSERT INTO guides VALUES('b1', 0, 'Chapter I', '<p>g</p>', 1)")
    db.run("INSERT INTO ai_cache VALUES('t2:k', '{}', 1)")
    db.run("INSERT INTO reading_log VALUES('2026-10-17', 'b1', 120, 60)")
    db.run("INSERT INTO cast VALUES('b1', 0, '{}', 1)")
    Catalog(data / "catalog.db")
    biblio_db(data / "biblio.db").close()
    (data / "config.json").write_text('{"nyt_key": "secret"}')
    dest, trash = tmp_path / "bk", tmp_path / "trash"
    first = backup.snapshot(data, dest, keep=1, trash=trash, log=lambda *a: None)
    time.sleep(1.1)
    second = backup.snapshot(data, dest, keep=1, trash=trash, log=lambda *a: None)
    assert not first.exists() and second.exists() and len(list(trash.iterdir())) == 1, "旧快照进废纸篓"
    names = {p.name for p in second.iterdir()}
    assert {"user.db", "catalog.db", "biblio.db", "config.json", "manifest.json"} <= names
    u = sqlite3.connect(second / "user.db")
    assert u.execute("SELECT progress_para FROM books WHERE id='b1'").fetchone() == (12,)
    assert "paras" not in {r[0] for r in u.execute("SELECT name FROM sqlite_master")}, "不备份全文"
    assert (second / "config.json").stat().st_mode & 0o077 == 0, "含 key 的配置只给本人读"
    db.run("UPDATE books SET progress_para=0, last_read=NULL")
    db.run("DELETE FROM guides")
    db.run("DELETE FROM reading_log")
    out = backup.restore_user(second / "user.db", data / "library.db")
    assert out["books"] == 1 and out["guides"] == 1 and out["reading_log"] == 1 and out["cast"] == 0, "人物已在，按主键跳过"
    assert db.one("SELECT progress_para FROM books WHERE id='b1'")["progress_para"] == 12
    assert db.one("SELECT sec FROM reading_log WHERE book='b1'")["sec"] == 120, "阅读时长也回来了"


def test_backup_push_links_previous(tmp_path, monkeypatch):
    """推到另一台机器：远端有上一份就 --link-dest 它；先写 .part 再改名；远端按 keep 清理。"""
    import subprocess
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
    import backup
    calls, prev = [], {"out": ""}

    def fake(cmd, **kw):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout=prev["out"], stderr="")
    monkeypatch.setattr(backup.subprocess, "run", fake)
    snap = tmp_path / "20261017-033000"; snap.mkdir()
    backup.push(snap, "box:/root/bk", keep=3, log=lambda *a: None)
    assert calls[0][:2] == ["ssh", "box"] and "mkdir -p /root/bk" in calls[0][2]
    assert calls[1][0] == "rsync" and "--checksum" in calls[1] and not any(a.startswith("--link-dest") for a in calls[1])
    assert calls[1][-2:] == [f"{snap}/", "box:/root/bk/20261017-033000.part/"], "先写 .part"
    assert "mv 20261017-033000.part 20261017-033000" in calls[2][2] and "head -n -3" in calls[2][2]
    assert "cmp" not in calls[2][2], "首次没有上一份，不比对"
    calls.clear(); prev["out"] = "/root/bk/20261017-033000/\n"
    backup.push(snap.with_name("20261018-033000"), "box:/root/bk", keep=3, log=lambda *a: None)
    assert "--link-dest=/root/bk/20261017-033000/" in calls[1], "第二份以上一份为增量基准"
    assert 'cmp -s "/root/bk/20261017-033000/$b" "$f" && ln -f' in calls[2][2], "相同的文件在远端换成上一份的硬链接"
    with pytest.raises(ValueError):
        backup.push(snap, "box", keep=3)


