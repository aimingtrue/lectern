"""同一本书：同一作品的不同版本、译本、副本归成一组（交给 library.dedupe 每组只留一份）。不跨语言合并：中文版和原版各留一份。

  python -m library.match build                                 重建分组（只读 catalog.db、library.db），打印统计
  python -m library.match show [--level review|auto] [--limit N]  看分组

判定（书名先繁转简、去版本字样，切成主书名 + 副标题）：
  同一 ISBN（opf / exth / 编目里的，或与 CIP 里的一致）且书名相容、套装与否一致 → 自动；否则待确认
  同语言、同主书名、同卷号：作者相同（外国作者的中文姓相同也算：伏尼契的各种译名）→ 自动；一方没有作者 → 待确认；作者不同 → 不是同一本
  副标题两边都有且不同 → 不是同一本；只有一边有：带卷号、或外文书的副标题不是「A Novel」这类泛称 → 待确认
  套装和单本 → 待确认；一组里出现两个不同的副标题（经由没有副标题的那本连起来）→ 整组待确认
  待确认的两本正文都已入库（library.db）时比对全书正文：相同 → 自动（只升不降：不同译本正文本来就不同）

数据在 data/biblio.db：
  same_book          gid（组员 fid 排序后 sha1 前 16 位）、fid（文字来源，没有用版式来源）、work、level（auto / review）、
                     reason（JSON 列表）、score（组内比对过的正文相似度最低值，没比对为空）、built
  same_book_review   gid → 人工确认 same / different；重建时保留（组员变了 gid 就变，旧确认自然不再适用）
  same_book_final    视图：全自动且没被否定的组 + 确认是同一本的组（gid, fid, work），一个 gid 是一本书
"""
from __future__ import annotations

from contextlib import nullcontext

from . import busy

import hashlib
import heapq
import json
import re
import sqlite3
import sys
import time
import unicodedata
from collections import Counter, defaultdict
from pathlib import Path

from . import digest
from .biblio import DB_NAME, _ro, trusted_isbns
from .biblio import open_db as biblio_db

SAME = 0.6                      # 正文相似度（切片的 Jaccard）≥ 这个值算同一份正文
SHINGLE, SKETCH = 5, 1024       # 中文 5 个字一片、外文 3 个词一片；每本书留哈希最小的 1024 片估算 Jaccard（bottom-k）
MIN_BODY = 2000                 # 正文（去掉头尾）不到 2000 字（外文 500 词）的不比
TITLE_BUCKET = 12               # 同一主书名超过这么多本的（「诗选」「Poems」）不按书名配对
ISBN_BUCKET = 6                 # 同一 ISBN 超过这么多本的多半是丛书号，不用


def open_db(path: Path) -> sqlite3.Connection:
    return biblio_db(path)                              # 表结构见 library/schemas/biblio.py


# ---------------------------------------------------------------- 繁转简
_cc = None


def t2s(s: str) -> str:
    global _cc
    if not s or not re.search(r"[㐀-鿿]", s):
        return s or ""
    if _cc is None:
        from opencc import OpenCC
        _cc = OpenCC("t2s")
    return _cc.convert(s)


# ---------------------------------------------------------------- 书名
EDITION = (r"(?:修订|增订|典藏|珍藏|纪念|插图|全译|精装|平装|彩图|注音|新|最新|完整|未删减|全本|足本|中英双语|双语|全新|升级|再版|"
           r"第[一二三四五六七八九十\d]+)版|(?:插图|全译|典藏|珍藏|精装|平装|修订|增订|校注|评注|彩图)本|定本")
SET = re.compile(r"(?<!\d)\d{1,2}\s*[-–~～至]\s*\d{1,2}(?:册|部|季)?\s*$|套装|全集|合集|(?:全|共)\s*[\d一二三四五六七八九十]+\s*(?:册|卷|本|部)|"
                 r"\d+\s*册|作品集|文集", re.I)
SERIES = re.compile(r"[（(\[【](?![^）)\]】]*(?:精编|完整|珍藏|典藏|套装|全新|足本|插图|合订))"     # 括号里的丛书名：「(毛姆文集)」
                    r"[^）)\]】]{2,}(?:全集|作品集|文集)[^）)\]】]*[）)\]】]")                        # 「（张爱玲全集04—小说）」；「（精编全集）」是合订本
VOL = re.compile(r"(?:第\s*([\d一二三四五六七八九十]+)\s*(?:卷|册|部|辑|季)|[卷册]\s*([\d一二三四五六七八九十]+)|"
                 r"(?:vol(?:ume)?|book|part)\.?\s*(\d+|[ivx]+\b|one|two|three|four|five)|[（(]\s*([\d一二三四五六七八九十]+)\s*[)）]$|"
                 r"\s(\d{1,2})$)", re.I)
ADAPT = re.compile(r"漫画|绘本版|连环画|青少版|青少年版|少儿版|儿童版|简写版|缩写版|改写版|注音版|彩绘版|图画版|"
                   r"graphic novel|retold|abridged", re.I)
ADAPT_AUTHOR = re.compile(r"改编|编绘|改写|adapted", re.I)          # 「著绘」是作者自己画的，不算改编
VOL_SUB = re.compile(r"vol|volume|book|part|卷|册|部|篇|辑|第|#\d")        # 副标题里有这些：多半是分册
GENERIC = re.compile(r"^(?:a|an|the)?(?:novel|thriller|memoir|mystery|novella|romance|story|stories|novelofsuspense|"
                     r"thrillernovel|reallife.*|truestory.*|biography|autobiography)$")
CN = {c: i for i, c in enumerate("零一二三四五六七八九十")}
WORDNUM = {"i": 1, "ii": 2, "iii": 3, "iv": 4, "v": 5, "vi": 6, "vii": 7, "viii": 8, "ix": 9, "x": 10,
           "one": 1, "two": 2, "three": 3, "four": 4, "five": 5}


def _num(s: str | None) -> int | None:
    if not s:
        return None
    if s.lower() in WORDNUM:
        return WORDNUM[s.lower()]
    if s.isdigit():
        return int(s)
    if len(s) == 1:
        return CN.get(s)
    return 10 + CN.get(s[-1], 0) if s.startswith("十") else None


def _clean(x: str) -> str:
    return re.sub(r"[\s《》「」『』“”\"'’·•.,，、！!？?;；]+", "", re.sub(r"^(the|a|an)\s+", "", x.strip()))


def title_key(t: str) -> tuple[tuple[str, str], int | None, bool]:
    """((主书名, 副标题), 卷号, 是否套装)。版本字样（修订版、插图本…）和括号里的说明去掉：不同版本算同一本书。"""
    t = t2s(unicodedata.normalize("NFKC", t or "")).lower()
    b = re.match(r"^\s*[\[【]([^\]】]{2,})[\]】](.*)$", t)
    tail_vol = None
    if b and re.match(r"^\s*(?:[.．]|\[\d+页\]|$)", b.group(2)):     # 「[书名].作者.扫描版」「[书名][247页]02」；「[管理]《…》」不算
        tail = re.search(r"(\d{1,2})\s*$", b.group(2))
        tail_vol = int(tail.group(1)) if tail else None              # 末尾的 02、03：分册
        t = b.group(1)                                             # 书名在括号里
    m = VOL.search(t)
    vol = _num(next(g for g in m.groups() if g)) if m else tail_vol
    is_set = bool(SET.search(SERIES.sub(" ", t)))          # 「1939-1945」是年份不是卷号；丛书名不算套装
    t = re.sub(r"[（(\[【][^）)\]】]*[）)\]】]", " ", t)              # 括号里的说明（版本、译者、出版社…）
    t = re.sub(EDITION, " ", t)
    parts = re.split(r"[:：—–]|\s-\s|——", t, maxsplit=1)
    return (_clean(parts[0]), _clean(parts[1]) if len(parts) > 1 else ""), vol, is_set


# ---------------------------------------------------------------- 作者
JUNK_AUTHOR = re.compile(r"^(?:administrators?|admin|(?:ms |lenovo )?user|微软用户|未知(?:作者)?|佚名|匿名|unknown|anonymous|"
                         r"bexp|ylmf|cnki|作者|请自己补充|www\.\S*|[\w.\-]+\.(?:com|net|cn|org))$", re.I)


def author_key(a: str) -> str:
    """第一作者：去掉国籍括号 [英]【美】（日）、著/译/编这类角色；制作软件、机构账号、网站之类的「作者」当作没有。"""
    a = t2s(unicodedata.normalize("NFKC", a or "")).replace("\x00", "").lower()
    a = re.sub(r"[\[【（(〔][^\]】）)〕]{1,8}[\]】）)〕]", "", a)
    a = re.split(r"[;；,，、/&]|\s+and\s+| 著| 译| 编|著|译|编著|主编|原著|改编", a)[0].strip()
    return "" if JUNK_AUTHOR.match(a) else re.sub(r"[\s·•．.\-_'’]", "", a)


def auth_same(a: str, b: str) -> bool | None:
    """同一作者 True；不同 False；有一方没有 None。前缀相同（至少 2 个字）也算：「刘瑜」与「刘瑜编」。"""
    if not a or not b:
        return None
    return a == b or (min(len(a), len(b)) >= 2 and (a.startswith(b) or b.startswith(a)))


def same_author(x: dict, y: dict) -> bool | None:
    """两本书的作者：auth_same，外国作者的中文姓相同也算同一人（伏尼契的各种译名）。"""
    s = auth_same(x["ak"], y["ak"])
    return True if s is False and x.get("sur") and x["sur"] == y.get("sur") else s


def foreign_surname(a: str | None) -> str:
    """外国作者的中文姓：「埃塞尔·莉莲·伏尼契(E. L. Voynich)」「艾•丽•伏尼契」「伏尼契」→ 伏尼契。
    去掉国籍和外文括号、著/译这类角色，取最后一个间隔号后面的部分（没有间隔号取整个名字），要 2 个以上汉字。"""
    a = t2s(unicodedata.normalize("NFKC", a or ""))
    a = re.sub(r"[\[【（(〔][^\]】）)〕]*[\]】）)〕]", "", a)
    a = re.split(r"[;；,，、/&]|\s+and\s+| 著| 译| 编|著|译|编著|主编|原著|改编", a)[0].strip()
    s = re.split(r"[·•．・.]", a)[-1].strip()
    return s if re.fullmatch(r"[㐀-鿿]{2,}", s) else ""


def lang_class(lang: str | None) -> str:
    return "zh" if lang == "zh" else "other"


def prepare(w: dict) -> dict:
    w["tk"], w["vol"], w["set"] = title_key(w["title"])
    w["ak"] = author_key(w["author"])
    w["sur"] = foreign_surname(w["author"])
    w["adapt"] = bool(ADAPT.search(t2s(unicodedata.normalize("NFKC", w["title"] or "")))
                      or ADAPT_AUTHOR.search(t2s(unicodedata.normalize("NFKC", w["author"] or ""))))
    w["lc"] = lang_class(w["lang"])
    w.setdefault("isbn", set())
    return w


def compat(x: dict, y: dict) -> tuple[bool | str, str]:
    """书名是否可能是同一本：(True | 'review' | False, 原因)。主书名、卷号相同；副标题要么一样，要么只有一边有。"""
    (m1, s1), (m2, s2) = x["tk"], y["tk"]
    if m1 != m2 or x["vol"] != y["vol"]:
        return False, "书名不同"
    if x.get("adapt") != y.get("adapt"):                        # 漫画版、青少版、改编本是另一部作品
        return False, "改编作品"
    if s1 and s2 and s1 != s2:
        return False, "副标题不同"
    other = "" if s1 == s2 else (s1 or s2)
    if other and VOL_SUB.search(other):
        return "review", "副标题带卷号"
    if other and x["lc"] != "zh" and not GENERIC.match(other):      # 外文书「系列名: 书名」很常见
        return "review", "一方有副标题"
    return True, ""


# ---------------------------------------------------------------- 配对
def pairs(works: list[dict]) -> dict[tuple[str, str], dict]:
    """{(作品a, 作品b): {"level": auto|review, "why": {原因}}}。同一对有一条自动的依据就算自动。"""
    out: dict = {}

    def add(x, y, level, why):
        if x["id"] == y["id"] or x["lc"] != y["lc"]:                # 不跨语言
            return
        k = tuple(sorted((x["id"], y["id"])))
        p = out.setdefault(k, {"level": level, "why": set(), "score": None})
        if level == "auto" and p["level"] != "auto":
            p["level"], p["why"] = "auto", set()
        if level == p["level"]:
            p["why"].add(why)

    by_isbn = defaultdict(list)
    for w in works:
        for i in w["isbn"]:
            by_isbn[i].append(w)
    for ws in by_isbn.values():
        if 1 < len(ws) <= ISBN_BUCKET:
            for x in ws[1:]:
                ok, _ = compat(x, ws[0])
                if ok is True and x["set"] == ws[0]["set"]:
                    add(ws[0], x, "auto", "同一 ISBN")
                else:
                    add(ws[0], x, "review", "同一 ISBN、书名不同")
    by_t = defaultdict(list)
    for w in works:
        if len(w["tk"][0]) >= 2:
            by_t[(w["lc"], w["tk"][0], w["vol"])].append(w)
    for ws in by_t.values():
        if not 1 < len(ws) <= TITLE_BUCKET:
            continue
        for a in range(len(ws)):
            for b in range(a + 1, len(ws)):
                x, y = ws[a], ws[b]
                ok, why = compat(x, y)
                if ok is False:
                    continue
                s = same_author(x, y)
                if s is False:
                    continue
                if ok == "review":
                    add(x, y, "review", why)
                elif x["set"] != y["set"]:
                    add(x, y, "review", "套装和单本")
                elif s:
                    add(x, y, "auto", "书名、作者相同")
                else:
                    add(x, y, "review", "书名相同、一方没有作者")
    # 合集和分册：主书名、作者相同，一边是合集（没有卷号）、一边是分册 —— 只进待确认（精编、选集可能不全）
    by_main = defaultdict(list)
    for w in works:
        if len(w["tk"][0]) >= 2 and w["ak"] and not w.get("adapt"):
            by_main[(w["lc"], _clean(VOL.sub("", w["tk"][0])), w["ak"])].append(w)    # 分册的卷号去掉再比
    for ws in by_main.values():
        sets = [w for w in ws if w["set"] and w["vol"] is None]
        vols = [w for w in ws if w["vol"] is not None]
        if sets and vols and len(sets) + len(vols) <= TITLE_BUCKET:
            for st in sets:
                for v in vols:
                    add(st, v, "review", "合集和分册")
    return out


def groups(works: list[dict], prs: dict) -> list[dict]:
    """并查集成组：[{gid, level, why, score, works: [作品…]}]。"""
    par: dict = {}

    def f(x):
        par.setdefault(x, x)
        while par[x] != x:
            par[x] = par[par[x]]
            x = par[x]
        return x
    for a, b in prs:
        ra, rb = f(a), f(b)
        if ra != rb:
            par[ra] = rb
    by = defaultdict(list)
    for k, p in prs.items():
        by[f(k[0])].append(p)
    members = defaultdict(set)
    for a, b in prs:
        members[f(a)].update((a, b))
    W = {w["id"]: w for w in works}
    out = []
    for r, ps in by.items():
        ws = sorted((W[i] for i in members[r]), key=lambda w: w["fid"])
        why = {y for p in ps for y in p["why"]}
        level = "auto" if all(p["level"] == "auto" for p in ps) else "review"
        wa = [w for w in ws if w["ak"]]
        if any(same_author(a, b) is False for i, a in enumerate(wa) for b in wa[i + 1:]):   # 经由没写作者的那本串起来的
            level = "review"
            why.add("组内作者不同")
        if len({w["tk"][1] for w in ws if w["tk"][1]}) > 1:      # 多半是同系列的不同分册，经由没有副标题的那本连起来
            level = "review"
            why.add("组内副标题不同")
        if len({bool(w.get("flip")) or not w.get("has_text", 1) for w in ws}) > 1:   # 一份只能翻阅、一份是文字：绘本画册的内容在图上
            level = "review"
            why.add("翻阅版和文字版")
        scores = [p["score"] for p in ps if p["score"] is not None]
        out.append({"gid": gid_of([w["fid"] for w in ws]), "level": level, "why": sorted(why),
                    "score": min(scores) if scores else None, "works": ws})
    return out


def gid_of(fids: list[str]) -> str:
    return hashlib.sha1(",".join(sorted(fids)).encode()).hexdigest()[:16]


# ---------------------------------------------------------------- 正文比对
PUNCT = re.compile(r"[\W_]+")
CJK = re.compile(r"[㐀-鿿]")


def norm_text(s: str) -> str | list[str]:
    """NFKC、繁转简、小写。中文：去掉空白和标点的字串（按字切片）；外文：词列表（按词切片）。
    外文去空格后的 5 个字母太短，同一作者的不同书也有 0.2–0.5 的重合；3 个词和 5 个汉字长度相当。"""
    s = t2s(unicodedata.normalize("NFKC", s or "")).lower()
    if len(CJK.findall(s[:20000])) > 1000:
        return PUNCT.sub("", s)
    return re.findall(r"[^\W_]+", s)


def body(lib: sqlite3.Connection, fid: str) -> str | list[str] | None:
    """书的正文（归一化后），去掉前 10%（版权页、序、目录）和后 5%（后记、广告）；全文没存或太短返回 None。"""
    rows = lib.execute("SELECT text FROM paras WHERE book=? AND kind='p' ORDER BY n", (fid,)).fetchall()
    return digest.body_text(r[0] for r in rows)


def _h(s: str) -> int:
    """稳定的 64 位哈希（内置 hash() 每个进程加的盐不同，存下来的指纹就没法比了）。"""
    return int.from_bytes(hashlib.blake2b(s.encode(), digest_size=8).digest(), "big")


def sketch(t: str | list[str]) -> list[int]:
    """全书切片（5 个字 / 3 个词）哈希里最小的 SKETCH 个：样本与开头长短、所在位置无关。"""
    if isinstance(t, str):
        hs = {_h(t[i:i + SHINGLE]) for i in range(len(t) - SHINGLE + 1)}
    else:
        hs = {_h(f"{t[i]} {t[i + 1]} {t[i + 2]}") for i in range(len(t) - 2)}
    return heapq.nsmallest(SKETCH, hs)


def similar(a: list[int], b: list[int]) -> float:
    """bottom-k 估算的 Jaccard：两本合起来最小的 k 个哈希里，两本都有的比例。"""
    sa, sb = set(a), set(b)
    u = heapq.nsmallest(SKETCH, sa | sb)
    return sum(1 for h in u if h in sa and h in sb) / len(u) if u else 0.0


def check_content(prs: dict, fid: dict, lib: sqlite3.Connection) -> dict:
    """待确认的配对：两本都有指纹（摘要里的，没有摘要时按全文现算）就比对，相同则升为自动。返回统计。"""
    st = {"pairs": 0, "checked": 0, "missing": 0, "upgraded": 0, "scores": []}
    cache: dict = {}

    def sk(w):
        f = fid[w]
        if f not in cache:
            d = digest.read(lib, f)
            if d is not None:
                cache[f] = digest.unpack(d["sketch"]) if d["sketch"] else None
            else:
                t = body(lib, f)
                cache[f] = sketch(t) if t else None
        return cache[f]
    for (a, b), p in prs.items():
        if p["level"] != "review":
            continue
        st["pairs"] += 1
        x, y = sk(a), sk(b)
        if x is None or y is None:
            st["missing"] += 1
            continue
        p["score"] = round(similar(x, y), 3)
        st["checked"] += 1
        st["scores"].append(p["score"])
        if p["score"] >= SAME:
            p["level"], p["why"] = "auto", {"正文相同"}
            st["upgraded"] += 1
    return st


# ---------------------------------------------------------------- 建表
def load(data_dir: Path) -> list[dict]:
    cat = _ro(data_dir / "catalog.db")
    try:
        works = [dict(r) for r in cat.execute(
            "SELECT w.id, w.title, w.author, w.lang, w.has_text, w.text_src IS NULL AS flip, f.fid FROM works w "
            "JOIN files f ON f.path=COALESCE(w.text_src, w.layout_src) WHERE w.kind='book' AND f.fid IS NOT NULL")]
    finally:
        cat.close()
    trusted = defaultdict(set)
    bp = data_dir / DB_NAME
    if bp.exists():
        bib = _ro(bp)
        try:
            for fid, isbns in trusted_isbns(bib).items():
                trusted[fid].update(isbns)
        finally:
            bib.close()
    for w in works:
        w["isbn"] = trusted.get(w["fid"], set())
        prepare(w)
    return works


def _level_counts(gs: list[dict]) -> tuple[Counter, Counter]:
    n, rm = Counter(), Counter()
    for g in gs:
        n[g["level"]] += 1
        rm[g["level"]] += len(g["works"]) - 1
    return n, rm


def build(data_dir: Path, content: bool = True) -> dict:
    t0 = time.time()
    works = load(data_dir)
    prs = pairs(works)
    before = _level_counts(groups(works, prs))
    cst = None
    lp = data_dir / "library.db"
    if content and lp.exists():
        lib = _ro(lp)
        try:
            cst = check_content(prs, {w["id"]: w["fid"] for w in works}, lib)
        finally:
            lib.close()
    gs = groups(works, prs)
    now = time.time()
    c = open_db(data_dir / DB_NAME)
    try:
        c.execute("DELETE FROM same_book")                        # 和下面的插入在同一个事务里：读的一方看不到半张表
        c.executemany("INSERT INTO same_book(gid,fid,work,level,reason,score,built) VALUES(?,?,?,?,?,?,?)",
                      [(g["gid"], w["fid"], w["id"], g["level"], json.dumps(g["why"], ensure_ascii=False), g["score"], now)
                       for g in gs for w in g["works"]])
        c.commit()
        decided = Counter(r[0] for r in c.execute(
            "SELECT r.decision FROM same_book_review r WHERE r.gid IN (SELECT gid FROM same_book)"))
        final = c.execute("SELECT COUNT(DISTINCT gid), COUNT(*) FROM same_book_final").fetchone()
    finally:
        c.close()
    after = _level_counts(gs)
    return {"works": len(works), "pairs": len(prs), "before": before, "after": after, "content": cst,
            "reasons": Counter(y for g in gs for y in g["why"]), "decided": decided,
            "final_groups": final[0], "final_removable": final[1] - final[0],
            "max_group": max((len(g["works"]) for g in gs), default=0), "seconds": round(time.time() - t0, 1)}


# ---------------------------------------------------------------- 读取（命令行、确认页共用）
FRONT = re.compile(r"isbn|版权|出版|印刷|印张|印数|开本|定价|字数|书号|发行|授权|电话|传真|责任编辑|在版编目|[ⅠⅡⅢⅣ]\s*[.．]|"
                   r"公众号|微信|请勿商用|商业用途|仅供|内部交流|整理|制作|著作权|@|http|www\.|copyright|all rights reserved|"
                   r"publish|printed in|library of congress|random house|trademark|a division of|imprint|reproduced|目录|contents|"
                   r"[.…⋯·]{4,}|^(?:[ivxlc]+\s+){4,}", re.I)
TOC = re.compile(r"第\s*[\d一二三四五六七八九十百]+\s*[章回节篇部卷]|chapter\s+\w+|[⋯…]+\s*\(\d+\)", re.I)
GARBLED = re.compile(r"[\u0080-\u00ff]")                 # GBK 正文按 Latin-1 解出来的乱码（ºï×Ó±¾…）


def snippet(lib: sqlite3.Connection, fid: str, n: int = 120) -> str:
    """正文开头一小段（跳过书名、作者行、版权页、目录、全大写的标题行和乱码）；没入库返回空串。
    n 不超过 digest.HEAD_CHARS 时读摘要，否则（或没有摘要）按全文现算。"""
    d = digest.read(lib, fid) if n <= digest.HEAD_CHARS else None
    if d is not None:
        out = d["head"] or ""
    else:
        rows = lib.execute("SELECT text FROM paras WHERE book=? AND kind='p' ORDER BY n LIMIT ?",
                           (fid, digest.HEAD_PARAS))
        out = digest.head_text((r[0] for r in rows), n)
    return out[:n].rstrip() + "…" if len(out) > n else out


def show(data_dir: Path, level: str | None = None, limit: int = 30) -> None:
    c = _ro(data_dir / DB_NAME)
    cat = _ro(data_dir / "catalog.db")
    try:
        rows = c.execute("SELECT s.*, r.decision FROM same_book s LEFT JOIN same_book_review r ON r.gid=s.gid "
                         + ("WHERE s.level=? " if level else "") + "ORDER BY s.gid", (level,) if level else ()).fetchall()
        by = defaultdict(list)
        for r in rows:
            by[r["gid"]].append(r)
        mark = {"same": "已确认同一本", "different": "已确认不是", None: ""}
        for gid, ms in list(by.items())[:limit]:
            r0 = ms[0]
            sc = f"  正文 {r0['score']:.2f}" if r0["score"] is not None else ""
            print(f"[{r0['level']}] {gid}  {'、'.join(json.loads(r0['reason']))}{sc}  {mark[r0['decision']]}")
            for m in ms:
                w = cat.execute("SELECT title, author FROM works WHERE id=?", (m["work"],)).fetchone()
                print(f"    {w['title'] if w else m['work']}  —  {(w['author'] if w else '') or '（无作者）'}")
        print(f"共 {len(by):,} 组" + (f"，只列前 {limit} 组" if len(by) > limit else ""))
    finally:
        c.close()
        cat.close()


# ---------------------------------------------------------------- 命令行
def main(argv: list[str]) -> int:
    from .config import load as load_cfg
    cfg = load_cfg()
    cmd = argv[0] if argv else ""
    opt = {argv[i]: argv[i + 1] for i in range(1, len(argv) - 1) if argv[i].startswith("--")}
    if cmd == "build":
        content = "--no-content" not in argv
        with busy.hold("同一本书判定", wait=True, kind="io") if content else nullcontext():   # 比对正文要大量读书库：排队等锁
            st = build(cfg.data, content=content)
        (n0, rm0), (n1, rm1) = st["before"], st["after"]
        print(f"完成（{st['seconds']:.0f} 秒）：图书作品 {st['works']:,}，配对 {st['pairs']:,}，最大的组 {st['max_group']} 本")
        print(f"比对正文前：自动 {n0['auto']:,} 组（可去掉 {rm0['auto']:,} 本），待确认 {n0['review']:,} 组（{rm0['review']:,} 本）")
        print(f"比对正文后：自动 {n1['auto']:,} 组（可去掉 {rm1['auto']:,} 本），待确认 {n1['review']:,} 组（{rm1['review']:,} 本）")
        print("原因：", dict(st["reasons"].most_common()))
        cs = st["content"]
        if cs:
            hist = Counter(min(9, int(s * 10)) for s in cs["scores"])
            print(f"正文比对：待确认配对 {cs['pairs']:,}，比对 {cs['checked']:,}，缺正文跳过 {cs['missing']:,}，"
                  f"判为相同 {cs['upgraded']:,}（阈值 {SAME}）")
            print("相似度分布：", {f"{k / 10:.1f}-{(k + 1) / 10:.1f}": hist[k] for k in range(10) if hist[k]})
        else:
            print("正文比对：未做（没有 library.db 或加了 --no-content）")
        print(f"已人工确认：{dict(st['decided']) or 0}；交给去重：{st['final_groups']:,} 组，可去掉 {st['final_removable']:,} 本")
        return 0
    if cmd == "show":
        show(cfg.data, opt.get("--level"), int(opt.get("--limit", 30)))
        return 0
    print(__doc__)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
