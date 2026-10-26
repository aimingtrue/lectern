"""概念导图：论述、科普、修行、哲学这类书（library/bookkind.py 定 map = concepts），右栏不画人物关系，
画当前这一节的关键概念、方法、观点和它们之间的关系，外加这一节的主旨一句话；正文读到哪个概念就点亮哪个。

按小节抽：目录里最细到二级的条目各是一节（「卷一」下的「第一章」……；只有一级目录的就是一章），
一节的正文按 2200 字分块各抽一份（一律 4k 上下文：和译文、书名翻译这些短任务一样，本机模型不用换上下文重载，
批量任务在跑时也排得上），多块时在本地合并（同一个说法归一个概念，按提及次数留前 12 个），主旨再问一次综合成一句；
太长的节均匀取 8 块。结果存 maps 表。形状和人物图一样
（people / links，people 里的 role 是这个概念的一句话解释），阅读页同一套画法。
"""
from __future__ import annotations

import json
import logging
import re
import time

from . import cast
from .web import toc4

log = logging.getLogger(__name__)
CHUNK, MAX_CHUNKS, LLM_TIMEOUT, CTX = 2200, 8, 300, 4096
KINDS = {"concept": "概念", "method": "方法", "claim": "观点", "person": "人物", "example": "例子"}

MAP = """你在帮读者理清《{title}》「{section}」这一节的思路。下面是这一节的一部分原文。只输出 JSON：
{{"thesis":"这部分的核心意思，一句话，不超过 40 字",
 "concepts":[{{"name":"关键概念、方法或观点的名称：原文里出现过的说法，原样照抄，2–8 个字","aliases":["原文里指同一个东西的其他说法，原样照抄"],"gist":"它在这里是什么意思，一句话，不超过 24 字","kind":"concept 概念 / method 方法 / claim 观点 / person 人物 / example 例子"}}],
 "links":[{{"a":"概念的 name","b":"另一个概念的 name","rel":"两者的关系，如 导致、包含、对立、方法、例证、前提，不超过 6 字"}}]}}
最多 10 个概念、14 条关系，挑真正撑起这部分论述的；name 和 aliases 必须是原文里原样出现过的字串（读到时要能点亮）。只写原文里有依据的，不要编造。"""
THESIS = """下面是同一节几个部分各自的核心意思。把它们综合成这一节的主旨，一句话，不超过 40 字。只输出 JSON：{"thesis":"……"}"""


def sections(b) -> list[tuple[int, str, int, int]]:
    """小节 [(目录序号, 标题, 首段, 末段 + 1)]：一级、二级目录条目，各自到下一个条目为止；没有正文的（卷名紧跟第一章）不算。"""
    title = (b["title"] or "").strip().lower()
    items = [(i, t, pn) for i, (lv, t, pg, pn) in enumerate(toc4(b)) if lv <= 2 and pn is not None and t.strip().lower() != title]
    n = b["n_paras"] or 0
    out = []
    for k, (i, t, s) in enumerate(items):
        e = items[k + 1][2] if k + 1 < len(items) else n
        if e > s:
            out.append((i, t, s, e))
    return out


def section_at(b, para: int):
    return next((c for c in reversed(sections(b)) if c[2] <= para), None)


def _s(v, n: int) -> str:
    return re.sub(r"\s+", " ", str(v)).strip()[:n] if isinstance(v, (str, int, float)) else ""


def _gist(v) -> str:
    """一句话解释，最多 30 字；长了在最后一个逗号、顿号处截断。"""
    return cast._role(v)


def clean(raw, text: str) -> dict:
    """模型输出 → 可存的结构：概念的名称、说法得是原文里出现过的（正文里才点得亮），一个说法只归一个概念；
    关系两端都得是这里的概念；n = 这一节里提到的次数（画圆点大小）。"""
    people, by = [], {}
    items = raw.get("concepts") if isinstance(raw, dict) else None
    for c in items or []:
        if not isinstance(c, dict) or len(people) >= 12:
            continue
        name = cast.norm_alias(_s(c.get("name"), 20))
        if not name or name.lower() in by:
            continue
        aliases = [a for a in dict.fromkeys([name] + [cast.norm_alias(_s(x, 20)) for x in (c.get("aliases") or []) if isinstance(x, str)])
                   if len(a) >= 2 and a in text and a.lower() not in by]
        if not aliases:
            continue                          # 原文里一个说法都找不到：多半是模型自己概括的词，读的时候点不亮
        if name not in aliases:
            name = aliases[0]
        k = str(c.get("kind") or "").strip().lower()
        people.append({"name": name, "aliases": aliases, "role": _gist(c.get("gist")), "kind": k if k in KINDS else "concept", "n": 0})
        for a in aliases:
            by[a.lower()] = name
    cnt: dict = {}
    for h in cast.mentions(text, people):
        cnt[h["name"]] = cnt.get(h["name"], 0) + 1
    for p in people:
        p["n"] = cnt.get(p["name"], 0)
    links, seen = [], set()
    for l in (raw.get("links") if isinstance(raw, dict) else None) or []:
        if not isinstance(l, dict) or len(links) >= 16:
            continue
        a, b, rel = by.get(_s(l.get("a"), 20).lower()), by.get(_s(l.get("b"), 20).lower()), _s(l.get("rel"), 8)
        if not a or not b or a == b or not rel or frozenset((a, b)) in seen:
            continue
        seen.add(frozenset((a, b)))
        links.append({"a": a, "b": b, "rel": rel})
    thesis = _s(raw.get("thesis"), 60) if isinstance(raw, dict) else ""
    return {"people": people, "links": links, "thesis": thesis}


def merge(parts: list[dict]) -> dict:
    """几块各自校验过的导图并成一份：名称或说法对得上的是同一个概念（说法并集、解释取先出现的、提及次数取最大，
    因为各块都按整节原文数过），按提及次数留前 12 个；关系两端都留下了才要，每对一条。"""
    people, owner = [], {}
    for part in parts:
        local = {}
        for p in part["people"]:
            q = next((owner[a.lower()] for a in p["aliases"] if a.lower() in owner), None)
            if q is None:
                q = {**p, "aliases": []}
                people.append(q)
            q["n"] = max(q["n"], p["n"])
            for a in p["aliases"]:
                if a.lower() not in owner:
                    owner[a.lower()] = q
                    q["aliases"].append(a)
            local[p["name"]] = q
        part["_local"] = local
    keep = sorted(people, key=lambda q: -q["n"])[:12]
    ids = {id(q) for q in keep}
    links, seen = [], set()
    for part in parts:
        for l in part["links"]:
            a, b = part["_local"].get(l["a"]), part["_local"].get(l["b"])
            if a and b and a is not b and id(a) in ids and id(b) in ids and frozenset((id(a), id(b))) not in seen and len(links) < 16:
                seen.add(frozenset((id(a), id(b))))
                links.append({"a": a["name"], "b": b["name"], "rel": l["rel"]})
    return {"people": [q for q in people if id(q) in ids], "links": links}


def chunks_of(text: str) -> list[str]:
    """按 CHUNK 字切块，尽量切在段落之间；多于 MAX_CHUNKS 块时均匀取 MAX_CHUNKS 块。"""
    out, cur = [], ""
    for para in text.split("\n\n"):
        while len(para) > CHUNK:                 # 一段就超长：硬切
            if cur:
                out.append(cur)
                cur = ""
            out.append(para[:CHUNK])
            para = para[CHUNK:]
        if cur and len(cur) + len(para) + 2 > CHUNK:
            out.append(cur)
            cur = ""
        cur = f"{cur}\n\n{para}" if cur else para
    if cur:
        out.append(cur)
    if len(out) > MAX_CHUNKS:
        out = [out[i * len(out) // MAX_CHUNKS] for i in range(MAX_CHUNKS)]
    return out


def extract(st, b, sec: int, title: str, start: int, end: int) -> dict:
    cfg, db = st.cfg, st.db
    text = "\n\n".join(p["text"] for p in db.paras_range(b["id"], start, end))
    if len(text) < 300:                       # 只有标题、几行字：不抽，记一份空的
        out = {"people": [], "links": [], "thesis": ""}
        db.run("INSERT OR REPLACE INTO maps VALUES(?,?,?,?)", (b["id"], sec, json.dumps(out), time.time()))
        return out
    parts = []
    for c in chunks_of(text):
        try:
            parts.append(clean(st.llm.chat_json(cfg.model, MAP.format(title=b["title"], section=title), c, num_ctx=CTX, timeout=LLM_TIMEOUT), text))
        except Exception as ex:  # noqa: BLE001
            log.warning("概念导图：一块失败 %s 第 %s 项：%s", b["id"], sec, ex)
    if not parts:
        raise RuntimeError("概念导图抽取失败：每一块都没有结果")
    if len(parts) == 1:
        out = parts[0]
    else:
        theses = [p["thesis"] for p in parts if p["thesis"]]
        try:
            thesis = _s(st.llm.chat_json(cfg.model, THESIS, "\n".join(f"- {t}" for t in theses), num_ctx=CTX, timeout=LLM_TIMEOUT).get("thesis"), 60)
        except Exception as ex:  # noqa: BLE001
            log.info("概念导图：主旨没综合上 %s 第 %s 项：%s", b["id"], sec, ex)
            thesis = ""
        out = {**merge(parts), "thesis": thesis or (theses[0] if theses else "")}
    db.run("INSERT OR REPLACE INTO maps VALUES(?,?,?,?)", (b["id"], sec, json.dumps(out, ensure_ascii=False), time.time()))
    return out


def ensure(st, b, para: int, stop=lambda: False, ahead: int = 2) -> None:
    """读到 para 时：当前这一节没抽就先抽，再抽后面 ahead 节（读到时不用等）。出错只记日志。"""
    secs = sections(b)
    k = next((j for j in range(len(secs) - 1, -1, -1) if secs[j][2] <= para), None)
    if k is None:
        return
    have = {r["sec"] for r in st.db.q("SELECT sec FROM maps WHERE book=?", (b["id"],))}
    for i, title, s, e in secs[k:k + 1 + ahead]:
        if stop():
            return
        if i in have:
            continue
        try:
            extract(st, b, i, title, s, e)
        except Exception as ex:  # noqa: BLE001
            log.warning("概念导图抽取失败 %s 第 %s 项：%s", b["id"], i, ex)
            return


def missing(db, b, para: int) -> bool:
    """读到的这一节还没有概念导图。"""
    sec = section_at(b, para)
    return bool(sec) and not db.one("SELECT 1 FROM maps WHERE book=? AND sec=?", (b["id"], sec[0]))


def at(db, b, para: int) -> dict:
    """读到 para 时这一节的概念导图；span = 这一节的段落范围（阅读页读出范围再来要），pending = 还没抽。"""
    sec = section_at(b, para)
    if not sec:
        first = sections(b)
        return {"people": [], "links": [], "thesis": "", "span": [0, first[0][2] if first else (b["n_paras"] or 0)], "pending": False}
    r = db.one("SELECT json FROM maps WHERE book=? AND sec=?", (b["id"], sec[0]))
    d = json.loads(r["json"]) if r else {"people": [], "links": [], "thesis": ""}
    return {**d, "title": sec[1], "span": [sec[2], sec[3]], "pending": r is None}


def register(app, st) -> None:
    from . import bookkind
    db = st.db

    @app.get("/api/books/{bid}/map")
    def map_api(bid: str, para: int = 0):
        """阅读页右栏的导图：kind = people（到这一章为止累计的人物关系，同 /cast）或 concepts（这一节的概念导图）。
        span = 这份图管的段落范围，读出范围再来要；mode = 读法（页面打开时还没定、后来定了的，页面据此重载）。"""
        b = st.need(bid)
        kind = bookkind.stored(db, bid) or bookkind.guess(db, b)
        if kind.get("map") == "concepts":
            return {**at(db, b, para), "kind": "concepts", "mode": kind["mode"]}
        cur = cast.chapter_at(b, para)
        if not cur:
            first = cast.chapters(b)
            return {"people": [], "links": [], "narrator": None, "kind": "people", "mode": kind["mode"], "pending": False,
                    "span": [0, first[0][2] if first else (b["n_paras"] or 0)]}
        rows = db.q("SELECT chapter, json FROM cast WHERE book=? AND chapter<=? ORDER BY chapter", (bid, cur[0]))
        out = cast.merge([json.loads(r["json"]) for r in rows])
        return {**out, "kind": "people", "mode": kind["mode"], "span": [cur[2], cur[3]],
                "pending": cur[0] not in {r["chapter"] for r in rows}}
