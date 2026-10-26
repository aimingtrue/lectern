"""新书入库：收件箱的文件名清洗、非书文件留下、内容相同的去重、移进书库的位置。全部用临时目录。"""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import time

import pytest

from library import ingest
from library.catalog import clean, strip_ads
from library.extract import book_id

OLD = 1_000_000_000.0          # 足够早的修改时间：不算「刚改过」
NOW = 2_000_000_000.0
MONTH = time.strftime("%Y-%m", time.localtime(NOW))


def put(p: Path, data: bytes = b"", mtime: float = OLD) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data or p.name.encode() * 50)
    os.utime(p, (mtime, mtime))
    return p


@pytest.mark.parametrize("raw, want", [
    ("More+Than+You+Know_+Finding+Financial+Wisdom【多多店铺：山川的书屋】", "More Than You Know_ Finding Financial Wisdom"),
    ("黑色大西洋：现代性与双重意识 【公众号：书单严选】", "黑色大西洋：现代性与双重意识"),
    ("不服周：楚国的奋斗与沉沦.fixed.fixed", "不服周：楚国的奋斗与沉沦"),
    ("提示：❥❥每个月免费代找一本书库没有的❥❥", "提示：每个月免费代找一本书库没有的"),
    ("高岛易断占断破解 (高岛嘉右卫门) (z-lib.org)", "高岛易断占断破解 (高岛嘉右卫门)"),
    ("C++ Primer", "C++ Primer"),
    ("三国争霸（全三册） (渤海小吏) (z-library.sk, 1lib.sk, z-lib.sk)", "三国争霸（全三册） (渤海小吏)"),
    ("公众号：我爱读书鸭】圣母", "圣母"),
    ("动物农场 公众号：精品书籍推荐分享", "动物农场"),
    ("不能承受的生命之轻 - 米兰·昆德拉（公众号：书单严选）获取免费电子书", "不能承受的生命之轻 - 米兰·昆德拉"),
    ("希腊三部曲：追逐阳光之岛+桃金娘的森林宝藏+众神的花园", "希腊三部曲：追逐阳光之岛+桃金娘的森林宝藏+众神的花园"),
    ("The Lady of the Rivers (Philippa Gregory [Gregory, Philippa])", "The Lady of the Rivers (Philippa Gregory [Gregory, Philippa])"),
])
def test_strip_ads(raw, want):
    assert strip_ads(raw) == want


def test_clean_still_turns_underscores_into_colons():
    assert clean("More+Than+You+Know_+Finding+Wisdom") == "More Than You Know: Finding Wisdom"


def test_clean_part_keeps_title_tags():
    assert ingest.clean_part("Sons and Lovers【儿子与情人】【淘宝店铺：星球素材库】") == "Sons and Lovers【儿子与情人】"
    assert ingest.clean_part("十角馆事件 - 【日】绫辻行人") == "十角馆事件 - 【日】绫辻行人"
    assert ingest.clean_part("【英文书库70000+】") == "【英文书库70000+】"
    assert ingest.clean_part("法医报告1+2+遗骨档案") == "法医报告1+2+遗骨档案"
    assert ingest.clean_part("What+I+Learned+Losing+a+Million+Dollars+(Columbia+Business+S...【淘宝店铺：星球素材库】") == \
        "What I Learned Losing a Million Dollars (Columbia Business S"


@pytest.mark.parametrize("rel, junk", [
    ("a/song.mp3", True),
    ("a/book.epub.downloading", True),
    ("pack/静读天下 – 最新破解版（安卓Mobi阅读器）.apk.1", True),
    ("pack/详细使用介绍3.0（一定要仔细看几遍）.docx", True),
    ("pack/好用的文件格式转换—想要什么格式可以随意转1758122096.pdf", True),
    ("pack/Kindle 推送教程使用必看/Kindle怎么导入电子书.pdf", True),
    ("pack/提示：❥❥每个月免费代找一本书库没有的❥❥/书单.txt", True),
    ("pack/脑的阅读：破解人类阅读之谜.mobi", False),                  # 书名里的「破解」「教程」「说明书」不是广告
    ("pack/跑者教程：精准拉伸+无伤跑法.mobi", False),
    ("pack/美好东西说明书 - 王立.mobi", False),
    ("pack/法律文书教程 - 杨树丽,安凤国.txt", False),
    ("pack/雾都孤儿【淘宝店铺：星球素材库】.txt", False),              # 广告只在文件名里：清洗掉，书留下
])
def test_junk(rel, junk):
    assert (ingest.junk_reason(Path(rel)) is not None) == junk


def test_target_layout(tmp_path):
    root = tmp_path / "lib"
    assert ingest.target(Path("x【淘宝店铺：a】.epub"), root, "2026-10") == root / "电子书/新书-2026-10/x.epub"
    assert ingest.target(Path("【英文书库70000+】/每周更新10本/A.EPUB"), root, "2026-10") == \
        root / "电子书/【英文书库70000+】/每周更新10本/A.epub"
    assert ingest.target(Path("杂志/TIME/2026-10.pdf"), root, "2026-10") == root / "杂志/TIME/2026-10.pdf"


def test_plan_and_apply(tmp_path):
    inbox, root = tmp_path / "下", tmp_path / "lib"
    have = put(root / "电子书/旧/old.epub", b"same content" * 100)
    put(root / f"电子书/新书-{MONTH}/b.epub", b"other" * 100)                     # 目标位置已有同名：加 (2)
    put(inbox / "dup-of-library.epub", b"same content" * 100)
    a1 = put(inbox / "pack/a【公众号：书单严选】.epub", b"aaa" * 100)
    put(inbox / "pack/sub/a copy.epub", b"aaa" * 100)                           # 收件箱里重复
    put(inbox / "b.epub", b"bbb" * 100)
    put(inbox / "pack/song.mp3")
    put(inbox / "fresh.epub", mtime=NOW)                           # 还在下载
    known = {book_id(have): str(have)}
    items = ingest.plan(inbox, root, known, now=NOW + 10)
    by = {it.src.name: it for it in items}
    assert by["dup-of-library.epub"].action == "trash"
    assert by["a copy.epub"].action == "trash" and "收件箱里重复" in by["a copy.epub"].why
    assert by["a【公众号：书单严选】.epub"].dest == root / "电子书/pack/a.epub"
    assert by["b.epub"].dest.name.startswith("b (2)")
    assert by["song.mp3"].action == "keep" and by["fresh.epub"].action == "keep"

    trash = tmp_path / "trash"
    with open(tmp_path / "log.txt", "w") as log:
        done = ingest.apply(items, inbox, trash, log)
    assert done["move"] == 2 and done["trash"] == 2 and done["failed"] == 0
    assert (root / "电子书/pack/a.epub").read_bytes() == b"aaa" * 100 and not a1.exists()
    assert (trash / "pack/sub/a copy.epub").exists() and (trash / "dup-of-library.epub").exists()
    assert not (inbox / "pack/sub").exists()                                  # 空文件夹清掉
    assert (inbox / "pack/song.mp3").exists() and (inbox / "fresh.epub").exists()   # 非书和还在下载的留下
    assert inbox.exists()


def test_library_fids(tmp_path):
    db = tmp_path / "catalog.db"
    c = sqlite3.connect(db)
    c.execute("CREATE TABLE files(path TEXT PRIMARY KEY, fid TEXT)")
    c.execute("INSERT INTO files VALUES('/x.epub', 'abc')")
    c.commit()
    c.close()
    assert ingest.library_fids(db) == {"abc": "/x.epub"}
    assert ingest.library_fids(tmp_path / "missing.db") == {}


def test_dot_named_books_are_moved_and_system_files_skipped(tmp_path):
    """下载来的书被藏成 .金粉世家/、.心血管.pdf：照常入库并去掉开头的「.」；.DS_Store、._xxx 不动。"""
    inbox, root = tmp_path / "下", tmp_path / "lib"
    put(inbox / "加餐/.金粉世家/金粉世家.epub", b"jf" * 100)
    put(inbox / "加餐/.心血管系统与疾病 第2版.pdf", b"xx" * 100)
    put(inbox / "加餐/._金粉世家.epub", b"meta" * 100)
    put(inbox / "加餐/.DS_Store", b"ds" * 100)
    by = {it.src.name: it for it in ingest.plan(inbox, root, {}, now=NOW + 10)}
    assert by["金粉世家.epub"].dest == root / "电子书/加餐/金粉世家/金粉世家.epub"
    assert by[".心血管系统与疾病 第2版.pdf"].dest == root / "电子书/加餐/心血管系统与疾病 第2版.pdf"
    assert "._金粉世家.epub" not in by and ".DS_Store" not in by


def test_same_fingerprint_different_bytes_stays_for_review(tmp_path):
    """指纹只看大小和头尾各 1MB：中段不同的不是同一个文件，不能不比就扔进废纸篓，留在收件箱人工看。"""
    inbox, root = tmp_path / "下", tmp_path / "lib"
    body = bytearray(b"x" * (3 << 20))
    have = put(root / "电子书/旧/book.pdf", bytes(body))
    body[(3 << 20) // 2] = ord("y")                                  # 只改中段一个字节
    put(inbox / "book.pdf", bytes(body))
    put(inbox / "same.pdf", have.read_bytes())
    known = {book_id(have): str(have)}
    by = {it.src.name: it for it in ingest.plan(inbox, root, known, now=NOW + 10)}
    assert book_id(inbox / "book.pdf") == book_id(have)
    assert by["book.pdf"].action == "keep" and "指纹相同但内容不同" in by["book.pdf"].why
    assert by["same.pdf"].action == "trash"
