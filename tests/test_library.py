"""基本回归测试：PDF 解析（分段、断字、页眉页脚、章节）+ 网站接口 + 数据库升级。"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pymupdf
import pytest
from fastapi import Request  # noqa: F401  （测试里的假音色服务用；注解是字符串，需在模块级可见）

ROOT = Path(__file__).resolve().parent.parent


def make_pdf(path: Path) -> None:
    doc = pymupdf.open()
    body = ("It was a bright cold day in April, and the clocks were striking thirteen. " * 6).strip()
    for n in range(8):
        pg = doc.new_page(width=420, height=595)
        pg.insert_text((180, 30), "THE TEST NOVEL", fontsize=8)            # 页眉（每页重复）
        pg.insert_text((205, 580), str(n + 1), fontsize=8)                 # 页码
        y = 70
        if n in (0, 4):
            pg.insert_text((150, y), f"Chapter {'I' if n == 0 else 'II'}", fontsize=18)
            y += 40
        # 一段：首行缩进，末行在下一页继续（最后一页除外）
        lines = ["    The hyphen-", "ated word and the vulner-", "able narrator went on and on"]
        for ln in lines:
            pg.insert_text((50, y), ln, fontsize=11)
            y += 15
        y += 10
        pg.insert_text((64, y), body[:60], fontsize=11)
        y += 15
        pg.insert_text((50, y), "and the paragraph ends here.", fontsize=11)
    doc.set_metadata({"title": "The Test Novel", "author": "A. Writer"})
    doc.save(path)


@pytest.fixture()
def lib(tmp_path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir()
    if (ROOT / "data" / "dict.db").exists():
        os.symlink(ROOT / "data" / "dict.db", data / "dict.db")
    monkeypatch.setenv("LIBRARY_DATA", str(data))
    books = tmp_path / "books"
    books.mkdir()
    make_pdf(books / "novel.pdf")
    return tmp_path


def test_parse(lib):
    from library.extract import parse
    p = parse(lib / "books" / "novel.pdf", {"hyphenated", "vulnerable"})
    assert p.title == "The Test Novel" and p.author == "A. Writer" and p.pages == 8
    texts = [x["text"] for x in p.paras]
    assert all(set(x) == {"kind", "text", "page"} for x in p.paras)
    assert not any("THE TEST NOVEL" in t for t in texts), "页眉应被去掉"
    assert not any(t.strip().isdigit() for t in texts), "页码应被去掉"
    joined = " ".join(texts)
    assert "vulnerable narrator" in joined, "断字应合并"
    assert "hyphenated word" in joined, "词典里有的 ASCII 断字应合并"
    assert [t for _, t, _, _ in p.toc] == ["Chapter I", "Chapter II"]
    assert [pg for _, _, pg, _ in p.toc] == [0, 4]
    assert [p.paras[n]["text"] for _, _, _, n in p.toc] == ["Chapter I", "Chapter II"]
    assert p.quality["paras"] > 0 and "score" in p.quality


def test_scan_and_api(lib):
    from fastapi.testclient import TestClient
    from library.config import load
    from library.db import DB
    from library.extract import PARSER_VER
    from library.scan import Scanner
    from library.web import create_app
    cfg = load()
    cfg.sources = [str(lib / "books")]
    db = DB(cfg.db_path)
    sc = Scanner(db, cfg, set())
    s = sc.run(cfg.sources)
    assert s.added == 1 and s.failed == 0
    assert sc.run(cfg.sources).skipped == 1, "第二次扫描应跳过未改动的书"
    app = create_app(cfg)
    c = TestClient(app)
    assert c.get("/").status_code == 200        # 跳到书城
    b = db.one("SELECT * FROM books")
    bid, n = b["id"], b["n_paras"]
    assert b["parser_ver"] == PARSER_VER and n > 5 and json.loads(b["quality"])["paras"] > 0
    assert c.get(f"/book/{bid}").status_code == 200
    assert f"/read/{bid}?para=" in c.get(f"/book/{bid}").text
    r = c.get(f"/read/{bid}")
    assert r.status_code == 200 and "reader/main.js?v=" in r.text
    meta = json.loads(r.text.split("window.BOOK=", 1)[1].split(";</script>", 1)[0])
    assert {"id", "title", "author", "pages", "n_paras", "has_text", "toc", "para", "sent"} <= set(meta)
    assert meta["n_paras"] == n and all(len(t) == 4 for t in meta["toc"])

    j = c.get(f"/api/books/{bid}/text", params={"from": 0, "count": 3}).json()
    assert j["total"] == n and [p["n"] for p in j["paras"]] == [0, 1, 2]
    assert j["paras"][0] == {"n": 0, "page": 0, "kind": "h", "text": "Chapter I", "sents": [[0, 9]], "speech": [], "speech_ok": False}
    for p in c.get(f"/api/books/{bid}/text", params={"from": 0, "count": 500}).json()["paras"]:
        assert all(p["text"][a:b] == p["text"][a:b].strip() and b > a for a, b in p["sents"])
    assert len(c.get(f"/api/books/{bid}/text", params={"from": 0, "count": 500}).json()["paras"]) == min(n, 200)
    assert c.get(f"/api/books/{bid}/text", params={"from": n + 5}).json()["paras"] == []

    c.post(f"/api/books/{bid}/progress", json={"para": 5, "sent": 2})
    b = db.one("SELECT * FROM books")
    assert (b["progress_para"], b["progress_sent"]) == (5, 2) and b["last_read"]
    assert b["progress_page"] == db.para_page(bid, 5) and abs(b["progress_frac"] - 5 / n) < 1e-9
    meta = json.loads(c.get(f"/read/{bid}").text.split("window.BOOK=", 1)[1].split(";</script>", 1)[0])
    assert (meta["para"], meta["sent"]) == (5, 2) and meta["rev"] == b["last_read"]

    # 两个页面（本机开着没关的旧标签、mini）：别处写过之后，停在旧版本的页面不能把进度盖回去
    old = meta["rev"]
    r = c.post(f"/api/books/{bid}/progress", json={"para": 7, "sent": 0, "rev": old})          # mini 往前读
    assert r.status_code == 200 and r.json()["rev"] > old
    mini = r.json()["rev"]
    r = c.post(f"/api/books/{bid}/progress", json={"para": 5, "sent": 2, "rev": old})          # 旧标签页隐藏时又存一遍
    assert r.status_code == 409 and r.json() == {"para": 7, "sent": 0, "rev": mini, "tab": None}
    assert c.get(f"/api/books/{bid}/progress").json() == {"para": 7, "sent": 0, "rev": mini, "tab": None}
    assert (db.book(bid)["progress_para"], db.book(bid)["progress_sent"]) == (7, 0)
    r = c.post(f"/api/books/{bid}/progress", json={"para": 8, "sent": 1, "rev": mini})         # 接上之后照常存
    assert r.status_code == 200 and db.book(bid)["progress_para"] == 8
    r = c.post(f"/api/books/{bid}/progress", json={"para": 3, "sent": 0, "rev": old, "force": True})   # 正在朗读的页面以它为准
    assert r.status_code == 200 and db.book(bid)["progress_para"] == 3
    c.post(f"/api/books/{bid}/progress", json={"para": 5, "sent": 2})                          # 升级前打开的页面不带 rev：照旧写
    assert db.book(bid)["progress_para"] == 5
    assert c.post(f"/api/books/{bid}/progress", json={"para": 5, "rev": "x"}).status_code == 422

    # 页面隐藏时用 sendBeacon 存（看不到回应，rev 没跟上）：之后没在朗读时再存，不能把自己当成别处、被拒后拉回旧位置
    r0 = c.get(f"/api/books/{bid}/progress").json()
    assert r0["tab"] is None                                                                   # 上一条是不带 tab 的旧页面写的
    r = c.post(f"/api/books/{bid}/progress", json={"para": 6, "sent": 0, "rev": r0["rev"], "tab": "mini"})
    seen = r.json()["rev"]
    c.post(f"/api/books/{bid}/progress", json={"para": 7, "sent": 1, "rev": seen, "tab": "mini", "force": True})   # beacon，回应没人看
    assert c.get(f"/api/books/{bid}/progress").json()["tab"] == "mini"
    r = c.post(f"/api/books/{bid}/progress", json={"para": 9, "sent": 0, "rev": seen, "tab": "mini"})            # 暂停后点了一句
    assert r.status_code == 200 and db.book(bid)["progress_para"] == 9
    r = c.post(f"/api/books/{bid}/progress", json={"para": 2, "sent": 0, "rev": seen, "tab": "macbook"})         # 别的页面照样被拒
    assert r.status_code == 409 and r.json()["tab"] == "mini" and r.json()["para"] == 9
    assert json.loads(c.get(f"/read/{bid}").text.split("window.BOOK=", 1)[1].split(";</script>", 1)[0])["tab"] == "mini"
    # 左下角的朗读时长：每段原速音频秒数（×10，按字数估）、句数、各句词数（每句封顶 40）
    ln = c.get(f"/api/books/{bid}/length").json()
    assert len(ln["a"]) == len(ln["s"]) == len(ln["w"]) == n
    p0 = db.paras_range(bid, 0, 1)[0]
    assert ln["s"][0] == len(p0["sents"]) and ln["a"][0] == round(len("Chapter I") / 14 * 10) and ln["w"][0] == 2
    assert all(w <= 40 * k for w, k in zip(ln["w"], ln["s"])) and sum(ln["a"]) > 0
    assert json.loads(c.get(f"/read/{bid}").text.split("window.BOOK=", 1)[1].split(";</script>", 1)[0])["spent"] == 0
    rows = db.q("SELECT para, sent, tab, ok FROM progress_log WHERE book=? ORDER BY at DESC LIMIT 2", (bid,))
    assert [tuple(r) for r in rows] == [(2, 0, "macbook", 0), (9, 0, "mini", 1)]               # 被拒的也记下：读到过的位置找得回来

    pr = c.get(f"/api/books/{bid}/prep", params={"start": 0, "end": 4}).json()
    want = {p["text"][a:b] for p in db.paras_range(bid, 0, 4) for a, b in p["sents"]}
    assert pr["total"] == len(want) and not pr["running"]

    # 去掉的功能
    for url in [f"/api/books/{bid}/flow", f"/api/books/{bid}/page/0.png", f"/api/books/{bid}/page/0/words",
                f"/api/books/{bid}/search?q=x", f"/api/books/{bid}/marks", "/api/marks.md", "/api/dict?w=went",
                "/api/vocab.csv", "/notes", "/words", "/search?q=clocks"]:
        assert c.get(url).status_code == 404, url
    for url in ["/api/explain", "/api/sentence", "/api/vocab"]:
        assert c.post(url, json={}).status_code in (404, 405), url
    r = c.get("/", follow_redirects=False)
    assert r.status_code in (302, 307) and r.headers["location"] == "/shelf"     # 网站只有书城和阅读
    for url in ["/admin", "/api/scan"]:
        assert c.get(url).status_code == 404, url
    assert c.post("/api/scan").status_code in (404, 405)


def test_reparse_when_parser_changes(lib):
    """解析器升级（parser_ver 变旧）：扫描时重新解析；打开阅读页/取正文时按需重新解析；进度按页换算到段落。"""
    from fastapi.testclient import TestClient
    from library.config import load
    from library.db import DB
    from library.extract import PARSER_VER
    from library.scan import Scanner
    from library.web import create_app
    cfg = load()
    db = DB(cfg.db_path)
    sc = Scanner(db, cfg, set())
    sc.run([str(lib / "books")])
    bid = db.one("SELECT id FROM books")["id"]
    db.run("UPDATE books SET parser_ver=0, progress_page=4, last_read=1")
    assert sc.run([str(lib / "books")]).added == 1, "旧版本解析的书即使文件没变也要重新解析"
    b = db.book(bid)
    assert b["parser_ver"] == PARSER_VER and b["last_read"] == 1
    assert b["progress_para"] == next(p["n"] for p in db.paras_range(bid, 0, 999) if p["page"] >= 4)
    db.run("UPDATE books SET parser_ver=0")
    c = TestClient(create_app(cfg))
    assert c.get(f"/api/books/{bid}/text?from=0&count=2").json()["total"] > 0
    assert db.book(bid)["parser_ver"] == PARSER_VER
    db.run("UPDATE books SET parser_ver=0")
    (lib / "books" / "novel.pdf").rename(lib / "gone.pdf")
    assert c.get(f"/api/books/{bid}/text").status_code == 200, "文件不在了：有旧段落就照旧给"
    db.run("DELETE FROM paras")
    db.run("UPDATE books SET n_paras=0")
    assert c.get(f"/api/books/{bid}/text").status_code == 410
    assert c.get(f"/read/{bid}").status_code == 410
    assert c.get(f"/book/{bid}").status_code == 200


def test_migration_from_v0(tmp_path):
    from library.db import VERSION
    """旧库（划线、生词、按页存的正文）→ 新库：先备份，去掉旧表，书和进度保留，全部标记待重新解析。"""
    import sqlite3
    from library.db import DB
    p = tmp_path / "library.db"
    c = sqlite3.connect(p)
    c.executescript("""
    PRAGMA journal_mode=WAL;
    CREATE TABLE books(id TEXT PRIMARY KEY, path TEXT NOT NULL, size INT, mtime REAL, title TEXT, author TEXT, pages INT,
      has_text INT, lang TEXT, typo_cover INT DEFAULT 0, toc TEXT, added REAL, status TEXT DEFAULT 'ok', error TEXT,
      last_read REAL, progress_page INT DEFAULT 0, progress_frac REAL DEFAULT 0, mode TEXT);
    CREATE TABLE pages(rid INTEGER PRIMARY KEY, book TEXT NOT NULL, page INT NOT NULL, paras BLOB, UNIQUE(book, page));
    CREATE VIRTUAL TABLE books_fts USING fts5(id UNINDEXED, title, author, tokenize='unicode61');
    CREATE VIRTUAL TABLE pages_fts USING fts5(text, content='', tokenize='porter unicode61');
    CREATE TABLE marks(id INTEGER PRIMARY KEY, book TEXT, page INT, kind TEXT, color TEXT, quote TEXT, note TEXT, anchor TEXT, created REAL);
    CREATE TABLE vocab(word TEXT PRIMARY KEY COLLATE NOCASE, lemma TEXT, book TEXT, page INT, sentence TEXT, zh TEXT,
      ipa TEXT, added REAL, reviews INT DEFAULT 0, known INT DEFAULT 0);
    CREATE TABLE ai_cache(key TEXT PRIMARY KEY, value TEXT, created REAL);
    CREATE TABLE guides(book TEXT, chapter INT, title TEXT, md TEXT, created REAL, PRIMARY KEY(book, chapter));
    INSERT INTO books(id,path,title,pages,toc,added,last_read,progress_page) VALUES('b1','/x/a.pdf','A',10,'[[1,"Chapter I",0]]',1,2,7);
    INSERT INTO books_fts(id,title,author) VALUES('b1','A','');
    INSERT INTO pages(book,page,paras) VALUES('b1',0,x'00');
    INSERT INTO pages_fts(rowid,text) VALUES(1,'hello world');
    INSERT INTO marks(book,page,quote) VALUES('b1',0,'q');
    INSERT INTO vocab(word) VALUES('w');
    INSERT INTO ai_cache VALUES('k','v',1);
    INSERT INTO guides VALUES('b1',0,'Chapter I','<p>g</p>',1);
    """)
    c.commit()
    c.close()
    db = DB(p)
    baks = list(tmp_path.glob("library.db.bak-*"))
    assert len(baks) == 1 and db.backup == baks[0]
    old = sqlite3.connect(baks[0])
    assert old.execute("SELECT COUNT(*) FROM marks").fetchone()[0] == 1, "备份里应保留旧数据"
    old.close()
    tables = {r[0] for r in db.q("SELECT name FROM sqlite_master WHERE type='table'")}
    assert not {"marks", "vocab", "pages", "pages_fts"} & tables and "paras" in tables
    b = db.book("b1")
    assert (b["title"], b["progress_page"], b["last_read"], b["parser_ver"], b["n_paras"]) == ("A", 7, 2, 0, 0)
    assert db.one("SELECT COUNT(*) n FROM ai_cache")["n"] == 1 and db.one("SELECT COUNT(*) n FROM guides")["n"] == 1
    assert db.one("PRAGMA user_version")[0] == VERSION
    DB(p)                                        # 再打开：不再迁移、不再备份
    assert len(list(tmp_path.glob("library.db.bak-*"))) == 1
    DB(tmp_path / "fresh" / "library.db")         # 新库：不备份
    assert not list((tmp_path / "fresh").glob("*.bak-*"))


def test_ipa():
    from library.phon import arpabet_to_ipa
    assert arpabet_to_ipa("F AH0 T AA1 G R AH0 F IY0") == "/fəˈtɑɡrəfi/"
    assert arpabet_to_ipa("K AE1 T") == "/kæt/"
    assert arpabet_to_ipa("AH0 B AW1 T") == "/əˈbaʊt/"


def test_tts_chunks():
    import importlib.util
    spec = importlib.util.spec_from_file_location("tts_server", ROOT / "tts" / "server.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)   # 不加载 MLX：模型只在 Voice() 里导入
    assert m.chunks("Short one.") == ["Short one."]
    long = ", ".join(["the trolley car is hurtling down the track at sixty miles an hour"] * 12) + "."
    parts = m.chunks(long)
    assert len(parts) > 1 and all(len(p) <= 420 for p in parts) and " ".join(parts).replace("  ", " ") == long
    assert m.clean("“Don’t” — he said…") == "\"Don't\" , he said..."
    zh = "他沉默了很久，" * 120 + "终于开口。"      # 没有空格的长中文句：以前硬切找不到空格会死循环
    parts = m.chunks(zh)
    assert len(parts) > 1 and all(len(p) <= 144 for p in parts) and "".join(parts) == zh
    assert m.is_zh("我与地坛。") and not m.is_zh("He said 道 means way.")
    assert m.clean("他说——不。") == "他说，不。"


def test_tts_and_translate_api(lib):
    """书库 → 音色服务：生成、缓存（同一句只生成一次）、服务没开时给出明确提示。"""
    import socket
    import threading
    import time as _t
    import uvicorn
    from fastapi import FastAPI
    from fastapi.responses import Response
    from fastapi.testclient import TestClient
    from library.config import load
    from library.web import create_app

    calls = []
    fake = FastAPI()

    @fake.get("/health")
    def health():
        return {"ok": True, "voice_id": "abc", "voice": "Example Voice"}

    @fake.post("/speak")
    async def speak(req: Request):
        calls.append((await req.json())["text"])
        return Response(b"RIFF....WAVEfake", media_type="audio/wav")

    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    srv = uvicorn.Server(uvicorn.Config(fake, host="127.0.0.1", port=port, log_level="error"))
    threading.Thread(target=srv.run, daemon=True).start()
    for _ in range(50):
        if srv.started:
            break
        _t.sleep(0.1)

    cfg = load()
    cfg.tts_url = f"http://127.0.0.1:{port}"
    c = TestClient(create_app(cfg))
    assert c.get("/api/tts/status").json()["voice"] == "Example Voice"
    r = c.get("/api/tts", params={"t": "What's the right thing to do?"})
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "audio/wav" and r.content.startswith(b"RIFF")
    c.get("/api/tts", params={"t": "What's  the right thing to do? "})
    assert calls == ["What's the right thing to do?"], "同一句（空白不同）只应生成一次"
    assert c.get("/api/tts", params={"t": "— …"}).status_code == 400
    assert c.get("/api/tts", params={"t": "他走了。"}).status_code == 200 and calls[-1] == "他走了。"   # 中文句也能读
    assert c.get("/api/voice/no-such-voice/bed.m4a").status_code == 404     # 没配背景音的音色
    assert c.get("/api/voice/..%2Fx/bed.m4a").status_code == 404
    srv.should_exit = True

    cfg2 = load()
    cfg2.tts_url = "http://127.0.0.1:9"   # 没有服务
    c2 = TestClient(create_app(cfg2))
    assert c2.get("/api/tts/status").json()["ok"] is False
    r = c2.get("/api/tts", params={"t": "Hello there."})
    assert r.status_code == 503 and "speech service is not running" in r.json()["detail"]

    if (ROOT / "data" / "dict.db").exists():
        class FakeLLM:
            def chat_json(self, *a, **k):
                return {"zh": "你觉得绝望", "notes": [], "gloss": []}
        c.app.state.lib.llm = FakeLLM()
        r = c.post("/api/translate", json={"text": "You feel desperate because the trolley car is hurtling down the track, Gatsby said.",
                                            "level": "std"}).json()   # 「标准」档：词频前 2500 名（track）不标；默认的「多」档会标
        assert r["zh"] == "你觉得绝望"
        ws = r["words"]
        lemmas = [w["lemma"] for w in ws]
        assert "desperate" in lemmas and "hurtle" in lemmas
        assert not {"feel", "because", "track", "Gatsby"} & set(lemmas)
        assert all(w["zh"] and "ipa" in w and "word" in w for w in ws)


def test_failed_reparse_not_retried_on_open(tmp_path):
    """新解析器解析失败的书：打开时不再每次重新解析（扫描时仍会重试）。"""
    from library.db import DB
    from library.extract import PARSER_VER
    from library.scan import Scanner
    db = DB(tmp_path / "library.db")
    pdf = tmp_path / "bad.pdf"
    pdf.write_bytes(b"not a pdf")
    db.mark_error("b1", str(pdf), 9, pdf.stat().st_mtime, "boom", PARSER_VER)
    assert db.book("b1")["parser_ver"] == PARSER_VER
    assert Scanner(db, None, None).refresh("b1") == "fresh"
