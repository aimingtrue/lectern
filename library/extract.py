"""PDF 解析：元数据、目录、整本书连续的段落流（去页眉页脚、去断字、识别章节标题）、封面。

只依赖 PyMuPDF。不保留原书排版：输出纯文字段落 [{"kind": "p"|"h", "text": 文字, "page": 段落起始页(0 起)}]。

分段是整本书一条行流（跨页），判断依据都从这本书自己统计出来（不写死磅值）：
正文字号、行距与段间距（只有明显两档时才用间距判断）、左边界与首行缩进、奇偶页右边界、
首行之后续行的位置（有的书对话整段缩进）、换行断字用的是 ‐ 还是 ASCII '-'。
"""
from __future__ import annotations

import bisect
import hashlib
import os
import re
import statistics
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

import pymupdf

from .text import HYPHENS, SOFT_HYPHENS, ends_sentence, join_lines, sentences

PARSER_VER = 2          # 分段/断字规则有改进就加 1：扫描和打开阅读页时会自动重新解析旧版本解析的书
PARSER_VERS = {".txt": 3, ".md": 1}   # 单独改进过的格式（只重新解析这一种格式的书）：3 = txt 按原文件的行和空行分段（library/txt.py）
#                            .md：资料（library/research.py）送进阅读器的 Markdown，按标题、段落、列表、表格行分段（library/md.py）


def parser_ver(path) -> int:
    """这个文件该用的解析器版本：库里记的版本比它旧就重新解析。"""
    return PARSER_VERS.get(Path(path).suffix.lower(), PARSER_VER)

# epub/mobi 排版时不许整段不换行（white-space: nowrap）：MuPDF 会把超出页宽的部分裁掉，正文就丢了。
# <pre> 里的换行和空行要留着（有的书整章是一个 <pre>，段与段之间空一行），只是长行照样折行
pymupdf.mupdf.fz_set_user_css("* { white-space: normal !important; } pre, pre * { white-space: pre-wrap !important; }")
LETTER_SPACED = re.compile(r"^(?:\S ){2,}\S(?: (?:\S ){1,}\S)*$")
HEAD_WORD = re.compile(r"^(chapter|part|book|section|prologue|epilogue|preface|introduction|contents|"
                       r"appendix|afterword|foreword|acknowledg\w*|notes|index|glossary|bibliography)\b", re.I)
ROMAN = re.compile(r"^(?=[IVXLCDM]+$)M{0,3}(CM|CD|D?C{0,3})(XC|XL|L?X{0,3})(IX|IV|V?I{0,3})$")
ZW = dict.fromkeys(map(ord, "⁠​﻿"), None)
QUOTES = "“‘\"'"
CJK = re.compile(r"[\u3400-\u9fff]")
ZH_TAIL = re.compile(r"[\u3400-\u9fff\u3000-\u303f\uff00-\uffef]\s*$")
QUOTE_MARKS = set("“”‘’\"'")
CLOSE_PUNCT = set("，。、；：！？）」』》〉】…—,.;:!?)]}·")
OPEN_PUNCT = set("（「『《〈【([{")
ZH_START = re.compile(r"[“‘「『（(\"']?[\u3400-\u9fff]")
ZH_END = re.compile(r"[。！？…」』”）]\s*$")             # 中文句末（ends_sentence 只认西文标点）


def book_id(path: Path) -> str:
    """内容指纹：大小 + 头尾各 1MB。同一本书换了位置也认得出。"""
    st = path.stat()
    h = hashlib.sha1(str(st.st_size).encode())
    with open(path, "rb") as f:
        h.update(f.read(1 << 20))
        if st.st_size > 2 << 20:
            f.seek(-(1 << 20), os.SEEK_END)
            h.update(f.read(1 << 20))
    return h.hexdigest()[:16]


def unspace(s: str) -> str:
    """'C h a p t e r I' → 'Chapter I'（字母间隔排版的标题）。"""
    if not LETTER_SPACED.match(s):
        return s
    words = re.split(r"\s{2,}", s)
    if len(words) == 1:
        # 单空格分隔：按大写字母切词（Chapter I），否则直接拼起来
        joined = s.replace(" ", "")
        m = re.match(r"^([A-Z][a-z]+)([IVXLCDM\d]+)$", joined)
        return f"{m.group(1)} {m.group(2)}" if m else joined
    return " ".join(w.replace(" ", "") for w in words)


def clean_title(name: str) -> str:
    name = re.sub(r"\.(pdf|PDF)$", "", name)
    name = re.sub(r"[_]+", " ", name)
    name = re.sub(r"\s*[\(\[]?(z-lib\.org|libgen|ebook|epub|pdf)[\)\]]?\s*", " ", name, flags=re.I)
    return re.sub(r"\s+", " ", name).strip(" -")


@dataclass
class Line:
    text: str
    x0: float
    x1: float
    y0: float
    y1: float
    size: float
    bold: bool
    page: int = 0
    head: bool = False


@dataclass
class Parsed:
    title: str
    author: str
    pages: int
    toc: list = field(default_factory=list)          # [(level, title, page0, para_n)]
    paras: list = field(default_factory=list)        # 整本书的段落 [{"kind","text","page"}]
    has_text: bool = True
    lang: str = "en"
    typo_cover: bool = False
    quality: dict = field(default_factory=dict)
    parser_ver: int = PARSER_VER


def _lines(page) -> list[Line]:
    out = []
    d = page.get_text("dict", flags=pymupdf.TEXT_PRESERVE_WHITESPACE | pymupdf.TEXT_MEDIABOX_CLIP)
    for b in d["blocks"]:
        if b.get("type") != 0:
            continue
        for l in b["lines"]:
            spans = [s for s in l["spans"] if s["text"].strip()]
            if not spans:
                continue
            if abs(l["dir"][1]) > 0.2:   # 竖排/旋转文字（页边）跳过
                continue
            text = "".join(s["text"] for s in l["spans"]).translate(ZW)
            text = re.sub(r"\s+", " ", text).strip()
            if not text:
                continue
            size = max(s["size"] for s in spans)
            bold = all(("Bold" in s["font"] or s["flags"] & 16) for s in spans)
            x0, y0, x1, y1 = l["bbox"]
            # 行首的全角空格（中文 epub 常把首行缩进直接写成「　　」）也算在行的范围里：从第一个字算起，缩进才看得出来
            first = spans[0]
            lead = len(first["text"]) - len(first["text"].lstrip())
            x0 = max(x0, first["bbox"][0])
            if lead and ("\u3000" in first["text"][:lead] or lead >= 2):
                x0 += (first["bbox"][2] - first["bbox"][0]) * lead / len(first["text"])
            out.append(Line(text, x0, x1, y0, y1, size, bold))
    # 同一行被拆成多个 line（不同字体）时合并
    out.sort(key=lambda a: (round(a.y0 / 3), a.x0))
    merged: list[Line] = []
    for ln in out:
        if merged and abs(merged[-1].y0 - ln.y0) < 2.5 and ln.x0 >= merged[-1].x1 - 2:
            m = merged[-1]
            m.text += ("" if m.text.endswith(" ") else " ") + ln.text
            m.x1, m.size, m.bold = ln.x1, max(m.size, ln.size), m.bold and ln.bold
        else:
            merged.append(ln)
    return merged


def _margin_key(t: str) -> str:
    return re.sub(r"\d+", "#", t.lower()).strip()


def _strip_furniture(pages: list[list[Line]], heights: list[float]) -> list[list[Line]]:
    """去掉页眉页脚：页面上下 8% 区域里、在很多页重复出现（数字归一后）的行，以及孤立页码。"""
    cnt: Counter = Counter()
    for lines, h in zip(pages, heights):
        keys = {_margin_key(l.text) for l in lines if l.y1 < h * 0.08 or l.y0 > h * 0.92}
        cnt.update(keys)
    n = len(pages)
    rep = {k for k, c in cnt.items() if n >= 6 and c >= max(3, n * 0.25)}
    out = []
    for lines, h in zip(pages, heights):
        keep = []
        for l in lines:
            edge = l.y1 < h * 0.08 or l.y0 > h * 0.92
            if edge and (_margin_key(l.text) in rep or re.fullmatch(r"[\divxlcdm\-–— ]{1,8}", l.text.lower())):
                continue
            keep.append(l)
        out.append(keep)
    return out


def _is_heading(l: Line, body: float, left: float, right: float) -> bool:
    t = unspace(l.text)
    if len(t) > 90 or t.endswith((",", ";")):
        return False
    big = l.size >= body * 1.18
    centered = (l.x0 - left) > (right - left) * 0.12 and abs((l.x0 - left) - (right - l.x1)) < (right - left) * 0.12
    if big and len(t) < 90:
        return True
    if HEAD_WORD.match(t) and len(t) < 60 and (centered or l.bold or big):
        return True
    if ROMAN.match(t.strip(".")) and centered:
        return True
    return bool(l.bold and centered and len(t) < 60 and not t.endswith("."))


def _pct(xs: list[float], q: float) -> float:
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(len(xs) * q))]


def _hyphen_end(t: str) -> bool:
    return len(t) > 1 and t.endswith(HYPHENS) and not t[-2].isspace()


@dataclass
class Layout:
    """从这本书自己统计出来的版式。"""
    body: float = 10.0
    left: dict = field(default_factory=lambda: {0: 50.0, 1: 50.0})     # 奇偶页左边界
    right: dict = field(default_factory=lambda: {0: 500.0, 1: 500.0})  # 奇偶页右边界（「这一行排满了吗」）
    cw: float = 5.0                  # 正文平均字宽
    ind: float = 5.0                 # 算作缩进的最小距离
    indent: float = 0.0              # 首行缩进量（0：这本书不用首行缩进）
    gap: float | None = None         # 段间距阈值（None：段与段之间没有额外间距，不用间距判断）
    big: float = 30.0                # 大空白（场景分隔）：任何书都算新段
    soft: bool = False               # 换行断字用 ‐ / 软连字符标记（行尾 ASCII '-' 就是真连字符）
    cont: dict = field(default_factory=dict)   # (首行相对位置, 引号开头) → 续行相对位置的计数

    def rel(self, l: Line) -> float:
        return l.x0 - self.left[l.page % 2]

    def predict(self, first: Line) -> float:
        """一段只看到首行时，续行应该在哪（相对左边界）。"""
        r, q = round(self.rel(first)), first.text[:1] in QUOTES
        c = self.cont.get((r, q), Counter())
        if sum(c.values()) < 5:            # 本档样本太少：把相邻 1pt 的也算上（字形边距的抖动）
            c = sum((cnt for (k, kq), cnt in self.cont.items() if kq == q and abs(k - r) <= 1), Counter())
        if sum(c.values()) >= 5:
            return float(c.most_common(1)[0][0])
        if self.indent and abs(r - self.indent) <= self.ind:
            return 0.0                      # 首行缩进：续行回到左边界
        return r                            # 其他情况当整段缩进（引文、诗）

    def has_room(self, p: Line, l: Line) -> bool:
        """上一行 p 后面还放得下下一行 l 的第一个词吗？放得下却换了行 = 段落在这里结束。"""
        room = self.right[p.page % 2] - p.x1
        if ZH_START.match(l.text):                   # 中文没有词间空格：下一行开头那一截不能拆开的字放得下就算
            return room > (_unit_em(l.text) + 1) * l.size
        first = l.text.split(" ", 1)[0]
        return room > (len(first) + 1.5) * self.cw   # 宽估一个字：拿不准时算放不下（接着）


def _unit_em(t: str) -> float:
    """中文行开头不能拆开的一截有几个全角字宽：引号前后、收尾标点之前、开头标点之后都不能折行（MuPDF 按 UAX #14），
    「需“寻”，“寻”不」会整个挤到下一行，上一行差好几个字也不是段尾。"""
    i = 1
    while i < len(t):
        a, b = t[i - 1], t[i]
        if a.isspace() or (a not in QUOTE_MARKS and b not in QUOTE_MARKS and b not in CLOSE_PUNCT
                           and a not in OPEN_PUNCT and (CJK.match(a) or CJK.match(b))):
            break
        i += 1
    return sum(1.0 if ord(c) >= 0x2E80 or c in QUOTE_MARKS else 0.5 for c in t[:i])


def _layout(stream: list[Line]) -> Layout:
    lay = Layout()
    sizes: Counter = Counter()
    for l in stream:
        sizes[round(l.size, 1)] += len(l.text)
    if not sizes:
        return lay
    body = lay.body = sizes.most_common(1)[0][0]
    B = [l for l in stream if abs(l.size - body) < 0.6]
    for par in (0, 1):
        ls = [l for l in B if l.page % 2 == par]
        if len(ls) < 30:
            ls = B
        if not ls:
            continue
        # 左边界：最靠左、且行数够多（≥ 最多那档的 10%）的位置，免得对话多的书把缩进位置当成左边界
        bk = Counter(round(l.x0) for l in ls)
        top = bk.most_common(1)[0][1]
        x = min(k for k, c in bk.items() if c >= top * 0.1)
        lay.left[par] = statistics.median(l.x0 for l in ls if round(l.x0) == x)
        lay.right[par] = _pct([l.x1 for l in ls], 0.99)
    for l in stream:
        par = l.page % 2
        l.head = _is_heading(l, body, lay.left[par], lay.right[par])
    B = [l for l in B if not l.head]
    bids = {id(l) for l in B}
    full = [(l.x1 - l.x0) / len(l.text) for l in B if len(l.text) >= 30]
    lay.cw = statistics.median(full) if full else body * 0.45
    lay.ind = max(2.0, body * 0.5)

    # 行距：同一页相邻两行正文之间的空白。明显分成两档（行距 / 段间距）才用间距分段
    pairs = [(a, b) for a, b in zip(stream, stream[1:]) if a.page == b.page and id(a) in bids and id(b) in bids]
    gaps = [b.y0 - a.y1 for a, b in pairs if -2 < b.y0 - a.y1 < body * 6]
    if gaps:
        normal = statistics.median(gaps)
        lo = [g for g in gaps if g <= normal + body * 0.25]
        hi = [g for g in gaps if g > normal + body * 0.25]
        hn = _pct(lo, 0.98)
        if len(hi) >= max(5, len(gaps) * 0.03):
            lh = _pct(hi, 0.1)
            if lh - hn >= body * 0.25:
                lay.gap = (lh + hn) / 2
        lay.big = hn + body * 1.2
    if lay.gap is not None:
        # 段落最后一行（句末收尾、后面还有空）后面大多没有额外间距：那几处大间距是标题、场景分隔，不是段间距
        # （否则同一页里只按间距分段，整页并成一段）
        ends = [b.y0 - a.y1 >= lay.gap for a, b in pairs
                if (ends_sentence(a.text) or ZH_END.search(a.text)) and lay.has_room(a, b)]
        if len(ends) >= 20 and sum(ends) < 0.5 * len(ends):
            lay.gap = None

    # 断字标记：行尾 ‐/软连字符 远多于行尾 ASCII '-' → 这本书用前者断字
    soft = sum(1 for a, b in zip(stream, stream[1:]) if a.text.endswith(SOFT_HYPHENS) and b.text[:1].isalpha())
    hard = sum(1 for a, b in zip(stream, stream[1:]) if a.text.endswith("-") and _hyphen_end(a.text) and b.text[:1].isalpha())
    lay.soft = soft >= 5 and soft >= hard * 3

    # 续行位置：同一页上明确是段首的行（前面是段间距/标题，或比上一行缩进）→ 它下一行在哪
    firsts: dict = {}
    for a, f, n in zip(stream, stream[1:], stream[2:]):
        if not (a.page == f.page == n.page) or id(f) not in bids or id(n) not in bids:
            continue
        g1, g2 = f.y0 - a.y1, n.y0 - f.y1
        if lay.gap is not None:
            start = a.head or g1 >= lay.gap
            ok = g2 < lay.gap
        else:
            start = a.head or lay.rel(f) - lay.rel(a) > lay.ind
            ok = g2 < lay.big
        if start and ok:
            key = (round(lay.rel(f)), f.text[:1] in QUOTES)
            lay.cont.setdefault(key, Counter())[round(lay.rel(n))] += 1
            firsts.setdefault(key, []).append(lay.rel(f))
    # 首行缩进量（精确值）：续行回到左边界的那类段首里最常见的位置
    style = [(sum(c.values()), k) for k, c in lay.cont.items()
             if k[0] > lay.ind and abs(c.most_common(1)[0][0]) <= lay.ind and sum(c.values()) >= 5]
    if style:
        lay.indent = round(statistics.median(firsts[max(style)[1]]), 1)
    else:
        offs = [round(lay.rel(l)) for l in B if lay.ind < lay.rel(l) < body * 6]
        if offs and len(offs) >= max(5, len(B) * 0.02):
            lay.indent = float(Counter(offs).most_common(1)[0][0])
    return lay


def _new_para(lay: Layout, cur: dict, p: Line, l: Line, nxt: Line | None) -> bool:
    """行 l 是否开始新段（p 是上一行，cur 是 p 所在的段，nxt 是 l 的下一行）。"""
    if abs(l.size - p.size) > max(1.0, lay.body * 0.12):
        return True                                  # 字号变了：脚注、引文、图注
    same = l.page == p.page
    if same:
        g = l.y0 - p.y1
        if g >= lay.big or (lay.gap is not None and g >= lay.gap):
            return True
    if _hyphen_end(p.text) and l.text[:1].isalnum():
        return False                                 # 断字：一定接着
    bx = cur["bx"] if cur["bx"] is not None else lay.predict(cur["first"])
    if abs(lay.rel(l) - bx) > lay.ind:
        return True                                  # 缩进/反缩进：和本段续行的位置对不上（跨页同样适用）
    if same and lay.gap is not None:
        return False                                 # 有段间距的书：同一页里间距正常就是同一段
    if nxt is not None and nxt.page == l.page and not nxt.head and abs(nxt.size - l.size) < 1 \
            and nxt.y0 - l.y1 < (lay.gap if lay.gap is not None else lay.big) \
            and abs(lay.rel(nxt) - bx) > lay.ind and abs(lay.rel(nxt) - lay.predict(l)) <= lay.ind:
        return True                                  # 看下一行：它按 l 是段首来排才对得上（翻页后的首行缩进）
    if not (ends_sentence(p.text) or ZH_END.search(p.text) or ZH_TAIL.search(p.text)):
        return False                                 # 上一行没说完（含 Mr. / T. 这类缩写）；中文行任何字后都能折行，没排满就是段尾
    if lay.indent and abs(lay.rel(l) - lay.indent) < 0.3 and abs(lay.rel(l) - bx) > 0.3:
        return True                                  # 正好落在首行缩进的位置，而不是本段续行的位置
    tail, head = p.text.rstrip()[-1:], l.text[:1]
    if (tail, head) in (("”", "“"), ("’", "‘"), ('"', '"')):
        return True                                  # 引号收尾 + 引号开头：换人说话
    return lay.has_room(p, l)                        # 后面还放得下却换了行：段落结束


def _segment(stream: list[Line], lay: Layout, words: set | None) -> list[dict]:
    paras: list[dict] = []
    cur = None
    prev = None
    for i, l in enumerate(stream):
        if l.head:
            t = unspace(l.text)
            if paras and paras[-1]["kind"] == "h" and prev is not None and prev.head and prev.page == l.page \
                    and l.y0 - prev.y1 < l.size * 1.2:
                paras[-1]["text"] += " " + t       # 两行标题合并：Chapter I / The Beginning
            else:
                paras.append({"kind": "h", "text": t, "page": l.page, "size": l.size})
            cur, prev = None, l
            continue
        if cur is None or _new_para(lay, cur, prev, l, stream[i + 1] if i + 1 < len(stream) else None):
            cur = {"kind": "p", "text": l.text, "page": l.page, "first": l, "bx": None}
            paras.append(cur)
        else:
            cur["text"] = join_lines(cur["text"], l.text, lay.soft, words)
            if cur["bx"] is None:
                cur["bx"] = lay.rel(l)
        prev = l
    out = []
    for p in paras:
        t = re.sub(r"\s+", " ", p["text"]).strip()
        if t:
            d = {"kind": p["kind"], "text": t, "page": p["page"]}
            if p["kind"] == "h":
                d["size"] = p["size"]
            out.append(d)
    return out


def vector_art(page, share: float = 0.05) -> bool:
    """页面上有成片的矢量图：非白色填充合计占页面 5% 以上（Standard Ebooks 的封面插画是矢量画的，get_images 看不到；
    白底矩形、分隔线、小 logo 不算）。"""
    area = page.rect.width * page.rect.height
    if not area:
        return False
    s = 0.0
    for d in page.get_drawings():
        c = d.get("fill")
        if not c or (min(c) > 0.9 if len(c) < 4 else max(c) < 0.1):    # 灰度 / RGB 近白；CMYK 近白
            continue
        s += min(d["rect"].width * d["rect"].height, area)
    return s / area >= share


def _norm(t: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", unspace(t).lower())


def toc_paras(toc: list, paras: list[dict]) -> list:
    """给目录每一项找到段落序号：页码之后的第一段；同一页附近有同名标题段时用标题段。"""
    pages = [p["page"] for p in paras]
    out = []
    for lv, t, pg, *_ in toc:
        i = bisect.bisect_left(pages, pg)
        n = min(i, max(0, len(paras) - 1))
        want = _norm(t)
        for j in range(i, min(len(paras), i + 40)):
            if paras[j]["page"] > pg + 1:
                break
            h = _norm(paras[j]["text"])
            if paras[j]["kind"] == "h" and want and h and (want in h or h in want):
                n = j
                break
        out.append((lv, t, pg, n))
    return out


def quality(paras: list[dict]) -> dict:
    """解析质量指标：用来在「管理」里找出最可疑的书。"""
    ps = [p["text"] for p in paras if p["kind"] == "p" and re.search(r"[A-Za-z]", p["text"])]
    n = len(ps)
    short = sum(1 for t in ps if len(t) < 25)
    frag = sum(1 for t in ps if not (ends_sentence(t) or t.endswith(("—", "–"))))
    hyph = sum(len(re.findall(r"\w[-‐­] [A-Za-z]", t)) for t in ps)
    sents = [b - a for t in ps for a, b in sentences(t)]
    long = sum(1 for k in sents if k > 400)
    q = {"paras": n, "heads": sum(1 for p in paras if p["kind"] == "h"), "sents": len(sents),
         "short": round(short / n, 4) if n else 0, "frag": round(frag / n, 4) if n else 0,
         "hyph": hyph, "long": long, "chars": sum(len(t) for t in ps)}
    q["score"] = suspicion(q)
    return q


def suspicion(q: dict) -> float:
    """一个数概括「这本书解析得可疑」：段尾不像句末的比例、极短段比例、残留断字和超长句的千分比。"""
    if not q or not q.get("paras"):
        return 0.0
    return round(100 * q["frag"] + 50 * q["short"] + 1000 * q["hyph"] / q["paras"]
                 + 1000 * q["long"] / max(1, q["sents"]), 1)


def parse(path: Path, words: set | None = None) -> Parsed:
    doc = pymupdf.open(path)
    meta = doc.metadata or {}
    raw = [_lines(p) for p in doc]
    heights = [p.rect.height for p in doc]
    pages = _strip_furniture(raw, heights)
    stream = []
    for pno, ls in enumerate(pages):
        for l in ls:
            l.page = pno
            stream.append(l)
    chars = sum(len(l.text) for l in stream)
    has_text = chars > 200 * max(1, min(doc.page_count, 5)) / 5
    lay = _layout(stream)
    paras = _segment(stream, lay, words)

    toc = [(lv, t.strip(), max(0, pg - 1)) for lv, t, pg, *_ in doc.get_toc(simple=False)] if doc.get_toc() else []
    if not toc:
        # 每页只取第一个标题，且标题够大或以 Chapter 之类开头
        seen = set()
        for p in paras:
            if p["kind"] == "h" and len(p["text"]) > 1 and p["page"] not in seen \
                    and (HEAD_WORD.match(p["text"]) or p["size"] >= lay.body * 1.35):
                seen.add(p["page"])
                toc.append((1, p["text"], p["page"]))
        toc = toc[:150]
    for p in paras:
        p.pop("size", None)
    toc = toc_paras(toc, paras)

    title = (meta.get("title") or "").strip()
    if not title or re.fullmatch(r"(untitled|microsoft word.*|.*\.(docx?|pdf|indd))", title, re.I):
        title = clean_title(path.name)
    author = (meta.get("author") or "").strip()
    if not author and paras:
        # 首页小字里出现 "by X" 或第二行是作者名
        first = [p["text"] for p in paras if p["page"] == 0][:6]
        for t in first:
            m = re.match(r"^by\s+(.{3,60})$", t, re.I)
            if m:
                author = m.group(1)
                break
        if not author and len(first) >= 2 and first[0].lower() == title.lower() and len(first[1]) < 50 \
                and re.fullmatch(r"[A-Z][\w.\- ]+", first[1]):
            author = first[1]

    sample = " ".join(p["text"] for p in paras if p["page"] < 20)[:20000]
    letters = [c for c in sample if c.isalpha()]
    ascii_ratio = sum(c.isascii() for c in letters) / max(1, len(letters))
    lang = "en" if ascii_ratio > 0.9 else "other"
    p0 = doc[0] if doc.page_count else None
    typo = bool(Path(path).suffix.lower() == ".pdf" and p0 is not None and not p0.get_images() and len(p0.get_text()) < 400
                and not vector_art(p0))   # epub/mobi 用自带封面图
    n = doc.page_count
    doc.close()
    return Parsed(title=title, author=author, pages=n, toc=toc, paras=paras, has_text=has_text, lang=lang,
                  typo_cover=typo, quality=quality(paras))


def render_cover(path: Path, dest: Path, width: int = 360) -> None:
    doc = pymupdf.open(path)
    page = doc[0]
    z = width / page.rect.width
    pix = page.get_pixmap(matrix=pymupdf.Matrix(z, z), alpha=False)
    dest.parent.mkdir(parents=True, exist_ok=True)
    pix.save(str(dest), jpg_quality=82) if dest.suffix == ".jpg" else pix.save(str(dest))
    doc.close()
