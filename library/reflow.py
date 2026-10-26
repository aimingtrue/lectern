"""碎片化正文的补救：有的中文书每个字、每个标点是单独的文本块，按版面分段会切成一字一段。

这时改用每页按阅读顺序取出的纯文字重新分段：
  · 三种取字顺序（版面排序 / PDF 原始顺序按纵向跳变断行 / 按字坐标重建行）里挑最像正常文字的
  · 行与行拼接（中文之间不加空格，西文之间加一个空格）
  · 行首缩进（两个以上空格或全角空格）处开新段
  · 全书几乎没有缩进时，在「。！？」等句末标点结尾、且明显短于正常行宽的行之后开新段
"""
from __future__ import annotations

import re
import statistics
from pathlib import Path

CJK = re.compile(r"[㐀-鿿豈-﫿　-〿＀-￯]")
END = re.compile(r"[。！？!?.…」』”\"）)]\s*$")
INDENT = re.compile(r"^(?:　|\s{2,})")


def fragmented(paras: list[dict]) -> bool:
    """一半以上的段落只有 1–3 个字：分段坏了。"""
    if len(paras) < 50:
        return False
    tiny = sum(1 for p in paras if len(p["text"].strip()) <= 3)
    return tiny / len(paras) > 0.5


def _join(a: str, b: str) -> str:
    if not a:
        return b
    if CJK.search(a[-1:]) or CJK.search(b[:1]):
        return a + b
    if a.endswith("-") and b[:1].islower():                       # 西文断字
        return a[:-1] + b
    return a + " " + b


PAGE_NO = re.compile(r"^[\s\d０-９ivxlcdmIVXLCDM第页頁\-—–·.()（）]+$")
CJK_SPACE = re.compile(r"(?<=[\u3400-\u9fff\u3000-\u303f\uff00-\uffef])\s+|\s+(?=[\u3400-\u9fff\u3000-\u303f\uff00-\uffef])")


def _key(line: str) -> str:
    return re.sub(r"[\d０-９]+", "#", re.sub(r"[\s\u3000]+", "", line))


def _naturalness(pages: list[list[str]]) -> float:
    """固定搭配反复出现的程度：字序乱了的文字（相邻两字随机组合）这个值低。"""
    import collections
    text = "".join("".join(ls) for ls in pages[:60])
    runs = re.findall(r"[\u3400-\u9fff]{2,}|[A-Za-z]{2,}", text)
    bg = [r[i:i + 2] for r in runs for i in range(len(r) - 1)]
    cnt = collections.Counter(bg)
    return sum(v for v in cnt.values() if v >= 3) / max(1, len(bg))


def _split_sentences(text: str, limit: int = 300) -> list[str]:
    """没有行结构时：在句末标点处把整页文字切成不超过约 limit 字的段。"""
    out, cur = [], ""
    for sent in re.findall(r".+?(?:[。！？!?]+[”」』\"）)]?|$)", text):
        if cur and len(cur) + len(sent) > limit:
            out.append(cur)
            cur = ""
        cur += sent
    if cur.strip():
        out.append(cur)
    return [x for x in out if x.strip()]


def _words(page, sort: bool) -> list[tuple]:
    """[(纵向中心, 左, 右, 高, 文字)]，按 PDF 原始顺序。用 words 而不是 rawdict：有的 PDF 用 rawdict 取不到正文的字。"""
    out = []
    for x0, y0, x1, y1, w, *_ in page.get_text("words", sort=sort):
        if w.strip():
            out.append(((y0 + y1) / 2, x0, x1, y1 - y0, w))
    return out


def _lines_by_chars(page) -> list[str]:
    """按字的坐标重建行：纵向相差不到半个字高的算同一行，行内按横向排；行首空出一个半字宽以上算缩进。
    专治「字的上下位置轻微抖动」的 PDF（按版面排序会把一行拆成两行交错，原始顺序又是一字一行）。"""
    chars = _words(page, sort=False)
    if not chars:
        return []
    h = statistics.median(c[3] for c in chars) or 1
    left = min(c[1] for c in chars)
    chars.sort()
    rows, cur = [], [chars[0]]
    for c in chars[1:]:
        if c[0] - cur[-1][0] > 0.5 * h:
            rows.append(cur)
            cur = [c]
        else:
            cur.append(c)
    rows.append(cur)
    out = []
    for r in rows:
        r.sort(key=lambda c: c[1])
        text, last = "", None
        for _, x0, x1, _, ch in r:
            if last is not None and x0 - last > 0.6 * h:
                text += " "
            text += ch
            last = x1
        out.append(("  " if r[0][1] - left > 1.5 * h else "") + text)
    return out


def _lines_in_stream_order(page) -> list[str]:
    """按 PDF 原始顺序取字（字序对），横向退回行首（或纵向跳过一行以上）时断行。
    有的书相邻两字交替落在两条略有高低的基线上：按纵向位置分行会把一行拆成两行，横向位置却始终向右。"""
    chars = _words(page, sort=False)
    if not chars:
        return []
    h = statistics.median(c[3] for c in chars) or 1
    left = min(c[1] for c in chars)
    out, text, cy, start, last = [], "", None, None, None
    for y, x0, x1, _, ch in chars:
        if cy is not None and (x0 < last - 2 * h or abs(y - cy) > 1.5 * h):   # 退回行首 / 跳过一行以上才断行
            out.append(("  " if start - left > 1.5 * h else "") + text)
            text, cy = "", None
        if cy is None:
            cy, start, last = y, x0, x0
        elif x0 - last > 0.6 * h:
            text += " "
        text += ch
        last = x1
    out.append(("  " if start - left > 1.5 * h else "") + text)
    return out


def reflow(path: Path) -> list[dict]:
    import pymupdf
    # 两种取字顺序：按版面位置排序 / PDF 内部原始顺序。有的书按版面排序会把相邻两行的字交错拼在一起，
    # 有的书原始顺序是乱的；取更像正常文字的那一种
    by_layout: list[list[str]] = []
    by_stream: list[list[str]] = []
    by_chars: list[list[str]] = []
    by_plain: list[list[str]] = []
    with pymupdf.open(path) as d:
        for pg in d:
            by_layout.append(pg.get_text("text", sort=True).splitlines())
            by_stream.append(_lines_in_stream_order(pg))
            by_chars.append(_lines_by_chars(pg))
            plain = ""
            for l in pg.get_text("text", sort=False).splitlines():
                plain = _join(plain, l.strip()) if l.strip() else plain
            by_plain.append(["  " + p for p in _split_sentences(plain)])
    # 几种取法里挑更像正常文字的；差不多时优先按版面排序（行结构最完整）。
    # 「整页纯文字」没有行结构，只在明显更好时才用（有的 PDF 只有这种取法拿得到完整、顺序正确的字）
    cands = [(by_layout, 1.0), (by_chars, 1.03), (by_stream, 1.05), (by_plain, 1.15)]
    pages = max(cands, key=lambda c: _naturalness(c[0]) / c[1])[0]
    # 页眉页脚：在很多页的头尾重复出现的行（数字归一后相同）；纯页码行
    seen: dict = {}
    for ls in pages:
        edge = [l for l in ls if l.strip()]
        for l in set(edge[:2] + edge[-2:]):
            seen[_key(l)] = seen.get(_key(l), 0) + 1
    # 以句末标点结尾的是正文（页眉页脚几乎不会以句号结尾），即使碰巧在多页的页尾重复也不删
    furniture = {k for k, n in seen.items() if n >= max(3, 0.3 * len(pages)) and not END.search(k)}
    pages = [[l for l in ls if l.strip() and _key(l) not in furniture and not PAGE_NO.match(l)] for ls in pages]
    lines = [l for ls in pages for l in ls if l.strip()]
    if not lines:
        return []
    width = statistics.median(len(l.strip()) for l in lines)
    indents = sum(1 for l in lines if INDENT.match(l)) / len(lines)
    paras, cur, cur_page = [], "", 0
    for pno, ls in enumerate(pages):
        for raw in ls:
            text = raw.strip()
            if not text:
                continue
            new = bool(cur) and (INDENT.match(raw) if indents > 0.03 else
                                 (END.search(prev) and len(prev) < width * 0.85))
            if new:
                paras.append({"kind": "p", "text": cur, "page": cur_page})
                cur = ""
            if not cur:
                cur_page = pno
            cur = _join(cur, text)
            prev = text
    if cur:
        paras.append({"kind": "p", "text": cur, "page": cur_page})
    for p in paras:
        p["text"] = CJK_SPACE.sub("", p["text"])                 # 中文前后不留空格
    return paras
