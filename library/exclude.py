"""排除书库：不想要、不会读的书移到废纸篓，并记住「这本书是谁」，以后再导入同一本（别的副本、格式、版本）时编目直接拦下。

  python -m library.exclude plan [--out 报告.tsv] [--no-protect]   按规则列出候选，写成报告（不动文件）；
                                                         --no-protect：保护名单不管，全部标「排除」
  python -m library.exclude review 报告.tsv [--apply]     按复核过的报告执行：「排除」的记进黑名单、文件移到废纸篓；「留」的记住
  python -m library.exclude add 作品编号 … [--why 原因] [--apply]   手动排除（不看保护名单）
  python -m library.exclude author 作者名 [--apply]       整个作者排除：书库里这个作者的书移走，以后的也拦下
  python -m library.exclude keep 作品编号 … [--paths 清单文件] [--why 原因]   记为「留着」，规则不再报出来
  python -m library.exclude sweep [--apply]              编目时拦下的文件、编目里命中黑名单的文件移到废纸篓
  python -m library.exclude gate [--since 秒] [--apply]   入库门禁：「扫描版」「质量」两条（不看 config，总是开着）命中的
                                                         直接排除，不用复核；--since 只查这个时间以后编目的作品（新书入库用）
  python -m library.exclude undo 清单 [--title 书名 …] [--keep] [--apply]
                                                         按清单把文件移回原处，撤掉这次加的黑名单（之后重新编目）；
                                                         --title 只撤销这几部，--keep 撤掉的同时记为「留着」
  python -m library.exclude stats

不加 --apply 都只打印会做什么。数据在 data/exclude.db（表结构见 library/schemas/exclude.py），只由本模块写。

规则：config.json 的 "exclude"，写法和 "taste_avoid" 相同（个人偏好只在本机配置里）："fiction/romance" 一个小类、
  "business" 整个大类、"教材"（书名像教材、讲义、习题集，taste.TEXTBOOK；口碑分 ≥ TEXTBOOK_KEEP 的标「留」）——
  这三种只看图书（kind='book'）；
  "dir:医学 药学"（作品有文件在这个目录下，可以写多级 "123/10月/医学 药学"，任何类别）；
  "mag:Bio Spectrum"（这个刊名的杂志、报纸、儿童刊，按 catalog.mag_key 比）；
  "扫描版"（图书的全部文件都是没有文字层的 pdf / djvu；只记指纹，以后同一本书的文字版照常进来）；
  "质量"（图书读不好：宁缺毋滥，见 quality_of；也只记指纹，以后排版好的版本照常进来）。
  分类用书城展示的那一份：作品文字来源（没有就版式来源）那个文件的分类，置信度够或按正文判断的（taste.CAT_OK）。
  只有「质量」规则看入库体检的指标（digest.db 的 gate、library.db 的段数）；分类、教材规则不看。
保护（规则永远不报；手动 add / author 不受限）：书架上有阅读进度的、豆瓣读过 / 在读 / 想读的、喜欢的作者（权重 > 0）、
  得过奖的、kept 里的。
报告第一列「决定」：排除 / 留 / 保护：原因。review 只执行「排除」和「留」，其余的行不动（保护的那行改成「排除」也照办）；同一规则内按口碑分从高到低，
  边界的书在最前面。
排除一部作品：它每个文件的指纹、ISBN、书名 + 作者都记进 blocked，全部文件移到 ~/.Trash/英文书库-排除-时间/（按原目录
  结构），清单在 logs/exclude-时间.txt；再从编目里删掉这些文件、重新归并作品。不永久删除任何文件。
编目（library.catalog）每个新文件读出指纹、ISBN、书名作者后查 blocked：命中的不进编目，记进 held 等 sweep。
"""
from __future__ import annotations

import csv
import json
import re
import shutil
import sqlite3
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

from .catalog import Catalog, mag_key, norm_author, norm_title
from .match import author_key
from .schemas import exclude as exclude_schema
TEXTBOOK = re.compile(r"教程|教材|讲义|习题|题库|原理[(（]第|第\s*\d+\s*版[)）]?$"
                      r"|\b(?:methods|textbook|principles of)\b", re.I)
CAT_OK = "(conf>=0.5 OR src='model')"      # only categories confident enough, or decided from the text


DB_NAME = "exclude.db"
SCANNED = "扫描版"           # 规则：全部文件都是没有文字层的 pdf / djvu（编目时 pdf_meta 抽样页看出来的，不用质量体检的结论）
QUALITY = "质量"             # 规则：读不好的书（宁缺毋滥），报告里写成「质量：原因」
# 网上的资料包、论坛整理的营销讲义：不是正式出版的书。盗版商家在文件名里留的「威信」「客服」不算（底下常是正规书）
LOOSE_DOC = re.compile(r"66顺|营销论坛|千里马营销|成长论坛|食品论坛|sicang8|edown|世界工厂|市场营销书籍集萃|"
                       r"世界知名公司经营智慧|PDF\s*\d+页|绝对精品|不看你会后悔", re.I)
SHORT_OK = ("kids", "comics")   # 绘本、漫画、儿童文学本来字就少，不按「几乎没有内容」排除
PICTURE = re.compile(r"绘|漫画|图画|插画|画集|画册|图鉴|童话|picture|comic", re.I)
# txt 章节文件的名字：编号（「01」「b15」「bill 008」「volume01」「5A 01」）或「content」「index」这类；单词书名（Inferno）不算
FRAGMENT = re.compile(r"(?=.{1,10}$)(?:[A-Za-z]{1,7}[\s_\-]?\d{1,4}[A-Za-z]?|\d{1,3}[A-Za-z]?|0\d{3}|\d[A-Za-z]\s?\d{1,4})|"
                      r"content|contents|index|toc|ad|readme|目录|地址", re.I)
TEASER_TITLE = re.compile(r"[（(【\[]\s*试读[本版]?\s*[）)】\]]|试读[本版]|PDF\s*试读|样章|免费章节|抢先版|[-_—]\s*连载$", re.I)
# 正文里的版权页：txt 是正式出版的书扫来的（「排版差」只删没有出版迹象的）
PRINTED = re.compile(r"出版社|出版发行|ISBN|版权所有|责任编辑|Copyright\s*(©|\(c\)|\d{4})|All rights reserved|"
                     r"Published by|First published|Printed in", re.I)
PUB_SIGNS = {"isbns": "SELECT fid FROM isbns", "cip": "SELECT fid FROM cip",
             "pubdates": "SELECT fid FROM pubdates WHERE date IS NOT NULL", "douban_meta": "SELECT fid FROM douban_meta",
             "descriptions": "SELECT fid FROM descriptions", "wd_works": "SELECT fid FROM wd_works",
             "first_pub": "SELECT fid FROM first_pub", "list_entries": "SELECT fid FROM list_entries WHERE fid IS NOT NULL",
             "same_book": "SELECT fid FROM same_book",
             "translations": "SELECT tr_fid FROM translations UNION SELECT orig_fid FROM translations"}
LAYOUT_VER = 1               # txt_flaw / txt.layout_flaw 的判法改了就加一：缓存的结论重查
TEXTBOOK_KEEP = 8.0          # 「教材」规则只按书名猜（「第N版」「讲义」）：口碑分到这个数的多是畅销的经典教科书、讲义，报告里标「留」
TRASH = Path.home() / ".Trash"
COLS = ["决定", "规则", "口碑分", "作品", "书名", "作者", "语言", "类别", "分类来源", "置信度", "文件数", "MB"]


def open_db(data: Path) -> sqlite3.Connection:
    data.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(data / DB_NAME, timeout=30)
    c.row_factory = sqlite3.Row
    exclude_schema.ensure(c)
    return c


def _ro(path: Path) -> sqlite3.Connection | None:
    if not path.exists():
        return None
    c = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30)
    c.row_factory = sqlite3.Row
    return c


def _tables(c) -> set[str]:
    return {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type IN ('table', 'view')")}


# ---------------------------------------------------------------- 身份键
def ta_key(title: str | None, author: str | None) -> str:
    """规范化书名 + 作者：和编目归并作品（catalog.group 的 "ta"）同一个键。没有作者时不用。"""
    t, a = norm_title(title or ""), norm_author(author or "")
    return f"{t}|{a}" if len(t) >= 2 and a else ""


def file_keys(f, fid_only: bool = False) -> list[tuple[str, str]]:
    """一个文件（编目 files 的一行）的身份键：都有指纹；图书再加 ISBN、书名 + 作者。
    fid_only：只要指纹（扫描版：不想要的是这份扫描件，不是这本书，以后的文字版照常进来）。"""
    out = [("fid", f["fid"])] if f["fid"] else []
    if f["kind"] == "book" and not fid_only:
        if f["isbn"]:
            out.append(("isbn", f["isbn"]))
        k = ta_key(f["title"], f["author"])
        if k:
            out.append(("ta", k))
    return out


class Blocklist:
    """blocked 和 held 读进内存（十几万条也只是几个集合），编目时每个新文件查几次。"""

    def __init__(self, rows=(), held=()):
        self.keys: dict[str, set] = defaultdict(set)
        for kind, key in rows:
            self.keys[kind].add(key)
        self.held = set(held)

    @classmethod
    def load(cls, data: Path) -> "Blocklist":
        c = _ro(data / DB_NAME)
        if c is None:
            return cls()
        try:
            if "blocked" not in _tables(c):
                return cls()
            return cls(c.execute("SELECT kind, key FROM blocked").fetchall(),
                       [r[0] for r in c.execute("SELECT path FROM held")])
        finally:
            c.close()

    def __bool__(self) -> bool:
        return any(self.keys.values())

    def hit(self, f) -> tuple[str, str] | None:
        for kind, key in file_keys(f):
            if key in self.keys[kind]:
                return kind, key
        if f["kind"] == "book" and self.keys["author"]:
            a = author_key(f["author"] or "")
            if a in self.keys["author"]:
                return "author", a
        if f["kind"] != "book" and self.keys["mag"]:
            m = mag_key(f["mag"] or "")
            if m in self.keys["mag"]:
                return "mag", m
        return None


def hold(data: Path, items: list[tuple[dict, tuple[str, str]]]) -> None:
    """编目时命中黑名单的文件：记进 held，等 sweep 移走。items：[(文件, 命中的键)]。"""
    c = open_db(data)
    try:
        now = time.time()
        c.executemany("INSERT OR REPLACE INTO held(path, fid, kind, key, title, at) VALUES(?,?,?,?,?,?)",
                      [(f["path"], f["fid"], k[0], k[1], f["title"], now) for f, k in items])
        c.commit()
    finally:
        c.close()


# ---------------------------------------------------------------- 规则与候选
def rules(data: Path) -> list[str]:
    try:
        return [str(x) for x in json.loads((data / "config.json").read_text()).get("exclude") or []]
    except (OSError, ValueError):
        return []


def scanned(files: list[dict]) -> bool:
    return bool(files) and all(f["fmt"] in ("pdf", "djvu") and not f["has_text"] and not f["error"] for f in files)


def rule_of(rs, top: str | None, sub: str | None, title: str | None, w: dict | None = None,
            paths: list[str] = (), files: list[dict] = ()) -> str | None:
    """作品命中的第一条规则。目录规则任何类别都看；刊名规则只看杂志、报纸、儿童刊；分类和教材只看图书。"""
    for r in rs:
        if r.startswith("dir:") and any(f"/{r[4:].strip('/')}/" in p for p in paths):
            return r
    if w is not None and w["kind"] != "book":
        mk = mag_key(w.get("mag") or "")
        return next((r for r in rs if r.startswith("mag:") and mk and mag_key(r[4:]) == mk), None)
    if top and f"{top}/{sub or ''}" in rs:
        return f"{top}/{sub}"
    if top and top in rs:
        return top
    if "教材" in rs and TEXTBOOK.search(title or ""):
        return "教材"
    if SCANNED in rs and scanned(list(files)):
        return SCANNED
    return None


def quality_of(w: dict, files: list[dict], m: dict | None, top: str | None = None, pages: int = 0,
               rated: bool = False, cover: bool = True, published: bool = False, flaw: str = "") -> str | None:
    """图书读不好的原因（宁缺毋滥：排版差、认不出字、不是正式的书），读得好返回 None。m：书城展示那份文件的
    入库体检指标（digest.gate 加段数 n），没入库的为 None——只看文件本身的毛病。门槛按全库分布和抽样定（见 data-model.md）。
    没有封面本身不删：没封面（covered）、没评分、没有出版迹象（published）、txt 又排版差（flaw，txt_flaw）四样都占才删。
    整本书只解析出一两段的不算：多是解析器的问题（txt 原先经 MuPDF 排版丢了空行，已改为按原文件分段，见 library/txt.py），不删书。"""
    fmts = {f["fmt"] for f in files}
    if not w.get("text_src") and not w.get("layout_src"):
        return "没有可读文件"          # 加密的 epub（「[精品]」一批）、mobi 标了读不出、没转换成 epub 的 azw3（新书流程先体检转换再过门禁）
    if fmts and fmts <= {"doc", "docx", "chm"}:
        return "Office 文档"
    title = w.get("title") or ""
    if fmts == {"txt"} and FRAGMENT.fullmatch(title.strip()):
        return "txt 章节碎片"                          # 「01」「b15」「bill 008」：一本书拆成的章节文件
    if LOOSE_DOC.search(" ".join([title] + [Path(f["path"]).name for f in files])):
        return "网络资料包"
    if TEASER_TITLE.search(title):
        return "试读残缺"
    if flaw and not cover and not rated and not published:
        return f"排版差（{flaw}）"
    if not m:
        return None
    n = m.get("n") or 1
    per = m["total"] / n                              # 平均每段字数
    shown = Path(w.get("text_src") or w.get("layout_src") or "").suffix.lower().lstrip(".")
    picture = top in SHORT_OK or PICTURE.search(f"{title} {w.get('author') or ''}") or (shown == "pdf" and pages >= 10)
    if m["total"] < 5000 and not picture and not rated:      # 绘本、漫画、多页的图画书本来字少；有评分的是正式出版的书
        return "几乎没有内容"
    if m["teaser"]:
        return "试读残缺"
    if m["sense"] is not None and m["sense"] < (0.7 if m["zh"] else 0.5):
        return "认不出字"                              # 中文满篇怪字；西文单词粘连（扫描识别、pdf 取字出错）
    if m["repl"] > 0.01 or m["spaced"] > 0.2:
        return "乱码或字间空格"
    if not m["zh"] and n >= 50 and m["tiny"] > 0.5 and per < 15:
        return "碎片化"
    return None


def book_works(cat: Catalog, kind: str | None = "book") -> tuple[dict, dict]:
    """作品 id → 作品，作品 id → 它的文件。"""
    where = "WHERE kind=?" if kind else ""
    ws = {r["id"]: dict(r) for r in cat.q(
        f"SELECT id, kind, title, author, lang, mag, bytes, n_files, text_src, layout_src, pages, added FROM works {where}",
        (kind,) if kind else ())}
    fs: dict = defaultdict(list)
    for f in cat.q("SELECT path, fid, kind, fmt, has_text, error, isbn, title, author, size, work FROM files "
                   "WHERE work IS NOT NULL"):
        if f["work"] in ws:
            fs[f["work"]].append(dict(f))
    return ws, fs


def context(data: Path) -> dict:
    """保护名单和分类、口碑（各库只读）。"""
    ctx = {"cats": {}, "score": {}, "awards": set(), "me_fids": set(), "me_works": set(), "fav": set(),
           "reading": set(), "kept": defaultdict(set), "gate": {},
           "seen_cover": {}, "ext_cover": set(), "bad_cover": set(), "pub": set()}
    b = _ro(data / "biblio.db")
    if b is not None:
        try:
            t = _tables(b)
            if "categories" in t:
                ctx["cats"] = {r["fid"]: dict(r) for r in b.execute(
                    f"SELECT fid, top, sub, conf, src FROM categories WHERE top IS NOT NULL AND {CAT_OK}")}
            if "score" in t:
                ctx["score"] = {r[0]: r[1] for r in b.execute("SELECT fid, score FROM score")}
            if "awards" in t:
                ctx["awards"] = {r[0] for r in b.execute("SELECT fid FROM awards")}
            if "me_books" in t:
                for r in b.execute("SELECT fid, work FROM me_books"):
                    ctx["me_fids"].add(r[0])
                    ctx["me_works"].add(r[1])
            if "me_authors" in t:
                ctx["fav"] = {r[0] for r in b.execute("SELECT akey FROM me_authors WHERE weight > 0")}
            if "seen" in t:
                ctx["seen_cover"] = {r[0]: r[1] for r in b.execute("SELECT fid, cover FROM seen WHERE cover IS NOT NULL")}
            if "covers" in t:
                for fid, p in b.execute("SELECT fid, path FROM covers WHERE status='ok' AND path IS NOT NULL"):
                    if (Path(p) if Path(p).is_absolute() else data / p).is_file():
                        ctx["ext_cover"].add(fid)
            if "cover_reject" in t:
                for fid, wk in b.execute("SELECT fid, work FROM cover_reject WHERE verdict='bad'"):
                    ctx["bad_cover"].update(x for x in (fid, wk) if x)
            for tb, sql in PUB_SIGNS.items():             # 出版迹象：ISBN、CIP、出版日期、豆瓣条目、简介、Wikidata、榜单…
                if tb in t:
                    ctx["pub"].update(r[0] for r in b.execute(sql))
        finally:
            b.close()
    lib = _ro(data / "library.db")
    paras = {}
    if lib is not None:
        try:
            if "books" in _tables(lib):
                ctx["reading"] = {r[0] for r in lib.execute("SELECT id FROM books WHERE last_read IS NOT NULL")}
                paras = {r[0]: r[1] for r in lib.execute("SELECT id, n_paras FROM books WHERE status='ok'")}
        finally:
            lib.close()
    ctx["gate"] = {}                                   # 「质量」规则用：入库体检指标 + 段数（书的 id 就是文件指纹）
    dg = _ro(data / "digest.db")
    if dg is not None:
        try:
            if "digest" in _tables(dg):
                for bid, g in dg.execute("SELECT book, gate FROM digest WHERE gate IS NOT NULL"):
                    if bid in paras:
                        ctx["gate"][bid] = json.loads(g) | {"n": paras[bid]}
        finally:
            dg.close()
    if (data / DB_NAME).exists():
        c = open_db(data)
        try:
            for r in c.execute("SELECT kind, key FROM kept"):
                ctx["kept"][r[0]].add(r[1])
        finally:
            c.close()
    return ctx


def protected(w: dict, fs: list[dict], ctx: dict) -> str | None:
    fids = {f["fid"] for f in fs}
    if fids & ctx["reading"]:
        return "在读"
    if fids & ctx["me_fids"] or w["id"] in ctx["me_works"]:
        return "豆瓣记录"
    if author_key(w["author"] or "") in ctx["fav"]:
        return "喜欢的作者"
    if fids & ctx["awards"]:
        return "获奖"
    if any(key in ctx["kept"][kind] for f in fs for kind, key in file_keys(f)):
        return "留着"
    return None


def covered(w: dict, fs: list[dict], ctx: dict) -> bool:
    """书城给这部作品出的是真封面（不是文字封面）：和 /store/cover 同一个口径——有外部封面；或者自带封面没判过
    不能用：pdf 第一页、epub / mobi / azw3 里的封面图（biblio.seen 记过的直接用，没记的现读文件）。"""
    from .biblio import has_cover
    fids = {f["fid"] for f in fs}
    if fids & ctx["ext_cover"]:
        return True
    if fids & ctx["bad_cover"] or w["id"] in ctx["bad_cover"]:
        return False
    for f in fs:
        if f["fmt"] == "pdf" and not f["error"]:
            return True
        has = ctx["seen_cover"].get(f["fid"])
        if has is None and f["fmt"] in ("epub", "mobi", "azw", "azw3"):
            try:
                has = has_cover(Path(f["path"]), f["fmt"])
            except Exception:
                has = 0
        if has:
            return True
    return False


def txt_flaw(path: str) -> str:
    """txt 排版差的原因（txt.layout_flaw）；没毛病、读不出、正文里有版权页（正式出版的书）的返回空串。"""
    from .txt import decode, layout_flaw, split
    try:
        paras = split(decode(Path(path).read_bytes()))
    except Exception:
        return ""
    if PRINTED.search(" ".join(paras[:200] + paras[-60:])):
        return ""
    return layout_flaw(paras) or ""


def flaws(data: Path, todo: dict[str, str]) -> dict[str, str]:
    """{fid: txt 路径} → {fid: txt_flaw}。结论按指纹缓存在 txt_layout（文件内容变了指纹就变）；没查过的多了用几个进程一起查。"""
    if not todo:
        return {}
    c = open_db(data)
    try:
        got = {r[0]: r[1] for r in c.execute("SELECT fid, flaw FROM txt_layout WHERE ver=?", (LAYOUT_VER,)) if r[0] in todo}
        new = [(f, p) for f, p in todo.items() if f not in got]
        if len(new) > 50:
            import os
            from concurrent.futures import ProcessPoolExecutor
            with ProcessPoolExecutor(min(6, os.cpu_count() or 2)) as ex:
                res = list(ex.map(txt_flaw, [p for _, p in new], chunksize=20))
        else:
            res = [txt_flaw(p) for _, p in new]
        now = time.time()
        c.executemany("INSERT OR REPLACE INTO txt_layout(fid, flaw, ver, at) VALUES(?,?,?,?)",
                      [(f, x, LAYOUT_VER, now) for (f, _), x in zip(new, res)])
        c.commit()
        return got | {f: x for (f, _), x in zip(new, res)}
    finally:
        c.close()


def main_fid(w: dict, fs: list[dict]) -> str | None:
    """书城展示用的那个文件（文字来源，没有就版式来源）。"""
    src = w["text_src"] or w["layout_src"]
    return next((f["fid"] for f in fs if f["path"] == src), fs[0]["fid"] if fs else None)


GATE = [SCANNED, QUALITY]    # 入库门禁：原文件读不好（扫描件、排版差、认不出字、不是正式的书），新书、存量书都过一遍


def plan(cat: Catalog, data: Path, rs: list[str], protect: bool = True, since: float = 0) -> list[dict]:
    """规则命中的作品，每行一部，带保护原因（protect=False：保护名单不管，全部标「排除」）。
    since：只看这个时间（秒）以后第一次编目的作品。排序：要排除的在前，按规则、口碑分从高到低。"""
    ctx = context(data)
    ws, fs = book_works(cat, kind=None)
    out = []
    vouch = {}                                         # 「排版差」只查没封面、没评分、没有出版迹象的 txt 图书
    if QUALITY in rs:
        for wid, w in ws.items():
            files = fs.get(wid, [])
            if (w["kind"] == "book" and (w["text_src"] or "").lower().endswith(".txt") and not (since and (w.get("added") or 0) < since)
                    and ctx["score"].get(main_fid(w, files)) is None and not {f["fid"] for f in files} & ctx["pub"]
                    and not covered(w, files, ctx)):
                vouch[wid] = next((f["fid"] for f in files if f["path"] == w["text_src"]), None)
    flaw = flaws(data, {f: ws[wid]["text_src"] for wid, f in vouch.items() if f})
    for wid, w in ws.items():
        if since and (w.get("added") or 0) < since:
            continue
        files = fs.get(wid, [])
        mf = main_fid(w, files)
        cat_ = ctx["cats"].get(mf) or next((ctx["cats"][f["fid"]] for f in files if f["fid"] in ctx["cats"]), {})
        rule = rule_of(rs, cat_.get("top"), cat_.get("sub"), w["title"], w, [f["path"] for f in files], files)
        if not rule and QUALITY in rs and w["kind"] == "book":
            why_q = quality_of(w, files, ctx["gate"].get(mf), cat_.get("top"), w.get("pages") or 0,
                               ctx["score"].get(mf) is not None, covered(w, files, ctx),
                               bool({f["fid"] for f in files} & ctx["pub"]), flaw.get(vouch.get(wid), ""))
            rule = f"{QUALITY}：{why_q}" if why_q else None
        if not rule:
            continue
        why = protected(w, files, ctx) if protect else None
        score = ctx["score"].get(mf)
        decide = f"保护：{why}" if why else "留" if rule == "教材" and (score or 0) >= TEXTBOOK_KEEP else "排除"
        out.append({"决定": decide, "规则": rule,
                    "口碑分": score, "作品": wid, "书名": w["title"], "作者": w["author"] or "",
                    "语言": w["lang"] or "", "类别": f"{cat_.get('top') or ''}/{cat_.get('sub') or ''}".strip("/"),
                    "分类来源": cat_.get("src") or "", "置信度": cat_.get("conf"),
                    "文件数": w["n_files"], "MB": round((w["bytes"] or 0) / 1e6, 1)})
    out.sort(key=lambda r: (r["决定"] != "排除", r["规则"], -(r["口碑分"] or 0), r["书名"] or ""))
    return out


def write_report(rows: list[dict], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, COLS, delimiter="\t", extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: ("" if v is None else v) for k, v in r.items()})


def read_report(path: Path) -> list[dict]:
    with open(path, newline="") as f:
        return [dict(r) for r in csv.DictReader(f, delimiter="\t")]


# ---------------------------------------------------------------- 记录与移动
def logs_dir(data: Path) -> Path:
    return data.parent / "logs"


def stamp() -> str:
    return time.strftime("%Y%m%d-%H%M%S")


def record(c, table: str, picks, why: str, rule: str, run: str | None = None) -> int:
    """picks：[(作品, 文件们)] 的身份键记进 blocked / kept。已有的键保留最早那次的记录（撤销时不会误删）。"""
    now = time.time()
    only = rule == SCANNED or rule.startswith(QUALITY)    # 不想要的是这份文件，不是这本书：只记指纹
    if table == "blocked":
        rows = [(k, v, why, rule, w["title"], w["author"], run, now)
                for w, fs in picks for f in fs for k, v in file_keys(f, only)]
        c.executemany("INSERT OR IGNORE INTO blocked(kind, key, why, rule, title, author, run, at) VALUES(?,?,?,?,?,?,?,?)", rows)
    else:
        rows = [(k, v, why, w["title"], w["author"], now) for w, fs in picks for f in fs for k, v in file_keys(f, only)]
        c.executemany("INSERT OR IGNORE INTO kept(kind, key, why, title, author, at) VALUES(?,?,?,?,?,?)", rows)
    c.commit()
    return len(rows)


def _dest(trash: Path, src: Path) -> Path:
    home = Path.home()
    rel = src.relative_to(home) if src.is_relative_to(home) else Path(src.name)
    d, i = trash / rel, 2
    while d.exists():
        d = (trash / rel).with_stem(f"{(trash / rel).stem} ({i})")
        i += 1
    return d


def move_files(cat: Catalog, data: Path, run: str, action: str, entries: list[dict], works: int = 0) -> tuple[int, int, Path]:
    """entries：[{path, size, hit, title}] 移到废纸篓，写清单；从编目里删掉这些文件、重新归并作品。"""
    trash = TRASH / f"英文书库-排除-{run}"
    log_path = logs_dir(data) / f"exclude-{run}.txt"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    moved, freed, gone = 0, 0, []
    with open(log_path, "w") as log:
        log.write(f"# run={run}\taction={action}\ttrash={trash}\n# 原路径\t废纸篓里的路径\t命中\t书名\n")
        for e in entries:
            src = Path(e["path"])
            if not src.exists():
                gone.append(e["path"])
                continue
            dest = _dest(trash, src)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dest))
            log.write(f"{src}\t{dest}\t{e['hit']}\t{e['title'] or ''}\n")
            gone.append(e["path"])
            moved += 1
            freed += e["size"] or 0
    with cat.lock:
        cat.conn.executemany("DELETE FROM files WHERE path=?", [(p,) for p in gone])
        cat.conn.commit()
    cat.rebuild()
    c = open_db(data)
    try:
        c.executemany("DELETE FROM held WHERE path=?", [(p,) for p in gone])
        c.execute("INSERT OR REPLACE INTO runs(id, action, works, files, bytes, manifest, at) VALUES(?,?,?,?,?,?,?)",
                  (run, action, works, moved, freed, str(log_path), time.time()))
        c.commit()
    finally:
        c.close()
    return moved, freed, log_path


def exclude_works(cat: Catalog, data: Path, picks: list[tuple[dict, list[dict], str, str]], action: str,
                  extra_keys: list[tuple[str, str, str, str]] = ()) -> tuple[int, int, Path]:
    """picks：[(作品, 文件们, 原因, 规则)]。先把身份键记进黑名单（移动中途出错，以后的导入也照样拦下），再移文件。
    extra_keys：另外要记的键 [(kind, key, 原因, 规则)]（整个作者排除）。"""
    run = stamp()
    c = open_db(data)
    try:
        for kind, key, why, rule in extra_keys:
            c.execute("INSERT OR IGNORE INTO blocked(kind, key, why, rule, run, at) VALUES(?,?,?,?,?,?)",
                      (kind, key, why, rule, run, time.time()))
        for w, fs, why, rule in picks:
            record(c, "blocked", [(w, fs)], why, rule, run)
    finally:
        c.close()
    entries = [{"path": f["path"], "size": f["size"], "hit": f"fid:{f['fid']}", "title": w["title"]}
               for w, fs, _, _ in picks for f in fs]
    return move_files(cat, data, run, action, entries, works=len(picks))


def sweep_entries(cat: Catalog, data: Path) -> list[dict]:
    """要移走的：编目时拦下的（还在原处的），加上编目里命中黑名单的文件（黑名单是后加的，或上次没移成）。"""
    blk = Blocklist.load(data)
    out = []
    if (data / DB_NAME).exists():
        c = open_db(data)
        try:
            for r in c.execute("SELECT path, kind, key, title FROM held"):
                p = Path(r["path"])
                if p.exists():
                    out.append({"path": r["path"], "size": p.stat().st_size, "hit": f"{r['kind']}:{r['key']}",
                                "title": r["title"]})
                else:
                    c.execute("DELETE FROM held WHERE path=?", (r["path"],))
            c.commit()
        finally:
            c.close()
    if blk:
        for f in cat.q("SELECT path, fid, kind, isbn, title, author, mag, size FROM files"):
            k = blk.hit(f)
            if k:
                out.append({"path": f["path"], "size": f["size"], "hit": f"{k[0]}:{k[1]}", "title": f["title"]})
    return out


def undo(cat: Catalog, data: Path, manifest: Path, apply: bool, titles: list[str] = (), keep: bool = False) -> int:
    """清单里的文件移回原处（原处已有同名文件的跳过），撤掉这次加的黑名单和清单里命中的键。
    titles：只撤销这几部作品（书名和清单里的一字不差）。keep：撤掉的黑名单键记进 kept，规则以后不再报这些书。"""
    lines = manifest.read_text().splitlines()
    head = dict(x.split("=", 1) for x in lines[0].lstrip("# ").split("\t") if "=" in x)
    run = head.get("run")
    rows = [l.split("\t") for l in lines if l and not l.startswith("#")]
    if titles:
        rows = [r for r in rows if len(r) > 3 and r[3] in titles]
        missing = set(titles) - {r[3] for r in rows}
        if missing:
            print(f"清单里没有这些书名：{'、'.join(sorted(missing))}")
    back = [(Path(r[0]), Path(r[1])) for r in rows if Path(r[1]).exists() and not Path(r[0]).exists()]
    hits = {tuple(r[2].split(":", 1)) for r in rows if ":" in r[2]}
    only = f" AND title IN ({','.join('?' * len(titles))})" if titles else ""
    c = open_db(data)
    try:
        n_run = c.execute(f"SELECT COUNT(*) FROM blocked WHERE run=?{only}", (run, *titles)).fetchone()[0]
        print(f"清单 {manifest.name}：移回 {len(back):,} 个文件（{len(rows) - len(back)} 个不在废纸篓或原处已有，跳过）；"
              f"撤掉这次加的黑名单 {n_run:,} 条、清单里命中的键 {len(hits):,} 个")
        if not apply:
            print("加 --apply 执行")
            return 0
        for src, dest in back:
            src.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(dest), str(src))
        if keep:
            c.execute("INSERT OR IGNORE INTO kept(kind, key, why, title, author, at) SELECT kind, key, '撤销排除并留下', "
                      f"title, author, ? FROM blocked WHERE run=?{only} AND kind != 'author'", (time.time(), run, *titles))
        c.execute(f"DELETE FROM blocked WHERE run=?{only}", (run, *titles))
        c.executemany("DELETE FROM blocked WHERE kind=? AND key=?", list(hits))
        c.executemany("DELETE FROM held WHERE path=?", [(str(s),) for s, _ in back])
        c.commit()
    finally:
        c.close()
    print("完成。重新编目（python -m library.catalog）后这些书回到书库")
    return 0


# ---------------------------------------------------------------- 命令行
def _arg(argv: list[str], name: str, default=None):
    if name in argv:
        i = argv.index(name)
        if i + 1 < len(argv):
            v = argv[i + 1]
            del argv[i:i + 2]
            return v
    return default


def _picks(cat: Catalog, ids: list[str]) -> list[tuple[dict, list[dict]]]:
    ws, fs = book_works(cat, kind=None)
    missing = [i for i in ids if i not in ws]
    if missing:
        print(f"编目里没有这些作品（重新编目后编号可能变了）：{', '.join(missing[:10])}")
    return [(ws[i], fs.get(i, [])) for i in ids if i in ws]


def _summary(picks, label: str) -> None:
    n = sum(len(fs) for _, fs in picks)
    size = sum(f["size"] or 0 for _, fs in picks for f in fs)
    print(f"{label}：{len(picks):,} 部作品、{n:,} 个文件，约 {size / 1e9:.1f} GB")
    for w, fs in picks[:10]:
        print(f"  {w['title']} · {w['author'] or '—'}（{len(fs)} 个文件）")


def _locked(fn):
    from . import busy
    try:
        with busy.hold("exclude", kind="io"):
            return fn()
    except busy.Busy as ex:
        print(f"有长任务在跑，这次不动：{ex}")
        return 1


def main(argv: list[str]) -> int:
    from .config import load
    argv = list(argv)
    cfg = load()
    data = cfg.data
    apply = "--apply" in argv
    argv = [a for a in argv if a != "--apply"]
    cmd = argv.pop(0) if argv else "stats"
    cat = Catalog(data / "catalog.db")

    if cmd == "plan":
        rs = rules(data)
        if not rs:
            print('config.json 里没有 "exclude" 规则（写法同 "taste_avoid"）')
            return 1
        out = Path(_arg(argv, "--out") or logs_dir(data) / f"exclude-plan-{stamp()}.tsv")
        rows = plan(cat, data, rs, protect="--no-protect" not in argv)
        write_report(rows, out)
        todo = [r for r in rows if r["决定"] == "排除"]
        print(f"规则 {', '.join(rs)}：命中 {len(rows):,} 部，要排除 {len(todo):,} 部（{sum(r['MB'] for r in todo) / 1e3:.1f} GB），"
              f"保护 {len(rows) - len(todo):,} 部")
        for rule, n in Counter(r["规则"] for r in todo).most_common():
            print(f"  {rule}：{n:,}")
        for why, n in Counter(r["决定"] for r in rows if r["决定"] != "排除").most_common():
            print(f"  {why}：{n:,}")
        print(f"报告：{out}\n复核：要留的那行「决定」改成「留」，然后 python -m library.exclude review {out} --apply")
        return 0

    if cmd == "gate":
        since = float(_arg(argv, "--since", 0) or 0)
        rows = plan(cat, data, GATE, since=since)
        ex = [r for r in rows if r["决定"] == "排除"]
        scope = f"{time.strftime('%m-%d %H:%M', time.localtime(since))} 以后编目的" if since else "全部"
        print(f"入库门禁（{scope}作品）：排除 {len(ex):,} 部（{sum(r['MB'] for r in ex) / 1e3:.1f} GB），"
              f"保护 {len(rows) - len(ex):,} 部（在读、豆瓣记录、获奖…，留着，要删用 add）")
        for rule, n in Counter(r["规则"] for r in ex).most_common():
            print(f"  {rule}：{n:,}")
        if not ex:
            return 0
        report = logs_dir(data) / f"exclude-gate-{stamp()}.tsv"
        write_report(rows, report)
        print(f"报告：{report}")
        if not apply:
            print("加 --apply 执行")
            return 0
        ws, fs = book_works(cat, kind=None)
        picks = [(ws[r["作品"]], fs.get(r["作品"], []), "入库门禁", r["规则"]) for r in ex if r["作品"] in ws]

        def go():
            moved, freed, log = exclude_works(cat, data, picks, "gate")
            print(f"完成：移到废纸篓 {moved:,} 个文件（{freed / 1e9:.1f} GB）\n清单：{log}")
            return 0
        return _locked(go)

    if cmd == "review":
        path = Path(argv[0])
        rows = read_report(path)
        ws, fs = book_works(cat, kind=None)
        ex = [r for r in rows if r["决定"].strip() == "排除"]
        kp = [r for r in rows if r["决定"].strip() == "留"]
        missing = [r["作品"] for r in ex + kp if r["作品"] not in ws]
        if missing:
            print(f"⚠️ {len(missing)} 部作品编目里已经没有了（重新编目后编号可能变了，或已经移走），跳过")
        ex_p = [(ws[r["作品"]], fs.get(r["作品"], []), "规则", r["规则"]) for r in ex if r["作品"] in ws]
        kp_p = [(ws[r["作品"]], fs.get(r["作品"], []), r["规则"]) for r in kp if r["作品"] in ws]
        _summary([(w, f) for w, f, _, _ in ex_p], "排除")
        print(f"留：{len(kp_p):,} 部")
        if not apply:
            print("加 --apply 执行")
            return 0

        def go():
            c = open_db(data)
            try:
                for w, f, rule in kp_p:                        # 扫描版留下的只记指纹，同上
                    record(c, "kept", [(w, f)], "复核时留下", rule)
            finally:
                c.close()
            mags = {(r["规则"], mag_key(r["规则"][4:])) for r in ex if r["规则"].startswith("mag:")}
            moved, freed, log = exclude_works(cat, data, ex_p, "review",       # 整个刊排除：以后的新一期编目时也拦下
                                              extra_keys=[("mag", k, "整个刊排除", r) for r, k in mags if k])
            print(f"完成：移到废纸篓 {moved:,} 个文件（{freed / 1e9:.1f} GB）\n清单：{log}")
            return 0
        return _locked(go)

    if cmd == "add":
        why = _arg(argv, "--why", "手动排除")
        picks = _picks(cat, argv)
        _summary(picks, "排除")
        if not apply:
            print("加 --apply 执行")
            return 0

        def go():
            moved, freed, log = exclude_works(cat, data, [(w, fs, why, "手动") for w, fs in picks], "add")
            print(f"完成：移到废纸篓 {moved:,} 个文件（{freed / 1e9:.1f} GB）\n清单：{log}")
            return 0
        return _locked(go)

    if cmd == "author":
        name = " ".join(argv)
        key = author_key(name)
        if not key:
            print("作者名是空的")
            return 1
        ws, fs = book_works(cat)
        picks = [(w, fs.get(i, [])) for i, w in ws.items() if author_key(w["author"] or "") == key]
        _summary(picks, f"作者 {name}（{key}）")
        if not apply:
            print("加 --apply 执行（以后这个作者的书编目时也会拦下）")
            return 0

        def go():
            rule = f"作者：{name}"
            moved, freed, log = exclude_works(cat, data, [(w, f, "整个作者排除", rule) for w, f in picks], "author",
                                              extra_keys=[("author", key, "整个作者排除", rule)])
            print(f"完成：移到废纸篓 {moved:,} 个文件（{freed / 1e9:.1f} GB）\n清单：{log}")
            return 0
        return _locked(go)

    if cmd == "keep":
        why = _arg(argv, "--why", "手动留下")
        paths = _arg(argv, "--paths")
        ids = list(argv)
        if paths:
            want = [l.strip() for l in Path(paths).read_text().splitlines() if l.strip() and not l.startswith("#")]
            found = {r["path"]: r["work"] for r in cat.q("SELECT path, work FROM files WHERE work IS NOT NULL")}
            ids += [found[p] for p in want if p in found]
            print(f"清单 {len(want)} 个路径，编目里找到 {sum(p in found for p in want)} 个")
        picks = _picks(cat, list(dict.fromkeys(ids)))
        c = open_db(data)
        try:
            n = record(c, "kept", picks, why, "")
        finally:
            c.close()
        print(f"记为留着：{len(picks):,} 部作品（{n:,} 个键）")
        return 0

    if cmd == "sweep":
        entries = sweep_entries(cat, data)
        print(f"要移到废纸篓：{len(entries):,} 个文件，约 {sum(e['size'] or 0 for e in entries) / 1e9:.1f} GB")
        for e in entries[:10]:
            print(f"  {e['path']}  ← {e['hit']}")
        if not apply or not entries:
            print("加 --apply 执行" if entries else "没有要移走的")
            return 0

        def go():
            moved, freed, log = move_files(cat, data, stamp(), "sweep", entries)
            print(f"完成：移到废纸篓 {moved:,} 个文件（{freed / 1e9:.1f} GB）\n清单：{log}")
            return 0
        return _locked(go)

    if cmd == "undo":
        titles = []
        while "--title" in argv:
            titles.append(_arg(argv, "--title"))
        keep = "--keep" in argv
        argv = [a for a in argv if a != "--keep"]
        run = lambda: undo(cat, data, Path(argv[0]), apply, titles, keep)    # noqa: E731
        return _locked(run) if apply else run()

    if cmd == "stats":
        if not (data / DB_NAME).exists():
            print("还没有排除过书")
            return 0
        c = open_db(data)
        try:
            for r in c.execute("SELECT kind, COUNT(*) n FROM blocked GROUP BY kind"):
                print(f"黑名单 {r['kind']}：{r['n']:,}")
            print(f"留着：{c.execute('SELECT COUNT(*) FROM kept').fetchone()[0]:,} 个键")
            print(f"编目时拦下、等 sweep：{c.execute('SELECT COUNT(*) FROM held').fetchone()[0]:,} 个文件")
            for r in c.execute("SELECT * FROM runs ORDER BY at DESC LIMIT 10"):
                print(f"  {r['id']} {r['action']}：{r['works'] or 0:,} 部、{r['files']:,} 个文件、"
                      f"{(r['bytes'] or 0) / 1e9:.1f} GB  {r['manifest']}")
        finally:
            c.close()
        return 0

    print(__doc__)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
