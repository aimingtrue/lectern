"""书城增强标签：联网书目信息、难度分级。结果在 catalog.db 的 enrich 表，可断点续跑。

  python -m library.enrich online   Open Library：出版年、主题、评分、页数、英文简介（英文书；每秒 1 次请求）
  python -m library.enrich level    难度：抽样约 5000 词，按本地词典词频算「读懂 95% 的词需要多少词汇量」
  python -m library.enrich all      依次跑上面两步

单级类别和一句话简介（原来的 label）已退役：书城只用两级分类（library/classify.py），enrich 表里旧的
category、cat_src、summary 列留着不再写。

只会把书名、作者发给 Open Library（用户同意的联网方式），不发送任何个人信息。
"""
from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import sys
import time
import zipfile
from bisect import bisect_left
from pathlib import Path

import httpx

from .catalog import CJK, Catalog, _xml, fold, norm_title

log = logging.getLogger(__name__)
UA = "Lectern/0.1 (personal offline reading library)"


# 读懂 95% 的词需要的词汇量（按词频名次）。用已知难度的书校准：童书约 3,500，现代小说 6,000–10,000，Ulysses 3 万以上
LEVELS = [(4000, "入门"), (6000, "进阶"), (8500, "中级"), (12000, "高级"), (10 ** 9, "挑战")]
LEVEL_NOTE = {"入门": "≤4,000 词", "进阶": "4,000–6,000 词", "中级": "6,000–8,500 词", "高级": "8,500–12,000 词",
              "挑战": "12,000 词以上"}
# 旧的单级类别名：书城已不显示；书目层用它给 CIP 分类号取个类别名（biblio.clc_category），存档用
CATEGORIES = ["文学小说", "悬疑惊悚", "爱情", "科幻奇幻", "恐怖", "历史小说", "青少年", "儿童",
              "传记回忆录", "历史", "商业经济", "心理自助", "科学科普", "社会政治", "哲学宗教",
              "健康生活", "艺术", "旅行", "计算机技术", "语言教育", "其他"]


def db(cat: Catalog) -> sqlite3.Connection:
    return cat.conn                               # enrich 表随 catalog.db 建好（library/schemas/catalog.py）


def upsert(c: sqlite3.Connection, work: str, **kw) -> None:
    cols = ", ".join(kw)
    for attempt in range(30):            # 联网补全、难度分级、书城可能同时写：被锁时等一会儿再试
        try:
            _upsert(c, work, cols, kw)
            return
        except sqlite3.OperationalError as ex:
            if "locked" not in str(ex) or attempt == 29:
                raise
            time.sleep(2)


def _upsert(c, work, cols, kw) -> None:
    c.execute(f"INSERT INTO enrich(work, {cols}) VALUES(?{', ?' * len(kw)}) "
              f"ON CONFLICT(work) DO UPDATE SET " + ", ".join(f"{k}=excluded.{k}" for k in kw),
              (work, *kw.values()))


# ---------------------------------------------------------------- Open Library
def main_title(t: str) -> str:
    t = re.sub(r"\s*[\(\[][^)\]]*[\)\]]", "", t or "").strip(" -:")      # 「[系列 05] - 书名」去掉方括号后不能剩空
    return re.split(r"\s*:\s*|\s+-\s+", t, maxsplit=1)[0].strip()


def surname(a: str) -> str:
    a = re.split(r"\s*(?:;|&|,| and )\s*", (a or "").strip())[0]
    return fold((a.split() or [""])[-1])


def matches(doc: dict, title: str, author: str) -> bool:
    nt, dt = norm_title(main_title(title)), norm_title(main_title(doc.get("title", "")))
    if not nt or not dt:
        return False
    title_ok = nt == dt or (min(len(nt), len(dt)) >= 8 and (nt.startswith(dt) or dt.startswith(nt)))
    if not title_ok:
        return False
    if not author:
        return nt == dt
    s = surname(author)
    return any(s and s in fold(n).split() for n in doc.get("author_name", []))


def clean_desc(d) -> str:
    if isinstance(d, dict):
        d = d.get("value", "")
    d = d or ""
    d = re.split(r"\n-{3,}|\n\*\*\*|\n\[\d+\]:", d)[0]                  # 去掉「----- 另见」和脚注链接
    d = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", d)                      # markdown 链接
    d = re.sub(r"\(\[source\]\[\d+\]\)|\[\d+\]", "", d)
    return re.sub(r"\s+\n", "\n", d).strip()[:3000]


class OL:
    def __init__(self, interval: float = 1.0):
        self.http = httpx.Client(base_url="https://openlibrary.org", headers={"User-Agent": UA}, timeout=30,
                                 follow_redirects=True)
        self.interval, self.last = interval, 0.0

    def get(self, path: str, params=None) -> dict | None:
        for attempt in range(5):
            wait = self.last + self.interval - time.time()
            if wait > 0:
                time.sleep(wait)
            self.last = time.time()
            try:
                r = self.http.get(path, params=params)
            except httpx.HTTPError as ex:
                log.warning("Open Library 请求失败 %s：%s", path, ex)
                time.sleep(5 * (attempt + 1))
                continue
            if r.status_code == 404:
                return None
            if r.status_code in (429, 500, 502, 503, 504):
                time.sleep(min(120, 10 * 2 ** attempt))
                continue
            r.raise_for_status()
            return r.json()
        raise RuntimeError(f"Open Library 多次失败：{path}")

    def alive(self) -> bool:
        try:
            return self.http.get("/search.json", params={"title": "emma", "author": "austen", "limit": 1,
                                                         "fields": "key"}).status_code == 200
        except httpx.HTTPError:
            return False

    def lookup(self, title: str, author: str) -> dict | None:
        fields = "key,title,author_name,first_publish_year,subject,ratings_average,ratings_count,number_of_pages_median"
        first = re.split(r"\s*(?:;|&| and )\s*", author or "")[0].strip()
        if not main_title(title):                       # 书名只有「[系列 07]」之类：Open Library 不接受空书名
            return None
        params = {"title": main_title(title), "limit": 5, "fields": fields}
        if first:
            params["author"] = first
        res = self.get("/search.json", params) or {}
        doc = next((d for d in res.get("docs", []) if matches(d, title, first)), None)
        if not doc and first:                           # 再试一次全文搜索：书名 + 作者姓
            res = self.get("/search.json", {"q": f"{main_title(title)} {first.split()[-1]}", "limit": 5,
                                            "fields": fields}) or {}
            doc = next((d for d in res.get("docs", []) if matches(d, title, first)), None)
        if not doc:
            return None
        work = self.get(doc["key"] + ".json") or {}
        subjects = (doc.get("subject") or work.get("subjects") or [])
        subjects = [s for s in subjects if not s.lower().startswith(("nyt:", "new york times", "accessible book",
                                                                     "protected daisy", "in library", "large type"))][:25]
        return {"ol_key": doc["key"], "year": doc.get("first_publish_year"), "subjects": subjects,
                "ol_author": (doc.get("author_name") or [""])[0],
                "rating": doc.get("ratings_average"), "rating_n": doc.get("ratings_count") or 0,
                "pages": doc.get("number_of_pages_median"), "description": clean_desc(work.get("description"))}


def run_online(cat: Catalog, limit: int = 0) -> None:
    c = db(cat)
    todo = c.execute("SELECT w.id, w.title, w.author FROM works w LEFT JOIN enrich e ON e.work=w.id "
                     "WHERE w.kind='book' AND w.lang!='zh' AND e.ol_status IS NULL ORDER BY w.id").fetchall()
    if limit:
        todo = todo[:limit]
    ol, n, hit, t0 = OL(interval=1.5), 0, 0, time.time()
    print(f"联网补全：待查 {len(todo):,} 本", flush=True)
    for w in todo:
        r, failures = None, 0
        while True:
            try:
                r = ol.lookup(w["title"], w["author"])
                break
            except Exception as ex:                     # Open Library 暂时不可用：等 10 分钟再试，连续 6 次（1 小时）才停
                failures += 1
                if ol.alive():                          # 服务正常、只是这本的查询出错：记下跳过，不卡住后面的书
                    log.warning("Open Library 查不了这本，跳过：%s（%s）", w["title"], ex)
                    r = "error"
                    break
                if failures >= 6:
                    print(f"\n停止：连续 1 小时请求失败（{ex}），下次接着跑", flush=True)
                    return
                print(f"Open Library 暂时不可用（{ex}），10 分钟后重试", flush=True)
                time.sleep(600)
        if r == "error":
            upsert(c, w["id"], ol_status="error", ol_at=time.time())
        elif r:
            hit += 1
            upsert(c, w["id"], ol_status="ok", ol_key=r["ol_key"], year=r["year"], ol_author=r["ol_author"],
                   subjects=json.dumps(r["subjects"], ensure_ascii=False), rating=r["rating"], rating_n=r["rating_n"],
                   pages=r["pages"], description=r["description"], ol_at=time.time())
        else:
            upsert(c, w["id"], ol_status="nomatch", ol_at=time.time())
        c.commit()
        n += 1
        if n % 25 == 0:
            rate = n / (time.time() - t0)
            print(f"{n:,}/{len(todo):,}  匹配 {hit:,}（{100 * hit / n:.0f}%）  约剩 {(len(todo) - n) / rate / 3600:.1f} 小时",
                  flush=True)
    print(f"完成：查了 {n:,} 本，匹配 {hit:,} 本", flush=True)


# ---------------------------------------------------------------- 难度
_VOCAB: dict | None = None


TAG_RANK = [("zk", 1500), ("gk", 3500), ("cet4", 4500), ("cet6", 7000), ("ky", 8000), ("toefl", 9000),
            ("ielts", 9000), ("gre", 15000)]
IRREGULAR = {"are": "be", "were": "be", "am": "be", "wo": "will", "ca": "can", "sha": "shall", "gonna": "go",
             "wanna": "want", "gotta": "get", "ain": "be"}


def vocab(dict_path: Path) -> dict:
    """词 → 词频名次（ECDICT frq）。变形词按原形算（变形表 + exchange 里的「0:原形」）；
    词典里有但没有词频的词，按考试等级标签估一个名次。完全查不到的词记 50001。"""
    global _VOCAB
    if _VOCAB is None:
        c = sqlite3.connect(f"file:{dict_path}?mode=ro", uri=True)
        rank, lemma_of, tagged = {}, {}, {}
        for w, f, tag, ex in c.execute("SELECT word, frq, tag, exchange FROM words"):
            w = w.lower()
            if f and f > 0:
                rank[w] = min(f, rank.get(w, f))
                continue
            m = re.search(r"(?:^|/)0:([^/]+)", ex or "")
            if m:
                lemma_of[w] = m.group(1).lower()
            tags = (tag or "").split()
            est = next((r for t, r in TAG_RANK if t in tags), None)
            if est:
                tagged[w] = est
        for form, lemma in c.execute("SELECT form, lemma FROM forms"):
            lemma_of.setdefault(form.lower(), lemma.lower())
        lemma_of.update(IRREGULAR)
        for w, lem in lemma_of.items():
            r = rank.get(lem)
            if r and (w not in rank or r < rank[w]):
                rank[w] = r
        for w, est in tagged.items():
            rank.setdefault(w, est)
        _VOCAB = rank
    return _VOCAB


def word_rank(w: str, rank: dict) -> int:
    if "'" in w:                                   # 缩写：don't → do，she'd → she，can't → can
        w = w[:-3] if w.endswith("n't") else w.split("'")[0]
        w = IRREGULAR.get(w, w)
    r = rank.get(w)
    if r:
        return r
    for suf, rep in (("ies", "y"), ("ied", "y"), ("ier", "y"), ("iest", "y"), ("ing", ""), ("ing", "e"), ("ed", ""),
                     ("ed", "e"), ("es", ""), ("s", ""), ("ly", ""), ("er", ""), ("er", "e"), ("est", ""), ("est", "e")):
        if w.endswith(suf) and len(w) - len(suf) >= 3:
            r = rank.get(w[: -len(suf)] + rep)
            if r:
                return r
    return 50001


def sample_text(path: Path, fmt: str, words: int = 6000) -> str:
    """从书的中段取样（跳过前言和附录）。"""
    out, n = [], 0
    if fmt == "epub":
        with zipfile.ZipFile(path) as z:
            root = _xml(z.read("META-INF/container.xml"))
            opf = next(el.get("full-path") for el in root.iter() if el.tag.endswith("rootfile"))
            o = _xml(z.read(opf))
            items = {el.get("id"): el.get("href") for el in o.iter() if el.tag.endswith("}item") or el.tag == "item"}
            spine = [items.get(el.get("idref")) for el in o.iter() if el.tag.endswith("itemref")]
            spine = [s for s in spine if s]
            base = os.path.dirname(opf)
            if not spine:                     # 没有 spine 的不规范 epub：按文件名顺序读全部网页文件
                base = ""
                spine = sorted(n for n in z.namelist() if n.lower().endswith((".xhtml", ".html", ".htm")))
            a, b = len(spine) // 4, max(len(spine) // 4 + 1, 3 * len(spine) // 4)
            for href in spine[a:b] or spine:
                try:
                    raw = z.read(os.path.normpath(os.path.join(base, href)).replace("\\", "/")).decode("utf-8", "ignore")
                except KeyError:
                    continue
                t = re.sub(r"<[^>]+>", " ", re.sub(r"(?is)<(script|style|head)\b.*?</\1>", " ", raw))
                t = re.sub(r"&[a-z#0-9]+;", " ", t)
                out.append(t)
                n += len(t.split())
                if n >= words:
                    break
    elif fmt == "txt":
        raw = path.read_bytes()
        for enc in ("utf-8", "gb18030", "latin-1"):
            try:
                t = raw.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        mid = len(t) // 3
        out.append(t[mid: mid + words * 7])
    else:
        import pymupdf
        with pymupdf.open(path) as d:
            pages = list(range(d.page_count // 4, max(d.page_count // 4 + 1, 3 * d.page_count // 4)))
            step = max(1, len(pages) // 15)
            for i in pages[::step]:
                t = d[i].get_text()
                out.append(t)
                n += len(t.split())
                if n >= words:
                    break
    return "\n".join(out)


def measure(text: str, rank: dict) -> dict | None:
    sents = [s for s in re.split(r"(?<=[.!?])[\"”’']?\s+", text) if len(re.findall(r"[A-Za-z]+", s)) >= 3]
    toks = re.findall(r"[^\W\d_]+(?:['’][^\W\d_]+)?", text)       # 含重音字母（Vázquez 不会被切碎）
    # 大写开头的词多是人名地名，不计入（句首的常见词也随之略去，对分布影响很小）
    words = [t.lower().replace("’", "'") for t in toks if t[0].islower() and t.isascii() or (t[0].islower() and "'" in t)]
    if len(words) < 800:
        return None
    ranks = sorted(word_rank(w, rank) for w in words)
    v95 = ranks[int(len(ranks) * 0.95)]
    v98 = ranks[int(len(ranks) * 0.98)]
    sent_len = sum(len(re.findall(r"[A-Za-z]+", s)) for s in sents) / max(1, len(sents))
    rare = 1 - bisect_left(ranks, 5001) / len(ranks)
    level = next(name for cap, name in LEVELS if v95 <= cap)
    return {"level": level, "v95": v95, "v98": v98, "sent_len": round(sent_len, 1), "rare": round(rare, 4)}


_DICT: Path | None = None


def _init_level(dict_path: str) -> None:
    global _DICT
    _DICT = Path(dict_path)


def _level_child(path: str, fmt: str):
    return measure(sample_text(Path(path), fmt), vocab(_DICT))


def run_level(cat: Catalog, dict_path: Path, workers: int = 4, timeout: float = 60) -> None:
    """每个线程配一个常驻子进程：个别坏文件让 PyMuPDF 崩溃或卡死时只丢这一本。"""
    import threading
    from concurrent.futures import ThreadPoolExecutor, as_completed

    from .isolate import Isolated, IsolatedError
    c = db(cat)
    todo = c.execute("SELECT w.id, w.text_src, f.fmt FROM works w JOIN files f ON f.path=w.text_src "
                     "LEFT JOIN enrich e ON e.work=w.id WHERE w.kind='book' AND w.lang!='zh' AND e.level_at IS NULL "
                     "ORDER BY w.id").fetchall()
    print(f"难度分级：待算 {len(todo):,} 本", flush=True)
    local = threading.local()
    workers_all: list = []

    def one(r):
        iso = getattr(local, "iso", None)
        if iso is None:
            iso = local.iso = Isolated(timeout, _init_level, (str(dict_path),))
            workers_all.append(iso)
        try:
            return r["id"], iso.call(_level_child, r["text_src"], r["fmt"]), ""
        except IsolatedError as ex:
            return r["id"], None, str(ex)
        except Exception as ex:
            return r["id"], None, f"{type(ex).__name__}: {ex}"[:200]

    n, bad, t0 = 0, 0, time.time()
    with ThreadPoolExecutor(workers) as ex:
        for fut in as_completed([ex.submit(one, r) for r in todo]):
            wid, m, err = fut.result()
            if m:
                upsert(c, wid, level_at=time.time(), **m)
            else:
                bad += 1
                upsert(c, wid, level_at=time.time(), level=None)
                if err:
                    log.warning("难度失败 %s：%s", wid, err)
            c.commit()                      # 每本都提交：不长时间占着写锁（联网补全同时在写）
            n += 1
            if n % 500 == 0:
                print(f"{n:,}/{len(todo):,}  {n / (time.time() - t0):.1f} 本/秒  无法取样 {bad}", flush=True)
    for iso in workers_all:
        iso.close()
    dist = c.execute("SELECT level, COUNT(*) FROM enrich WHERE level IS NOT NULL GROUP BY level").fetchall()
    print("完成：" + "、".join(f"{lv} {k:,}" for lv, k in dist), flush=True)


def main(argv: list[str]) -> int:
    from .config import load
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname).1s %(message)s", datefmt="%H:%M:%S")
    cfg = load()
    cat = Catalog(cfg.data / "catalog.db")
    cmd = argv[0] if argv else "all"
    limit = int(argv[1]) if len(argv) > 1 else 0
    if cmd in ("online", "all"):
        run_online(cat, limit)
    if cmd in ("level", "all"):
        run_level(cat, cfg.dict_path)
    if cmd == "label":
        print("label（单级类别与一句话简介）已退役：书城只用两级分类，见 python -m library.classify")
        return 0
    if cmd not in ("online", "level", "all"):
        print(__doc__)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
