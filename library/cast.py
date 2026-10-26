"""人物关系图：按一级章节用本机大模型抽出人物（称呼、别名、身份）和两两关系，存 cast 表；
阅读页按读到的章节累计合并（只看读过的章，不剧透），正文里出现谁就点亮谁。

抽取挂在按章预生成里（library/narrate.py 的 _prep_run），不另占长任务锁；也可以 POST /api/books/{bid}/cast/{chapter} 单独跑。
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time

from fastapi import FastAPI, HTTPException

from . import busy
from .web import toc4

log = logging.getLogger(__name__)
CHUNK, MAX_CHUNKS = 12000, 6
LLM_TIMEOUT = 300                  # 每次模型调用的上限（秒）：空闲时一块约 30 s；一次超时不能拖住整章的预生成
NOT_ALIAS = {"i", "me", "my", "he", "she", "him", "her", "they", "we", "you", "it", "the", "narrator", "叙述者", "我"}
TITLES = {"mr", "mrs", "ms", "miss", "dr", "sir", "lady", "lord", "madame", "mme", "captain", "colonel", "major", "professor",
          "uncle", "aunt", "doctor", "old", "young", "little"}

MAP = """你在帮中国读者读英文原著《{title}》。下面是「{chapter}」的一部分原文（可能有 PDF 换行瑕疵）。只输出 JSON：
{{"people":[{{"name":"这个人物在原文里最常用的称呼，照原文语言原样写","aliases":["原文里指同一个人的其他叫法：姓、名、带称谓的写法，每个都照原文原样写"],"role":"身份、与主要人物的关系，简体中文，不超过 20 字","gender":"m 男 / f 女 / ? 不明"}}],
 "links":[{{"a":"人物的 name","b":"另一人物的 name","rel":"两人的关系，简体中文，不超过 8 字"}}],
 "narrator":"第一人称叙述者（用「我」讲故事的那个人物）在 people 里的 name；第三人称叙述、或叙述者不是书中人物就写 null"}}
只写这段原文里出场或被明确提到的人物（最多 12 个），只写原文里有依据的关系；第一人称叙述者用他的名字，原文没给名字就写「叙述者」。不要编造。"""
REDUCE = """把同一章几个部分各自抽出的人物和关系合并成一份：同一个人只留一条（aliases 合并，role 取更完整的那个，gender 取明确的那个）；
关系去重，同一对人有矛盾时取更明确的一条；narrator 取各部分一致的那个（没有就 null）。只输出同样结构的 JSON，人物最多 15 个，关系最多 25 条。"""


def chapters(b) -> list[tuple[int, str, int, int]]:
    """一级章节 [(目录序号, 标题, 首段, 末段 + 1)]，范围和章节导读、预生成一致。"""
    title = (b["title"] or "").strip().lower()
    lv1 = [(i, t, pn) for i, (lv, t, pg, pn) in enumerate(toc4(b)) if lv == 1 and t.strip().lower() != title]   # 目录里的书名项不是章
    n = b["n_paras"] or 0
    return [(i, t, s, max(lv1[k + 1][2] if k + 1 < len(lv1) else n, s + 1)) for k, (i, t, s) in enumerate(lv1)]


def chapter_at(b, para: int):
    """段落 para 在哪一章；没有目录、或在第一章之前返回 None。"""
    return next((c for c in reversed(chapters(b)) if c[2] <= para), None)


def _s(v, n: int) -> str:
    return re.sub(r"\s+", " ", str(v)).strip()[:n] if isinstance(v, (str, int, float)) else ""


def _role(v) -> str:
    """身份一句话，最多 30 字；模型写长了就在最后一个逗号、顿号处截断，不留半句。"""
    full = _s(v, 200)
    if len(full) <= 30:
        return full
    cut = full[:30]
    k = max(cut.rfind(c) for c in "，、；,;")
    return cut[:k] if k >= 8 else cut


def _words(name: str) -> set[str]:
    return {w for w in re.findall(r"[A-Za-z\u3400-\u9fff]+", re.sub(r"['’]s\b", "", name.lower())) if w not in TITLES}


FEMALE, MALE = {"mrs", "miss", "ms", "lady", "madame", "mme", "aunt"}, {"mr", "sir", "lord", "uncle"}


def gender(name: str) -> str | None:
    """称谓里带的性别：Mrs. Wilson 和 Mr. Wilson 不是一个人。"""
    t = name.split()[0].lower().rstrip(".") if name.split() else ""
    return "f" if t in FEMALE else "m" if t in MALE else None


def possessive(name: str) -> bool:
    """「Gatsby’s butler」这种：说的是别人，不是 Gatsby。"""
    return bool(re.search(r"['’]s\s", name))


def plausible(alias: str, name: str, aliases=()) -> bool:
    """这个叫法真能指这个人：单个词的（昵称、姓）都行；多个词的得和称呼、或这个人某个单词的叫法共享一个实词
    （Mr. Carraway ← Nick Carraway，或 ← Nick + Carraway）；「Finnish woman」「Mrs. Sigourney Howard」这种模型顺手挂上的不要。"""
    if alias == name or len(alias.split()) == 1:
        return True
    base = _words(name) | {a.lower() for a in aliases if len(a.split()) == 1}
    return bool(_words(alias) & base)


def norm_alias(a: str) -> str:
    """叫法规整：去掉所有格（Gatsby's → Gatsby）、首尾的引号和标点。"""
    return re.sub(r"['’]s$", "", a.strip().strip("\"“”‘’'.,;:!?()[]")).strip()


def ok_alias(a: str) -> bool:
    """能当作在正文里认人的叫法：至少 2 个字符、含字母或汉字、不是代词之类；英文的要大写开头、不超过 3 个词、
    不是「A stout man」这种描述。"""
    if len(a) < 2 or a.lower() in NOT_ALIAS or not re.search(r"[A-Za-z㐀-鿿]", a):
        return False
    if re.search(r"[A-Za-z]", a) and (len(a.split()) > 3 or re.match(r"(?i)(a|an|the|some|one)\s", a)):
        return False
    return not a[0].isalpha() or a[0].isupper() or bool(re.search(r"[㐀-鿿]", a))


def mentions(text: str, people: list[dict]) -> list[dict]:
    """句子（或整章）里提到的人：别名按长的先配（Mrs. Wilson 先于 Wilson），整词、区分大小写，所有格的 's 也算；
    返回 [{a, b, name}]，位置不重叠、按出现顺序。前端 static/reader/main.js 里有同样的一份。"""
    pairs = sorted(((a, p["name"]) for p in people for a in p["aliases"]), key=lambda x: -len(x[0]))
    hits: list[dict] = []
    for a, name in pairs:
        for m in re.finditer(rf"(?<![A-Za-z]){re.escape(a)}(?:['’]s)?(?![A-Za-z])", text):
            if not any(m.start() < h["b"] and m.end() > h["a"] for h in hits):
                hits.append({"a": m.start(), "b": m.end(), "name": name})
    return sorted(hits, key=lambda h: h["a"])


def clean(raw, text: str) -> dict:
    """模型输出 → 可存的结构：人物有 name 和能认人的别名、role ≤ 30 字；关系两端都得是这里的人物、rel ≤ 12 字、每对只留一条。
    n = 这段原文里提到的次数（别名按位置去重），画图时定圆点大小。"""
    people, by_alias = [], {}
    for p in (raw.get("people") if isinstance(raw, dict) else None) or []:
        if not isinstance(p, dict) or len(people) >= 15:
            continue
        name = norm_alias(_s(p.get("name"), 40))
        if not name or name.lower() in by_alias:
            continue
        aliases = [a for a in dict.fromkeys([name] + [norm_alias(_s(x, 40)) for x in (p.get("aliases") or []) if isinstance(x, str)])
                   if a and ok_alias(a) and a.lower() not in by_alias]
        if not aliases:
            continue
        if not ok_alias(name):                   # 模型把叙述者写成「叙述者」、把人写成「butler」：有真名就用真名当称呼
            name = aliases[0]
        aliases = [a for a in aliases if plausible(a, name, aliases)]
        g = str(p.get("gender") or "").strip().lower()[:1]
        person = {"name": name, "aliases": aliases, "role": _role(p.get("role")), "n": 0}
        if g in ("m", "f") or (g := gender(name)):      # 模型没说的，称谓里带的也算（Mrs. Wilson）
            person["g"] = g
        people.append(person)
        by_alias[name.lower()] = name
        for a in aliases:
            by_alias[a.lower()] = name
    cnt: dict = {}
    for h in mentions(text, people):
        cnt[h["name"]] = cnt.get(h["name"], 0) + 1
    for p in people:
        p["n"] = cnt.get(p["name"], 0)
    links, seen = [], set()
    for l in (raw.get("links") if isinstance(raw, dict) else None) or []:
        if not isinstance(l, dict) or len(links) >= 25:
            continue
        a, b, rel = by_alias.get(_s(l.get("a"), 40).lower()), by_alias.get(_s(l.get("b"), 40).lower()), _s(l.get("rel"), 12)
        if not a or not b or a == b or not rel or frozenset((a, b)) in seen:
            continue
        seen.add(frozenset((a, b)))
        links.append({"a": a, "b": b, "rel": rel})
    return {"people": people, "links": links, "narrator": narrator_of(raw, by_alias, people)}


NARRATOR_ROLE = re.compile(r"(?:^|[，,、；; ])(?:第一人称|故事|本书|小说)?(?:叙述者|讲述者|叙事者|narrator)(?![的之])", re.I)   # 「叙述者的表妹」不算


def narrator_of(raw, by_alias: dict, people: list[dict]) -> str | None:
    """这一章的叙述者（第一人称讲故事的那个人物）的称呼：模型给的 narrator 对得上人物表就用它；
    没给（早先抽的章）就看谁的身份写着「叙述者」。第三人称叙述返回 None。"""
    v = by_alias.get(_s(raw.get("narrator"), 40).lower()) if isinstance(raw, dict) else None
    if v:
        return v
    return next((p["name"] for p in people if NARRATOR_ROLE.search(p.get("role") or "")), None)


def merge(chaps: list[dict]) -> dict:
    """几章累计：同一个人（name 或别名对得上，别名被两个人共用的不算）合并，别名并集、身份取最近一章的、提及次数相加，
    称呼用最完整的那个（Jordan Baker 胜过 Miss Baker）；关系并集，同一对人取最近一章的说法。"""
    people: list[dict] = []
    owner: dict = {}                     # 小写叫法 → 人物（被不止一个人用的叫法记 None）

    words = _words

    def compatible(a: str, b: str) -> bool:
        """靠别名对上的两个称呼是不是同一个人：短的那个名字的词都在长的里（Gatsby ⊂ Jay Gatsby）；
        Myrtle Wilson 和 George Wilson 只是同姓，不算。"""
        if possessive(a) != possessive(b) or (gender(a) and gender(b) and gender(a) != gender(b)):
            return False
        wa, wb = words(a), words(b)
        return bool(wa and wb) and (wa <= wb or wb <= wa)

    def keys(x: dict) -> set[str]:
        """能用来认人的「名」：全名里去掉姓（最后一个词）的部分，加上单个词、又不是这个人的姓的叫法。
        姓不算（一家人都姓 Wilson），带称谓的只有姓（Mrs. Wilson）也不算。"""
        toks = x["name"].split()
        out = set()
        if len(toks) >= 2 and not gender(x["name"]) and not possessive(x["name"]):
            out |= words(" ".join(toks[:-1]))
        sur = toks[-1].lower() if len(toks) >= 2 else None
        out |= {a.lower() for a in [x["name"]] + x["aliases"] if len(a.split()) == 1 and a.lower() not in TITLES and a.lower() != sur}
        return out

    def related(p: dict, q: dict) -> bool:
        """没有共同叫法时，凭「名」认：Nick ⊂ Nick Carraway，Myrtle（Mrs. Wilson 的叫法）⊂ Myrtle Wilson。"""
        if possessive(p["name"]) != possessive(q["name"]) or (gender(p["name"]) and gender(q["name"]) and gender(p["name"]) != gender(q["name"])):
            return False
        return bool(keys(p) & words(q["name"])) or bool(keys(q) & words(p["name"]))

    def better(a: str, b: str) -> bool:
        """称呼 a 比 b 更完整：实词更多；一样多时不带称谓的胜过带称谓的（Jordan Baker > Miss Baker）。"""
        wa, wb = words(a), words(b)
        return len(wa) > len(wb) or (len(wa) == len(wb) and len(a.split()) < len(b.split()))

    def find(p: dict):
        q = owner.get(p["name"].lower())
        if q:
            return q
        hits = {id(q): q for a in p["aliases"] if (q := owner.get(a.lower())) and compatible(p["name"], q["name"])}
        if not hits:                     # 叫法都没对上：凭「名」认（Nick ⊂ Nick Carraway），且只有一个这样的人
            hits = {id(q): q for q in people if related(p, q)}
        return next(iter(hits.values())) if len(hits) == 1 else None
    links: dict = {}                     # 两个人物对象 → 关系
    narrator = None                      # 叙述者：取最近一章说了的
    for ch in chaps:
        who = {}
        for p in ch.get("people") or []:
            q = find(p)
            if q is None:
                q = {"name": p["name"], "aliases": [], "role": "", "n": 0}
                people.append(q)
            elif better(p["name"], q["name"]):
                q["name"] = p["name"]
            for a in [p["name"]] + [a for a in p["aliases"] if plausible(a, p["name"], p["aliases"] + q["aliases"])]:   # 早先存的章也按现在的规则过一遍
                if a.lower() not in owner:
                    owner[a.lower()] = q
                    q["aliases"].append(a)
                elif owner[a.lower()] is not q:
                    owner[a.lower()] = None    # 两个人都这么叫（Wilson 夫妇）：这个叫法不再用来认人
            q["role"] = p.get("role") or q["role"]
            q["n"] += p.get("n") or 0
            if p.get("g") and not q.get("g"):
                q["g"] = p["g"]
            who[p["name"]] = q
        for l in ch.get("links") or []:
            a, b = who.get(l["a"]), who.get(l["b"])
            if a and b and a is not b:
                links[frozenset((id(a), id(b)))] = (a, b, l["rel"])
        n = ch.get("narrator") or next((p["name"] for p in ch.get("people") or [] if NARRATOR_ROLE.search(p.get("role") or "")), None)
        if n and n in who:
            narrator = who[n]
    for q in people:
        q["aliases"] = [a for a in q["aliases"] if owner.get(a.lower()) is q]
        for a in q["aliases"]:                   # 称呼用最完整的那个叫法（Mrs. Wilson → Myrtle Wilson；所有格的「Gatsby's mansion」不算）
            if not possessive(a) and better(a, q["name"]):
                q["name"] = a
        if q["name"] not in q["aliases"]:
            q["aliases"].insert(0, q["name"])
    linked = {id(x) for a, b, _ in links.values() for x in (a, b)}
    people = [q for q in people if q["n"] or id(q) in linked or q is narrator]   # 正文里从没按名字出现、又没有关系的，不画（叙述者除外：他在讲故事）
    return {"people": people, "links": [{"a": a["name"], "b": b["name"], "rel": rel} for a, b, rel in links.values()],
            "narrator": narrator["name"] if narrator is not None else None}


_all_cache: dict = {}


def people_all(db, bid: str) -> dict:
    """全书（已抽的所有章）累计的人物与叙述者：对白配音按它认人（Nick 和 Nick Carraway 是一个人、一种声音）。
    按 (书, 行数, 最近一次抽取时间) 缓存；查一次主键索引 < 0.1 ms。"""
    k = db.one("SELECT COUNT(*) n, MAX(created) t FROM cast WHERE book=?", (bid,))
    key = (bid, k["n"], k["t"])
    if key not in _all_cache:
        rows = db.q("SELECT json FROM cast WHERE book=? ORDER BY chapter", (bid,))
        _all_cache.clear()
        _all_cache[key] = merge([json.loads(r["json"]) for r in rows])
    return _all_cache[key]


def extract(st, b, chapter: int, title: str, start: int, end: int) -> dict:
    """抽一章：原文按 12k 字分块（最多 6 块）各抽一份，多块时再合并；结果校验后存 cast 表。"""
    cfg, db = st.cfg, st.db
    text = "\n\n".join(p["text"] for p in db.paras_range(b["id"], start, end))
    if len(text) < 600:                      # 扉页、只有标题的项：没有正文可抽，记一份空的，模型不会凭书名编人物
        out = {"people": [], "links": []}
        db.run("INSERT OR REPLACE INTO cast VALUES(?,?,?,?)", (b["id"], chapter, json.dumps(out), time.time()))
        return out
    chunks = [text[i:i + CHUNK] for i in range(0, len(text), CHUNK)][:MAX_CHUNKS] or [""]
    parts = []
    for c in chunks:                         # 一块超时（120 s）就跳过这一块，别的块照抽；全部失败才算失败
        try:
            parts.append(st.llm.chat_json(cfg.model, MAP.format(title=b["title"], chapter=title), c, num_ctx=16384, timeout=LLM_TIMEOUT))
        except Exception as ex:  # noqa: BLE001
            log.warning("人物抽取：一块失败 %s 第 %s 项：%s", b["id"], chapter, ex)
    if not parts:
        raise RuntimeError("人物抽取失败：每一块都没有结果")
    raw = parts[0] if len(parts) == 1 else st.llm.chat_json(cfg.model, REDUCE, json.dumps(parts, ensure_ascii=False), num_ctx=16384, timeout=LLM_TIMEOUT)
    out = clean(raw, text)
    db.run("INSERT OR REPLACE INTO cast VALUES(?,?,?,?)", (b["id"], chapter, json.dumps(out, ensure_ascii=False), time.time()))
    return out


def ensure(st, b, para: int, stop=lambda: False) -> None:
    """读到 para 时该有的人物图：当前章没有就先抽当前章，再补前面缺的章（只抽、不重抽）。出错只记日志。"""
    cur = chapter_at(b, para)
    if not cur:
        return
    have = {r["chapter"] for r in st.db.q("SELECT chapter FROM cast WHERE book=?", (b["id"],))}
    todo = [cur] + [c for c in chapters(b) if c[0] < cur[0]]
    for i, title, s, e in todo:
        if stop():
            return
        if i in have:
            continue
        try:
            extract(st, b, i, title, s, e)
        except Exception as ex:  # noqa: BLE001
            log.warning("人物抽取失败 %s 第 %s 项：%s", b["id"], i, ex)
            return


def register(app: FastAPI, st) -> None:
    db = st.db

    @app.get("/api/books/{bid}/cast")
    def cast_api(bid: str, para: int = 0):
        """读到 para 时的累计人物图（到这一章为止）。pending = 这一章还没抽（预生成跑起来会补）。"""
        b = st.need(bid)
        cur = chapter_at(b, para)
        if not cur:
            return {"people": [], "links": [], "narrator": None, "chapter": None, "pending": False}
        rows = db.q("SELECT chapter, json FROM cast WHERE book=? AND chapter<=? ORDER BY chapter", (bid, cur[0]))
        out = merge([json.loads(r["json"]) for r in rows])
        return {**out, "chapter": cur[0], "pending": cur[0] not in {r["chapter"] for r in rows}}

    @app.post("/api/books/{bid}/cast/{chapter}")
    def cast_start(bid: str, chapter: int):
        """单独抽一章（长任务，和导读一样严格串行）；预生成正在跑时拒绝。"""
        b = st.fresh(bid)
        c = next((c for c in chapters(b) if c[0] == chapter), None)
        if not c:
            raise HTTPException(404, "no such chapter")
        why = st.busy_reason()
        if why:
            raise HTTPException(409, f"{why}，为避免同时跑两件重活，稍后再试")

        def run():
            try:
                with busy.hold("character extraction"):
                    extract(st, b, *c)
            except Exception as ex:  # noqa: BLE001
                log.warning("人物抽取失败 %s 第 %s 项：%s", bid, chapter, ex)
        threading.Thread(target=run, daemon=True).start()
        return {"ok": True}
