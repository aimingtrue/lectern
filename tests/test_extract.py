"""分段与断字：用 PyMuPDF 排出几种版式的合成书，解析回来必须和原文的段落一字不差。

版式：首行缩进 + 对话整段缩进 + 段间距 + ‐ 断字（类似盖茨比）；首行缩进无段间距 + ASCII 断字；
无缩进 + 段间距（block 样式）。在页尾/页首专门放了：排满的最后一行（段落跨页）、短行收尾（下一页新段）、
对话跨页、‐ 跨页断字、行尾 ASCII 合成词（living-room、Fifty-Ninth）、行尾 Mr.、章节标题。
"""
from __future__ import annotations

import random
from pathlib import Path

import pymupdf
import pytest

from library.extract import parse, vector_art

FONT = pymupdf.Font("cjk")          # PyMuPDF 自带（Droid Sans Fallback），能排出 ‐ “ ” ’ — 这些字符
SIZE, LEAD, LEFT, W, TOP, BOTTOM = 11, 15, 50, 300, 60, 540
SENTS = """The house stood at the end of a long road lined with old elms.
She had never seen the sea before that summer.
Nobody in the village could remember a winter so cold.
He folded the letter twice and put it in his pocket without a word.
The train was late again, and the platform was crowded with tired people.
It was the kind of evening that makes everyone talk a little too much.
Somewhere a dog barked, and then the street was quiet once more.
They walked along the river until the lamps came on one by one.
Her father had built the boat with his own hands.
I remember the smell of rain on the hot stones of the courtyard.
The orchestra played a slow waltz that nobody danced to.
By morning the snow had covered every trace of their footsteps.
His voice was low and steady, as if he had rehearsed the speech.
We waited in the hall while the clock ticked on the wall.
The garden was full of roses, and the air was heavy with their scent.""".split("\n")
WORDS = "the old house stood quiet under grey sky while people walked along river and talked".split()


def width(t: str) -> float:
    return FONT.text_length(t, SIZE)


def wrap(text: str, first: float, rest: float, hy: str) -> list[str]:
    """贪心折行。词里的 | 是可断字的位置（断开时行尾加 hy）；合成词可在 '-' 后断开。"""
    lines, cur = [], ""
    words = text.split(" ")
    i = 0
    while i < len(words):
        w = words[i]
        lim = first if not lines else rest
        cand = (cur + " " + w if cur else w).replace("|", "")
        if width(cand) <= lim:
            cur = cand
            i += 1
            continue
        # 放不下：试着在 | 或 - 处断开
        cuts = [k for k, c in enumerate(w) if c in "|-"]
        for k in reversed(cuts):
            head = w[:k].replace("|", "") + (hy if w[k] == "|" else "-")
            if width((cur + " " + head) if cur else head) <= lim:
                lines.append((cur + " " + head) if cur else head)
                cur, words[i] = "", w[k + 1:]
                break
        else:
            if not cur:                      # 一个词比一行还长：硬放
                cur, i = cand, i + 1
            lines.append(cur)
            cur = ""
    if cur:
        lines.append(cur)
    return lines


def full_line(ending: str, lim: float, rnd: random.Random) -> str:
    """以 ending 结尾、尽量排满一行（再多一个词就放不下）。"""
    line = ending
    while True:
        cand = rnd.choice(WORDS) + " " + line
        if width(cand) > lim:
            return line[0].upper() + line[1:]
        line = cand


class Book:
    def __init__(self, path: Path, gap: float = 0, indent: float = 14, block: float | None = None, hy: str = "‐"):
        self.path, self.gap, self.indent, self.block, self.hy = path, gap, indent, block, hy
        self.doc = pymupdf.open()
        self.expected: list[str] = []
        self.heads: list = []
        self.pg = None
        self.y = BOTTOM + 1
        self.n = 0

    def new_page(self):
        self.pg = self.doc.new_page(width=420, height=595)
        self.pg.insert_font(fontname="U", fontbuffer=FONT.buffer)
        self.n += 1
        self.text(150, 30, "THE SYNTHETIC NOVEL", 8)           # 页眉
        self.text(205, 580, str(self.n), 8)                     # 页码
        self.y = TOP

    def text(self, x, y, t, size=SIZE):
        self.pg.insert_text((x, y), t, fontsize=size, fontname="U")

    def heading(self, t: str):
        self.new_page()
        self.text((420 - FONT.text_length(t, 18)) / 2, self.y, t, 18)
        self.y += 45
        self.expected.append(t)
        self.heads.append([1, t, self.n])

    def para(self, text: str = "", dialogue: bool = False, lines: list[str] | None = None, brk: int | None = None,
             expect: str | None = None):
        """一段。dialogue：整段缩进（block 版式）。lines：手工给定的行。brk：第 brk 行之前强制换页。"""
        bx = self.block if (dialogue and self.block is not None) else 0
        fx = bx if dialogue and self.block is not None else self.indent
        if lines is None:
            lines = wrap(text, W - fx, W - bx, self.hy)
        if self.pg is not None and self.y != TOP:
            self.y += self.gap
        for i, ln in enumerate(lines):
            if self.pg is None or self.y > BOTTOM or i == brk:
                self.new_page()
            self.text(LEFT + (fx if i == 0 else bx), self.y, ln)
            self.y += LEAD
        self.expected.append(expect if expect is not None else text.replace("|", ""))

    def end_page(self):
        self.y = BOTTOM + 1

    def save(self) -> Path:
        self.doc.save(self.path)
        return self.path


def filler(rnd: random.Random, n: int, dialogue_ratio: float = 0.35) -> list[tuple[str, bool]]:
    out = []
    for _ in range(n):
        if rnd.random() < dialogue_ratio:
            s = " ".join(rnd.sample(SENTS, rnd.randint(1, 3)))
            out.append(("“" + s[:-1] + ",” she said quietly. “" + rnd.choice(SENTS) + "”", True))
        else:
            out.append((" ".join(rnd.sample(SENTS, rnd.randint(2, 5))), False))
    return out


LONG = ("vul|ner|able nar|ra|tor re|mem|bered every|thing ex|tra|or|di|nary un|for|get|table con|ver|sa|tions "
        "in|ter|mi|na|ble af|ter|noons mag|nif|i|cent cel|e|bra|tions de|ter|mi|na|tion ir|re|sist|ible "
        "con|temp|tu|ous mel|an|choly en|thu|si|as|tic").split()


def hyphenated(rnd: random.Random) -> str:
    """一段长词很多的文字：折行时多半要在词中断开（‐）。"""
    ws = rnd.sample(LONG, 10)
    return "The " + " and ".join(" ".join(ws[k:k + 2]) for k in range(0, 10, 2)) + " were remembered."


def texts(p) -> list[str]:
    return [x["text"] for x in p.paras]


def mismatch(got: list[str], want: list[str]) -> str:
    for i, (a, b) in enumerate(zip(got, want)):
        if a != b:
            return f"#{i}\n got: {a!r}\nwant: {b!r}"
    return f"段落数不同：{len(got)} vs {len(want)}"


# ---------------------------------------------------------------- 类似盖茨比：首行缩进、对话整段缩进、段间距、‐ 断字
def gatsby_like(tmp: Path, gap: float = 7) -> Book:
    rnd = random.Random(7)
    b = Book(tmp / "g.pdf", gap=gap, indent=14, block=13.5, hy="‐")
    b.heading("Chapter One")
    for i, (t, d) in enumerate(filler(rnd, 26)):
        b.para(t, dialogue=d)
        if i % 2 == 0:
            b.para(hyphenated(rnd))
    # 1) 页尾排满、以句号结束，下一页接着（左边界）→ 同一段
    p1 = full_line("and that was how it ended.", W, rnd)
    first = full_line("and so we went on", W - 14, rnd)
    b.para(lines=[first, p1, "Nevertheless, the story was not over, and we all knew it."], brk=2,
           expect=f"{first} {p1} Nevertheless, the story was not over, and we all knew it.")
    # 2) 页尾短行收尾，下一页首行缩进 → 新段
    b.para("It was late when we finally got home.")
    b.end_page()
    b.para("The next morning the whole town was talking about the party.")
    # 3) 对话整段缩进、跨页
    b.para(lines=["“I don’t know what you mean,” he said, looking at the", "garden. “I never said anything of the kind to anybody.”"],
           dialogue=True, brk=1,
           expect="“I don’t know what you mean,” he said, looking at the garden. “I never said anything of the kind to anybody.”")
    # 4) ‐ 断字：页内与跨页
    b.para(lines=["Through the French win‐", "dows we could see the lawn and the bright"], brk=1,
           expect="Through the French windows we could see the lawn and the bright")
    b.para(lines=["The room was full of sun‐", "light and of the noise of the guests."],
           expect="The room was full of sunlight and of the noise of the guests.")
    # 5) 行尾 ASCII 合成词：保留连字符、不加空格
    b.para(lines=["We sat in the living-", "room until midnight, and then drove up Fifty-", "Ninth Street to the hotel."],
           expect="We sat in the living-room until midnight, and then drove up Fifty-Ninth Street to the hotel.")
    # 6) 行尾 Mr.（页内、跨页）不断段
    b.para(lines=["At the door stood Mr.", "Albrucksburger with his hat in his hand, and Mrs.", "Ulysses Swett behind him."],
           brk=2, expect="At the door stood Mr. Albrucksburger with his hat in his hand, and Mrs. Ulysses Swett behind him.")
    b.heading("Chapter Two")
    for t, d in filler(rnd, 14):
        b.para(t, dialogue=d)
    return b


@pytest.mark.parametrize("gap", [7, 0])
def test_gatsby_like_roundtrip(tmp_path, gap):
    b = gatsby_like(tmp_path, gap)
    p = parse(b.save())
    got = texts(p)
    assert got == b.expected, mismatch(got, b.expected)
    assert [x["kind"] for x in p.paras if x["text"].startswith("Chapter")] == ["h", "h"]
    # 段落的起始页：跨页的段落记它开始的那一页
    pages = {x["text"][:30]: x["page"] for x in p.paras}
    assert pages["Through the French windows we "] + 1 == \
        next(x["page"] for x in p.paras if x["text"].startswith("The room was full of sunlight"))
    # 目录：从标题识别，段落序号指向标题段
    assert [(t, p.paras[n]["text"]) for _, t, _, n in p.toc] == [("Chapter One", "Chapter One"), ("Chapter Two", "Chapter Two")]


def test_gatsby_like_hyphen_rules(tmp_path):
    b = gatsby_like(tmp_path)
    path = b.save()
    doc = pymupdf.open(path)
    assert sum(ln.endswith("\u2010") for pg in doc for ln in pg.get_text().splitlines()) >= 10, "测试书里应有足够多的 ‐ 断字"
    # 词典里有 livingroom 也不能合并：这本书用 ‐ 断字，行尾 ASCII '-' 是真连字符
    p = parse(path, {"livingroom", "fiftyninth"})
    j = " ".join(texts(p))
    assert all(w.replace("|", "") in j for w in LONG)
    assert "living-room" in j and "Fifty-Ninth" in j and "livingroom" not in j and "Fifty- Ninth" not in j
    assert "‐" not in j


def test_outline_toc_maps_to_heading(tmp_path):
    b = gatsby_like(tmp_path)
    b.doc.set_toc(b.heads)
    p = parse(b.save())
    assert [t for _, t, _, _ in p.toc] == ["Chapter One", "Chapter Two"]
    for _, t, pg, n in p.toc:
        assert p.paras[n]["kind"] == "h" and p.paras[n]["text"] == t and p.paras[n]["page"] == pg


# ---------------------------------------------------------------- 首行缩进、没有段间距、ASCII 断字（词典判断）
def test_indent_style_no_gaps(tmp_path):
    rnd = random.Random(3)
    b = Book(tmp_path / "i.pdf", gap=0, indent=16, block=None, hy="-")
    b.heading("Chapter I")
    for i, (t, d) in enumerate(filler(rnd, 40, dialogue_ratio=0.3)):
        b.para(t, dialogue=d)
        if i % 7 == 3:
            b.para("The vul|ner|able old man re|mem|bered the house.")
    p1 = full_line("the door was locked.", W, rnd)
    first = full_line("we tried the gate and", W - 16, rnd)
    b.para(lines=[first, p1, "Afterwards we sat on the steps and waited for the rain."], brk=2,
           expect=f"{first} {p1} Afterwards we sat on the steps and waited for the rain.")
    b.para("That was the last we saw of him.")
    b.end_page()
    b.para("Years later I went back to the house.")
    b.para(lines=["He shook hands with Mr.", "Wolfshiem and sat down."], expect="He shook hands with Mr. Wolfshiem and sat down.")
    p = parse(b.save(), {"vulnerable", "remembered"})
    got = texts(p)
    assert got == b.expected, mismatch(got, b.expected)


# ---------------------------------------------------------------- block 版式：无缩进、段间距
def test_block_style(tmp_path):
    rnd = random.Random(5)
    b = Book(tmp_path / "b.pdf", gap=9, indent=0, block=None, hy="‐")
    b.heading("Introduction")
    for t, d in filler(rnd, 30, dialogue_ratio=0.2):
        b.para(t)
        b.para("It was ex|tra|or|di|nary, and every|body knew it was ex|tra|or|di|nary.")
    p1 = full_line("the meeting was over.", W, rnd)
    first = full_line("we waited and", W, rnd)
    b.para(lines=[first, p1, "Nevertheless, nobody wanted to leave the room."], brk=2,
           expect=f"{first} {p1} Nevertheless, nobody wanted to leave the room.")
    b.para("Short and done.")
    b.end_page()
    b.para("A new paragraph at the top of the page.")
    p = parse(b.save())
    got = texts(p)
    assert got == b.expected, mismatch(got, b.expected)


def test_quality_metrics(tmp_path):
    p = parse(gatsby_like(tmp_path).save())
    q = p.quality
    assert q["paras"] > 50 and q["sents"] > q["paras"] and q["hyph"] == 0 and q["long"] == 0
    assert 0 <= q["frag"] < 0.05 and q["score"] < 10


# ---------------------------------------------------------------- 真书回归：The Great Gatsby（本机没有这本 PDF 时跳过）
def gatsby_pdf() -> Path | None:
    """GATSBY_PDF 环境变量 > 书库数据库里记录的路径（只读打开） > 已知位置。"""
    import os
    import sqlite3
    root = Path(__file__).resolve().parent.parent
    cands = [os.environ.get("GATSBY_PDF", "")]
    db = root / "data" / "library.db"
    if db.exists():
        try:
            c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            cands += [r[0] for r in c.execute("SELECT path FROM books WHERE path LIKE '%gatsby%'")]
            c.close()
        except sqlite3.Error:
            pass
    cands += [str(Path.home() / "Documents/judou/samples/the-great-gatsby.pdf")]
    return next((Path(p) for p in cands if p and Path(p).exists()), None)


@pytest.fixture(scope="module")
def gatsby():
    path = gatsby_pdf()
    if path is None:
        pytest.skip("本机没有 The Great Gatsby PDF")
    from library.dictionary import Dictionary
    root = Path(__file__).resolve().parent.parent
    words = Dictionary(root / "data" / "dict.db").words() if (root / "data" / "dict.db").exists() else None
    return path, parse(path, words)


def test_gatsby_regressions(gatsby):
    import re
    from library.text import ends_sentence, split
    path, p = gatsby
    paras = p.paras
    txt = "\n".join(x["text"] for x in paras)

    def same_para(a, b):
        return any(a in x["text"] and b in x["text"] for x in paras)

    def same_sent(a, b):
        return any(a in s and b in s for x in paras for s in split(x["text"]))

    # 断字：行尾 ASCII '-' 是真合成词（这本书用 ‐ 断字）；大写续行不加空格；跨页断字要接上
    for bad in ["livingroom", "wellbeing", "machineguns", "Fifty- Ninth", "Fitz- Peters", "‐"]:
        assert bad not in txt, bad
    assert "living-room" in txt and "well-being" in txt and "machine-guns" in txt
    assert "Fifty-Ninth" in txt and "Fitz-Peters" in txt
    assert not re.search(r"\w[-‐] \w", txt), re.findall(r".{20}\w[-‐] \w.{20}", txt)[:5]
    assert not any(x["text"].endswith(("-", "‐")) for x in paras)
    # 页内误分段
    assert same_para("obvious suppressions.", "Reserving judgements") and "suppressions. Reserving" in txt
    assert "and Mr. Albrucksburger" in txt
    assert not any(x["text"].endswith(("Mr.", "Mrs.")) for x in paras)
    # 断句
    assert same_sent("village?”", "he asked helplessly")
    assert same_sent("Doctor T. J.", "Eckleburg")
    # 段落数：正文 1621 段 + 书名页
    ps = [x for x in paras if x["kind"] == "p"]
    assert 1600 <= len(ps) <= 1640, len(ps)
    frag = [x["text"][-40:] for x in ps if not (ends_sentence(x["text"]) or x["text"].endswith(("—", "–")))]
    assert len(frag) <= 12, frag
    assert [t for _, t, _, _ in p.toc][1:] == [f"Chapter {r}" for r in "I II III IV V VI VII VIII IX".split()]
    assert all(paras[n]["text"] == t for _, t, _, n in p.toc[1:])


def test_gatsby_matches_plain_text(gatsby):
    """同目录有 Standard Ebooks 的纯文本时：正文段落必须和它逐段一致。"""
    import re
    path, p = gatsby
    ref = path.with_suffix(".txt")
    if not ref.exists():
        pytest.skip("没有对照用的纯文本")

    def norm(t):
        return re.sub(r"\s+", " ", t.replace("⁠", "")).strip()
    truth = [norm(x) for x in ref.read_text().split("\n\n") if norm(x) and norm(x) != "---"]
    got = {norm(x["text"]) for x in p.paras}
    missing = [t[:60] for t in truth if t not in got]
    assert not missing, missing[:5]


def test_vector_art_cover_is_not_typo(tmp_path):
    """第一页只有矢量画的插图（没有位图）也不是文字封面；白底矩形、小 logo、分隔线不算插图。"""
    def make(name, draw):
        doc = pymupdf.open()
        pg = doc.new_page(width=420, height=595)
        pg.insert_text((120, 300), "The Great Gatsby", fontsize=20)
        draw(pg)
        for _ in range(3):
            doc.new_page(width=420, height=595).insert_text((50, 60), SENTS[0], fontsize=SIZE)
        path = tmp_path / name
        doc.save(path)
        return path

    art = make("art.pdf", lambda pg: pg.draw_rect(pymupdf.Rect(110, 100, 310, 240), color=None, fill=(0.1, 0.12, 0.23)))
    plain = make("plain.pdf", lambda pg: (pg.draw_rect(pg.rect, color=None, fill=(1, 1, 1), overlay=False),
                                          pg.draw_rect(pymupdf.Rect(190, 500, 230, 520), color=None, fill=(0.1, 0.3, 0.5)),
                                          pg.draw_line((60, 330), (360, 330))))
    assert vector_art(pymupdf.open(art)[0]) and not vector_art(pymupdf.open(plain)[0])
    assert parse(art).typo_cover is False
    assert parse(plain).typo_cover is True
