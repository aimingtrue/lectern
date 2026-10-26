"""文字处理：断句（全站唯一的断句规则）+ 行尾断字拼接。

断句只在服务端做：存进数据库的是每段的句子偏移 [[起, 止], …]，前端、朗读、译文、预生成都用同一份，
所以「同一句」在各处一定是同一个字符串。
"""
from __future__ import annotations

import re

# 句末标点：. ! ? … ‽ 及其连写（?! …），以及空格隔开的省略号 ". . ."
_TERM = re.compile(r"(?:\.[  ]){2,}\.|[.!?…‽]+")
CLOSERS = "”’\"')]»"
OPENERS = "“‘\"'([«"
# 绝不在其后断句的缩写（称谓、拉丁缩写）
HARD_ABBR = {"mr", "mrs", "ms", "dr", "st", "mt", "messrs", "mme", "mlle", "prof", "rev", "gen", "col", "capt",
             "lt", "sgt", "hon", "vs", "cf", "al", "e.g", "i.e", "viz", "fig", "esp", "approx", "dept"}
# 后面跟数字时不断句：No. 5、pp. 12、Vol. 3
NUM_ABBR = {"no", "nos", "vol", "vols", "pp", "p", "ch", "chap", "art", "sec"}
# 可能就是句末的缩写：只有下一个词明显是句首常用词时才断（It was five p.m. The sun…）
SOFT_ABBR = {"etc", "a.m", "p.m", "inc", "ltd", "co", "bros", "jr", "sr", "corp"}
STARTERS = set("""the a an he she it i we they you his her its their our my your this that these those there then
but and so yet or if when while as at in on after before since once now later still however meanwhile""".split())
_TOKEN = re.compile(r"(?<![A-Za-z0-9])([A-Za-z]+(?:\.[A-Za-z]+)*)$")
_WORD = re.compile(r"[A-Za-z]+")


def _abbr_kind(before: str) -> str:
    """句号前那个词是什么：'hard' 绝不断 / 'num' 后跟数字不断 / 'soft' 视下一个词 / ''（普通词）。"""
    m = _TOKEN.search(before)
    if not m:
        return ""
    tok = m.group(1)
    low = tok.lower()
    if low in HARD_ABBR:
        return "hard"
    if len(tok) == 1 and tok.isupper():
        # 单个大写字母 = 名字缩写（T. J. Eckleburg、F. Scott）；代词 I 除外，除非前面也是缩写（I. M. Pei）
        if tok != "I" or re.search(r"(?<![A-Za-z])[A-Z]\.\s*$", before[: m.start()]):
            return "hard"
        return ""
    if low in NUM_ABBR:
        return "num"
    if low in SOFT_ABBR or (len(tok) >= 3 and re.fullmatch(r"(?:[A-Za-z]\.)+[A-Za-z]", tok)):
        return "soft"   # 也包括 U.S、U.K 这类点号缩写
    return ""


def _closers_end(text: str, j: int) -> int:
    """从 j 起吞掉收尾的引号/括号；嵌套引号允许中间隔一个空格（’ ”），但只在后面是结尾/空白/另一个收尾符时。"""
    n = len(text)
    while j < n:
        c = text[j]
        if c in CLOSERS:
            j += 1
            continue
        if c in "  " and j + 1 < n and text[j + 1] in CLOSERS:
            k = j + 2
            if k >= n or text[k].isspace() or text[k] in CLOSERS or text[j + 1] == "”":
                j = k
                continue
        break
    return j


def _boundary(text: str, i: int, j: int, end: int) -> bool:
    """text[i:j] 是句末标点、text[j:end] 是收尾引号。在 end 处断句吗？"""
    n = len(text)
    if end < n and not text[end].isspace():
        return False                         # 3.14、$5.00、example.com、U.S.A、wait…what
    k = end
    while k < n and (text[k].isspace() or text[k] in OPENERS):
        k += 1
    if k >= n:
        return True
    c = text[k]
    if c.islower() or c in ",;:—–-)]":
        return False                         # “…village?” he asked / “Yes.” — and then
    term = text[i:j]
    if term == ".":
        kind = _abbr_kind(text[:i])
        if kind == "hard":
            return False
        if kind == "num":
            return not c.isdigit()
        if kind == "soft":
            w = _WORD.match(text, k)
            return bool(w and w.group(0).lower() in STARTERS)
    return True


HAN = re.compile(r"[\u3400-\u9fff\uf900-\ufaff]")
_ZH_TERM = re.compile(r"[。！？!?…]+")
_ZH_CLOSERS = "”’\"'」』）》】〕)"
_ZH_LONG, _ZH_PIECE = 200, 150


def is_zh(text: str) -> bool:
    """以中文为主：汉字数 ×3 ≥ 拉丁字母数（一个汉字大约顶一个英文词）。和 tts/server.py 的判断一致。"""
    han = len(HAN.findall(text))
    return han > 0 and han * 3 >= len(_WORD_CHARS.findall(text))


_WORD_CHARS = re.compile(r"[A-Za-z]")


def _zh_sentences(text: str) -> list[list[int]]:
    """中文断句：。！？… 之后直接断（中文标点后没有空格），连同收尾的引号括号；后面紧跟逗号顿号时不断
    （“……”，他说）。超过 200 字的长句再按 ；： 然后 ， 切到 150 字以内，免得一句读一分钟。"""
    spans, start = [], 0
    for m in _ZH_TERM.finditer(text):
        end = m.end()
        while end < len(text) and text[end] in _ZH_CLOSERS:
            end += 1
        if end < len(text) and text[end] in "，、,；;：:":
            continue
        seg = text[start:end]
        if seg.count("“") + seg.count("「") > seg.count("”") + seg.count("」"):
            close = min((k for k in (text.find("”", end), text.find("」", end)) if k >= 0), default=-1)
            if 0 <= close - end <= 30:
                continue                      # 引号里的短句（“拜呵！拜呵！”）读到收尾引号再断
        if end > start:
            spans.append([start, end])
            start = end
    spans.append([start, len(text)])
    out = []
    for a, b in spans:
        out.extend(_zh_cut(text, a, b) if b - a > _ZH_LONG else [[a, b]])
    return out


def _zh_cut(text: str, a: int, b: int) -> list[list[int]]:
    """长句在标点后切开，每段尽量不超过 150 字；先试 ；：，不够再加上 ，、。"""
    for marks in ("；;：:", "；;：:，,、"):
        cuts = [i + 1 for i in range(a, b - 1) if text[i] in marks]
        out, s, last = [], a, None
        for c in cuts:
            if c - s > _ZH_PIECE and last:
                out.append([s, last])
                s = last
            last = c
        out.append([s, b])
        if all(y - x <= _ZH_LONG for x, y in out):
            return out
    out = [[x, min(x + _ZH_PIECE, b)] for x in range(a, b, _ZH_PIECE)]   # 连标点都没有：按字数硬切
    return out


def sentences(text: str) -> list[tuple[int, int]]:
    """一段文字 → 句子的 (起, 止) 偏移（按 Python 字符，即 Unicode 码点），每句已去掉首尾空白。

    没有句末标点的文字（标题等）整段是一句；只由引号/标点组成的碎片并入前一句。以中文为主的段落按中文规则断。
    """
    spans: list[list[int]] = [] if not is_zh(text) else _zh_sentences(text)
    if spans:
        return _tidy(text, spans)
    start = 0
    for m in _TERM.finditer(text):
        i, j = m.start(), m.end()
        if j <= start:
            continue
        end = _closers_end(text, j)
        if _boundary(text, i, j, end):
            spans.append([start, end])
            start = end
    spans.append([start, len(text)])
    return _tidy(text, spans)


def _tidy(text: str, spans: list[list[int]]) -> list[tuple[int, int]]:
    out: list[list[int]] = []
    for a, b in spans:
        while a < b and text[a].isspace():
            a += 1
        while b > a and text[b - 1].isspace():
            b -= 1
        if a >= b:
            continue
        if out and not any(ch.isalnum() for ch in text[a:b]):
            out[-1][1] = b                    # 孤立的引号、省略号：并入前一句
            continue
        out.append([a, b])
    return [(a, b) for a, b in out]


def split(text: str) -> list[str]:
    return [text[a:b] for a, b in sentences(text)]


def ends_sentence(line: str) -> bool:
    """一行（或一段）是否以真正的句末收尾：Mr. / T. 这类缩写、名字缩写不算；收尾引号、冒号算。"""
    t = line.rstrip()
    if not t:
        return False
    if t.endswith(("”", ":")):
        return True
    k = len(t)
    while k > 0 and (t[k - 1] in CLOSERS or t[k - 1] in "  "):
        k -= 1
    m = re.search(r"(?:[.!?…‽]+)$", t[:k])
    if not m:
        return False
    if m.group(0) == ".":
        return _abbr_kind(t[: m.start()]) != "hard"
    return True


def utf16_spans(text: str, spans) -> list[list[int]]:
    """码点偏移 → UTF-16 偏移（JS 字符串下标）。只有 BMP 以外的字符（emoji 等）才会不同。"""
    if all(ord(c) < 0x10000 for c in text):
        return [[a, b] for a, b in spans]
    pre = [0]
    for c in text:
        pre.append(pre[-1] + (2 if ord(c) >= 0x10000 else 1))
    return [[pre[a], pre[b]] for a, b in spans]


# ---------------------------------------------------------------- 行尾断字
SOFT_HYPHENS = ("‐", "­")   # ‐ 与软连字符：排版软件只在换行断字时插入
HYPHENS = ("-",) + SOFT_HYPHENS
DASHES = ("—", "–")


def join_lines(a: str, b: str, soft_marks: bool, words: set | None = None) -> str:
    """把下一行 b 接到 a 后面。

    soft_marks=True：这本书用 ‐/软连字符标记换行断字 → 行尾的 ASCII '-' 是真正的合成词连字符，保留且不加空格。
    否则 ASCII '-' 后跟小写字母时查词典：拼起来是一个词就去掉连字符（vulner-able），否则保留（well-known）。
    行尾是连字符时一律不加空格（Fifty-Ninth、Fitz-Peters）。
    """
    if not a:
        return b
    if not b:
        return a
    last = a[-1]
    if last in HYPHENS and len(a) > 1 and not a[-2].isspace():
        stem = a[:-1]
        if last == "­":
            return stem + b
        if last == "‐" and (soft_marks or b[:1].islower()):
            return stem + b
        if last == "-" and not soft_marks and b[:1].islower():
            head = re.findall(r"[A-Za-z]+$", stem)
            tail = re.findall(r"^[A-Za-z]+", b)
            if words is not None and head and tail and (head[0] + tail[0]).lower() in words:
                return stem + b
        return a + b
    if a.endswith(DASHES) or b.startswith(DASHES):
        return a + b
    return a + " " + b
