"""口碑：各来源的评分与综合口碑分。数据在 data/biblio.db，以 files.fid（内容指纹）为键，同一作品的每个文件都写。

  python -m library.ratings goodreads DIR         UCSD Goodreads 数据集（DIR 里放 goodreads_books.json.gz 和
                                                 goodreads_book_works.json.gz），本地按 ISBN 对接，不联网
  python -m library.ratings ol [--limit N] [--all] Open Library：先并入 enrich 里按书名查到的评分，再按 ISBN 查
                                                 （每 1.5 秒 1 次请求；默认跳过中文书，--all 也查中文书）
  python -m library.ratings douban [--limit N] [--all]  豆瓣：按 ISBN 查公开条目页（每 3–5 秒 1 次请求，如实标明是本地程序；
                                                 被拒绝就停，不绕过；默认只查中文书）
  python -m library.ratings hardcover [--limit N]  Hardcover：没有 Goodreads 评分的书按 ISBN 批量查（补 2017 年以后的新书）；
                                                 token 放在环境变量 HARDCOVER_TOKEN 或 data/hardcover.token
  python -m library.ratings hardcover-title [--limit N]  Hardcover 书名搜索：各来源都没评分的外文书按书名 + 作者查
                                                 （书名和作者的姓都对上才收），顺带首版年
  python -m library.ratings ego [FILE]            自己的豆瓣记录（ego 导出的 douban-books-raw.json）：只在详情页显示，不进综合分
  python -m library.ratings recheck               书名核对上线前查到的评分里可能有别的书的：清掉，下次各来源重查
  python -m library.ratings score                 重算综合口碑分（上面每一步跑完都会自动重算）

  ratings  fid × 来源 → 原始分、满分、评分人数、外部编号、链接
  tries    作品 × 来源 → 查过没有（ok / none），增量重跑时跳过
  score    fid → 综合口碑分（0–10，只用来排序和筛选）、卡片上显示哪个来源

综合口碑分：每个来源换算成 10 分制，再按该来源在全库的均分做贝叶斯平均（评分人数少的往均分拉），
多个来源按评分人数的对数和来源的权威程度加权（豆瓣、Goodreads 最重，Hardcover 次之，Open Library 最轻）。只把 ISBN、书名和作者发给 Open Library，不发送任何个人信息。
"""
from __future__ import annotations

import gzip
import html
import json
import logging
import math
import re
import sqlite3
import sys
import time
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from .biblio import DB_NAME, _ro, _works, norm_isbn
from .biblio import open_db as biblio_db
from .catalog import CJK, fold, norm_author, norm_title

log = logging.getLogger(__name__)
EGO_FILE = Path("~/Documents/ego/content/douban-books-raw.json").expanduser()



@dataclass(frozen=True)
class Source:
    name: str             # 页面上显示的名字；空的不写来源（Open Library 评分人数少，只写「★ 分数」）
    scale: int            # 满分
    prior: int            # 贝叶斯平均的先验人数：评分人数远小于它时，分数主要由全库均分决定
    min_n: int            # 少于这么多人评分不显示
    weight: float = 1.0   # 权威程度：几个来源一起算综合分时的分量（豆瓣、Goodreads 是读者最多的评分平台）


SOURCES = {
    "douban": Source("豆瓣", 10, 1000, 10),         # 先验人数按真实数据调过：小了会让几百人评的冷门书、套装排到最前
    "goodreads": Source("Goodreads", 5, 3000, 20),
    "hardcover": Source("Hardcover", 5, 300, 10, 0.7),    # 用户比 Goodreads 少得多；先验人数等有数据后再按真实分布调
    # Open Library 评分人数一般只有几十，先验 20 时 37 人打 4.7 星的书综合 8.8，排在 Goodreads 几十万人 4.5 星的书前面
    "openlibrary": Source("", 5, 150, 5, 0.3),
}
PERSONAL = {"douban_me": "我在豆瓣"}              # 自己打的分：只显示，不进综合分
PREFER = {"zh": ["douban", "goodreads", "hardcover", "openlibrary"], "en": ["goodreads", "hardcover", "douban", "openlibrary"]}
HIGH = 8.2                                        # 「口碑高分」：综合口碑分（10 分制）不低于它


def open_db(path: Path) -> sqlite3.Connection:
    return biblio_db(path)                              # 表结构见 library/schemas/biblio.py


def put(c: sqlite3.Connection, fids, src: str, score: float, scale: int, n: int, ext_id=None, url=None,
        title=None) -> None:
    c.executemany("INSERT OR REPLACE INTO ratings(fid,src,score,scale,n,ext_id,url,fetched,ext_title) "
                  "VALUES(?,?,?,?,?,?,?,?,?)",
                  [(f, src, score, scale, n, ext_id, url, time.time(), title) for f in fids])


def tried(c: sqlite3.Connection, src: str) -> set:
    return {r[0] for r in c.execute("SELECT work FROM tries WHERE src=?", (src,))}


def mark(c: sqlite3.Connection, work: str, src: str, status: str) -> None:
    c.execute("INSERT OR REPLACE INTO tries VALUES(?,?,?,?)", (work, src, status, time.time()))


def heal(c: sqlite3.Connection, fids: dict, src: str) -> int:
    """书库换了文件（去重合并、重新转换）后，评分还挂在旧文件的 fid 上，已查记录却按作品记：
    挂在已不存在文件上的评分删掉；标着查到了评分、但现在的文件上没有评分的作品清掉已查记录，这一轮重查。"""
    live = {f for fs in fids.values() for f in fs}
    rated = {r[0] for r in c.execute("SELECT fid FROM ratings WHERE src=?", (src,))}
    c.executemany("DELETE FROM ratings WHERE src=? AND fid=?", [(src, f) for f in rated - live])
    rated &= live
    redo = [r[0] for r in c.execute("SELECT work FROM tries WHERE src=? AND status='ok'", (src,))
            if r[0] in fids and not rated & set(fids[r[0]])]
    c.executemany("DELETE FROM tries WHERE src=? AND work=?", [(src, w) for w in redo])
    c.commit()
    return len(redo)


META_SRC = {"opf", "exth", "catalog"}             # 电子书元数据里的 ISBN：和书名不一致的约 4%，且基本只是写法不同
WEAK_ORDER = {"pdf": 0, "text": 1}               # 版权页、正文里扫到的：正文的约 30% 是书末广告页上别的书


def library_isbns(data_dir: Path, raw: bool = False) -> tuple[list, dict, dict, dict]:
    """作品、作品的文件 fid、作品用来查评分的 ISBN、这些 ISBN 是否要核对书名（weak）。
    有元数据 ISBN 时只用它们；没有时才按「pdf 版权页 → 正文」的顺序用，查到的书名必须和我们的一致。
    raw=True：不分级，按书里出现的先后（旧规则，清理时比对用）。"""
    cat = _ro(data_dir / "catalog.db")
    try:
        works, files = _works(cat)
        titles = {r["id"]: r["title"] for r in cat.execute("SELECT id, title FROM works WHERE kind='book'")}
    finally:
        cat.close()
    for w in works:
        w["title"] = titles.get(w["id"]) or ""
    c = open_db(data_dir / DB_NAME)
    try:
        by_fid = defaultdict(list)
        for r in c.execute("SELECT fid, isbn13, src FROM isbns ORDER BY rowid"):
            by_fid[r["fid"]].append((r["isbn13"], r["src"]))
    finally:
        c.close()
    fids, isbns, weak = {}, {}, {}
    for w in works:
        fs = [f["fid"] for f in files.get(w["id"], []) if f["fid"]]
        fids[w["id"]] = list(dict.fromkeys(fs))
        found = [x for f in fs for x in by_fid.get(f, [])]
        meta = [i for i, s in found if s in META_SRC]
        if raw:
            isbns[w["id"]], weak[w["id"]] = list(dict.fromkeys(i for i, _ in found)), False
        elif meta:
            isbns[w["id"]], weak[w["id"]] = list(dict.fromkeys(meta)), False
        else:
            order = sorted(found, key=lambda x: WEAK_ORDER.get(x[1], 9))      # sorted 是稳定的：同级按出现先后
            isbns[w["id"]], weak[w["id"]] = list(dict.fromkeys(i for i, _ in order)), bool(order)
    return works, fids, isbns, weak


def _title_forms(t: str) -> set:
    """主书名（去副标题、括号）和全名，各自折叠成小写、去标点、词之间一个空格。"""
    t = t or ""
    main = re.split(r"\s*[:：(（\[【]", t, maxsplit=1)[0]
    return {x for x in (re.sub(r"^(the|a|an) ", "", fold(main)), re.sub(r"^(the|a|an) ", "", fold(t))) if x}


def _contains(short: str, long_: str) -> bool:
    if CJK.search(short):                                  # 中文不分词：至少 4 个字才按包含算
        a, b = short.replace(" ", ""), long_.replace(" ", "")
        return len(a) >= 4 and a in b
    return len(short.replace(" ", "")) >= 6 and f" {short} " in f" {long_} "     # 西文按整词：changes ≠ exchanges


def same_title(ours: str, *theirs) -> bool:
    """书名核对：主书名或全名相同，或较长的包含较短的（中文至少 4 个字，西文按整词、至少 6 个字母）。"""
    a = _title_forms(ours)
    for t in theirs:
        for x in a:
            for y in _title_forms(t):
                if x.replace(" ", "") == y.replace(" ", ""):
                    return True
                if _contains(*sorted((x, y), key=len)):
                    return True
    return False


# ---------------------------------------------------------------- Goodreads（UCSD 数据集，2017 年）
GR_ISBN13 = re.compile(r'"isbn13": "(\d{13})"')
GR_ISBN = re.compile(r'"isbn": "([\dXx]{10})"')


def _int(s) -> int:
    try:
        return int(s)
    except (TypeError, ValueError):
        return 0


def import_goodreads(data_dir: Path, gr_dir: Path) -> dict:
    """图书文件按 ISBN 找到 Goodreads 的作品编号，再从作品文件取作品级的评分（所有版本合计）。"""
    works, fids, isbns, weak = library_isbns(data_dir)
    title = {w["id"]: w["title"] for w in works}
    want = defaultdict(set)                       # ISBN-13 → 我们的作品
    for wid, xs in isbns.items():
        for i in xs:
            want[i].add(wid)
    hits = defaultdict(dict)                      # 我们的作品 → {Goodreads 作品编号: (那个版本的链接, 书名)}
    st = {"works": len(works), "with_isbn": sum(1 for v in isbns.values() if v), "matched": 0, "rated": 0, "mismatch": 0}
    with gzip.open(gr_dir / "goodreads_books.json.gz", "rt", encoding="utf-8") as f:
        for line in f:
            keys = {m.group(1) for m in GR_ISBN13.finditer(line)}
            keys |= {k for m in GR_ISBN.finditer(line) if (k := norm_isbn(m.group(1)))}
            ours = set().union(*(want.get(k, ()) for k in keys)) if keys else set()
            if not ours:
                continue
            b = json.loads(line)
            if not b.get("work_id"):
                continue
            gt = b.get("title_without_series") or b.get("title") or ""
            for wid in ours:
                if weak[wid] and not same_title(title[wid], gt, b.get("title")):
                    st["mismatch"] += 1                   # 正文里扫到的 ISBN 是别的书
                    continue
                hits[wid].setdefault(b["work_id"], (b.get("url") or b.get("link"), gt))
    need = {g for h in hits.values() for g in h}
    gw = {}
    with gzip.open(gr_dir / "goodreads_book_works.json.gz", "rt", encoding="utf-8") as f:
        for line in f:
            m = re.search(r'"work_id": "(\d+)"', line)
            if not m or m.group(1) not in need:
                continue
            r = json.loads(line)
            n = _int(r.get("ratings_count"))
            if n:
                gw[r["work_id"]] = (_int(r.get("ratings_sum")) / n, n)
    c = open_db(data_dir / DB_NAME)
    c.execute("DELETE FROM ratings WHERE src='goodreads'")
    for wid, h in hits.items():
        st["matched"] += 1
        best = max((g for g in h if g in gw), key=lambda g: gw[g][1], default=None)   # 套装等对上多个作品：取人数最多的
        if best:
            st["rated"] += 1
            avg, n = gw[best]
            put(c, fids[wid], "goodreads", round(avg, 2), 5, n, best, h[best][0], h[best][1])
    c.commit()
    c.close()
    st["score"] = compute(data_dir)
    return st


# ---------------------------------------------------------------- Open Library
OL_FIELDS = "key,title,ratings_average,ratings_count"


def from_enrich(data_dir: Path) -> int:
    """enrich 表里按书名、作者查到的评分并进来；按 ISBN 查到的不覆盖。"""
    _, fids, _, _ = library_isbns(data_dir)
    cat = _ro(data_dir / "catalog.db")
    try:
        rows = cat.execute("SELECT work, rating, rating_n, ol_key FROM enrich WHERE rating IS NOT NULL AND rating_n>0").fetchall()
    except sqlite3.OperationalError:              # 还没跑过联网补全
        rows = []
    finally:
        cat.close()
    c = open_db(data_dir / DB_NAME)
    have = {r[0] for r in c.execute("SELECT fid FROM ratings WHERE src='openlibrary' AND ext_id LIKE 'isbn:%'")}
    n = 0
    for r in rows:
        fs = [f for f in fids.get(r["work"], []) if f not in have]
        if fs:
            put(c, fs, "openlibrary", round(r["rating"], 2), 5, r["rating_n"], r["ol_key"],
                f"https://openlibrary.org{r['ol_key']}" if r["ol_key"] else None)
            n += 1
    c.commit()
    c.close()
    return n


def fetch_ol(data_dir: Path, limit=None, all_langs: bool = False, client=None) -> dict:
    from .enrich import OL
    works, fids, isbns, weak = library_isbns(data_dir)
    c = open_db(data_dir / DB_NAME)
    healed = heal(c, fids, "openlibrary")
    done = tried(c, "openlibrary")
    todo = [w for w in works if isbns[w["id"]] and w["id"] not in done and (all_langs or w["lang"] != "zh")]
    if limit:
        todo = todo[:limit]
    ol = client or OL(interval=1.5)
    st = {"todo": len(todo), "checked": 0, "rated": 0, "stopped": None}
    print(f"Open Library 按 ISBN 查评分：待查 {len(todo):,} 本（其中换了文件要重查的 {healed:,} 本）", flush=True)
    for w in todo:
        doc, mismatch = None, False
        try:
            for i in isbns[w["id"]][:2]:                      # 套装总号查不到时再试第一册
                res = ol.get("/search.json", {"isbn": i, "fields": OL_FIELDS, "limit": 1}) or {}
                doc = next(iter(res.get("docs") or []), None)
                if doc and weak[w["id"]] and not same_title(w["title"], doc.get("title")):
                    doc, mismatch = None, True                # 正文里扫到的 ISBN 是别的书
                    continue
                if doc:
                    break
        except Exception as ex:                               # Open Library 不可用：停下，下次接着查
            st["stopped"] = str(ex)
            print(f"\n停止：{ex}（下次接着查）", flush=True)
            break
        n = (doc or {}).get("ratings_count") or 0
        if n:
            st["rated"] += 1
            put(c, fids[w["id"]], "openlibrary", round(doc["ratings_average"], 2), 5, n, f"isbn:{doc['key']}",
                f"https://openlibrary.org{doc['key']}", doc.get("title"))
        mark(c, w["id"], "openlibrary", "ok" if n else ("mismatch" if mismatch else "none"))
        c.commit()
        st["checked"] += 1
        if st["checked"] % 50 == 0:
            print(f"{st['checked']:,}/{len(todo):,}  有评分 {st['rated']:,}", flush=True)
    c.close()
    return st


# ---------------------------------------------------------------- 豆瓣（按 ISBN 慢速查公开条目页）
DOUBAN_ID = re.compile(r"/subject/(\d+)")
DB_AVG = re.compile(r'property="v:average">\s*([\d.]*)\s*<')
DB_VOTES = re.compile(r'property="v:votes">\s*(\d+)\s*<')
DB_ISBN = re.compile(r'<span class="pl">\s*ISBN:?\s*</span>\s*([\dXx\-]+)')
DB_TITLE = re.compile(r'property="v:itemreviewed">([^<]*)<')
DOUBAN_UA = "Lectern/0.1 (personal offline reading library; one request every few seconds)"


class Blocked(Exception):
    """豆瓣拒绝了（403/418/429、验证页、要求登录）：整轮停下，不绕过。"""


def parse_douban(page: str, isbn: str) -> tuple[float, int] | None | bool:
    """条目页 → (分数, 人数)；无人评价或人数不足 → None；页面上的 ISBN 和查的不是同一本 → False。"""
    m = DB_ISBN.search(page)
    if not m or norm_isbn(m.group(1)) != isbn:            # 豆瓣会把查不到的 ISBN 跳到别的条目
        return False
    avg, votes = DB_AVG.search(page), DB_VOTES.search(page)
    if not avg or not avg.group(1) or not votes:
        return None
    return float(avg.group(1)), int(votes.group(1))


class Douban:
    def __init__(self, delay=(3.0, 5.0)):
        import httpx
        self.http = httpx.Client(base_url="https://book.douban.com", headers={"User-Agent": DOUBAN_UA}, timeout=30,
                                 follow_redirects=True)
        self.delay, self.last = delay, 0.0

    def lookup(self, isbn: str) -> dict | bool:
        """{"id", "url", "title", "score", "n"}（没有评分时 score、n 为 None）；豆瓣没有这本 → False。"""
        import random
        wait = self.last + random.uniform(*self.delay) - time.time()
        if wait > 0:
            time.sleep(wait)
        self.last = time.time()
        r = self.http.get(f"/isbn/{isbn}/")
        url = str(r.url)
        if r.status_code in (403, 418, 429) or "sec.douban.com" in url or "accounts.douban.com" in url:
            raise Blocked(f"HTTP {r.status_code} {url}")
        if r.status_code == 404:
            return False
        r.raise_for_status()
        m = DOUBAN_ID.search(url)
        got = parse_douban(r.text, isbn)
        if not m or got is False:
            return False
        t = DB_TITLE.search(r.text)
        return {"id": m.group(1), "url": f"https://book.douban.com/subject/{m.group(1)}/",
                "title": html.unescape(t.group(1)).strip() if t else "",
                "score": got[0] if got else None, "n": got[1] if got else None}


def fetch_douban(data_dir: Path, limit=None, all_langs: bool = False, client=None) -> dict:
    """默认只查中文书；每本最多试两个 ISBN（套装总号查不到时试第一册）。被拒绝就停，已查的记在 tries 里，下次接着查。"""
    works, fids, isbns, weak = library_isbns(data_dir)
    c = open_db(data_dir / DB_NAME)
    healed = heal(c, fids, "douban")
    done = tried(c, "douban")
    todo = [w for w in works if isbns[w["id"]] and w["id"] not in done and (all_langs or w["lang"] == "zh")]
    if limit:
        todo = todo[:limit]
    db = client or Douban()
    st = {"todo": len(todo), "checked": 0, "rated": 0, "stopped": None}
    print(f"豆瓣按 ISBN 查评分：待查 {len(todo):,} 本（其中换了文件要重查的 {healed:,} 本）", flush=True)
    errors = 0
    for w in todo:
        got, mismatch = False, False
        try:
            for i in isbns[w["id"]][:2]:
                got = db.lookup(i)
                if got and weak[w["id"]] and not same_title(w["title"], got["title"]):
                    got, mismatch = False, True               # 正文里扫到的 ISBN 是别的书
                    continue
                if got is not False:
                    break
            errors = 0
        except Blocked as ex:
            st["stopped"] = f"豆瓣拒绝访问：{ex}"
            print(f"\n停止：{st['stopped']}（过几个小时再跑，已查的不会重查）", flush=True)
            break
        except Exception as ex:                       # 网络抖动：这本先跳过不记，连续 5 次就停
            errors += 1
            log.warning("豆瓣请求失败：%s", ex)
            if errors >= 5:
                st["stopped"] = f"连续失败：{ex}"
                print(f"\n停止：{st['stopped']}", flush=True)
                break
            time.sleep(60)
            continue
        if got and got["n"]:
            st["rated"] += 1
            put(c, fids[w["id"]], "douban", got["score"], 10, got["n"], got["id"], got["url"], got["title"])
        mark(c, w["id"], "douban", "ok" if got and got["n"] else "norating" if got else "mismatch" if mismatch else "none")
        c.commit()
        st["checked"] += 1
        if st["checked"] % 50 == 0:
            print(f"{st['checked']:,}/{len(todo):,}  有评分 {st['rated']:,}", flush=True)
    c.close()
    return st


# ---------------------------------------------------------------- Hardcover（官方 GraphQL API，需要个人 token）
HC_URL = "https://api.hardcover.app/v1/graphql"
HC_QUERY = ("query($isbns: [String!]) { editions(where: {isbn_13: {_in: $isbns}}) "
            "{ isbn_13 title book { id title rating ratings_count } } }")
HC_BATCH = 50                      # 一次请求查 50 个 ISBN：只算一个顶层字段，最省每日额度
UA_HC = "Lectern/0.1 (personal offline reading library; ratings for books the user owns)"


class HardcoverStop(Exception):
    """token 无效、额度用完（429）或接口拒绝：整轮停下，下次接着查。"""


def hardcover_token(data_dir: Path) -> str | None:
    import os
    if t := os.environ.get("HARDCOVER_TOKEN", "").strip():
        return t
    p = data_dir / "hardcover.token"
    return p.read_text().strip() if p.exists() else None


class Hardcover:
    def __init__(self, token: str, interval: float = 1.5):
        import httpx
        token = token if token.lower().startswith("bearer ") else f"Bearer {token}"
        self.http = httpx.Client(headers={"authorization": token, "User-Agent": UA_HC}, timeout=40)
        self.interval, self.last = interval, 0.0

    def lookup(self, isbns: list[str]) -> dict:
        """ISBN-13 → (Hardcover 书号, 平均分, 评分人数, 书名, 版本名)；一个 ISBN 对上多个版本时取评分人数最多的书。"""
        wait = self.last + self.interval - time.time()
        if wait > 0:
            time.sleep(wait)
        self.last = time.time()
        r = self.http.post(HC_URL, json={"query": HC_QUERY, "variables": {"isbns": isbns}})
        if r.status_code in (401, 403, 429):
            raise HardcoverStop(f"HTTP {r.status_code} {r.text[:200]}")
        r.raise_for_status()
        body = r.json()
        if body.get("errors"):
            raise HardcoverStop(f"GraphQL 错误：{json.dumps(body['errors'], ensure_ascii=False)[:300]}")
        out = {}
        for ed in (body.get("data") or {}).get("editions") or []:
            b, i = ed.get("book") or {}, ed.get("isbn_13")
            if not i or not b.get("id"):
                continue
            got = (str(b["id"]), b.get("rating"), b.get("ratings_count") or 0, b.get("title") or "", ed.get("title") or "")
            if i not in out or got[2] > out[i][2]:
                out[i] = got
        return out


def fetch_hardcover(data_dir: Path, limit=None, client=None) -> dict:
    """只查还没有 Goodreads 评分的书（Goodreads 数据集停在 2017 年）；每本最多带两个 ISBN 进批次。"""
    works, fids, isbns, weak = library_isbns(data_dir)
    c = open_db(data_dir / DB_NAME)
    healed = heal(c, fids, "hardcover")
    done = tried(c, "hardcover")
    has_gr = {r[0] for r in c.execute("SELECT fid FROM ratings WHERE src='goodreads'")}
    todo = [w for w in works if isbns[w["id"]] and w["id"] not in done and not has_gr & set(fids[w["id"]])]
    if limit:
        todo = todo[:limit]
    st = {"todo": len(todo), "checked": 0, "rated": 0, "stopped": None}
    if not todo:
        c.close()
        return st
    if client is None:
        token = hardcover_token(data_dir)
        if not token:
            c.close()
            st["stopped"] = "没有 token：设置环境变量 HARDCOVER_TOKEN，或把 token 写进 data/hardcover.token"
            return st
        client = Hardcover(token)
    print(f"Hardcover 按 ISBN 查评分：待查 {len(todo):,} 本（其中换了文件要重查的 {healed:,} 本）", flush=True)
    batch: list = []

    def flush():
        want = [i for w in batch for i in isbns[w["id"]][:2]]
        got = client.lookup(want)
        for w in batch:
            cand = [got[i] for i in isbns[w["id"]][:2] if i in got]
            ok = [g for g in cand if not weak[w["id"]] or same_title(w["title"], g[3], g[4])]   # 正文 ISBN：核对书名
            hit = max(ok, key=lambda g: g[2], default=None)
            if hit and hit[1] and hit[2]:
                st["rated"] += 1
                put(c, fids[w["id"]], "hardcover", round(hit[1], 2), 5, hit[2], hit[0], f"https://hardcover.app/id/book/{hit[0]}",
                    hit[3])
            mark(c, w["id"], "hardcover", "ok" if hit and hit[2] else "mismatch" if cand and not ok else "none")
        c.commit()
        st["checked"] += len(batch)
        batch.clear()
        if st["checked"] % 500 < HC_BATCH:
            print(f"{st['checked']:,}/{len(todo):,}  有评分 {st['rated']:,}", flush=True)

    try:
        for w in todo:
            batch.append(w)
            if len(batch) >= HC_BATCH:
                flush()
        if batch:
            flush()
    except Exception as ex:                       # 额度用完、token 失效、网络断了：这一批不记，下次接着查
        st["stopped"] = str(ex)
        print(f"\n停止：{ex}（下次接着查）", flush=True)
    c.close()
    return st


# ---------------------------------------------------------------- 清理：按新规则重查
HC_SEARCH = 'query($q: String!) { search(query: $q, query_type: "Book", per_page: 5, page: 1) { results } }'


def hardcover_search(client: Hardcover, title: str, author: str) -> list[dict]:
    """书名 + 作者搜索 → Hardcover 的书（[{id, title, author_names, rating, ratings_count, release_year}]）。"""
    wait = client.last + client.interval - time.time()
    if wait > 0:
        time.sleep(wait)
    client.last = time.time()
    r = client.http.post(HC_URL, json={"query": HC_SEARCH, "variables": {"q": f"{title} {author}".strip()[:200]}})
    if r.status_code in (401, 403, 429):
        raise HardcoverStop(f"HTTP {r.status_code} {r.text[:200]}")
    r.raise_for_status()
    body = r.json()
    if body.get("errors"):
        raise HardcoverStop(f"GraphQL 错误：{json.dumps(body['errors'], ensure_ascii=False)[:300]}")
    res = ((body.get("data") or {}).get("search") or {}).get("results") or {}
    return [h.get("document") or {} for h in res.get("hits") or []]


def hc_match(docs: list[dict], title: str, author: str) -> dict | None:
    """书名对得上、作者的姓也对得上的书里取评分人数最多的一本；单凭书名不收（同名书很多）。"""
    from .enrich import surname
    s = surname(author)
    ok = [d for d in docs if s and same_title(title, d.get("title") or "")
          and any(s in fold(a).split() for a in d.get("author_names") or [])]
    return max(ok, key=lambda d: d.get("ratings_count") or 0, default=None)


def fetch_hardcover_title(data_dir: Path, limit=None, client=None) -> dict:
    """没有 ISBN 能对上评分的外文书（各来源都没评分、有作者），按书名 + 作者搜 Hardcover：评分 → ratings(hardcover)，
    首版年 → first_pub(hardcover)。查过的记在 tries（src='hardcover_title'），下次跳过。"""
    cat = _ro(data_dir / "catalog.db")
    try:
        works, files = _works(cat)
        meta = {r["id"]: (r["title"] or "", r["author"] or "") for r in
                cat.execute("SELECT id, title, author FROM works WHERE kind='book'")}
    finally:
        cat.close()
    c = open_db(data_dir / DB_NAME)
    rated = {r[0] for r in c.execute(f"SELECT fid FROM ratings WHERE src IN ({','.join('?' * len(SOURCES))})", list(SOURCES))}
    done = tried(c, "hardcover_title")
    todo = []
    for w in works:
        fl = [f["fid"] for f in files.get(w["id"], []) if f["fid"]]
        t, a = meta.get(w["id"], ("", ""))
        if w["lang"] != "zh" and fl and t and a and not CJK.search(t + a) and w["id"] not in done and not rated & set(fl):
            todo.append((w, fl, t, a))
    if limit:
        todo = todo[:limit]
    st = {"todo": len(todo), "checked": 0, "rated": 0, "year": 0, "stopped": None}
    if not todo:
        c.close()
        return st
    if client is None:
        token = hardcover_token(data_dir)
        if not token:
            c.close()
            st["stopped"] = "没有 token：设置环境变量 HARDCOVER_TOKEN，或把 token 写进 data/hardcover.token"
            return st
        client = Hardcover(token)
    print(f"Hardcover 按书名查评分：待查 {len(todo):,} 本", flush=True)
    errors = 0
    try:
        for w, fl, t, a in todo:
            try:
                hit = hc_match(hardcover_search(client, t, a), t, a)
                errors = 0
            except HardcoverStop:
                raise
            except Exception as ex:                   # 网络抖动：这本先不记，连续 5 次就停
                errors += 1
                if errors >= 5:
                    raise HardcoverStop(f"连续失败：{ex}") from None
                time.sleep(30)
                continue
            if hit and hit.get("rating") and hit.get("ratings_count"):
                put(c, fl, "hardcover", round(float(hit["rating"]), 2), 5, int(hit["ratings_count"]), str(hit["id"]),
                    f"https://hardcover.app/id/book/{hit['id']}", hit.get("title"))
                st["rated"] += 1
            if hit and isinstance(hit.get("release_year"), int) and 1000 < hit["release_year"] <= 2100:
                c.executemany("INSERT OR REPLACE INTO first_pub VALUES(?,?,?)", [(f, "hardcover", hit["release_year"]) for f in fl])
                st["year"] += 1
            mark(c, w["id"], "hardcover_title", "ok" if hit else "none")
            c.commit()
            st["checked"] += 1
            if st["checked"] % 200 == 0:
                print(f"{st['checked']:,}/{len(todo):,}  有评分 {st['rated']:,}  首版年 {st['year']:,}", flush=True)
    except HardcoverStop as ex:
        st["stopped"] = str(ex)
        print(f"\n停止：{ex}（下次接着查）", flush=True)
    c.close()
    return st


def recheck(data_dir: Path) -> dict:
    """ISBN 分级、书名核对上线前查到的评分里，可能有别的书的分。要重查的：
    ISBN 要核对书名、且当时查到了评分的书；以及新旧规则选出的 ISBN 不一样的书。清掉它们的评分和已查记录。"""
    works, fids, isbns, weak = library_isbns(data_dir)
    _, _, old, _ = library_isbns(data_dir, raw=True)
    c = open_db(data_dir / DB_NAME)
    st = {}
    for src in ("douban", "openlibrary", "hardcover"):
        status = dict(c.execute("SELECT work, status FROM tries WHERE src=?", (src,)).fetchall())
        redo = [w["id"] for w in works if w["id"] in status and
                ((weak[w["id"]] and status[w["id"]] == "ok") or old[w["id"]][:2] != isbns[w["id"]][:2])]
        for wid in redo:
            c.execute(f"DELETE FROM ratings WHERE src=? AND fid IN ({','.join('?' * len(fids[wid])) or 'NULL'})",
                      [src, *fids[wid]])
            c.execute("DELETE FROM tries WHERE src=? AND work=?", (src, wid))
        st[src] = len(redo)
    c.commit()
    c.close()
    return st


# ---------------------------------------------------------------- 自己的豆瓣记录（ego）


def _title_key(t: str) -> str:
    t = re.split(r"\s*[:：]\s*", t or "", maxsplit=1)[0]          # 豆瓣书名常带副标题
    return norm_title(t)


def import_ego(data_dir: Path, path: Path = EGO_FILE) -> dict:
    """按书名（去副标题）对接；同名的书不止一本时再按作者区分，区分不了就不要。"""
    items = json.loads(Path(path).read_text(encoding="utf-8"))
    cat = _ro(data_dir / "catalog.db")
    try:
        rows = cat.execute("SELECT id, title, author FROM works WHERE kind='book'").fetchall()
    finally:
        cat.close()
    by_title = defaultdict(list)
    for r in rows:
        by_title[_title_key(r["title"])].append(r)
    _, fids, _, _ = library_isbns(data_dir)
    st = {"items": len(items), "rated": 0, "matched": 0, "ambiguous": 0}
    c = open_db(data_dir / DB_NAME)
    c.execute("DELETE FROM ratings WHERE src='douban_me'")
    for it in items:
        stars = _int(it.get("rating"))
        if not 1 <= stars <= 5:
            continue
        st["rated"] += 1
        cand = by_title.get(_title_key(it.get("title", "")), [])
        if len(cand) > 1 and it.get("author"):
            a = norm_author(it["author"])
            cand = [r for r in cand if a and (a in fold(r["author"] or "").replace(" ", "") or norm_author(r["author"]) == a)]
        if len(cand) != 1:
            st["ambiguous"] += len(cand) > 1
            continue
        m = DOUBAN_ID.search(it.get("url") or "")
        put(c, fids.get(cand[0]["id"], []), "douban_me", stars, 5, 1, m.group(1) if m else None, it.get("url"))
        st["matched"] += 1
    c.commit()
    c.close()
    return st


# ---------------------------------------------------------------- 综合口碑分
def bayes(x: float, n: int, prior_mean: float, prior_n: int) -> float:
    return (n * x + prior_n * prior_mean) / (n + prior_n)


def combine(found: dict, means: dict) -> tuple[float, int]:
    """found: 来源 → (10 分制分数, 评分人数)。各来源的贝叶斯分加权平均，权重 = log(1+人数) × 人数/(人数+先验人数) × 权威程度：
    几十万人和几万人的来源分量相当，几个人的来源分量很小，Open Library、Hardcover 比豆瓣、Goodreads 轻。
    人数不到显示门槛的来源不参与；全都不到时才用它们。"""
    use = {s: v for s, v in found.items() if v[1] >= SOURCES[s].min_n} or found
    tw = ts = 0.0
    for s, (x, n) in use.items():
        w = math.log1p(n) * n / (n + SOURCES[s].prior) * SOURCES[s].weight
        ts += w * bayes(x, n, means[s], SOURCES[s].prior)
        tw += w
    return round(ts / tw, 3), sum(n for _, n in found.values())


def shown(found: dict, lang: str) -> str | None:
    """卡片上显示哪个来源：中文书先看豆瓣，其余先看 Goodreads；人数不够的不显示。"""
    return next((s for s in PREFER["zh" if lang == "zh" else "en"]
                 if s in found and found[s][1] >= SOURCES[s].min_n), None)


def compute(data_dir: Path) -> dict:
    """全量重算（几万行几秒）。同一作品各文件的评分相同，按作品算一次再写回每个文件。"""
    cat = _ro(data_dir / "catalog.db")
    try:
        works, files = _works(cat)
    finally:
        cat.close()
    c = open_db(data_dir / DB_NAME)
    raw = defaultdict(dict)
    for r in c.execute(f"SELECT * FROM ratings WHERE src IN ({','.join('?' * len(SOURCES))}) AND n>0", list(SOURCES)):
        raw[r["fid"]][r["src"]] = (r["score"] * 10 / r["scale"], r["n"], r["score"])
    per_work = {}
    for w in works:
        found = {}
        for f in files.get(w["id"], []):
            for s, v in raw.get(f["fid"], {}).items():
                found.setdefault(s, v)
        if found:
            per_work[w["id"]] = (w, found)
    means = {}
    for s in SOURCES:
        xs = [found[s][0] for _, found in per_work.values() if s in found]
        means[s] = sum(xs) / len(xs) if xs else 7.0
    out = []
    for wid, (w, found) in per_work.items():
        sc, total = combine({s: v[:2] for s, v in found.items()}, means)
        src = shown(found, w["lang"])
        for f in {f["fid"] for f in files.get(wid, []) if f["fid"]}:
            out.append((f, sc, total, src, found[src][2] if src else None, found[src][1] if src else None))
    c.execute("DELETE FROM score")
    c.executemany("INSERT OR REPLACE INTO score VALUES(?,?,?,?,?,?)", out)
    c.commit()
    c.close()
    return {"works": len(per_work), "means": {s: round(m, 2) for s, m in means.items()}}


# ---------------------------------------------------------------- 页面用
def people(n: int) -> str:
    """「12.3 万人」「9,999 人」"""
    return (f"{n / 10000:.1f}".removesuffix(".0") + " 万人") if n >= 10000 else f"{n:,} 人"


def fmt_score(src: str, score: float) -> str:
    return f"{score:.1f}" if SOURCES.get(src, SOURCES["openlibrary"]).scale == 10 else f"{score:.2f}"


def card_label(src: str | None, score) -> tuple[str, str] | None:
    """卡片上的短标签与悬停提示：豆瓣写「豆瓣 9.1」，5 分制写「★ 4.3」。"""
    if not score:
        return None
    s = SOURCES.get(src or "openlibrary", SOURCES["openlibrary"])
    if s.scale == 10:
        return f"{s.name} {score:.1f}", s.name
    return f"★ {score:.1f}", s.name or "评分"


def badges(rows, lang: str) -> list[dict]:
    """详情页每个来源一个：名字、分数、人数、链接。有豆瓣或 Goodreads 时不再列 Open Library（人数少，只会添乱）。
    自己打的分放最后。"""
    order = PREFER["zh" if lang == "zh" else "en"]
    by = {r["src"]: r for r in rows}
    out = []
    for s in order:
        r = by.get(s)
        if r and r["n"] >= SOURCES[s].min_n:
            name = SOURCES[s].name
            if not name and out:
                continue
            text = (f"{name} {fmt_score(s, r['score'])} · {people(r['n'])}评" if name
                    else f"★ {r['score']:.1f}（{people(r['n'])}评分）")
            out.append({"src": s, "text": text, "url": r["url"] if name else None})
    for s, name in PERSONAL.items():
        if r := by.get(s):
            out.append({"src": s, "text": f"{name} {'★' * int(r['score'])}", "url": r["url"]})
    return out


# ---------------------------------------------------------------- 命令行
def main(argv: list[str]) -> int:
    from .config import load
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname).1s %(message)s", datefmt="%H:%M:%S")
    data = load().data
    cmd = argv[0] if argv else ""
    args = [a for a in argv[1:] if not a.startswith("--")]
    opt = {argv[i]: argv[i + 1] for i in range(1, len(argv) - 1) if argv[i].startswith("--") and argv[i] != "--all"}
    if cmd == "goodreads" and args:
        print(import_goodreads(data, Path(args[0]).expanduser()))
        return 0
    if cmd == "ol":
        print(f"并入按书名查到的评分：{from_enrich(data):,} 本")
        print(fetch_ol(data, int(opt["--limit"]) if "--limit" in opt else None, "--all" in argv))
        print(compute(data))
        return 0
    if cmd == "douban":
        print(fetch_douban(data, int(opt["--limit"]) if "--limit" in opt else None, "--all" in argv))
        print(compute(data))
        return 0
    if cmd == "hardcover":
        print(fetch_hardcover(data, int(opt["--limit"]) if "--limit" in opt else None))
        print(compute(data))
        return 0
    if cmd == "hardcover-title":
        print(fetch_hardcover_title(data, int(opt["--limit"]) if "--limit" in opt else None))
        print(compute(data))
        return 0
    if cmd == "ego":
        print(import_ego(data, Path(args[0]).expanduser() if args else EGO_FILE))
        return 0
    if cmd == "recheck":
        print("清掉、待重查：", recheck(data))
        print(compute(data))
        return 0
    if cmd == "score":
        print(compute(data))
        return 0
    print(__doc__)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
