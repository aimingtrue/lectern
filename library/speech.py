"""多角色配音：对白归属 + 按人物性别分配音色 + 一句话按对白切段。

  对白   正文里成对引号包住的部分（“…” "…" ‘…’ 「…」『…』），按段记区间；一个区间 = 一条对白
  归属   按章预生成时（library/narrate.py 的 _prep_run，在人物抽取之后）、以及全书准备（library/roles.py）时，
         把这一段范围里的对白编号【n】插进原文，交给本机模型只回答每条是谁说的（优先用人物表里的称呼）和性别 m / f / ?。
         结果存 speech 表（按段），每一块都做成的范围才记进 speech_done（失败的块下次只补缺的段）；
         人物的性别和音色记在 speakers
  认人   说话人按全书人物表（cast 表各章累计，library/cast.py 的 people_all）归到同一个人：Nick 和 Nick Carraway 是一个人，
         一个人只有一种声音；叙述者（第一人称讲故事的那个人物）自己的对白用旁白音色，不另配
  年龄   分音色前，还没定年龄的说话人一次交给本机模型（称呼 · 人物表里的身份 · 第一句对白）判断是孩子还是大人，
         记在 speakers.age（child / adult），定了不变
  音色   按性别和年龄从装好的同语言音色里挑：孩子只用同性别的童声（voices.json 里 age: child 的合成童声，没装才退回成人音色），
         大人不用童声；按 tts/voices.json 的 cast_order（好听、知名度高的在前）从前往后，戏份重的人物先挑，
         不用旁白那一个，这本书里没人用过的优先；池子用完了复用用得最少的；性别不明的人物用旁白音色。不随机
  切段   朗读一句时：句子落在对白区间里的部分用说话人的音色，其余用旁白，相邻同音色的合并；没有对白的句子原样一段
任何书都一样：全部按 book 键，打开一章就自动做；中文对白走中文音色池。
"""
from __future__ import annotations

import json
import logging
import re
import time

from .text import is_zh

log = logging.getLogger(__name__)
CHUNK = 10000                  # 一次交给模型的原文字数（含没有对白的段落，作为上下文）
LLM_TIMEOUT = 300              # 每次模型调用的上限（秒）：空闲时一块 10k 字约 36 s，和别的模型任务抢时会慢几倍
PAIRS = {"“": "”", "\"": "\"", "‘": "’", "「": "」", "『": "』", "«": "»"}
SPEAKABLE = re.compile(r"[A-Za-z0-9㐀-鿿]")
UNKNOWN = {"", "?", "？", "unknown", "narrator", "叙述者", "不明", "none", "null"}

PROMPT = """你在帮中国读者读英文原著《{title}》，要给对白配上说话人的声音。下面是正文的一部分，引号里的对白前面标了编号【n】。
只输出 JSON：{{"quotes":[{{"i":编号,"who":"说这句话的人物","g":"m 男 / f 女 / ? 不明"}}]}}
who：优先用人物表里的称呼；表里没有就照原文的叫法写（如 the clerk）；第一人称叙述者说的写他的名字；实在判断不了写 "?"。
每个编号都要有一条，不要漏、不要编造。
人物表：{names}"""
AGE_PROMPT = """你在帮中国读者读英文原著《{title}》，要给人物配声音：孩子用童声，大人用成年人的声音。
下面每行是书里一个说话的人物：称呼 · 身份 · 他说过的一句话。判断每个人在书里说话时是孩子还是大人。
只输出 JSON：{{"ages":[{{"who":"称呼，照抄","a":"c 孩子（大约 13 岁以下，含幼儿）/ a 大人（含青少年）/ ? 判断不了"}}]}}
每个人物都要有一条，不要漏、不要编造。"""


# ---------------------------------------------------------------- 引号
def quotes(text: str) -> list[tuple[int, int]]:
    """一段里的对白区间 [(起, 止)]，含引号本身，按码点。只取最外层；没闭合的引号到段末为止。
    单引号 ‘ 只在开头、空白或左括号之后算开，没闭合的不算；’ 后面紧跟字母的（it’s）不算关。直引号 " 成对开关。"""
    out: list[tuple[int, int]] = []
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c not in PAIRS or (c == "‘" and not (i == 0 or text[i - 1].isspace() or text[i - 1] in "([{（「")):
            i += 1
            continue
        close, depth, j = PAIRS[c], 1, i + 1
        while j < n:
            d = text[j]
            if d == close and (close != "’" or not (j + 1 < n and text[j + 1].isalpha())):
                depth -= 1
                if depth == 0:
                    break
            elif d == c and c != close and not (c == "‘" and not (text[j - 1].isspace() or text[j - 1] in "([{（「")):
                depth += 1
            j += 1
        if j >= n:                                   # 没闭合：双引号到段末；单引号（’Tis、省略写法）不算
            if c == "‘":
                i += 1
                continue
            j = n - 1
        if SPEAKABLE.search(text[i + 1:j]):
            out.append((i, j + 1))
        i = j + 1
    return out


def quoted_sents(db, bid: str, sents: list[tuple[int, str]]) -> set[tuple[int, str]]:
    """哪些句子的朗读计划取决于对白归属：落在所在段落某个引号区间里的（含自己没有引号字符、处在跨句对白中间的，
    如 “Let's go. Tomorrow!” 的第二句）。其余句子不管归属做没做，segments() 都只切出一段旁白，缓存键一样。
    段落读不到或句子定位不到的按「不等归属」处理（和整句旁白一致）。"""
    out: set[tuple[int, str]] = set()
    spans: dict[int, tuple[str, list[tuple[int, int]]]] = {}
    for n, s in sents:
        if n not in spans:
            p = db.one("SELECT text FROM paras WHERE book=? AND n=?", (bid, n))
            spans[n] = (p["text"], quotes(p["text"])) if p else ("", [])
        text, qs = spans[n]
        a = text.find(s)
        if a >= 0 and any(x < a + len(s) and a < y for x, y in qs):
            out.add((n, s))
    return out


# ---------------------------------------------------------------- 归属
def _s(v, n: int) -> str:
    return re.sub(r"\s+", " ", str(v)).strip()[:n] if isinstance(v, (str, int, float)) else ""


def _gender(v) -> str:
    g = _s(v, 4).lower()[:1]
    return g if g in ("m", "f") else "?"


def clean(raw, numbered: list[tuple[int, int, int]], by_alias: dict[str, str] | None = None) -> dict:
    """模型输出 → {编号: (who, g)}。who 规整：去首尾引号标点、按人物表别名归到称呼；不明的不要。"""
    by_alias = by_alias or {}
    out: dict[int, tuple[str, str]] = {}
    valid = {i for i, _, _ in numbered}
    for q in (raw.get("quotes") if isinstance(raw, dict) else None) or []:
        if not isinstance(q, dict):
            continue
        try:
            i = int(q.get("i"))
        except (TypeError, ValueError):
            continue
        who = _s(q.get("who"), 40).strip("\"“”‘’'.,;:!?()[]【】 ")
        if i not in valid or who.lower() in UNKNOWN or not SPEAKABLE.search(who):
            continue
        who = by_alias.get(who.lower(), who)
        out[i] = (who, _gender(q.get("g")))
    return out


def chunks(paras: list[dict]) -> list[list[dict]]:
    """段落按 CHUNK 字数分块；一块里至少一段。"""
    out, cur, size = [], [], 0
    for p in paras:
        if cur and size + len(p["text"]) > CHUNK:
            out.append(cur); cur, size = [], 0
        cur.append(p); size += len(p["text"])
    if cur:
        out.append(cur)
    return out


def numbered_text(paras: list[dict], start_no: int = 1) -> tuple[str, list[tuple[int, int, int]]]:
    """把【n】插到每条对白前面；返回 (文本, [(编号, 段号, 对白下标)])。"""
    lines, index, k = [], [], start_no
    for p in paras:
        qs = quotes(p["text"])
        if not qs:
            lines.append(p["text"]); continue
        s, last = "", 0
        for qi, (a, b) in enumerate(qs):
            s += p["text"][last:a] + f"【{k}】"
            index.append((k, p["n"], qi)); k += 1
            last = a
        lines.append(s + p["text"][last:])
    return "\n\n".join(lines), index


def attribute(st, b, paras: list[dict], names: list[str], by_alias: dict[str, str], stop=lambda: False) -> tuple[int, bool]:
    """给这些段落的对白做归属并存表（每段一行：[{a, b, who}]）；返回 (存了几段, 是否每一块都做成了)。已有行的段不重做。
    一块失败（模型超时、返回的不是 JSON、一条对白都没认出）就跳过这一块、记为没做完：这些段没有归属行，下次再补。"""
    db, cfg = st.db, st.cfg
    have = {r["para"] for r in db.q("SELECT para FROM speech WHERE book=?", (b["id"],))}
    todo = [p for p in paras if p["n"] not in have and quotes(p["text"])]
    if not todo:
        return 0, True
    ctx = {p["n"]: p for p in paras}
    done, ok = 0, True
    for part in chunks(paras):
        if stop():
            return done, False
        if not any(p["n"] not in have and quotes(p["text"]) for p in part):
            continue
        text, index = numbered_text(part)
        try:
            raw = st.llm.chat_json(cfg.model, PROMPT.format(title=b["title"], names="、".join(names[:40]) or "（还没抽出人物）"),
                                   text, num_ctx=16384, timeout=LLM_TIMEOUT)
        except Exception as ex:  # noqa: BLE001
            log.warning("对白归属：一块失败 %s：%s", b["id"], ex)
            ok = False
            continue
        got = clean(raw, index, by_alias)
        if index and not got:
            log.warning("对白归属：一块一条都没认出 %s（%d 条对白）", b["id"], len(index))
            ok = False
            continue
        per: dict[int, list] = {}
        for k, n, qi in index:
            per.setdefault(n, [])
            if k in got:
                a, z = quotes(ctx[n]["text"])[qi]
                per[n].append({"a": a, "b": z, "who": got[k][0], "g": got[k][1]})
        with db.lock:
            for n, spans in per.items():
                if n in have:
                    continue
                db.conn.execute("INSERT OR REPLACE INTO speech(book, para, json) VALUES(?,?,?)",
                                (b["id"], n, json.dumps([{"a": s["a"], "b": s["b"], "who": s["who"]} for s in spans], ensure_ascii=False)))
                for s in spans:
                    db.conn.execute("INSERT INTO speakers(book, name, gender, voice, created) VALUES(?,?,?,NULL,?) "
                                    "ON CONFLICT(book, name) DO UPDATE SET gender=CASE WHEN gender IN ('m','f') THEN gender ELSE excluded.gender END",
                                    (b["id"], s["who"], s["g"], time.time()))
                done += 1
            db.conn.commit()
    return done, ok


def ensure(st, b, start: int, end: int, stop=lambda: False, pool: list[dict] | None = None, exclude: set[str] = frozenset()) -> None:
    """预生成一段范围 [start, end) 时调用：没做过归属就做，记到 speech_done；然后把全书说话人按人物表归并、给还没音色的分音色
    （pool = 装好的音色，exclude = 旁白）。归属时的人物表取 cast 表里到这一章为止的累计。"""
    db = st.db
    from . import cast
    done = db.one("SELECT 1 FROM speech_done WHERE book=? AND start=? AND end>=?", (b["id"], start, end))
    if done and not db.one("SELECT 1 FROM speech WHERE book=? AND para>=? AND para<?", (b["id"], start, end)) \
            and any(quotes(p["text"]) for p in db.paras_range(b["id"], start, end)):
        db.run("DELETE FROM speech_done WHERE book=? AND start=?", (b["id"], start))   # 早先版本整章失败也记了做完：当没做
        done = None
    if not done:
        cur = cast.chapter_at(b, start)
        rows = db.q("SELECT json FROM cast WHERE book=? AND chapter<=? ORDER BY chapter", (b["id"], cur[0] if cur else 1 << 30))
        people = cast.merge([json.loads(r["json"]) for r in rows])["people"] if rows else []
        names = [p["name"] for p in people]
        by_alias = {a.lower(): p["name"] for p in people for a in p["aliases"]}
        for p in people:                                  # 人物表里已知性别的，先记下来
            if p.get("g") in ("m", "f"):
                db.run("INSERT INTO speakers(book, name, gender, voice, created) VALUES(?,?,?,NULL,?) "
                       "ON CONFLICT(book, name) DO UPDATE SET gender=CASE WHEN gender IN ('m','f') THEN gender ELSE excluded.gender END",
                       (b["id"], p["name"], p["g"], time.time()))
        _, ok = attribute(st, b, db.paras_range(b["id"], start, end), names, by_alias, stop)
        if stop():
            return
        if ok:                                        # 没做完的不记：下次预生成 / 全书准备只补缺的段
            db.run("INSERT OR REPLACE INTO speech_done(book, start, end, created) VALUES(?,?,?,?)", (b["id"], start, end, time.time()))
    if pool:
        if any(v.get("age") == "child" for v in pool):   # 装了童声才需要分孩子大人
            classify_ages(st, b)
        assign_all(db, b["id"], pool, set(exclude))


# ---------------------------------------------------------------- 认人 + 音色分配
def classify_ages(st, b) -> int:
    """还没定年龄、要配音的说话人（性别明确的）交给本机模型判断孩子 / 大人，写进 speakers.age；判断不了的按大人记（不反复问）。
    返回写了几个。模型调用失败只记日志，下次再试。"""
    from . import cast
    db, bid = st.db, b["id"]
    todo = [r["name"] for r in db.q("SELECT name FROM speakers WHERE book=? AND age IS NULL AND gender IN ('m','f')", (bid,))]
    if not todo:
        return 0
    people = cast.people_all(db, bid)["people"]
    first: dict[str, str] = {}
    for r in db.q("SELECT s.json j, p.text t FROM speech s JOIN paras p ON p.book=s.book AND p.n=s.para WHERE s.book=? ORDER BY s.para", (bid,)):
        for q in json.loads(r["j"]):
            first.setdefault(q["who"], r["t"][q["a"]:q["b"]])
    n = 0
    for k in range(0, len(todo), 60):
        part = todo[k:k + 60]
        lines = "\n".join(f"{w} · {(resolve(w, people) or {}).get('role') or '—'} · {_s(first.get(w, ''), 120) or '—'}" for w in part)
        try:
            raw = st.llm.chat_json(st.cfg.model, AGE_PROMPT.format(title=b["title"]), lines, num_ctx=8192, timeout=LLM_TIMEOUT)
        except Exception as ex:  # noqa: BLE001
            log.warning("人物年龄：模型调用失败 %s：%s", bid, ex)
            continue
        got = {_s(a.get("who"), 40).lower(): _s(a.get("a"), 4).lower()[:1] for a in (raw.get("ages") if isinstance(raw, dict) else None) or []
               if isinstance(a, dict)}
        for w in part:
            db.run("UPDATE speakers SET age=? WHERE book=? AND name=? AND age IS NULL",
                   ("child" if got.get(w.lower()) == "c" else "adult", bid, w))
            n += 1
    return n


def _age(v) -> str:
    return "child" if v == "child" else "adult"


def _want(pool: list[dict], lang: str, gender: str, age: str) -> str:
    """这个人该用哪个年龄段的音色：孩子用童声；这个语言、性别的童声一个都没装（或都不配角色）时退回成人音色。"""
    if age == "child" and any(v.get("lang") == lang and v.get("gender") == gender and v.get("age") == "child" and v.get("cast") is not False
                              for v in pool):
        return "child"
    return "adult"


def resolve(name: str, people: list[dict]) -> dict | None:
    """对白的说话人 → 全书人物表里的那个人（按称呼或别名，不分大小写，所有格去掉）；表里没有返回 None。"""
    key = re.sub(r"['’]s$", "", (name or "").strip()).lower()
    return next((p for p in people if any(a.lower() == key for a in [p["name"]] + p.get("aliases", []))), None)


def ranked(pool: list[dict], lang: str, gender: str, exclude: set[str], age: str = "adult") -> list[str]:
    """可选音色，按偏好从前往后：tts/voices.json 里 cast_order 排过的（pick 小的在前）先，没排的按目录顺序排在后面；
    标了 "cast": false 的（克隆出来不像这个性别等）不配给角色。年龄要对上：孩子用童声（age: child），大人不用童声；
    这个性别的童声一个都没装时孩子退回成人音色。"""
    want = _want(pool, lang, gender, age)
    cands = [v for v in pool if v.get("lang") == lang and v.get("gender") == gender and v["id"] not in exclude and v.get("cast") is not False
             and _age(v.get("age")) == want]
    cands.sort(key=lambda v: (v.get("pick") is None, v.get("pick") or 0))
    return [v["id"] for v in cands]


def pick(cands: list[str], used: dict[str, int]) -> str | None:
    """这本书里还没人用的第一个；都用过了就用得最少的（并列取靠前的）。"""
    return next((c for c in cands if not used.get(c)), None) or (min(cands, key=lambda c: (used.get(c, 0), cands.index(c))) if cands else None)


def _valid(v, lang: str, pool: list[dict], exclude: set[str], locked: bool = False, gender: str | None = None, age: str | None = None) -> bool:
    """已分到的音色还能用：装着、同语言、不是旁白、年龄对得上（孩子配了成人音色、而童声装着时要换；锁定的书也换，
    童声是后来才有的）；锁定的书不看 cast: false（锁的时候用的就一直用）。"""
    x = next((x for x in pool if x["id"] == v), None) if v and v not in exclude else None
    if x is None or x.get("lang") != lang or not (locked or x.get("cast") is not False):
        return False
    return age is None or gender not in ("m", "f") or _age(x.get("age")) == _want(pool, lang, gender, age)


def locked(db, bid: str) -> bool:
    return db.one("SELECT 1 FROM voice_lock WHERE book=?", (bid,)) is not None


def assign_all(db, bid: str, pool: list[dict], exclude: set[str]) -> dict[str, str | None]:
    """全书说话人的音色：按人物表归并（同一个人的几条 speakers 行一种声音；叙述者自己的对白不配，用旁白），
    戏份重的（人物表提及次数 + 3 × 对白条数）先挑，每人取 ranked() 里这本书还没人用的第一个。已分过、仍有效的不动。
    返回 {说话人: 音色 id 或 None}。"""
    from . import cast
    allc = cast.people_all(db, bid)
    people, narrator = allc["people"], allc["narrator"]
    rows = {r["name"]: dict(r) for r in db.q("SELECT name, gender, voice, created, age FROM speakers WHERE book=?", (bid,))}
    lines: dict[str, int] = {}
    lang_of: dict[str, str] = {}                     # 说话人 → 对白语言（按第一条）
    for r in db.q("SELECT s.json j, p.text t FROM speech s JOIN paras p ON p.book=s.book AND p.n=s.para WHERE s.book=? ORDER BY s.para", (bid,)):
        for q in json.loads(r["j"]):
            lines[q["who"]] = lines.get(q["who"], 0) + 1
            said = r["t"][q["a"]:q["b"]] if SPEAKABLE.search(r["t"][q["a"]:q["b"]]) else r["t"]   # 区间对不上正文（切出空串）时按整段判断
            lang_of.setdefault(q["who"], "zh" if is_zh(said) else "en")
    groups: list[tuple[int, dict | None, list[dict]]] = []   # (戏份, 人物, 这个人的 speakers 行)
    taken: set[str] = set()
    for p in people:
        mine = [r for n, r in rows.items() if n not in taken and resolve(n, [p]) is p]
        if not mine:
            continue
        taken |= {r["name"] for r in mine}
        groups.append((p["n"] + 3 * sum(lines.get(r["name"], 0) for r in mine), p, mine))
    groups += [(3 * lines.get(n, 0), None, [r]) for n, r in rows.items() if n not in taken]
    groups.sort(key=lambda g: -g[0])
    out: dict[str, str | None] = {}

    def put(names: list[str], gender: str, v: str | None) -> None:
        for n in names:
            if rows[n]["voice"] != v or rows[n]["gender"] != gender:
                db.run("UPDATE speakers SET voice=?, gender=? WHERE book=? AND name=?", (v, gender, bid, n))
            out[n] = v
    lock = locked(db, bid)
    plan: list[tuple[list[str], str, str, str, str | None]] = []   # (叫法, 性别, 年龄, 语言, 已有的音色)
    used: dict[str, int] = {}                     # 已分好的先都算上：新人物不论排在前后都避开它们
    for _, p, mine in groups:
        names = [r["name"] for r in mine]
        gender = next((r["gender"] for r in mine if r["gender"] in ("m", "f")), (p or {}).get("g") or "?")
        if (p is not None and p["name"] == narrator) or gender not in ("m", "f"):   # 叙述者的对白就是旁白的声音；性别不明也用旁白
            put(names, gender, None)
            continue
        lang = next((lang_of[n] for n in names if n in lang_of), "en")
        age = _age(next((r["age"] for r in mine if r["age"]), None))
        have = [r["voice"] for r in sorted(mine, key=lambda r: r["created"] or 0) if _valid(r["voice"], lang, pool, exclude, lock, gender, age)]
        if have:
            used[have[0]] = used.get(have[0], 0) + 1
        plan.append((names, gender, age, lang, have[0] if have else None))
    for names, gender, age, lang, v in plan:
        if v is None:
            v = pick(ranked(pool, lang, gender, exclude, age), used)
            if v:
                used[v] = used.get(v, 0) + 1
        put(names, gender, v)
    return out


def assign(db, bid: str, name: str, gender: str, lang: str, pool: list[dict], exclude: set[str], age: str = "adult") -> str | None:
    """给一个说话人（没在人物表里的）挑音色：ranked() 里这本书还没人用的第一个；结果写进 speakers.voice。
    性别不明、没装这种音色返回 None。"""
    cands = ranked(pool, lang, gender, exclude, age)
    if gender not in ("m", "f") or not cands:
        return None
    used: dict[str, int] = {}
    for r in db.q("SELECT voice FROM speakers WHERE book=? AND voice IS NOT NULL", (bid,)):
        used[r["voice"]] = used.get(r["voice"], 0) + 1
    v = pick(cands, used)
    db.run("INSERT INTO speakers(book, name, gender, voice, created) VALUES(?,?,?,?,?) "
           "ON CONFLICT(book, name) DO UPDATE SET voice=excluded.voice", (bid, name, gender, v, time.time()))
    return v


def voice_for(db, bid: str, name: str, lang: str, pool: list[dict], narrator: str) -> str | None:
    """人物的音色；None = 用旁白（叙述者自己的对白、性别不明、没装这种音色、没见过的人）。
    按全书人物表认人：同一个人的几种叫法一种声音（任一叫法下已分好的就用）；没分过、或撞上了旁白就（重新）分。"""
    from . import cast
    allc = cast.people_all(db, bid)
    p = resolve(name, allc["people"])
    if p is not None and p["name"] == allc["narrator"]:
        return None
    names = list(dict.fromkeys([name] + ([p["name"]] + p.get("aliases", []) if p else [])))
    rows = db.q(f"SELECT name, gender, voice, created, age FROM speakers WHERE book=? AND name IN ({','.join('?' * len(names))})", (bid, *names))
    gender = next((r["gender"] for r in rows if r["gender"] in ("m", "f")), (p or {}).get("g"))
    age = _age(next((r["age"] for r in rows if r["age"]), None))
    have = [r["voice"] for r in sorted(rows, key=lambda r: r["created"] or 0) if _valid(r["voice"], lang, pool, {narrator}, locked(db, bid), gender, age)]
    if have:
        return have[0]
    if not rows:
        return None
    if gender not in ("m", "f"):
        return None
    if p is None:
        return assign(db, bid, name, gender, lang, pool, {narrator}, age)
    out = assign_all(db, bid, pool, {narrator})
    return next((out[n] for n in names if n in out), None)


# ---------------------------------------------------------------- 切段
def segments(text: str, a: int, b: int, spans: list[dict], voice_of, narrator) -> list[tuple[str, str]]:
    """段落 text 里第 [a, b) 句按对白区间切段：[(文字, 音色)]。voice_of(who, 文字) → 音色或 None；narrator(文字) → 旁白音色。
    相邻同音色合并；没有可读字符的碎片并入前一段（没有前一段就并入后一段）。"""
    cuts = sorted({a, b} | {min(max(s["a"], a), b) for s in spans} | {min(max(s["b"], a), b) for s in spans})
    raw: list[tuple[str, str]] = []
    for x, y in zip(cuts, cuts[1:]):
        piece = text[x:y]
        if not piece:
            continue
        who = next((s["who"] for s in spans if s["a"] <= x and y <= s["b"]), None)
        v = (voice_of(who, piece) if who else None) or narrator(piece)
        raw.append((piece, v))
    out: list[list] = []
    for piece, v in raw:
        if out and (out[-1][1] == v or not SPEAKABLE.search(piece)):
            out[-1][0] += piece
        elif not out and not SPEAKABLE.search(piece) and len(raw) > 1:
            out.append([piece, None])                 # 开头的碎片：等下一段来认领
        elif out and out[-1][1] is None:
            out[-1] = [out[-1][0] + piece, v]
        else:
            out.append([piece, v])
    res = [(re.sub(r"\s+", " ", p).strip(), v if v is not None else narrator(p)) for p, v in out]
    return [(p, v) for p, v in res if p] or [(re.sub(r"\s+", " ", text[a:b]).strip(), narrator(text[a:b]))]


def for_text(db, bid: str, paras: list[dict], voices: list[dict]) -> str | None:
    """给 /text 的每段加 speech（[[a, b, 人物, 性别, 音色名, 是否叙述者]]）和 speech_ok（这段所在范围做过归属）。
    人物用全书人物表里的称呼（Nick → Nick Carraway）；叙述者自己的对白音色名留空（前端显示旁白音色）。返回叙述者的称呼。"""
    if not paras:
        return None
    from . import cast
    allc = cast.people_all(db, bid)
    lo, hi = paras[0]["n"], paras[-1]["n"]
    rows = {r["para"]: json.loads(r["json"]) for r in db.q("SELECT para, json FROM speech WHERE book=? AND para BETWEEN ? AND ?", (bid, lo, hi))}
    done = db.q("SELECT start, end FROM speech_done WHERE book=? AND start<=? AND end>?", (bid, hi, lo))
    sp = {r["name"]: r for r in db.q("SELECT name, gender, voice FROM speakers WHERE book=?", (bid,))}
    vname = {v["id"]: v["name"] for v in voices}
    for p in paras:
        p["speech_ok"] = p["n"] in rows or any(d["start"] <= p["n"] < d["end"] for d in done)
        out = []
        for s in rows.get(p["n"], []):
            who = resolve(s["who"], allc["people"])
            r = sp.get(s["who"])
            narr = who is not None and who["name"] == allc["narrator"]
            g = r["gender"] if r and r["gender"] in ("m", "f") else (who or {}).get("g") or ""
            out.append([s["a"], s["b"], who["name"] if who else s["who"], g, vname.get(r["voice"], "") if r and r["voice"] and not narr else "", int(narr)])
        p["speech"] = out
    return allc["narrator"]
