"""Markdown：资料页的渲染（library/research.py），以及送进阅读器时的解析（和 txt 一样不经 MuPDF）。

渲染：表格、代码块、标题锚点（中文标题也有锚点）；```mermaid 代码块交给页面上的 mermaid 画图；
相对链接和图片地址由调用方改写成书库里的地址。

解析成阅读器的段落流 [{"kind": "h"|"p", "text", "page"}]：
  · 标题 → h（只有一个一级标题、又在最前面时它是书名，不进目录，其余标题升一级）
  · 段落、列表项、引文 → p；嵌套列表的每一项各自一段
  · 表格每一行一段：「首列 — 表头 值，表头 值」（两列的表是「名字 — 说明」），读出来是一句话；超过 TABLE_ROWS 行的只读前面的，后面一句话带过
  · 图片读它的说明文字（「图：…」）；代码块、mermaid 图不读
"""
from __future__ import annotations

import re
from html.parser import HTMLParser
from pathlib import Path
from typing import Callable

import markdown
from markdown.extensions.toc import TocExtension, slugify_unicode
from markdown.treeprocessors import Treeprocessor

from .extract import CJK, PARSER_VERS, Parsed, quality
from .txt import PAGE, decode, width

TABLE_ROWS = 20


class _Links(Treeprocessor):
    def __init__(self, md, fix):
        super().__init__(md)
        self.fix = fix

    def run(self, root):
        for el in root.iter():
            for attr in ("href", "src"):
                v = el.get(attr)
                if v is not None:
                    el.set(attr, self.fix(v, attr))


MERMAID = re.compile(r'<pre><code class="language-mermaid">(.*?)</code></pre>', re.S)


def render(text: str, fix: Callable[[str, str], str] | None = None) -> tuple[str, list]:
    """Markdown → (HTML, 目录 [{level, id, name, children}])。fix(地址, "href"|"src") 改写链接和图片地址。"""
    md = markdown.Markdown(extensions=["tables", "fenced_code", "sane_lists",
                                       TocExtension(slugify=slugify_unicode, permalink=False)])
    if fix:
        md.treeprocessors.register(_Links(md, fix), "bb_links", 4)     # 在行内处理（20）之后：链接和图片已经是元素
    out = md.convert(text)
    out = MERMAID.sub(lambda m: f'<pre class="mermaid">{m.group(1)}</pre>', out)
    return out, md.toc_tokens


def title_of(text: str) -> str:
    """第一个一级标题；没有就空。"""
    m = re.search(r"^#\s+(.+?)\s*#*\s*$", text, re.M)
    return re.sub(r"[*_`]", "", m.group(1)).strip() if m else ""


# ---------------------------------------------------------------- 阅读器：HTML → 段落
BLOCKS = {"p", "li", "h1", "h2", "h3", "h4", "h5", "h6", "dt", "dd"}


class _Blocks(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out: list[tuple[str, int, str]] = []        # (kind, 标题级别, 文字)
        self.cur: tuple[str, int] | None = None
        self.buf: list[str] = []
        self.skip = 0                                     # 在 <pre> 里：代码、mermaid 不读
        self.table: list[tuple[bool, list[str]]] | None = None
        self.row: list[tuple[bool, str]] | None = None
        self.cell: list[str] | None = None

    def flush(self) -> None:
        t = re.sub(r"\s+", " ", "".join(self.buf)).strip()
        if t and self.cur:
            kind, lv = self.cur
            self.out.append((kind, lv, t))
        self.buf = []

    def handle_starttag(self, tag, attrs):
        if tag == "pre":
            self.flush()
            self.skip += 1
        elif self.skip:
            return
        elif tag == "table":
            self.flush()
            self.table = []
        elif tag == "tr" and self.table is not None:
            self.row = []
        elif tag in ("td", "th") and self.row is not None:
            self.cell = []
        elif tag == "img":
            alt = (dict(attrs).get("alt") or "").strip()
            if alt:
                cur = self.cur
                self.flush()
                self.out.append(("p", 0, ("图：" if CJK.search(alt) else "Figure: ") + alt))
                self.cur = cur
        elif tag == "br":
            (self.cell if self.cell is not None else self.buf).append(" ")
        elif tag in BLOCKS:
            self.flush()                                  # 列表项里套着段落、子列表：前面的字先成一段
            lv = int(tag[1]) if tag[0] == "h" and tag[1:].isdigit() else 0
            self.cur = ("h" if lv else "p", lv)

    def handle_endtag(self, tag):
        if tag == "pre":
            self.skip = max(0, self.skip - 1)
        elif self.skip:
            return
        elif tag in ("td", "th") and self.row is not None and self.cell is not None:
            self.row.append((tag == "th", re.sub(r"\s+", " ", "".join(self.cell)).strip()))
            self.cell = None
        elif tag == "tr" and self.table is not None and self.row is not None:
            self.table.append((all(h for h, _ in self.row), [c for _, c in self.row]))
            self.row = None
        elif tag == "table" and self.table is not None:
            self.out.extend(("p", 0, t) for t in table_lines(self.table))
            self.table = None
        elif tag in BLOCKS:
            self.flush()
            self.cur = ("p", 0) if tag in ("li", "dd") else None    # 列表项里子列表之后的字也算一段

    def handle_data(self, data):
        if self.skip:
            return
        if self.cell is not None:
            self.cell.append(data)
        elif self.table is None:
            self.buf.append(data)


def table_lines(rows: list[tuple[bool, list[str]]]) -> list[str]:
    """表格每行一句：「首列 — 表头 值，表头 值」。没有表头的按顺序连起来。"""
    head = next((cells for is_head, cells in rows if is_head), None)
    body = [cells for is_head, cells in rows if not is_head]
    zh = any(CJK.search(c) for _, cells in rows for c in cells)
    sep = "，" if zh else ", "
    out = []
    for cells in body[:TABLE_ROWS]:
        if not any(cells):
            continue
        if head and len(cells) == 2:                     # 两列：「名字 — 说明」，不念表头
            out.append(" — ".join(c for c in cells if c))
        elif head and len(cells) > 1:
            parts = [f"{h} {v}".strip() if h else v for h, v in zip(head[1:], cells[1:]) if v]
            out.append(f"{cells[0]} — {sep.join(parts)}" if cells[0] else sep.join(parts))
        else:
            out.append(sep.join(c for c in cells if c))
    if len(body) > TABLE_ROWS:
        n = len(body) - TABLE_ROWS
        out.append(f"（表格还有 {n} 行，见资料页。）" if zh else f"(The table has {n} more rows; see the document page.)")
    return [t for t in out if t]


def parse_md(path: Path, words: set | None = None) -> Parsed:
    text = decode(path.read_bytes())
    p = _Blocks()
    p.feed(render(text)[0])
    p.close()
    p.flush()
    blocks = p.out
    if not any(k == "p" for k, _, _ in blocks):
        raise RuntimeError("Markdown 里没有正文")
    title = path.stem
    heads = [lv for k, lv, _ in blocks if k == "h"]
    if heads and heads.count(1) == 1 and blocks[0][0] == "h" and blocks[0][1] == 1:
        title = blocks[0][2]                              # 唯一的一级标题在最前面：是书名，不当章节
        blocks = blocks[1:]
        heads = heads[1:]
    top = min(heads) if heads else 1
    paras, pos, toc = [], 0, []
    for kind, lv, t in blocks:
        if kind == "h":                                   # 标题段就是目录项的位置（toc_paras 按名字找，认不出中文标题）
            toc.append((lv - top + 1, t, pos // PAGE, len(paras)))
        paras.append({"kind": kind, "text": t, "page": pos // PAGE})
        pos += width(t)
    sample = " ".join(t for _, _, t in blocks[:400])[:20000]
    letters = [c for c in sample if c.isalpha()]
    lang = "en" if sum(c.isascii() for c in letters) / max(1, len(letters)) > 0.9 else "other"
    return Parsed(title=title, author="资料", pages=pos // PAGE + 1, toc=toc[:300], paras=paras,
                  has_text=True, lang=lang, quality=quality(paras), parser_ver=PARSER_VERS[".md"])
