"""txt 解析：按原文件的行、空行和缩进分段（不经 MuPDF 排版：MuPDF 把 txt 排成页时空行丢了，整本只剩一两段）。

txt 的几种写法，都从这本书自己的统计判断：
  · 一行一段（段与段之间可有空行）：行很长、行尾几乎都是句末标点
  · 定宽折行（古腾堡式，每行六七十字母或三十来个汉字）：行尾多在句子中间。段落边界是
    空行、缩进比上一行深（首行缩进、引文、诗）、明显短于行宽的行（段落最后一行）
  · 隔行空行（每行后面都跟一个空行）的折行书：一个空行只是行距，两个以上才分段
  · 行内的一串空格（转换时换行被吞掉，「…mind.     If we were」）：段落在这里断开
编码按字节判断（UTF-8 / UTF-16 / cp1252 / GB18030 / Big5）。页码按字数折算（约 2000 个字母或 1000 个汉字一页），
和原先 MuPDF 排 txt 的页数相当。
"""
from __future__ import annotations

import re
import unicodedata
from collections import Counter
from pathlib import Path

from .extract import CJK, HEAD_WORD, PARSER_VERS, ROMAN, ZH_END, ZW, Parsed, quality, toc_paras
from .text import ends_sentence, join_lines, sentences

WIDE = re.compile(r"[\u1100-\u115f\u2e80-\ua4cf\uac00-\ud7a3\uf900-\ufaff\ufe30-\ufe4f\uff00-\uff60\uffe0-\uffe6]")
RADICALS = {c: unicodedata.normalize("NFKC", chr(c)) for c in range(0x2F00, 0x2FD6)}   # 康熙部首「⼀⼆」→「一二」（pdf 转来的 txt）
INDENT = re.compile(r"[ \t\u3000\xa0]*")
# 行内的段落分隔：句末标点（含收尾引号）后面一串空格或两个以上全角空格
INNER = re.compile(r"(?<=[.!?…。！？”’\"'」』)）:：;；])(?: {4,}|\t+|[ \t]*\u3000{2,}[ \t]*)(?=\S)")
CONT_END = re.compile(r"[A-Za-z0-9\u3400-\u9fff,，、;；\-]$")     # 行尾在句子中间（折行）
ZH_HEAD = re.compile(r"^(?:第[零〇一二三四五六七八九十百千万两\d０-９]+[章回节卷部篇集幕折话]|[卷部][零〇一二三四五六七八九十百千\d]+"
                     r"|(?:序|序言|序章|自序|前言|引言|引子|楔子|后记|尾声|跋|目录|终章|番外)(?:$|[\s\u3000:：]))")
OPENS = re.compile(r"[\"'“‘(\[「『（A-Z\u3400-\u9fff]")             # 新段的开头：大写字母、引号、汉字
DECOR = " \u3000◇◆□■☆★○●·•※〓=*#~～-—"           # 标题两边的装饰符：◇  第三章  ◇
PAGE_NO = re.compile(r"[-—–\s]*(?:(?:[Pp]age|[Pp]\.)\s*)?(?:\d{1,4}|[ivxlc]{1,7})[-—–\s]*|第\s*\d{1,4}\s*页")   # 罗马数字页码只认小写（大写的 I、II 是章节号）
SKIP = object()
PAGE = 2000                     # 一页约多少「宽度」（字母 1、汉字 2）
LONG = 8000                     # 超过这么长的段（整本没分段的 txt）在句末切成不超过 CHUNK 的几段
CHUNK = 3000


def decode(raw: bytes) -> str:
    """按字节的样子判断编码：
      · UTF-8（可带 BOM）
      · 有 BOM 或零字节很多：UTF-16
      · 高位字节（≥0x80）不到 5%：西文 txt 夹着几个 cp1252 字节（弯引号等），按 cp1252
      · 其余：GBK/GB18030 或 Big5 的中文（不转会被读成「ºï×Ó±¾ÔÚ」）
    UTF-16 不能拿来「试」：任何偶数长度的字节都能解成一堆汉字，英文书会变成「潣祳挠慨扭牥」。"""
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        pass
    head = raw[:4096]
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")) or head.count(b"\x00") > len(head) // 5:
        try:
            return raw.decode("utf-16" if raw.startswith((b"\xff\xfe", b"\xfe\xff")) else
                              "utf-16-le" if head[1::2].count(b"\x00") > head[0::2].count(b"\x00") else "utf-16-be")
        except UnicodeDecodeError:
            pass
    if sum(b >= 0x80 for b in raw) < 0.05 * len(raw):
        return raw.decode("cp1252", "replace")
    for enc in ("gb18030", "big5"):
        try:
            t = raw.decode(enc)
        except UnicodeDecodeError:
            continue
        if CJK.search(t[:5000]):
            return t
    return raw.decode("utf-8", "replace")


def _ends(line: str) -> bool:
    return ends_sentence(line) or bool(ZH_END.search(line))


def _flat(line: str) -> str:
    return re.sub(r"\s+", " ", line).strip()


def width(s: str) -> int:
    return len(s) + len(WIDE.findall(s))


def _indent(line: str) -> int:
    lead = INDENT.match(line).group()
    return lead.count(" ") + lead.count("\xa0") + 4 * lead.count("\t") + 2 * lead.count("\u3000")


def _pct(xs: list[int], q: float) -> int:
    return sorted(xs)[min(len(xs) - 1, int(len(xs) * q))] if xs else 0


def _join(a: str, b: str, words: set | None) -> str:
    if CJK.search(a[-1:]) or CJK.search(b[:1]):
        return a + b
    return join_lines(a, b, False, words)


def _chunks(text: str) -> list[str]:
    """整本（或整章）没分段：在句末切成不超过 CHUNK 字的几段。"""
    out, start, last = [], 0, 0
    for _, b in sentences(text):
        if b - start > CHUNK and last > start:
            out.append(text[start:last].strip())
            start = last
        last = b
    out.append(text[start:].strip())
    return [t for t in out if t]


def _drop_furniture(lines: list[str | None]) -> list[str | None]:
    """从 pdf 转来的 txt 每页留着页码和页眉（「STORIES」「12」）：页码行多到平均每两页一个、页眉（同一行反复出现，
    多半紧挨着页码）时去掉，连同它们前后的空行（段落跨页时前后两截接得上；页尾是句末时留一个空行分段）。
    章节号「1」「2」稀疏得多，不受影响。"""
    body = [(i, l.strip()) for i, l in enumerate(lines) if l is not None]
    chars = sum(len(t) for _, t in body)
    nums = {i for i, t in body if PAGE_NO.fullmatch(t)}
    if len(nums) < 5 or len(nums) < chars / 6000:
        return lines
    pos = {i: k for k, (i, _) in enumerate(body)}
    near: dict = {}
    for i, t in body:
        if i not in nums and len(t) <= 60:
            k = pos[i]
            hit = any(body[j][0] in nums for j in range(max(0, k - 2), min(len(body), k + 3)))
            n, h = near.get(t, (0, 0))
            near[t] = (n + 1, h + hit)
    heads = {t for t, (n, h) in near.items() if n >= 5 and h >= 0.6 * n}
    drop = nums | {i for i, t in body if t in heads}
    out: list[str | None] = []
    for i, l in enumerate(lines):
        if i in drop:
            while out and out[-1] is None:
                out.pop()
            if out and out[-1] is not SKIP and _ends(out[-1]):
                out.append(None)                # 页尾正好是句末：多半也是段尾，留一个空行分段
            out.append(SKIP)
        elif not (l is None and out and out[-1] is SKIP):
            out.append(l)
    return [l for l in out if l is not SKIP]


def split(text: str, words: set | None = None) -> list[str]:
    """txt 全文 → 段落文字（已去掉首尾空白、行内多余空白）。"""
    text = text.replace("\r\n", "\n").replace("\r", "\n").translate(ZW).translate(RADICALS)
    text = re.sub(r"[\x0b\x0c\x85\u2028\u2029]", "\n", text)
    lines: list[str | None] = []
    for raw in text.split("\n"):
        raw = raw.rstrip()
        if not raw.strip():
            lines.append(None)
            continue
        parts = INNER.split(raw)
        lines.append(parts[0])
        for p in parts[1:]:                     # 行内分段：后面几截当作缩进开头的新行
            lines += [None, None, "    " + p]
    lines = _drop_furniture(lines)
    body = [_flat(l) for l in lines if l is not None]
    if not body:
        return []
    w = _pct([width(t) for t in body], 0.95)
    cont = sum(1 for t in body if CONT_END.search(t)) / len(body)
    wrapped = w <= 250 and cont >= 0.15         # 定宽折行：行不长、不少行尾在句子中间
    # 隔行空行：折行书里绝大多数「块」只有一行 → 一个空行是行距，连续两个以上才分段
    blocks, run, single = 0, 0, 0
    for l in lines + [None]:
        if l is None:
            if run:
                blocks += 1
                single += run == 1
            run = 0
        else:
            run += 1
    spaced = wrapped and blocks >= 20 and single >= 0.7 * blocks
    # 既没有空行也没有缩进的折行书：只能看行尾——句末收尾、又没排满的行也是段落最后一行
    bare = wrapped and blocks < 0.02 * len(body) and sum(_indent(l) > 1 for l in lines if l) < 0.03 * len(body)
    paras: list[str] = []
    cur, prev, blank = None, None, 0
    for l in lines:
        if l is None:
            blank += 1
            continue
        t = _flat(l)
        new = (cur is None or not wrapped or blank >= (2 if spaced else 1)
               or _indent(l) > _indent(prev) + 1                       # 缩进比上一行深：首行缩进、引文、诗
               or width(_flat(prev)) < 0.75 * w                        # 上一行明显短：段落最后一行
               or (bare and width(_flat(prev)) < 0.9 * w and _ends(prev) and OPENS.match(t)))
        if new:
            if cur:
                paras.append(cur)
            cur = t
        else:
            cur = _join(cur, t, words)
        prev, blank = l, 0
    if cur:
        paras.append(cur)
    return [c for p in paras for c in (_chunks(p) if len(p) > LONG else [p])]


HAN = re.compile(r"[\u3400-\u9fff]")
FW_PUNCT = re.compile(r"[，。！？；：、]")
HW_PUNCT = re.compile(r"(?<=[\u3400-\u9fff])[,.?!;:](?=[\u3400-\u9fff\"“]|$)")      # 汉字后面的半角标点
AD_LINE = re.compile(r"w\s*w\s*w\s*[.．]|https?://|[.．]\s*(com|net|cn|org)\b|txt\s*(下载|小说|电子书)|小说(下载|天堂|网)|"
                     r"电子书下载|手机阅读|更多精彩|免费下载|最新章节|请记住本|收藏本站|笔趣阁|[qQ]{2}群|求(月票|推荐票|收藏)", re.I)


def layout_flaw(paras: list[str]) -> str | None:
    """txt 排版差的原因（library.exclude「质量」规则用），没有明显毛病返回 None：中文整本几乎没有标点（殆知阁一类古籍）、
    三成以上的字在没分段被硬切的大块里（换行丢了）、五行以上网站水印、中文用半角标点。门槛按书库里没封面的 txt 抽样定。"""
    if not paras:
        return None
    total = sum(len(p) for p in paras)
    smp = "".join(paras[::max(1, len(paras) // 400)])
    han = len(HAN.findall(smp))
    zh = han > 0.3 * max(1, sum(c.isalpha() for c in smp))
    fw, hw = len(FW_PUNCT.findall(smp)), len(HW_PUNCT.findall(smp))
    if zh and fw + hw < 0.02 * han:
        return "没有标点"
    if sum(len(p) for p in paras if len(p) >= 0.9 * CHUNK) >= 0.3 * total:
        return "没分段"
    if sum(1 for p in paras if len(p) < 200 and AD_LINE.search(p)) >= 5:
        return "网站水印"
    if zh and hw >= 30 and hw >= fw:
        return "半角标点"
    return None


def is_heading(t: str) -> bool:
    """单独一段的章节标题：Chapter 1 / CHAPTER TWO. TWO MAKE A QUARREL / Preface / XII / 第三章 / 序言。"""
    if CJK.search(t):
        t = t.strip(DECOR)
        return len(t) <= 40 and bool(ZH_HEAD.match(t)) and not re.search(r"[，。！？；]$", t)
    if len(t) > 80 or t.endswith((",", ";", ":", "!", "?")):
        return False
    n = len(t.split())
    if HEAD_WORD.match(t) and n <= 10 and (not t.endswith(".") or n <= 4 or t.isupper()):
        return True
    if ROMAN.match(t.strip(". ")) or re.fullmatch(r"\d{1,3}\.?", t):
        return True
    return (t.isupper() and n <= 8 and sum(c.isalpha() for c in t) >= 3 and t[0] not in "\"'“‘("
            and not ends_sentence(t.rstrip(".")))


def parse_txt(path: Path, words: set | None = None) -> Parsed:
    texts = split(decode(path.read_bytes()), words)
    if not texts:
        raise RuntimeError("txt 里没有文字")
    kinds = ["h" if is_heading(t) else "p" for t in texts]
    # 剧本的角色名（GOUGH.、MAYOR.）：同一个「标题」出现三次以上，不是章节
    seen = Counter(t for t, k in zip(texts, kinds) if k == "h")
    kinds = ["p" if k == "h" and seen[t] >= 3 else k for t, k in zip(texts, kinds)]
    i = 0
    while i < len(kinds):                       # 连着三个以上的「标题」是书前的目录：当正文段，不进目录
        j = i
        while j < len(kinds) and kinds[j] == "h":
            j += 1
        if j - i >= 3:                          # 目录后面紧跟着的第一章（和目录里某一项同名）照样是标题
            last = j - 1 if texts[j - 1] in texts[i:j - 1] else j
            kinds[i:last] = ["p"] * (last - i)
        i = j + 1
    paras, pos = [], 0
    for t, k in zip(texts, kinds):
        paras.append({"kind": k, "text": t, "page": pos // PAGE})
        pos += width(t)
    pages = pos // PAGE + 1
    toc = toc_paras([(1, p["text"], p["page"]) for p in paras if p["kind"] == "h"][:300], paras)

    title = path.stem
    author = ""
    for t in texts[:8]:
        m = re.match(r"^by\s+(.{3,60})$", t, re.I)
        if m:
            author = m.group(1)
            break
    sample = " ".join(texts[:400])[:20000]
    letters = [c for c in sample if c.isalpha()]
    lang = "en" if sum(c.isascii() for c in letters) / max(1, len(letters)) > 0.9 else "other"
    return Parsed(title=title, author=author, pages=pages, toc=toc, paras=paras, has_text=True, lang=lang,
                  quality=quality(paras), parser_ver=PARSER_VERS[".txt"])
