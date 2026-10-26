"""扫描清理的安全性：书库目录不在（拔盘、改名）或是空的时候，不能把书从书库删掉。"""
from __future__ import annotations

from pathlib import Path

import pytest

from library.config import load
from library.db import DB
from library.scan import Scanner
from test_library import make_pdf


@pytest.fixture()
def env(tmp_path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setenv("LIBRARY_DATA", str(data))
    cfg = load()
    db = DB(cfg.db_path)
    return tmp_path, db, Scanner(db, cfg, None)


def two_books(d: Path) -> None:
    d.mkdir(parents=True)
    make_pdf(d / "a.pdf")
    make_pdf(d / "b.pdf")
    (d / "b.pdf").write_bytes((d / "b.pdf").read_bytes() + b"\n%x")   # 内容不同，免得被当成同一本的副本


def count(db) -> int:
    return db.one("SELECT COUNT(*) AS n FROM books")["n"]


def test_missing_root_keeps_books_and_progress(env):
    tmp, db, sc = env
    books = tmp / "Volumes" / "Elements SE" / "Books"
    two_books(books)
    assert sc.run([str(books)]).added == 2
    db.conn.execute("UPDATE books SET progress_page=5 WHERE path LIKE '%a.pdf'")
    db.conn.commit()
    books.parent.rename(tmp / "unplugged")                     # 拔盘
    s = sc.run([str(books)])
    assert count(db) == 2 and s.errors and "folder missing" in s.errors[0]["error"]
    (tmp / "unplugged").rename(books.parent)                   # 插回：不用重新解析，进度还在
    s = sc.run([str(books)])
    assert s.added == 0 and s.skipped == 2
    assert db.one("SELECT progress_page AS p FROM books WHERE path LIKE '%a.pdf'")["p"] == 5


def test_missing_root_does_not_block_other_roots(env):
    tmp, db, sc = env
    gone, here = tmp / "gone", tmp / "here"
    two_books(gone)
    here.mkdir()
    for n in ("c", "d"):
        make_pdf(here / f"{n}.pdf")
        (here / f"{n}.pdf").write_bytes((here / f"{n}.pdf").read_bytes() + f"\n%{n}".encode())
    sc.run([str(gone), str(here)])
    assert count(db) == 4
    gone.rename(tmp / "gone_away")
    (here / "c.pdf").unlink()                                  # 存在的目录里真的删掉一本：照常移除
    sc.run([str(gone), str(here)])
    paths = sorted(Path(r["path"]).name for r in db.q("SELECT path FROM books"))
    assert paths == ["a.pdf", "b.pdf", "d.pdf"]


def test_empty_root_with_books_is_not_cleaned(env):
    tmp, db, sc = env
    books = tmp / "mnt"
    two_books(books)
    sc.run([str(books)])
    for f in books.iterdir():                                  # 挂载点还在但里面是空的
        f.rename(tmp / f.name)
    s = sc.run([str(books)])
    assert count(db) == 2 and any("nothing removed, to be safe" in e["error"] for e in s.errors)


def test_root_prefix_is_path_aware(env):
    tmp, db, sc = env
    a, b = tmp / "Books", tmp / "Books2"
    a.mkdir()
    make_pdf(a / "a.pdf")
    (a / "a.pdf").write_bytes((a / "a.pdf").read_bytes() + b"\n%y")
    two_books(b)
    sc.run([str(a), str(b)])
    (b / "a.pdf").unlink()
    sc.run([str(a)])                                           # 只扫 /Books：/Books2 下的书不该被当成它的
    assert count(db) == 3


def test_config_save_keeps_user_keys(tmp_path, monkeypatch):
    """保存设置不能清掉用户自己写进 config.json 的字段（API key 等）。"""
    import json
    from library.config import load
    monkeypatch.setenv("LIBRARY_DATA", str(tmp_path))
    (tmp_path / "config.json").write_text(json.dumps({"sources": ["/a"], "nyt_key": "k1", "google_books_key": "k2"}))
    cfg = load()
    cfg.sources = ["/b"]
    cfg.save()
    d = json.loads((tmp_path / "config.json").read_text())
    assert d["sources"] == ["/b"] and d["nyt_key"] == "k1" and d["google_books_key"] == "k2"


def test_file_vanishing_mid_scan_does_not_abort(env, monkeypatch):
    """扫描途中有文件被移走：这一本记失败，其余照常入库。"""
    tmp, db, sc = env
    books = tmp / "Books"
    two_books(books)
    real = Scanner.add_file

    def flaky(self, path, *a, **k):
        if path.name == "a.pdf":
            raise FileNotFoundError(2, "No such file or directory", str(path))
        return real(self, path, *a, **k)
    monkeypatch.setattr(Scanner, "add_file", flaky)
    s = sc.run([str(books)])
    assert s.added == 1 and s.failed == 1 and count(db) == 1
    assert any(e["path"].endswith("a.pdf") and "FileNotFoundError" in e["error"] for e in s.errors)


def test_scan_syncs_titles_from_catalog(env):
    """编目规则改了（Anna's Archive 文件名拆开）之后，没重新解析的书也跟上编目里的书名作者。"""
    from library.catalog import Catalog
    tmp, db, sc = env
    Catalog(tmp / "data" / "catalog.db").conn.close()
    books = tmp / "Books"
    books.mkdir()
    make_pdf(books / "Book Lovers -- Emily Henry -- 2022 -- Penguin -- 0593440870.pdf")
    assert sc.run([str(books)]).added == 1
    row = db.one("SELECT id, title, author FROM books")
    assert (row["title"], row["author"]) == ("Book Lovers", "Emily Henry")
    db.conn.execute("UPDATE books SET title='Book Lovers -- Emily Henry -- 2022', author=''")
    db.conn.commit()
    s = sc.run([str(books)])
    assert s.skipped == 1
    row = db.one("SELECT title, author FROM books")
    assert (row["title"], row["author"]) == ("Book Lovers", "Emily Henry")
