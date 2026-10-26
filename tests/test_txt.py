"""txt 按原文件的行、空行、缩进分段（library/txt.py）；epub/mobi 里中文首行缩进、零段距、<pre> 的分段（library/extract.py）。

回归：txt 原先交给 MuPDF 排版再按版面合段，空行丢了，《Up From Slavery》整本一段、《Cabin Fever》18 段。
"""
from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from library.txt import is_heading, parse_txt, split

SENTS = ["The house stood at the end of a long road lined with old elms.",
         "She had never seen the sea before that summer, and the thought of it kept her awake.",
         "Nobody in the village could remember a winter so cold, or a spring that came so late.",
         "He folded the letter twice and put it in his pocket without a word to anyone.",
         "The train was late again, and the platform was crowded with tired people going home."]
PARAS = [" ".join(SENTS[(i + k) % 5] for k in range(2 + i % 3)) for i in range(40)]


def wrap(text: str, width: int = 70) -> list[str]:
    lines, cur = [], ""
    for w in text.split():
        if cur and len(cur) + 1 + len(w) > width:
            lines.append(cur)
            cur = w
        else:
            cur = f"{cur} {w}" if cur else w
    return lines + [cur]


def test_one_paragraph_per_line_crlf():
    """《Up From Slavery》式：一行一段、段间空行、CRLF。"""
    text = "\r\n\r\n".join(PARAS) + "\r\n"
    assert split(text) == PARAS


def test_one_paragraph_per_line_without_blank_lines():
    assert split("\n".join(PARAS)) == PARAS


def test_wrapped_with_blank_lines():
    """古腾堡式：每行 70 字母折行，段间空行。"""
    text = "\n\n".join("\n".join(wrap(p)) for p in PARAS)
    assert split(text) == PARAS


def test_wrapped_with_indent_only():
    """折行、没有空行，首行缩进四个空格。"""
    text = "\n".join("    " + "\n".join(wrap(p)) for p in PARAS)
    assert split(text) == PARAS


def test_wrapped_bare():
    """折行、既没空行也没缩进（《二十年后》）：段尾短行、句末收尾又没排满的行是段落最后一行。"""
    text = "\n".join("\n".join(wrap(p)) for p in PARAS)
    assert split(text) == PARAS


def test_double_spaced():
    """每行后面都空一行：一个空行只是行距，两个以上才分段。"""
    text = "\n\n\n".join("\n\n".join(wrap(p)) for p in PARAS)
    assert split(text) == PARAS


def test_inline_paragraph_marks():
    """转换时换行被吞掉，段落之间只剩一串空格（《Bentham》）。"""
    text = "\n\n".join("     ".join(PARAS[i:i + 4]) for i in range(0, 40, 4))
    assert split(text) == PARAS


def test_page_numbers_and_running_heads_removed():
    """pdf 转来的 txt：每页的页码、页眉去掉，跨页的段落接回去。"""
    lines = [l for p in PARAS for l in wrap(p) + [""]]
    out = []
    for i in range(0, len(lines), 12):
        out += lines[i:i + 12] + ["", "STORIES", "", str(i // 12 + 1), ""]
    got = split("\n".join(out))
    assert got == PARAS
    assert not any(t in ("STORIES",) or t.isdigit() for t in got)


ZH = ["孔若君拿出他用来乘放各种光盘的盒子，从中寻找能切换图片的软件。软件安装完毕后，孔若君开始尝试。",
      "“我自己编一个。”孔若君说。",
      "孔若君在编制电脑软件方面有一定程度的天赋，上高二时，他参加过全国青少年计算机软件大赛，获得了二等奖。",
      "饮水机旁的地上有一桶矿泉水，但殷静显然懒得换水。孔若君转身想回去，他想了想，走到饮水机旁。"] * 10


def test_chinese_indent_per_line_gbk(tmp_path):
    """一行一段、全角空格缩进、GBK 编码。"""
    p = tmp_path / "zh.txt"
    p.write_bytes(("第一章 不可思议的一幕\r\n" + "".join("　　" + t + "\r\n" for t in ZH)).encode("gb18030"))
    r = parse_txt(p)
    assert [x["text"] for x in r.paras] == ["第一章 不可思议的一幕"] + ZH
    assert r.paras[0]["kind"] == "h" and r.toc[0][1] == "第一章 不可思议的一幕"


def test_chinese_wrapped():
    """中文定宽折行（每行 30 字）、首行全角空格缩进；没缩进时按句末短行分段。"""
    rows = lambda t, ind: [ind + t[:28]] + [t[i:i + 30] for i in range(28, len(t), 30)]
    assert split("\n".join(l for t in ZH for l in rows(t, "　　"))) == ZH
    assert split("\n".join(l for t in ZH for l in rows(t, ""))) == ZH


def test_headings():
    for t in ["CHAPTER TWO. TWO MAKE A QUARREL", "Chapter 1", "Preface", "XII", "第三章 西安与黄土地带", "◇  第三章  ◇", "序言"]:
        assert is_heading(t), t
    for t in ["He said nothing.", "HELP!", '"SEPTEMBER 15, 1864.', "他说：第一章是这样写的。"]:
        assert not is_heading(t), t


def test_contents_list_and_speaker_names_not_headings(tmp_path):
    """书前的目录（一串标题）、剧本里反复出现的角色名不进目录。"""
    p = tmp_path / "play.txt"
    body = ["CONTENTS", "CHAPTER I", "CHAPTER II", "CHAPTER III", "CHAPTER I"]
    for k in range(4):
        body += ["MAYOR.", PARAS[k], "GOUGH.", PARAS[k + 1]]
    p.write_text("\n\n".join(body))
    r = parse_txt(p)
    heads = [x["text"] for x in r.paras if x["kind"] == "h"]
    assert heads == ["CHAPTER I"] and [t for _, t, *_ in r.toc] == ["CHAPTER I"]


@pytest.fixture()
def lib(tmp_path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setenv("LIBRARY_DATA", str(data))
    root = tmp_path / "书库"
    root.mkdir()
    from library.config import load
    cfg = load()
    cfg.sources = [str(root)]
    return cfg, root


def test_scan_reparses_old_txt(lib):
    """入库：txt 走新的分段，记 txt 的解析器版本；旧版本（MuPDF 排版）入库的 txt 再扫描时重新解析。"""
    from library.db import DB
    from library.extract import PARSER_VER, PARSER_VERS, parser_ver
    from library.scan import Scanner
    cfg, root = lib
    p = root / "Up From Slavery.txt"
    p.write_text("\r\n\r\n".join(PARAS))
    db = DB(cfg.db_path)
    sc = Scanner(db, cfg, set())
    assert parser_ver(p) == PARSER_VERS[".txt"] > PARSER_VER and parser_ver(root / "a.epub") == PARSER_VER
    assert sc.add_file(p, full=True) == "added"
    b = db.one("SELECT id, n_paras, parser_ver FROM books WHERE path=?", (str(p),))
    assert b["n_paras"] == 40 and b["parser_ver"] == PARSER_VERS[".txt"]
    assert sc.add_file(p) == "skipped"
    db.run("UPDATE books SET parser_ver=2, n_paras=1 WHERE id=?", (b["id"],))
    assert sc.add_file(p) == "added"
    assert db.one("SELECT n_paras FROM books WHERE id=?", (b["id"],))["n_paras"] == 40


# ---------------------------------------------------------------- epub / mobi 的同类症状
def make_epub(path: Path, body: str, css: str = "") -> None:
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("mimetype", "application/epub+zip")
        z.writestr("META-INF/container.xml",
                   '<?xml version="1.0"?><container xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
                   '<rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>'
                   '</rootfiles></container>')
        z.writestr("OEBPS/content.opf",
                   '<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" '
                   'xmlns:dc="http://purl.org/dc/elements/1.1/"><metadata><dc:title>T</dc:title></metadata>'
                   '<manifest><item id="c1" href="ch1.xhtml" media-type="application/xhtml+xml"/></manifest>'
                   '<spine><itemref idref="c1"/></spine></package>')
        z.writestr("OEBPS/ch1.xhtml", f"<html><head><style>{css}</style></head><body>{body}</body></html>")


def _paras(path: Path) -> list[str]:
    from library.scan import _init_words, _parse_child
    _init_words(set())
    return [x["text"] for x in _parse_child(str(path)).paras if x["kind"] == "p"]


def test_epub_chinese_fullwidth_indent_no_margin(tmp_path):
    """中文 epub：段距为 0，首行缩进直接写成「　　」（《20几岁要懂得的处世心理学》原先整本一段）。"""
    p = tmp_path / "zh.epub"
    make_epub(p, "".join(f"<p>　　{t}</p>" for t in ZH * 3), "p { margin: 0; text-indent: 0 }")
    assert _paras(p) == ZH * 3


def test_epub_chinese_no_indent_no_margin(tmp_path):
    """中文 epub 既没缩进也没段距：句末收尾、后面还放得下字的行是段落最后一行。"""
    p = tmp_path / "zh2.epub"
    make_epub(p, "".join(f"<p>{t}</p>" for t in ZH * 3), "p { margin: 0; text-indent: 0 }")
    assert _paras(p) == ZH * 3


def test_epub_scene_gaps_do_not_force_spacing_mode(tmp_path):
    """段距为 0 的英文书，只有标题、场景分隔有大间距：不能当成「按段间距分段」，整页并成一段（《Crocodile Tears》）。"""
    p = tmp_path / "en.epub"
    body = "".join((f'<h2 style="margin:2em 0">Chapter {i + 1}</h2>' if i % 4 == 0 else "") + f"<p>{t}</p>"
                   for i, t in enumerate(PARAS * 2))
    make_epub(p, body, "p { margin: 0; text-indent: 0 }")
    got = _paras(p)
    assert len(got) >= 0.9 * 80 and sum(t in PARAS for t in got) >= 0.85 * 80   # 原先每页一段（约 8 段）
    # 分不开的只剩「最后一行正好排满」的段（零段距、无缩进，版面上本来就看不出）


def test_epub_pre_keeps_blank_lines(tmp_path):
    """整章一个 <pre>、段与段之间空一行（《重返普罗旺斯》）：换行和空行留着，长行照样折行。"""
    p = tmp_path / "pre.epub"
    make_epub(p, "<pre>" + "\n\n".join(ZH * 3) + "</pre>")
    assert _paras(p) == ZH * 3
