"""P3 增强标签（书目匹配、简介清洗、难度、类别规则）+ P4 入库（按作品取文字来源、加入书库接口、原版翻页）。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from _make import make_epub, make_mobi, make_pdf

from library import enrich as E


# ---------------------------------------------------------------- P3
def test_ol_match():
    doc = {"title": "The Women", "author_name": ["Kristin Hannah"]}
    assert E.matches(doc, "The Women: A Novel", "Kristin Hannah")
    assert not E.matches(doc, "The Women", "Someone Else")
    assert not E.matches({"title": "Women in Love", "author_name": ["D. H. Lawrence"]}, "The Women", "Kristin Hannah")
    assert E.matches({"title": "Emma", "author_name": ["Jane Austen"]}, "Emma", "")          # 没作者时要求书名完全一致
    assert not E.matches({"title": "Emma Gets Lost", "author_name": ["X"]}, "Emma", "")


def test_clean_desc():
    d = {"value": "A [great](https://x.y) book.\r\n\n----------\nAlso contained in:\n- [Other](/works/OL1W)"}
    assert E.clean_desc(d) == "A great book."
    assert E.clean_desc(None) == ""


def test_measure_orders_easy_below_hard():
    rank = {"the": 1, "was": 2, "a": 3, "cat": 900, "sat": 1500, "on": 10, "mat": 4000, "be": 2, "do": 20, "it": 8,
            "ephemeral": 14000, "perspicacious": 30000, "obfuscate": 20000, "she": 30}
    easy = ("The cat sat on the mat. It was a cat. Don't do it. She's on the mat. " * 120)
    hard = ("The ephemeral cat sat on the perspicacious mat. It was obfuscated. " * 120)
    m1, m2 = E.measure(easy, rank), E.measure(hard, rank)
    assert m1["v95"] < m2["v95"]
    assert m1["level"] == "入门" and m2["level"] == "挑战"
    assert E.measure("too short", rank) is None
    assert E.word_rank("don't", rank) == 20 and E.word_rank("she'd", rank) == 30   # 缩写还原
    assert E.word_rank("cats", rank) == 900


def test_sample_text_epub(tmp_path):
    p = tmp_path / "b.epub"
    make_epub(p, "B", "A", body="hello world " * 50)
    assert "hello world" in E.sample_text(p, "epub")


# ---------------------------------------------------------------- P4
@pytest.fixture()
def lib(tmp_path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setenv("LIBRARY_DATA", str(data))
    root = tmp_path / "书库"
    d = root / "电子书/书单/01.The Test Novel"
    make_epub(d / "The Test Novel.epub", "The Test Novel", "Ann Writer",
              body="</p><p>".join(f"It was a bright cold day in April, paragraph {i}." for i in range(300)))
    make_mobi(d / "The Test Novel.mobi", "The Test Novel", "Ann Writer")
    make_pdf(d / "The Test Novel.pdf", "Test novel pdf text")
    make_pdf(root / "电子书/其他/Plain Book - Bob Author.pdf", "A plain book with a text layer")
    make_pdf(root / "杂志/TIME丨时代周刊/TIME 2024/TIME 2024-01-15.pdf", "magazine text")
    from library.catalog import Catalog
    from library.config import load
    cfg = load()
    cfg.sources = [str(root)]
    Catalog(data / "catalog.db").scan(cfg.sources)
    return cfg, root


def test_scan_ingests_text_sources_only(lib):
    from library.db import DB
    from library.scan import Scanner
    cfg, root = lib
    db = DB(cfg.db_path)
    s = Scanner(db, cfg, set()).run(cfg.sources)
    assert s.failed == 0, s.errors
    paths = {Path(r["path"]).name: r for r in db.q("SELECT * FROM books")}
    assert set(paths) == {"The Test Novel.epub", "Plain Book - Bob Author.pdf"}    # 不入库 mobi/版式 pdf/杂志
    b = paths["The Test Novel.epub"]
    assert (b["title"], b["author"]) == ("The Test Novel", "Ann Writer") and b["n_paras"] > 0
    assert not (cfg.covers / f"{b['id']}.jpg").exists()      # 只存摘要的书不预画封面：第一次要显示时现画
    assert Scanner(db, cfg, set()).run(cfg.sources).skipped == 2                    # 断点续扫
    from fastapi.testclient import TestClient
    from library.web import create_app
    r = TestClient(create_app(cfg)).get(f"/cover/{b['id']}.jpg")
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg"
    assert (cfg.covers / f"{b['id']}.jpg").exists()


def _crash():
    import os
    os._exit(139)


def _sleep():
    import time
    time.sleep(30)


def _ok(x):
    return x * 2


def test_isolated_survives_crash_and_timeout():
    from library.isolate import Isolated, IsolatedError
    iso = Isolated(timeout=3)
    assert iso.call(_ok, 21) == 42
    with pytest.raises(IsolatedError, match="crashed"):
        iso.call(_crash)
    assert iso.call(_ok, 1) == 2                      # 自动重开
    with pytest.raises(IsolatedError, match="did not finish"):
        iso.call(_sleep)
    assert iso.call(_ok, 2) == 4
    iso.close()


def test_scan_falls_back_to_other_format(lib):
    """文字来源（mobi）编目时读得出元数据、入库时 PyMuPDF 打不开：换同一作品的 txt 入库，失败记录随之清掉。"""
    from library.catalog import Catalog
    from library.db import DB
    from library.scan import Scanner
    cfg, root = lib
    d = root / "电子书/书单/02.Broken Book"
    make_mobi(d / "Broken Book.mobi", "Broken Book", "Carl Broke")
    (d / "Broken Book.txt").write_text("It was a plain text edition of the broken book. " * 200)
    cat = Catalog(cfg.data / "catalog.db")
    cat.scan(cfg.sources)
    assert cat.one("SELECT text_src FROM works WHERE title='Broken Book'")["text_src"].endswith(".mobi")
    db = DB(cfg.db_path)
    s = Scanner(db, cfg, set()).run(cfg.sources)
    names = {Path(r["path"]).name: r["status"] for r in db.q("SELECT path, status FROM books")}
    assert names.get("Broken Book.txt") == "ok", (names, s.errors)
    assert "Broken Book.mobi" not in names                      # 失败记录已清掉
    assert cat.one("SELECT text_src FROM works WHERE title='Broken Book'")["text_src"].endswith(".txt")


def test_sanitize_bad_text():
    from types import SimpleNamespace
    from library.scan import fix_text, sanitize
    assert fix_text("abc\ud83d") == "abc"
    assert fix_text("ÊÀ½ç¿ç¹ú¹«Ë¾") == "世界跨国公司"            # GBK 被当成 Latin-1 的乱码
    assert fix_text("Café au lait") == "Café au lait"              # 正常的西文不动
    p = sanitize(SimpleNamespace(title="T\ud800", author=None, toc=[(1, "ÊÀ½ç", 0, 0)],
                                 paras=[{"text": "x\udfff"}]))
    assert (p.title, p.toc[0][1], p.paras[0]["text"]) == ("T", "世界", "x")


def test_reflow_fragmented_cjk(tmp_path, monkeypatch):
    """一字一段的中文书：检测到碎片化就按页面纯文字重新分段，页眉页码去掉。
    （真实书里碎片化来自同一行字的基线高低不齐，测试 PDF 不好复现，所以用替身模拟原解析器的碎片输出。）"""
    import pymupdf
    from types import SimpleNamespace
    from library import scan
    from library.reflow import fragmented, reflow
    assert fragmented([{"text": "字"}] * 60 + [{"text": "一整段正常的文字"}] * 10)
    assert not fragmented([{"text": "一整段正常的文字，很长。"}] * 60)
    p = tmp_path / "frag.pdf"
    doc = pymupdf.open()
    body = "这是一本用来测试的中文书，每个字都单独排版，原来的解析器会把它切成一字一段。"
    seasons = "春夏秋冬晴雨"
    bodies = []
    for n in range(6):
        pg = doc.new_page(width=300, height=420)
        pg.insert_text((120, 20), "测试页眉", fontname="china-s", fontsize=9)
        pg.insert_text((145, 410), str(n + 1), fontsize=9)
        y = 60
        for para in range(3):
            text = seasons[n] + "天第" + "一二三"[para] + "段：" + body    # 每页正文不同（真书也是）
            bodies.append(text)
            x = 44                                        # 段首缩进
            for ch in text:
                if x > 262:
                    x, y = 20, y + 16
                pg.insert_text((x, y), ch, fontname="china-s", fontsize=11)
                x += 12
            y += 26
    doc.save(p)
    paras = reflow(p)
    texts = [x["text"] for x in paras]
    assert texts == bodies, texts[:3]                              # 每段拼回整句，中文之间不留空格
    assert len(texts) == 18 and not fragmented(paras)
    # 原解析器给出碎片时，入库流程改用重新分段
    frag = SimpleNamespace(title="t", author="", toc=[(1, "第一章", 0, 0)], paras=[{"kind": "p", "text": c, "page": 0} for c in body * 3])
    monkeypatch.setattr(scan, "parse", lambda path, words: frag)
    r = scan._parse_child(str(p))
    assert [x["text"] for x in r.paras] == texts and r.toc[0][:3] == (1, "第一章", 0)


def test_timeout_falls_back_to_simple_reflow(lib, monkeypatch):
    """常规解析超时的大部头：改用按页纯文字分段入库。"""
    from library import scan
    from library.db import DB
    cfg, root = lib
    monkeypatch.setattr(scan, "PARSE_TIMEOUT", 3)
    p = root / "电子书/其他/Plain Book - Bob Author.pdf"
    sc = scan.Scanner(DB(cfg.db_path), cfg, set())
    monkeypatch.setattr(scan, "_parse_child", _slow_parse)
    assert sc.add_file(p, force=True) == "added"
    b = sc.db.one("SELECT * FROM books WHERE path=?", (str(p),))
    assert b["status"] == "ok" and b["n_paras"] > 0 and b["title"] == "Plain Book"   # 书名仍来自编目



def test_database_locked_is_not_a_parse_failure(lib, monkeypatch):
    """别的进程正在写库（database is locked）：不是书读不了，书库和编目都不记失败，下次照常入库。"""
    import sqlite3
    from library import scan
    from library.db import DB
    cfg, root = lib
    p = root / "电子书/其他/Plain Book - Bob Author.pdf"
    sc = scan.Scanner(DB(cfg.db_path), cfg, set())
    real = sc.db.save_book

    def locked(*a, **k):
        raise sqlite3.OperationalError("database is locked")
    monkeypatch.setattr(sc.db, "save_book", locked)
    assert sc.add_file(p, force=True) == "failed"
    assert sc.db.one("SELECT status FROM books WHERE path=?", (str(p),)) is None
    if sc.catalog is not None:
        assert (sc.catalog.q("SELECT gate FROM files WHERE path=?", (str(p),)) or [{"gate": None}])[0]["gate"] is None
    monkeypatch.setattr(sc.db, "save_book", real)
    assert sc.add_file(p, force=True) == "added"


def test_replacement_chars_counts_text_not_zip_bytes(tmp_path):
    """压缩数据里碰巧有「�」的 3 个字节不算；正文里的才算。"""
    import sys, zipfile
    sys.path.insert(0, "tools")
    import quality_gate as G
    e = tmp_path / "a.epub"
    with zipfile.ZipFile(e, "w") as z:
        z.writestr("img.jpg", b"\x00\xef\xbf\xbd" * 50, compress_type=zipfile.ZIP_STORED)
        z.writestr("c.xhtml", "<p>fine text</p>")
    assert G.replacement_chars(e) == 0
    with zipfile.ZipFile(e, "a") as z:
        z.writestr("d.xhtml", "<p>bad \ufffd here</p>")
    assert G.replacement_chars(e) == 1

def _slow_parse(path):
    import time
    time.sleep(30)


def test_sanitize_removes_spaces_between_cjk():
    from types import SimpleNamespace
    from library.scan import sanitize
    p = sanitize(SimpleNamespace(title="t", author="", toc=[], paras=[{"text": "阿 不 思 露 出 灿 烂 的 笑 容。"},
                                                                     {"text": "Hello world, keep spaces."}]))
    assert p.paras[0]["text"] == "阿不思露出灿烂的笑容。" and p.paras[1]["text"] == "Hello world, keep spaces."


def test_fix_replacement_punctuation():
    from library.scan import fix_replacement
    assert fix_replacement("The boy couldn�t see. �Flight Lieutenant,� he said.") == \
        "The boy couldn’t see. “Flight Lieutenant,” he said."
    assert fix_replacement("suspense�his most gripping work") == "suspense—his most gripping work"
    assert fix_replacement("Copyright � 2004 by Sidney Sheldon") == "Copyright © 2004 by Sidney Sheldon"
    assert fix_replacement("中文�不动") == "中文�不动"                     # 中文段落不猜


def test_gbk_txt_is_decoded(tmp_path):
    """GBK 编码的中文 txt：按 GB18030 读，不会变成「ºï×Ó±¾ÔÚ」。"""
    from library.scan import _init_words, _parse_child
    p = tmp_path / "gbk.txt"
    p.write_bytes(("第一章 杀人越货\n\n　　现代都市，一栋老式的公寓里。" * 40).encode("gb18030"))
    _init_words(set())
    text = " ".join(x["text"] for x in _parse_child(str(p)).paras)
    assert "杀人越货" in text and "现代都市" in text and "º" not in text


def test_quality_gate_verdicts(monkeypatch):
    """质量体检的判定：电子书先用 calibre 复查正文；繁体书、古籍不按生僻字删除。"""
    import sys
    sys.path.insert(0, "tools")
    import quality_gate as G
    base = {"total": 50000, "tiny": 0.0, "repl": 0.0, "zh": True, "sense": 0.95, "spaced": 0.0, "latin1": 0.0,
            "teaser": False}
    monkeypatch.setattr(G, "is_text_book", lambda p: True)                     # calibre 取得出正文、文字是主体
    assert G.verdict(dict(base, total=0), 0, "epub", 3000, True)[0] == "修复"
    assert G.verdict(dict(base, tiny=0.9, total=1500), 500, "epub", 300, True)[0] == "修复"
    assert G.verdict(dict(base, tiny=0.51, total=29000), 575, "epub", 100, True)[0] == ""   # 诗集：短行多但平均每段 50 字
    monkeypatch.setattr(G, "is_text_book", lambda p: False)                    # 取不出正文，或只有零散配字：图像书
    assert G.verdict(dict(base, total=300), 5, "epub", 16, True)[0] == "转翻阅"   # 十几页的绘本
    assert G.verdict(dict(base, total=0), 0, "epub", 4, True)[0] == "删除"
    assert G.verdict(dict(base, total=0), 0, "pdf", 300, True)[0] == "翻阅"
    assert G.verdict(dict(base, sense=0.75), 500, "epub", 300, True)[0] == ""    # 繁体书、古籍：不删
    assert G.verdict(dict(base, sense=0.55), 500, "epub", 300, True)[0] == "删除"  # 通篇错字



def test_quality_gate_completeness():
    """入库正文和原文件对照：mobi 不完整就转 epub，epub 被截断就重新解析，mobi 中段损坏就删除。"""
    import sys
    sys.path.insert(0, "tools")
    import quality_gate as G
    base = {"total": 50000, "tiny": 0.0, "repl": 0.0, "zh": False, "sense": 0.95, "spaced": 0.0, "latin1": 0.0,
            "teaser": False}
    assert G.verdict(base, 500, "mobi", 300, True, comp=(40000, 100000, 0))[0] == "重转"
    assert G.verdict(base, 500, "epub", 300, True, comp=(40000, 100000, 0))[0] == "重新入库"
    assert G.verdict(base, 500, "mobi", 300, True, comp=(95000, 100000, 0))[0] == ""      # 九成以上：完整
    assert G.verdict(base, 500, "mobi", 300, True, comp=(500, 1000, 0))[0] == ""          # 原文件太短不判
    assert G.verdict(base, 500, "mobi", 300, True, comp=(95000, 100000, 3))[0] == "删除"   # 文件损坏
    assert G.verdict(base, 500, "pdf", 300, True)[0] == ""                                # pdf 不查

def test_is_text_book(monkeypatch, tmp_path):
    """文字书 = 文字是主体；漫画只有零散配字（平均每张图十几个字）不算。"""
    import sys, zipfile
    sys.path.insert(0, "tools")
    import quality_gate as G
    comic = tmp_path / "comic.epub"
    with zipfile.ZipFile(comic, "w") as z:
        for i in range(200):
            z.writestr(f"img/{i}.jpg", b"x")
    monkeypatch.setattr(G, "calibre_chars", lambda p: 3044)
    assert not G.is_text_book(str(comic))                       # 200 张图、3000 字：漫画
    monkeypatch.setattr(G, "calibre_chars", lambda p: 63715)
    assert G.is_text_book(str(comic))                           # 6 万字：文字书


def test_txt_encodings_not_misread_as_utf16(tmp_path):
    """西文 txt 夹着几个 cp1252 字节：按 cp1252 读，不能被当成 UTF-16 解成一堆汉字；真 UTF-16、GBK 照常。"""
    import re
    from library.scan import _init_words, _parse_child
    _init_words(set())
    en = ("Phileas Fogg, without getting in the least flurried, proposed to buy the elephant. " * 60
          + "He said \u201cyes\u201d and that\u2019s all.\r\n")
    cases = {
        "cp1252.txt": en.encode("cp1252"),
        "utf16.txt": "﻿".encode("utf-16-le") + ("Phileas Fogg bought the elephant. " * 60).encode("utf-16-le"),
        "gbk.txt": ("第一章 杀人越货\n\n　　现代都市，一栋老式的公寓里。" * 40).encode("gb18030"),
    }
    for name, data in cases.items():
        p = tmp_path / name
        p.write_bytes(data)
        text = " ".join(x["text"] for x in _parse_child(str(p)).paras)
        if name == "gbk.txt":
            assert "杀人越货" in text
        else:
            assert "Phileas Fogg" in text and not re.search(r"[㐀-鿿]", text), name
    assert "“yes”" in " ".join(x["text"] for x in _parse_child(str(tmp_path / "cp1252.txt")).paras)
