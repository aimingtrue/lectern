"""书目层：ISBN、中文书 CIP（中图分类号、主题词、丛书、原书名、出版社、出版年）、上架日期、外部封面。

  python -m library.biblio build [--limit N] [--wait]  从书里读 ISBN 与 CIP（只读书文件和 catalog.db），可增量重跑；
                                                    拿 io 锁，有别的 io 长任务时 --wait 就等，否则退出
  python -m library.biblio arrivals                 从 .ingest 搬运清单推出「上架日期」
  python -m library.biblio covers [--limit N] [--no-title]   没有自带封面（或自带的判过不能用）的书到 Open Library 取封面：
                                                    先按可信 ISBN，再把剩下的外文书按书名 + 作者搜（--no-title 不搜）

数据在 data/biblio.db（不改 catalog.db 结构），全部以 files.fid（内容指纹）为键：
  isbns     fid → ISBN-13，来源 opf / exth / text / pdf / catalog
  cip       fid → 中图分类号、类别、主题词、丛书、原书名、出版社、出版年、CIP 里的 ISBN
  arrivals  fid → 本地目录名推出的日期（不准，书城不用；书城的上架日期见 pubdates，由 library.pubdate 从权威平台查）
  covers    fid → 外部封面（ok / none），图片在 data/store_covers/ext/<fid>.jpg
  cover_reject  fid → 自带封面不能用（bad 人工确认的错图 / suspect 像扉页），由 library.covers 标记
  seen      已解析的文件（fid、大小、修改时间）与解析结果，增量重跑时跳过
同一作品的书目信息写到作品里每个文件的 fid 上：按文字来源、按作品编号查都查得到。
"""
from __future__ import annotations

import datetime as dt
import html
import json
import logging
import os
import posixpath
import re
import signal
import sqlite3
import sys
import threading
import time
import unicodedata
import zipfile
from collections import Counter, defaultdict
from multiprocessing import Pool
from pathlib import Path
from urllib.parse import unquote

from .catalog import _xml
from .enrich import CATEGORIES
from .schemas import biblio as biblio_schema
from .stamp import db_stamp

log = logging.getLogger(__name__)
DB_NAME = "biblio.db"
BIBLIO_VER = 1                      # 解析规则升级时加 1：seen 里旧版本的文件会重新解析
UA = "lectern/0.1 (personal library; https://github.com/aimingtrue/lectern)"
COVER_URL = "https://covers.openlibrary.org/b/isbn/{}-L.jpg?default=false"



def open_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(path, timeout=30)
    c.row_factory = sqlite3.Row
    biblio_schema.ensure(c)                            # 结构见 library/schemas/biblio.py
    c.execute("PRAGMA synchronous=NORMAL")
    return c


def _ro(path: Path) -> sqlite3.Connection:
    """只读打开（catalog.db 有别的任务在写，这里绝不写）。"""
    c = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30)
    c.row_factory = sqlite3.Row
    return c


# ---------------------------------------------------------------- ISBN
DASH = "\u2010\u2011\u2012\u2013\u2014\u2015\u2212\uff0d"


def _ok13(s: str) -> bool:
    return sum(int(c) * (1 if i % 2 == 0 else 3) for i, c in enumerate(s)) % 10 == 0


def _ok10(s: str) -> bool:
    return sum((10 if c == "X" else int(c)) * (10 - i) for i, c in enumerate(s)) % 11 == 0


def _to13(s10: str) -> str:
    body = "978" + s10[:9]
    return body + str((10 - sum(int(c) * (1 if i % 2 == 0 else 3) for i, c in enumerate(body)) % 10) % 10)


def norm_isbn(s) -> str | None:
    """任意写法 → ISBN-13；校验位不对的、不像 ISBN 的返回 None。ISBN-10 转成 13 位。"""
    if not s:
        return None
    s = unicodedata.normalize("NFKC", str(s)).strip()
    s = re.sub(r"^(?:urn\s*:\s*)?isbn(?:-?1[03])?\s*[:：]?\s*", "", s, flags=re.I)
    s = re.sub(rf"[\s\-{DASH}]", "", s).upper()
    if re.fullmatch(r"97[89]\d{10}", s) and _ok13(s):
        return s
    if re.fullmatch(r"\d{9}[\dX]", s) and _ok10(s):
        return _to13(s)
    return None


ISBN_LABEL = re.compile(rf"ISBN(?:-?1[03])?\s*[:：]?\s*((?:[\dX][\s\-{DASH}]{{0,2}}){{10,13}})", re.I)
ISBN_BARE = re.compile(rf"(?<![\dX])97[89](?:[\s\-{DASH}]?\d){{10}}(?!\d)")


def _first_isbn(run: str) -> str | None:
    """「ISBN 978-7-…-5 2021」这种后面跟着别的数字：先试前 13 位，再试前 10 位。"""
    ds = re.sub(r"[^\dX]", "", run.upper())
    return (norm_isbn(ds[:13]) if ds.startswith(("978", "979")) else None) or \
        (norm_isbn(ds[:10]) if re.fullmatch(r"\d{9}[\dX]", ds[:10]) else None)


def find_isbns(text: str) -> list[str]:
    """正文（版权页）里所有合法的 ISBN-13，按出现顺序去重。带「ISBN」字样的 10 位号也转成 13 位。"""
    if not text:
        return []
    t = unicodedata.normalize("NFKC", text)
    hits = []
    for m in ISBN_LABEL.finditer(t):
        i = _first_isbn(m.group(1))
        if i:
            hits.append((m.start(), i))
    for m in ISBN_BARE.finditer(t):
        i = norm_isbn(m.group())
        if i:
            hits.append((m.start(), i))
    out = []
    for _, i in sorted(hits):
        if i not in out:
            out.append(i)
    return out


# ---------------------------------------------------------------- 中图分类号 → 书城类别
# 按最长前缀匹配（先试 4 位、再 3 位、2 位、1 位）；只用 enrich.CATEGORIES 里有的类别名，Z 综合性图书等归不了的不给
CLC_CATEGORY = {
    "A": "社会政治",                                    # 马列毛邓
    "B": "哲学宗教", "B84": "心理自助", "B821": "心理自助",   # 心理学；人生哲学（多是励志书）
    "C": "社会政治", "C93": "商业经济", "C912": "心理自助",   # 社会科学总论；管理学；人际交往
    "D": "社会政治",                                    # 政治、法律
    "E": "历史",                                        # 军事（大众读物多是战争史）
    "F": "商业经济",                                    # 经济
    "G": "语言教育", "G0": "社会政治", "G1": "社会政治", "G2": "社会政治",   # 文化、传媒
    "G3": "科学科普", "G78": "健康生活", "G8": "健康生活",                  # 科研；家庭教育；体育
    "H": "语言教育",                                    # 语言文字
    "I": "文学小说", "I28": "儿童",                     # 文学；中国儿童文学（外国儿童文学见 _CHILD_LIT）
    "J": "艺术",                                        # 艺术
    "K": "历史", "K81": "传记回忆录", "K82": "传记回忆录", "K83": "传记回忆录",   # 历史；人物传记
    "K9": "旅行",                                       # 地理（含旅游地理）
    "N": "科学科普", "O": "科学科普", "P": "科学科普", "Q": "科学科普",          # 自然科学
    "R": "健康生活",                                    # 医药卫生
    "S": "科学科普", "T": "科学科普", "U": "科学科普", "V": "科学科普", "X": "科学科普",
    "TP": "计算机技术", "TN": "计算机技术",              # 自动化与计算机；电子与通信
    "TS97": "健康生活", "TU": "艺术",                   # 生活（烹饪、服饰）；建筑
}
_CHILD_LIT = re.compile(r"^I[13-7]\d{2}\.8")            # 外国文学 I+国别+.8 = 儿童文学（I712.8 美国儿童文学）；I2xx.8 不是
# 文学类按主题词细分体裁；任何类别下主题词带「传记 / 回忆录」都归传记
GENRE = [("科幻奇幻", r"科幻|幻想|奇幻|魔幻"), ("悬疑惊悚", r"侦探|推理|悬疑|惊悚|犯罪|间谍"), ("恐怖", r"恐怖|灵异"),
         ("爱情", r"爱情|言情"), ("历史小说", r"历史小说"), ("儿童", r"儿童|童话|图画故事"), ("青少年", r"青少年")]
GENRE_RES = [(c, re.compile(p)) for c, p in GENRE]
assert set(CLC_CATEGORY.values()) | {c for c, _ in GENRE} | {"传记回忆录", "儿童"} <= set(CATEGORIES)   # 只用书城有的类别


def clc_category(clc: str | None, subjects: list | None = None) -> str | None:
    if not clc:
        return None
    subj = " ".join(subjects or [])
    if re.search(r"传记|回忆录", subj):
        return "传记回忆录"
    cat = next((CLC_CATEGORY[clc[:n]] for n in (4, 3, 2, 1) if clc[:n] in CLC_CATEGORY), None)
    if clc.startswith("I"):
        if _CHILD_LIT.match(clc):
            cat = "儿童"
        for c, r in GENRE_RES:
            if r.search(subj):
                return c
    return cat


# ---------------------------------------------------------------- CIP
CIP_HEAD = re.compile(r"(?:在版|預行|预行)[编編]目")
ROMAN = {"Ⅰ": "\ue001", "Ⅱ": "\ue002", "Ⅲ": "\ue003", "Ⅳ": "\ue004"}     # 先换成私用区记号，NFKC 不会动
ROMAN_ASCII = re.compile(r"(?<![A-Za-z])(IV|III|II|I)\s*[.、,]")
CLC_RE = re.compile(r"[A-Z]{1,2}\d[\d.]*(?:[-=]\d+)*|[A-Z]-\d+")
CLC_OCR = re.compile(r"1(\d{2,3}(?:\.\d+)*(?:-\d+)*)")          # 扫描识别把 I 认成 1：1247.5 → I247.5
PUB_RE = re.compile(r"-{1,2}\s*([^\s:/;,()]{1,12}?)\s*:\s*([^,\d\n();:/]{2,40}?)\s*,?\s*((?:19|20)\d\d)(?:\.\d{1,2})?")
ED_RE = re.compile(r"((?:19|20)\d\d)\s*年\s*\d{1,2}\s*月\s*[\u4e00-\u9fff]{0,3}\s*第\s*[\d一二三四五六七八九十]+\s*版")
PRINT_RE = re.compile(r"((?:19|20)\d\d)\s*年\s*\d{1,2}\s*月\s*[\u4e00-\u9fff]{0,3}\s*第\s*[\d一二三四五六七八九十]+\s*次\s*印")
ORIG_RE = re.compile(r"(?:书名原文|原书名|原文书名|原版书名|英文书名|書名原文|原書名|译自|譯自)\s*:?\s*(.+?)\s*(?=ISBN|\n|$)")
PRESS = r"[\u4e00-\u9fff·•]{2,24}?(?:出版社|书局|書局|印书馆|印書館|出版公司|出版集团|出版集團|书店|書店)"
PUB2_RE = re.compile(rf"([\u4e00-\u9fff]{{2,4}})\s*:\s*({PRESS})\s*,?\s*((?:19|20)\d\d)")   # 破折号被扫描成「一」时
SERIESY = re.compile(r"丛书|叢書|系列|文库|文庫|译丛|譯叢|书系|書系|丛刊|大系|经典|經典|译林|文丛|馆")


def _norm(s: str) -> str:
    for k, v in ROMAN.items():
        s = s.replace(k, v)
    s = re.sub(r"[\u2460-\u2473]", ";", s)                  # ①…⑳ → 分隔符（NFKC 会把它们变成数字）
    s = unicodedata.normalize("NFKC", s)
    s = ROMAN_ASCII.sub(lambda m: {"I": "\ue001", "II": "\ue002", "III": "\ue003", "IV": "\ue004"}[m.group(1)], s)
    s = re.sub(f"[{DASH}]", "-", s)
    s = re.sub(r"[ \t\u00a0]+", " ", s)
    return re.sub(r"\s*\n\s*", "\n", s)


def _sec(blk: str, a: str, b: str | None) -> str:
    i = blk.find(a)
    if i < 0:
        return ""
    j = blk.find(b, i + 1) if b else -1
    s = blk[i + 1: j if j > 0 else None]
    return s.split("\n")[0] if not b or j < 0 else s


def _clc_at(s: str) -> str | None:
    """紧跟在 Ⅳ（新版 CIP 是 Ⅰ）后面的分类号。"""
    s = re.sub(r"^[\s.;:,、]+", "", s.replace(" ", ""))
    m = CLC_RE.match(s)
    if m:
        return m.group().rstrip(".")
    m = CLC_OCR.match(s)
    return "I" + m.group(1).rstrip(".") if m else None


def _clean_press(s: str, strict: bool = False) -> str | None:
    """出版社名：去网址、括号里的地址、「出版发行」字样。strict 时必须像出版社（…出版社/书局/出版公司）。"""
    s = re.sub(r"https?://\S+|www\.\S+|[(（].*$", "", s or "")
    s = re.sub(r"(?<=[\u4e00-\u9fff])\s+(?=[\u4e00-\u9fff])", "", s)       # 「上海文  艺出版社」
    s = re.sub(r"(?:出版发行|出版發行|出版|发行|發行)$", "", s.strip(" |:：/;·-"))
    m = re.search(PRESS, s)
    if strict and not m:
        return None
    s = m.group() if m else s.split()[0] if s.split() else ""
    return s if 2 <= len(s) <= 30 else None


def parse_cip(text: str) -> dict | None:
    """版权页文字 → CIP 字段；没有「在版编目 / 預行編目」时返回 None。"""
    if not text:
        return None
    m = CIP_HEAD.search(text)
    if not m:
        return None
    win = _norm(text[max(0, m.start() - 1500): m.start() + 3000])
    blk = _norm(text[m.start(): m.start() + 1500])
    k = re.search(r"核字[^\n]*", blk)
    blk = blk[: k.end()] if k else blk[:700]
    h = re.search(r"数据|資料|数據|資料", blk[:40])
    desc = blk[h.end() if h else 4:]
    cut = min([x for x in (desc.find("ISBN"), desc.find("\ue001")) if x >= 0] or [len(desc)])
    desc = desc[:cut]
    out = {"clc": None, "subjects": [], "series": None, "orig_title": None, "publisher": None,
           "pub_year": None, "isbn13": None}
    # 中图分类号：Ⅳ 之后，有多个（①I247.5 ②K825）取第一个；2024 年起的新版 CIP 只有「Ⅰ. K820.7」；
    # 都没有时看「核字」上一行是不是单独一个分类号
    out["clc"] = _clc_at(_sec(blk, "\ue004", None)) if "\ue004" in blk else \
        next((x for x in (_clc_at(_sec(blk, t, None)) for t in "\ue003\ue002\ue001" if t in blk) if x), None)
    if not out["clc"] and k:
        prev = blk[: k.start()].rstrip().rsplit("\n", 2)
        if len(prev) >= 2 and re.fullmatch(r"\s*(?:[A-Z]{1,2}\s?\d[\d.]*(?:-\d+)*|1\d{2,3}(?:\.\d+)*)\s*", prev[-2]):
            out["clc"] = _clc_at(prev[-2])
        else:                                            # 新版 CIP 的 Ⅰ 被识别成「1.」：「… ISBN … 1. G301 中国国家版本馆…」
            g = re.search(r"(?<![\d.])1\s*\.\s*([A-Z]{1,2}\s?\d[\d.]*(?:-\d+)*)\s*(?:中国|$)", blk[: k.start()])
            out["clc"] = _clc_at(g.group(1)) if g else None
    # 主题词：Ⅲ 和 Ⅳ 之间，按 ①② 分开；Ⅲ 认不出来（扫描成 EL、in.）时取 Ⅳ 前最后一个「…」之后
    if "\ue003" in blk:
        s3 = _sec(blk, "\ue003", "\ue004")
    elif "\ue004" in blk:
        s3 = blk[: blk.find("\ue004")]
        s3 = s3[s3.rfind("...") + 3:] if "..." in s3 else ""
        s3 = re.sub(r"^\s*[A-Za-z]{1,3}\s*(?:\.|(?=;)|\n)", "", s3)
    else:
        s3 = ""
    out["subjects"] = [x for x in (re.sub(r"\s+", "", p).strip(".;,") for p in s3.split(";")) if x]
    # 出版地:出版社,年
    desc = re.sub(r"\n(?!\()", "", desc)                   # 出版项被排版折行（「江苏凤凰文艺\n出版社」）；单独一行的（丛书）保留
    tail = desc[desc.find("/"):] if "/" in desc else desc
    p = PUB_RE.search(tail) or PUB2_RE.search(tail)
    if p:
        out["publisher"] = _clean_press(p.group(2))
    # 丛书：出版项之后的括号（排除「2021.1重印」）；没有出版项时只认带丛书/系列字样的括号
    after = tail[p.end():] if p else desc
    for g in re.findall(r"\(([^()]{2,40})\)", after):
        g = re.split(r"\s*[/;]\s*", g)[0].strip()
        if g and not re.search(r"重印|印刷|^[\d.\s]+$", g) and (p or SERIESY.search(g)):
            out["series"] = g
            break
    if not out["series"]:
        g = re.search(r"\(([^()]{2,30})\)", _sec(blk, "\ue001", "\ue002"))
        if g and SERIESY.search(g.group(1)):
            out["series"] = g.group(1).strip()
    o = ORIG_RE.search(blk)
    if o:
        out["orig_title"] = o.group(1).strip(" .;:")[:200] or None
    if not out["publisher"]:                             # 版权页「出版发行｜…」那一行
        for q in re.finditer(r"(?:出版发行|出版發行|出\s*版\s*者|出\s*版)\s*[:|/\-]?\s*([^\n]{2,60})", win):
            out["publisher"] = _clean_press(q.group(1), strict=True)
            if out["publisher"]:
                break
    if not out["publisher"]:
        q = re.search(PRESS, win)
        out["publisher"] = q.group() if q else None
    # 出版年：版次那一行的年份（多个时取最新的那一版）> 第一次印刷的年份 > CIP 出版项里的年份
    this_year = dt.date.today().year + 1
    eds = [int(y) for y in ED_RE.findall(win) if 1949 <= int(y) <= this_year]
    prints = [int(y) for y in PRINT_RE.findall(win) if 1949 <= int(y) <= this_year]
    out["pub_year"] = max(eds) if eds else prints[0] if prints else int(p.group(3)) if p else None
    isb = find_isbns(blk) or find_isbns(win)
    out["isbn13"] = isb[0] if isb else None
    if "預行" in blk[:10] or "预行" in blk[:10]:            # 台湾的预行编目：主题词是「1.小說 2.…」，没有中图号
        t = re.search(r"(?:^|\s)1\.\s*([^\s\d][^\n]*)", blk[blk.find("ISBN") if "ISBN" in blk else 0:])
        if t and not out["subjects"]:
            out["subjects"] = [x.strip() for x in re.split(r"\s*\d\.\s*", t.group(1)) if x.strip()][:6]
            out["subjects"] = [re.sub(r"\s+\d{3}(?:\.\d+)?.*$", "", x) for x in out["subjects"]]
        out["clc"] = None
    if not any(out[k] for k in ("clc", "isbn13", "publisher", "subjects")):
        return None
    return out


# ---------------------------------------------------------------- 从文件读文字
BLOCK = re.compile(r"<(?:br|/p|/div|/h\d|/li|/tr|/td|/section)\b[^>]*>", re.I)


def html_text(b: bytes) -> str:
    s = b.decode("utf-8", "replace")
    s = re.sub(r"(?is)<(head|script|style)\b.*?</\1>", " ", s)
    s = BLOCK.sub("\n", s)
    return html.unescape(re.sub(r"<[^>]+>", "", s))


def _read(z: zipfile.ZipFile, name: str, cap: int = 400_000) -> bytes:
    with z.open(name) as f:                     # 只读前一段，防压缩炸弹
        return f.read(cap)


def epub_data(path: Path, n: int = 20) -> tuple[list[str], str]:
    """(OPF 里像 ISBN 的 dc:identifier, 书脊顺序前 n 个 + 最后 2 个 html 的文字)。版权页有时在书末。"""
    ids, order = [], []
    with zipfile.ZipFile(path) as z:
        names = z.namelist()
        try:
            root = _xml(z.read("META-INF/container.xml"))
            opf = next(el.get("full-path") for el in root.iter() if el.tag.endswith("rootfile"))
            o = _xml(_read(z, opf, 2_000_000))
            items, spine = {}, []
            for el in o.iter():
                tag = el.tag.rsplit("}", 1)[-1]
                if tag == "identifier":
                    txt = (el.text or "").strip()
                    scheme = " ".join(v for k, v in el.attrib.items() if k.endswith("scheme")).lower()
                    if "isbn" in scheme or "isbn" in txt.lower() or re.fullmatch(r"[\d\- X]{10,17}", txt):
                        ids.append(txt)
                elif tag == "item":
                    items[el.get("id")] = el.get("href") or ""
                elif tag == "itemref":
                    spine.append(el.get("idref"))
            base = opf.rsplit("/", 1)[0] + "/" if "/" in opf else ""
            order = [posixpath.normpath(base + unquote(items[i])) for i in spine if items.get(i)]
            order = [x for x in order if x in names]
        except Exception:
            pass
        if not order:
            order = sorted(x for x in names if re.search(r"\.x?html?$", x, re.I))
        pick = order[:n] + [x for x in order[-2:] if x not in order[:n]]
        text = "\n".join(html_text(_read(z, x)) for x in pick)
    return ids, text


def pdf_text(path: Path, pages: int = 6) -> str:
    import pymupdf
    pymupdf.TOOLS.mupdf_display_errors(False)          # 坏 pdf 的 MuPDF 报错不刷屏（异常照样抛出）
    with pymupdf.open(path) as d:
        return "\n".join(d[i].get_text() for i in range(min(pages, d.page_count)))


def txt_text(path: Path, cap: int = 64_000) -> str:
    b = open(path, "rb").read(cap)
    for enc in ("utf-8", "gb18030"):
        try:
            return b.decode(enc)
        except UnicodeDecodeError:
            pass
    return b.decode("utf-8", "replace")


def extract(task: tuple) -> dict:
    """一个文件 → ISBN [(isbn, 来源)]、CIP、有没有自带封面。在子进程里跑；坏文件只记错误。"""
    fid, path, fmt, size, mtime = task
    out = {"fid": fid, "path": path, "size": size, "mtime": mtime, "isbns": [], "cip": None, "cover": None, "err": None}
    p = Path(path)
    signal.signal(signal.SIGALRM, _alarm)
    signal.alarm(120)                           # 坏文件卡死时不拖住整个建库
    try:
        text, src = "", "text"
        if fmt == "epub":
            ids, text = epub_data(p)
            out["isbns"] += [(i, "opf") for i in map(norm_isbn, ids) if i]
        elif fmt in ("mobi", "azw", "azw3"):
            from .catalog import mobi_meta
            i = norm_isbn(mobi_meta(p).get("isbn"))
            if i:
                out["isbns"].append((i, "exth"))
        elif fmt == "pdf":
            text, src = pdf_text(p), "pdf"
        elif fmt == "txt":
            text = txt_text(p)
        if text:
            cip = parse_cip(text)
            out["cip"] = cip
            first = [cip["isbn13"]] if cip and cip["isbn13"] else []
            have = {i for i, _ in out["isbns"]}
            out["isbns"] += [(i, src) for i in dict.fromkeys(first + find_isbns(text)) if i not in have]
    except Exception as ex:
        out["err"] = f"{type(ex).__name__}: {ex}"[:200]
    try:
        signal.alarm(30)
        out["cover"] = has_cover(p, fmt)
    except Exception:
        out["cover"] = 0
    signal.alarm(0)
    return out


def _alarm(*_):
    raise TimeoutError("解析超时")


def has_cover(p: Path, fmt: str) -> int:
    """书城能不能从文件本身拿到封面（与 store.make_cover 一样的判断；pdf 总能渲染第一页）。"""
    from .covers import epub_cover, mobi_cover
    if fmt == "pdf":
        return 1
    if fmt == "epub":
        return int(bool(epub_cover(p)))
    if fmt in ("mobi", "azw", "azw3"):
        return int(bool(mobi_cover(p)))
    return 0


# ---------------------------------------------------------------- 建库
def _works(cat: sqlite3.Connection, limit=None) -> tuple[list, dict]:
    works = [dict(r) for r in cat.execute(
        "SELECT id, lang, text_src, layout_src FROM works WHERE kind='book' ORDER BY id" + (f" LIMIT {int(limit)}" if limit else ""))]
    files = defaultdict(list)
    for r in cat.execute("SELECT f.path, f.fid, f.fmt, f.size, f.mtime, f.isbn, f.work FROM files f "
                         "JOIN works w ON w.id=f.work WHERE w.kind='book'"):
        files[r["work"]].append(dict(r))
    return works, files


def build(data_dir: Path, progress=None, limit=None) -> dict:
    t0 = time.time()
    cat = _ro(data_dir / "catalog.db")
    works, files = _works(cat, limit)
    cat.close()
    c = open_db(data_dir / DB_NAME)
    seen = {r["fid"]: (r["size"], r["mtime"], r["ver"]) for r in c.execute("SELECT fid, size, mtime, ver FROM seen")}
    tasks, want = [], set()
    for w in works:
        by_path = {f["path"]: f for f in files.get(w["id"], [])}
        srcs = [w["text_src"]]
        t = by_path.get(w["text_src"])
        if w["layout_src"] and (not t or t["fmt"] != "epub"):       # 文字来源不是 epub 时，pdf 版权页也读一下
            srcs.append(w["layout_src"])
        for s in srcs:
            f = by_path.get(s)
            if not f or f["fid"] in want:
                continue
            want.add(f["fid"])
            k = seen.get(f["fid"])
            if k and k[0] == f["size"] and abs((k[1] or 0) - (f["mtime"] or 0)) < 1 and k[2] == BIBLIO_VER:
                continue
            tasks.append((f["fid"], f["path"], f["fmt"], f["size"], f["mtime"]))
    st = {"works": len(works), "files": len(want), "parsed": 0, "skipped": len(want) - len(tasks), "errors": 0,
          "error_samples": []}
    batch = []

    def flush():
        c.executemany("INSERT OR REPLACE INTO seen(fid,size,mtime,path,isbns,cip,cover,err,ver) VALUES(?,?,?,?,?,?,?,?,?)",
                      [(r["fid"], r["size"], r["mtime"], r["path"], json.dumps(r["isbns"]),
                        json.dumps(r["cip"], ensure_ascii=False) if r["cip"] else None, r["cover"], r["err"],
                        BIBLIO_VER) for r in batch])
        c.commit()
        batch.clear()

    if tasks:
        with Pool(min(8, os.cpu_count() or 4), maxtasksperchild=500) as pool:
            for r in pool.imap_unordered(extract, tasks, chunksize=4):
                st["parsed"] += 1
                if r["err"]:
                    st["errors"] += 1
                    if len(st["error_samples"]) < 20:
                        st["error_samples"].append(f"{r['path']}：{r['err']}")
                batch.append(r)
                if len(batch) >= 200:
                    flush()
                if progress:
                    progress(st["parsed"], len(tasks))
    flush()
    if limit:
        cat = _ro(data_dir / "catalog.db")
        works, files = _works(cat)
        cat.close()
    st.update(merge(c, works, files))
    c.execute("INSERT OR REPLACE INTO meta VALUES('version', ?)", (str(BIBLIO_VER),))
    c.execute("INSERT OR REPLACE INTO meta VALUES('build_at', ?)", (dt.datetime.now().isoformat(timespec="seconds"),))
    c.commit()
    c.close()
    st["seconds"] = round(time.time() - t0, 1)
    return st


def merge(c: sqlite3.Connection, works: list, files: dict) -> dict:
    """seen 里每个文件的解析结果 → 作品的 ISBN 与 CIP，写到作品所有文件的 fid 上（全量重写，几万行几秒）。"""
    parsed = {r["fid"]: r for r in c.execute("SELECT fid, isbns, cip FROM seen")}
    isb_rows, cip_rows = [], []
    st = {"with_isbn": Counter(), "with_cip": 0, "with_clc": 0, "categories": Counter(), "pub_years": Counter()}
    for w in works:
        fl = files.get(w["id"], [])
        fl = sorted(fl, key=lambda f: (f["path"] != w["text_src"], f["path"] != w["layout_src"]))
        got, cip = [], None
        for f in fl:
            r = parsed.get(f["fid"])
            if r:
                got += [tuple(x) for x in json.loads(r["isbns"] or "[]")]
                if not cip and r["cip"]:
                    cip = json.loads(r["cip"])
        got += [(i, "catalog") for i in (norm_isbn(f["isbn"]) for f in fl) if i]
        seen_i, uniq = set(), []
        for i, s in got:
            if i not in seen_i:
                seen_i.add(i)
                uniq.append((i, s))
        fids = list(dict.fromkeys(f["fid"] for f in fl))
        if uniq:
            st["with_isbn"][w["lang"] or "?"] += 1
        for fid in fids:
            isb_rows += [(fid, i, s) for i, s in uniq]
        if cip:
            st["with_cip"] += 1
            cat = clc_category(cip.get("clc"), cip.get("subjects"))
            if cip.get("clc"):
                st["with_clc"] += 1
            st["categories"][cat] += 1
            st["pub_years"][cip.get("pub_year")] += 1
            for fid in fids:
                cip_rows.append((fid, cip.get("clc"), cat, json.dumps(cip.get("subjects") or [], ensure_ascii=False),
                                 cip.get("series"), cip.get("orig_title"), cip.get("publisher"), cip.get("pub_year"),
                                 cip.get("isbn13")))
    c.execute("BEGIN IMMEDIATE")
    try:
        c.execute("DELETE FROM isbns")
        c.execute("DELETE FROM cip")
        c.executemany("INSERT OR IGNORE INTO isbns(fid,isbn13,src) VALUES(?,?,?)", isb_rows)
        c.executemany("INSERT OR REPLACE INTO cip(fid,clc,category,subjects,series,orig_title,publisher,pub_year,isbn13) "
                      "VALUES(?,?,?,?,?,?,?,?,?)", cip_rows)
        c.execute("COMMIT")
    except Exception:
        c.execute("ROLLBACK")
        raise
    return st


# ---------------------------------------------------------------- 上架日期
DAY8 = re.compile(r"(?:新书推荐|书友留言)\s*((?:19|20)\d\d)(\d\d)(\d\d)(?!\d)")
MON6 = re.compile(r"(?:新书推荐|书友留言)\s*((?:19|20)\d\d)(\d\d)(?!\d)")


def _date(y, m, d) -> str | None:
    try:
        return dt.date(int(y), int(m), int(d)).isoformat()
    except ValueError:
        return None


def arrival_date(src: str) -> tuple[str, str] | None:
    """搬运前的原路径（…/Downloads/1/2025年/05月/新书推荐20250512/…）→ (上架日期, 推出日期的目录名)。
    新书推荐/书友留言+8 位日期 > 6 位 YYMMDD 目录 > 新书推荐/书友留言+YYYYMM > 「YYYY年/MM月」> 「YYYY年」。"""
    rel = src.split("/Downloads/1/", 1)[1] if "/Downloads/1/" in src else src
    segs = rel.split("/")[:-1]
    for s in reversed(segs):
        m = DAY8.search(s)
        if m and _date(*m.groups()):
            return _date(*m.groups()), s
    for s in reversed(segs):
        m = re.match(r"(\d\d)(\d\d)(\d\d)(?!\d)", s)
        if m and _date(2000 + int(m.group(1)), m.group(2), m.group(3)):
            return _date(2000 + int(m.group(1)), m.group(2), m.group(3)), s
    for s in reversed(segs):
        m = MON6.search(s)
        if m and _date(m.group(1), m.group(2), 1):
            return _date(m.group(1), m.group(2), 1), s
    for i, s in enumerate(segs):
        y = re.fullmatch(r"((?:19|20)\d\d)\s*年", s)
        if not y:
            continue
        mo = re.fullmatch(r"(\d{1,2})\s*月", segs[i + 1]) if i + 1 < len(segs) else None
        if mo and _date(y.group(1), mo.group(1), 1):
            return _date(y.group(1), mo.group(1), 1), f"{s}/{segs[i + 1]}"
        return _date(y.group(1), 1, 1), s
    return None


def import_arrivals(data_dir: Path, ingest_dir: Path | None = None) -> int:
    """读 .ingest/*.json 搬运清单：还在的文件 → 上架日期。同一作品的其他文件也记上（已有更早日期的不改）。
    ingest_dir 默认是放书的主文件夹下的 .ingest。"""
    from .extract import book_id
    if ingest_dir is None:
        from .config import load
        ingest_dir = load().library_root() / ".ingest"
    cat = _ro(data_dir / "catalog.db")
    by_path = {r["path"]: (r["fid"], r["work"]) for r in cat.execute("SELECT path, fid, work FROM files")}
    of_work = defaultdict(set)
    for fid, work in by_path.values():
        if work:
            of_work[work].add(fid)
    work_of = {fid: work for fid, work in by_path.values()}
    cat.close()
    rows = {}
    for mf in sorted(Path(ingest_dir).glob("*.json")):
        try:
            done = json.loads(mf.read_text()).get("done") or []
        except Exception as ex:
            log.warning("清单读不了 %s: %s", mf, ex)
            continue
        for d in done:
            dst = d.get("dst") or ""
            got = arrival_date(d.get("src") or "")
            if not got or not os.path.exists(dst):
                continue
            fid = by_path.get(dst, (None,))[0]
            if not fid:
                try:
                    fid = book_id(Path(dst))
                except OSError:
                    continue
            if fid not in rows or got[0] < rows[fid][0]:
                rows[fid] = got
    direct = len(rows)
    for fid, got in list(rows.items()):              # 作品里的其他文件
        for o in of_work.get(work_of.get(fid), ()):
            if o not in rows or got[0] < rows[o][0]:
                rows[o] = got
    c = open_db(data_dir / DB_NAME)
    c.executemany("INSERT INTO arrivals(fid,date,src) VALUES(?,?,?) ON CONFLICT(fid) DO UPDATE SET "
                  "date=excluded.date, src=excluded.src WHERE excluded.date < arrivals.date",
                  [(fid, d, s) for fid, (d, s) in rows.items()])
    c.execute("INSERT OR REPLACE INTO meta VALUES('arrivals_at', ?)", (dt.datetime.now().isoformat(timespec="seconds"),))
    c.commit()
    c.close()
    return direct


# ---------------------------------------------------------------- 外部封面
def _image_ok(b: bytes) -> bool:
    if len(b) <= 1024 or not (b[:3] == b"\xff\xd8\xff" or b[:8] == b"\x89PNG\r\n\x1a\n" or b[:3] == b"GIF"):
        return False
    try:
        import pymupdf
        pix = pymupdf.Pixmap(b)
        return pix.width >= 50 and pix.height >= 50
    except Exception:
        return False


def _jpeg(b: bytes) -> bytes:
    if b[:3] == b"\xff\xd8\xff":
        return b
    import pymupdf
    pix = pymupdf.Pixmap(b)
    if pix.colorspace is None or pix.colorspace.n not in (1, 3) or pix.alpha:
        pix = pymupdf.Pixmap(pymupdf.csRGB, pix)
        if pix.alpha:
            pix = pymupdf.Pixmap(pix, 0)
    return pix.tobytes("jpeg", jpg_quality=88)


def cover_needs(data_dir: Path) -> list[dict]:
    """要外部封面的作品：文字来源没有自带封面（txt 等）且作品里没有 pdf，或者自带封面判过不能用（cover_reject）。
    带上 ISBN 列表（可能为空）。"""
    cat = _ro(data_dir / "catalog.db")
    works, files = _works(cat)
    cat.close()
    c = open_db(data_dir / DB_NAME)
    cov = {r["fid"]: r["cover"] for r in c.execute("SELECT fid, cover FROM seen")}
    isb = trusted_isbns(c)
    rejected = {r[0] for r in c.execute("SELECT fid FROM cover_reject")}
    c.close()
    out = []
    for w in works:
        fl = files.get(w["id"], [])
        t = next((f for f in fl if f["path"] == w["text_src"]), None)
        own = not any(f["fid"] in rejected for f in fl)      # 自带封面判过不能用（扉页、logo…）的：也要外部封面
        if own and any(f["fmt"] == "pdf" for f in fl):
            continue
        if own and t:
            has = cov.get(t["fid"])
            if has is None:
                try:
                    has = has_cover(Path(t["path"]), t["fmt"])
                except Exception:
                    has = 0
            if has:
                continue
        fids = list(dict.fromkeys(([t["fid"]] if t else []) + [f["fid"] for f in fl]))
        if fids:
            out.append({"id": w["id"], "lang": w["lang"], "fids": fids, "isbns": isb.get(fids[0], [])})
    return out


def trusted_isbns(c: sqlite3.Connection) -> dict[str, list[str]]:
    """可信 ISBN：书自己的元数据里的（opf、exth、编目），或者和 CIP 里的一致的；正文里扫到的不算（约 30% 是书末
    广告里别的书）。fid → [ISBN-13]，按入库顺序。同一本书判定、Wikidata、Google Books、外部封面都用这一条规则。
    评分（ratings.library_isbns）和出版日期（pubdate.TRUSTED）有意不同：没有可信 ISBN 时还会试正文里的、但要求书名对得上。"""
    out: dict[str, list[str]] = defaultdict(list)
    if not c.execute("SELECT 1 FROM sqlite_master WHERE name='isbns'").fetchone():
        return out
    for fid, isbn in c.execute("SELECT i.fid, i.isbn13 FROM isbns i LEFT JOIN cip p ON p.fid=i.fid AND p.isbn13=i.isbn13 "
                               "WHERE i.src IN ('opf', 'exth', 'catalog') OR p.fid IS NOT NULL ORDER BY i.rowid"):
        if isbn not in out[fid]:
            out[fid].append(isbn)
    return out


def set_cover(c: sqlite3.Connection, fids, status: str, src: str, path: str | None = None) -> None:
    """covers 表唯一的写入口（biblio.fetch_covers、sources.google_books 都经它写）：作品里每个文件的 fid 都记一行。"""
    now = time.time()
    c.executemany("INSERT OR REPLACE INTO covers(fid,status,src,path,fetched) VALUES(?,?,?,?,?)",
                  [(f, status, src, path, now) for f in fids])


def add_fid_alias(path: Path, pairs) -> int:
    """fid_alias 表唯一的写入口（library.dedupe 经它写）：[(移走的 fid, 保留的 fid)]。返回写入的行数。"""
    pairs = [(a, b) for a, b in pairs if a and b and a != b]
    if not pairs:
        return 0
    c = open_db(path)
    try:
        with c:
            c.executemany("INSERT OR REPLACE INTO fid_alias(old_fid,new_fid,at) VALUES(?,?,?)",
                          [(a, b, time.time()) for a, b in pairs])
    finally:
        c.close()
    return len(pairs)


def fid_aliases(c: sqlite3.Connection) -> dict[str, str]:
    """移走的 fid → 现在保留的 fid（连续去重时顺着链走到最后一份）。"""
    m = {r[0]: r[1] for r in c.execute("SELECT old_fid, new_fid FROM fid_alias")}
    out = {}
    for a in m:
        b, seen = m[a], {a}
        while b in m and b not in seen:
            seen.add(b)
            b = m[b]
        out[a] = b
    return out


def set_reject(c: sqlite3.Connection, fids, work: str, verdict: str | None, why: str = "") -> None:
    """cover_reject 表唯一的写入口（library.covers 经它写）：作品里每个文件的 fid 都记一行；verdict=None 撤销。
    已经人工判 bad 的，机器初筛（suspect）不降级。"""
    assert verdict in (None, "bad", "suspect"), verdict
    fids = list(fids)
    if verdict is None:
        c.executemany("DELETE FROM cover_reject WHERE fid=?", [(f,) for f in fids])
        return
    now = time.time()
    c.executemany("INSERT INTO cover_reject(fid,work,verdict,why,at) VALUES(?,?,?,?,?) ON CONFLICT(fid) DO UPDATE SET "
                  "work=excluded.work, verdict=excluded.verdict, why=excluded.why, at=excluded.at "
                  "WHERE NOT (cover_reject.verdict='bad' AND excluded.verdict='suspect')",
                  [(f, work, verdict, why, now) for f in fids])


def cover_override(data_dir: Path, fids) -> tuple[str | None, Path | None]:
    """这些文件（一部作品）的自带封面结论和外部封面：(bad / suspect / None, 外部封面文件或 None)。只读书目库。
    入库（scan）和 library.covers 用；书城用内存里的 Biblio。"""
    p = data_dir / DB_NAME
    fids = list(fids)
    if not p.exists() or not fids:
        return None, None
    c = _ro(p)
    try:
        if not c.execute("SELECT 1 FROM sqlite_master WHERE name='cover_reject'").fetchone():
            return None, None
        qs = ",".join("?" * len(fids))
        vs = {r[0] for r in c.execute(f"SELECT verdict FROM cover_reject WHERE fid IN ({qs})", fids)}
        verdict = "bad" if "bad" in vs else "suspect" if vs else None
        ext = None
        for r in c.execute(f"SELECT path FROM covers WHERE fid IN ({qs}) AND status='ok' AND path IS NOT NULL", fids):
            q = Path(r[0]) if Path(r[0]).is_absolute() else data_dir / r[0]
            if q.is_file():
                ext = q
                break
        return verdict, ext
    finally:
        c.close()


def fetch_covers(data_dir: Path, limit=None, delay=1.5) -> dict:
    """按 ISBN 到 Open Library 取封面（每个 ISBN 一次请求、请求间隔 delay 秒）。只发 ISBN，不发其他信息。
    429 / 5xx：按 30 秒起翻倍退避重试，连续 5 次失败就停（下次再跑接着来）。"""
    import httpx
    need = [w for w in cover_needs(data_dir) if w["isbns"]]
    c = open_db(data_dir / DB_NAME)
    done = {r["fid"] for r in c.execute("SELECT fid FROM covers")}
    need = [w for w in need if not any(f in done for f in w["fids"])]
    out_dir = data_dir / "store_covers" / "ext"
    out_dir.mkdir(parents=True, exist_ok=True)
    st = {"candidates": len(need), "tried": 0, "ok": 0, "none": 0, "requests": 0, "stopped": None}
    fails = 0
    with httpx.Client(headers={"User-Agent": UA}, timeout=30, follow_redirects=True) as cl:
        for w in need[: limit or None]:
            st["tried"] += 1
            status, path = "none", None
            for isbn in w["isbns"]:
                while True:
                    if st["requests"]:
                        time.sleep(delay)
                    st["requests"] += 1
                    try:
                        r = cl.get(COVER_URL.format(isbn))
                        code = r.status_code
                    except httpx.HTTPError as ex:
                        code, r = 599, None
                        log.info("请求失败 %s: %s", isbn, ex)
                    blocked = code not in (200, 404) or (code == 200 and not _image_ok(r.content))
                    if code == 429 or code >= 500 or blocked:
                        fails += 1
                        if fails >= 5:
                            st["stopped"] = f"连续 {fails} 次 {code}，先停下"
                            break
                        wait = 30 * 2 ** (fails - 1)
                        ra = r.headers.get("retry-after", "") if r is not None else ""
                        wait = int(ra) if ra.isdigit() else wait
                        log.warning("Open Library %s，%s 秒后重试", code, wait)
                        time.sleep(wait)
                        continue
                    fails = 0
                    break
                if st["stopped"]:
                    break
                if code == 200 and _image_ok(r.content):
                    path = f"store_covers/ext/{w['fids'][0]}.jpg"
                    (data_dir / path).write_bytes(_jpeg(r.content))
                    status = "ok"
                    break
            if st["stopped"]:
                break
            st[status] += 1
            set_cover(c, w["fids"], status, "openlibrary", path)
            c.commit()
    c.close()
    return st


COVER_ID_URL = "https://covers.openlibrary.org/b/id/{}-L.jpg?default=false"
TITLE_COVER_SRC = "openlibrary-title"           # src_tries 的来源名，也是 covers.src


def fetch_covers_by_title(data_dir: Path, limit=None, delay=1.5, ol=None, http=None) -> dict:
    """没有自带封面、也没按 ISBN 取到外部封面的外文书（多是 txt，没有可信 ISBN）：按书名 + 作者到 Open Library 搜，
    核对同 library.enrich（主书名相同、作者的姓对上）；对上的作品有封面编号就取它的封面。没有作者的不查（同名书多，
    错封面比文字封面更糟）；中文书不查（Open Library 里很少）。只发书名和作者。查过的记在 src_tries，不重查；
    Open Library 连续失败就停下，没查完的下次接着查。"""
    import httpx
    from .enrich import OL, main_title, matches
    cat = _ro(data_dir / "catalog.db")
    try:
        meta = {r["id"]: (r["title"] or "", r["author"] or "") for r in
                cat.execute("SELECT id, title, author FROM works WHERE kind='book'")}
    finally:
        cat.close()
    c = open_db(data_dir / DB_NAME)
    have = {r[0] for r in c.execute("SELECT fid FROM covers WHERE status='ok'")}
    done = {r[0] for r in c.execute("SELECT fid FROM src_tries WHERE source=?", (TITLE_COVER_SRC,))}
    need = [w for w in cover_needs(data_dir) if w["lang"] != "zh" and meta.get(w["id"], ("", ""))[1].strip()
            and not have & set(w["fids"]) and w["fids"][0] not in done]
    st = {"candidates": len(need), "tried": 0, "ok": 0, "none": 0, "stopped": None}
    ol = ol or OL(interval=delay)
    cl = http or httpx.Client(headers={"User-Agent": UA}, timeout=30, follow_redirects=True)
    out_dir = data_dir / "store_covers" / "ext"
    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        for w in need[: limit or None]:
            title, author = meta[w["id"]]
            first = re.split(r"\s*(?:;|&| and )\s*", author)[0].strip()
            try:
                res = ol.get("/search.json", {"title": main_title(title), "author": first, "limit": 5,
                                              "fields": "key,title,author_name,cover_i"}) or {}
                doc = next((d for d in res.get("docs", []) if d.get("cover_i") and matches(d, title, first)), None)
                img = None
                if doc:
                    time.sleep(delay)
                    r = cl.get(COVER_ID_URL.format(doc["cover_i"]))
                    img = r.content if r.status_code == 200 and _image_ok(r.content) else None
            except (RuntimeError, httpx.HTTPError) as ex:          # Open Library 不可用：停下，这本不记
                st["stopped"] = str(ex)
                break
            st["tried"] += 1
            if img:
                path = f"store_covers/ext/{w['fids'][0]}.jpg"
                (data_dir / path).write_bytes(_jpeg(img))
                set_cover(c, w["fids"], "ok", TITLE_COVER_SRC, path)
                st["ok"] += 1
            else:
                st["none"] += 1
            now = time.time()
            c.executemany("INSERT OR REPLACE INTO src_tries VALUES(?,?,?,?)",
                          [(TITLE_COVER_SRC, f, "ok" if img else "none", now) for f in w["fids"]])
            c.commit()
            if st["tried"] % 100 == 0:
                print(f"{st['tried']:,}/{len(need):,}  取到 {st['ok']:,}", flush=True)
    finally:
        c.close()
        if http is None:
            cl.close()
    return st


# ---------------------------------------------------------------- 书城读取端
class Biblio:
    """书城用的只读视图（内存里）。biblio.db 或它的 WAL 文件变了才重新读。"""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.lock = threading.Lock()
        self.stamp = None
        self.cip: dict[str, sqlite3.Row] = {}
        self.arrivals: dict[str, str] = {}
        self._covers: dict[str, str] = {}
        self._isbns: dict[str, list[str]] = {}
        self._reject: dict[str, str] = {}                 # fid 和标记时的作品编号 → bad / suspect

    def _stamp(self):
        p = self.path
        return db_stamp(p)

    def get(self) -> "Biblio":
        st = self._stamp()
        if st != self.stamp:
            with self.lock:
                if st != self.stamp:
                    self._load()
                    self.stamp = st
        return self

    def _load(self) -> None:
        if not self.path.exists():
            self.cip, self.arrivals, self._covers, self._isbns, self._reject = {}, {}, {}, {}, {}
            return
        c = _ro(self.path)
        try:
            cip = {r["fid"]: r for r in c.execute("SELECT * FROM cip")}
            arrivals = {r["fid"]: r["date"] for r in c.execute("SELECT fid, date FROM arrivals")}
            covers = {r["fid"]: r["path"] for r in c.execute("SELECT fid, path FROM covers WHERE status='ok' AND path IS NOT NULL")}
            isbns: dict[str, list[str]] = {}
            for r in c.execute("SELECT fid, isbn13 FROM isbns ORDER BY rowid"):
                isbns.setdefault(r["fid"], []).append(r["isbn13"])
            reject: dict[str, str] = {}
            if c.execute("SELECT 1 FROM sqlite_master WHERE name='cover_reject'").fetchone():
                for r in c.execute("SELECT fid, work, verdict FROM cover_reject ORDER BY verdict='bad'"):   # bad 后写，盖过 suspect
                    reject[r["fid"]] = reject[r["work"]] = r["verdict"]
        finally:
            c.close()
        self.cip, self.arrivals, self._covers, self._isbns, self._reject = cip, arrivals, covers, isbns, reject

    def cover_path(self, fid: str) -> Path | None:
        p = self._covers.get(fid)
        if not p:
            return None
        p = Path(p) if Path(p).is_absolute() else self.path.parent / p
        return p if p.exists() else None

    def cover_verdict(self, key: str | None) -> str | None:
        """自带封面的结论（bad / suspect / None）：key 是文件 fid，或标记时的作品编号（书城封面缓存按作品编号存）。"""
        return self._reject.get(key) if key else None

    def isbns(self, fid: str) -> list[str]:
        return list(self._isbns.get(fid, ()))


# ---------------------------------------------------------------- 命令行
def main(argv: list[str]) -> int:
    from .config import load
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname).1s %(message)s", datefmt="%H:%M:%S")
    cfg = load()
    cmd = argv[0] if argv else ""
    opt = {argv[i]: argv[i + 1] for i in range(1, len(argv) - 1) if argv[i].startswith("--")}
    limit = int(opt["--limit"]) if "--limit" in opt else None
    if cmd == "build":
        def prog(done, total):
            if done % 500 == 0 or done == total:
                print(f"\r{done}/{total}", end="", flush=True)
        from . import busy
        try:
            with busy.hold("bibliography", kind="io", wait="--wait" in argv):    # 要打开几万本书：和扫描、去重错开
                st = build(cfg.data, prog, limit)
        except busy.Busy as ex:
            print(f"⚠️ {ex}")
            return 1
        print(f"\n完成（{st['seconds']:.0f} 秒）：作品 {st['works']:,}，文件 {st['files']:,}，新解析 {st['parsed']:,}，"
              f"跳过 {st['skipped']:,}，出错 {st['errors']}")
        print("有 ISBN：", dict(st["with_isbn"].most_common()))
        print(f"有 CIP：{st['with_cip']:,}，有中图号：{st['with_clc']:,}")
        print("类别：", dict(st["categories"].most_common()))
        print("出版年（近年）：", {y: n for y, n in sorted((k, v) for k, v in st["pub_years"].items() if k and k >= 2015)})
        for e in st["error_samples"][:10]:
            print("⚠️", e)
        return 0
    if cmd == "arrivals":
        n = import_arrivals(cfg.data, Path(opt["--dir"]).expanduser()) if "--dir" in opt else import_arrivals(cfg.data)
        print(f"上架日期：{n:,} 个文件")
        return 0
    if cmd == "covers":
        st = fetch_covers(cfg.data, limit, float(opt.get("--delay", 1.5)))
        print(st)
        if "--no-title" not in argv:
            print("按书名：", fetch_covers_by_title(cfg.data, limit, float(opt.get("--delay", 1.5))))
        if st["ok"]:                     # 自带封面判过不能用的书可能刚取到外部封面：换掉书城缓存和阅读库里的旧图
            from .covers import refresh, rejected_works
            r = refresh(cfg.data, rejected_works(cfg.data))
            print(f"自带封面判过不能用的书：清掉书城缓存 {r['store']} 个，重画阅读库封面 {r['reader']} 本")
        return 0
    print(__doc__)
    return 1


if __name__ == "__main__":
    from library.biblio import main as _main       # 子进程按 library.biblio 找 extract（不是 __main__）
    sys.exit(_main(sys.argv[1:]))
