"""书的读法：阅读页按它决定翻不翻、注不注、默认多快、右栏画什么图。

  en  外文书（英文、日文……非中文）：整句译文、生词标注，语速偏慢、句间停顿长
  wy  文言书（《史记》这类古籍，含附译文、注释的版本）：文言句译成白话、注文言词；书里的白话句（译文、注释）不译
  zh  白话书（母语）：不译、不注，默认语速高一档、停顿短

中文书文言还是白话，先按逐句的文言判定（is_wenyan）抽样算比例：明显的直接定，夹在中间的（讲史读物大段引原文、
古籍全译本原文和译文交错，两者比例都在 4%–25%）交给本机模型看书名、目录和样句定。
导图：人物关系（小说、史书、传记，library/cast.py）或概念结构（论述、科普、修行、哲学，library/concepts.py），
中文书由模型按书的类型选；外文书照旧画人物关系。结果存 book_kind 表，每本书只问一次模型。
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time

from .text import is_zh

log = logging.getLogger(__name__)

HAN = re.compile(r"[㐀-鿿豈-﫿]")
# 白话的痕迹：现代汉语的虚词、助词、常用双音词。文言里几乎不出现（的、了、们、这、吗、呢……）
MODERN = re.compile(
    r"没有|什么|怎么|因为|所以|但是|已经|可以|一个|一些|自己|就是|他们|我们|你们|她们|这样|那样|还是|非常|如果|虽然|"
    r"时候|知道|认为|觉得|进行|开始|起来|出来|下来|回来|东西|事情|地方|现在|当时|以后|以前|多次|打败|攻打|率领|名叫|"
    r"派人|派了|于是|只是|还有|就|都|会|在|这|那|的|了|们|吗|呢|吧|么|着|很|把|被|啊|呀|嘛|给|让|跟")
# 文言的痕迹：强的（白话里基本不用：曰、矣、焉、哉、句末的也 / 乎）记 2 分，常见文言虚词记 1 分
STRONG = re.compile(r"曰|矣|焉|哉|兮|欤|弗|毋|吾|汝|盍|曷|寡人|也(?=[，。！？；：”」、]|$)|乎(?=[，。！？；：”」、]|$)|耳(?=[。！？]|$)")
MID = re.compile(r"之|其|而|者|则|乃|遂|亦|皆|於|莫|故|何|岂|诸|尝|卒|夫|盖|若|焉|乎")
WY_SURE, ZH_SURE = 0.25, 0.04      # 抽样里文言句占比 ≥ 25% 直接定文言，< 4% 直接定白话，中间问模型


# 确凿的白话痕迹：上面那些去掉文言里也常用的字（都南郑、在此一举、于是、被坚执锐、会于……），文言书里放宽判定时用
MODERN_SURE = re.compile(
    r"没有|什么|怎么|因为|所以|但是|已经|可以|一个|一些|一种|自己|就是|他们|我们|你们|她们|这样|那样|还是|非常|如果|虽然|"
    r"时候|知道|认为|觉得|进行|开始|起来|出来|下来|回来|东西|事情|地方|现在|当时|以后|以前|其实|实际|意思|是说|"
    r"这|那|的|了|们|吗|呢|吧|么|着|很|啊|呀|嘛|把|给|边")
# 注释条目：「④穷：困窘」「(17)伏：同服」「255.猾──侵扰」，以及「大名：指称王之名」这种「词：解释」（冒号前不是「曰、云、言」之类）
NOTE = re.compile(r"^\s*(?:[①-⑳㉑-㉟]|[\[(（]\d+[\])）]|\d+[.．、])|^[^：:，。“”‘’「」]{1,10}(?<![曰云言问谓对语告书诺呼])[：:]")


def is_wenyan(text: str, loose: bool = False) -> bool:
    """这一句是不是文言：至少 4 个汉字；没有白话痕迹时有一个强标记（或 4 分）就算；
    长句（≥ 20 字）偶有一两个白话字（「在」「会」）时，文言分要远多于白话痕迹。
    loose（文言书里用）：史书的叙事句常常一个虚词都没有（「项梁起东阿，西，再破秦军，益轻秦」），
    5 字以上、没有确凿的白话痕迹（MODERN_SURE）、又不是注释条目的也算——这种书里的白话（译文、注释）几乎句句有「的、了、这」。"""
    h = len(HAN.findall(text))
    if h < 4 or not is_zh(text):
        return False
    m, s, c = len(MODERN.findall(text)), len(STRONG.findall(text)), len(MID.findall(text))
    score = 2 * s + c
    if (m == 0 and (s >= 1 and score >= 2 or score >= 4)) or (h >= 20 and score >= 5 * m + 5):
        return True
    return loose and h >= 5 and not MODERN_SURE.search(text) and not NOTE.match(text)


def sample(db, bid: str, n_paras: int, windows: int = 30, width: int = 20) -> list[str]:
    """全书均匀取 windows 处、每处 width 段，拆成中文句子（跳过开头 3% 的扉页、版权页、序）。"""
    out = []
    lo = n_paras * 3 // 100
    span = max(1, n_paras - lo)
    for k in range(windows):
        s = lo + span * k // windows
        for p in db.paras_range(bid, s, min(n_paras, s + width)):
            for a, b in p["sents"]:
                t = p["text"][a:b]
                if len(HAN.findall(t)) >= 4 and is_zh(t):
                    out.append(t)
    return out


def share(sents: list[str]) -> float:
    return sum(map(is_wenyan, sents)) / len(sents) if sents else 0.0


def lang_of(db, bid: str) -> str:
    """书的主语言：开头 120 段里以中文为主的段落占多数就是 zh（只决定默认音色、排版；朗读时每句各按各的语言）。"""
    ps = [p["text"] for p in db.paras_range(bid, 0, 120) if len(p["text"]) > 8]
    return "zh" if ps and sum(is_zh(t) for t in ps) * 2 > len(ps) else "en"


PROMPT = """你在为一本中文书定「读法」。读者是中文母语者。只输出 JSON：
{"style":"wy 或 zh","map":"people 或 concepts","why":"一句话理由"}
style：正文主体是文言（古籍原文，包括附有白话译文、注释的古籍）写 wy；正文主体是现代白话（包括大段引用古文的现代著作、讲史读物、白话译本）写 zh。
map：右栏配哪种导图最能帮读者理清这本书。故事、历史、传记、回忆录，人物和人物关系是主线的写 people；
论述、科普、哲学、宗教修行、方法、教材，概念和观点是主线的写 concepts。"""


def _ask(st, b, sents: list[str], wy: list[str]) -> dict:
    toc = json.loads(b["toc"] or "[]") if isinstance(b["toc"], str) else (b["toc"] or [])
    heads = "、".join(t[1] for t in toc[:30] if len(t) > 1)
    step = max(1, len(sents) // 16)
    lines = sents[::step][:16] + [s for s in wy[:8] if s not in sents[::step]]
    user = (f"书名：{b['title']}\n作者：{b['author'] or '不详'}\n目录：{heads or '（没有）'}\n"
            f"抽样句子（其中文言句约占 {share(sents):.0%}）：\n" + "\n".join(f"- {s[:80]}" for s in lines))
    return st.llm.chat_json(st.cfg.model, PROMPT, user, num_ctx=4096, timeout=120)


_cache: dict = {}
_locks: dict = {}
_guard = threading.Lock()


def stored(db, bid: str) -> dict | None:
    if bid in _cache:
        return _cache[bid]
    r = db.one("SELECT json FROM book_kind WHERE book=?", (bid,))
    v = json.loads(r["json"]) if r else None
    if v:
        _cache[bid] = v
    return v


_guessed: dict = {}


def guess(db, b) -> dict:
    """不问模型能定的部分：语言、文言句比例和据此的猜测（中间地带先当白话）。导图先按人物关系。
    按 (书, 段数) 记在内存里：译文接口每句都要问读法，抽样只做一次。"""
    key = (b["id"], b["n_paras"] or 0)
    if key not in _guessed:
        if lang_of(db, b["id"]) != "zh":
            v = {"mode": "en", "map": "people", "share": 0.0, "by": "rule"}
        else:
            f = share(sample(db, b["id"], b["n_paras"] or 0))
            v = {"mode": "wy" if f >= WY_SURE else "zh", "map": "people", "share": round(f, 3), "by": "guess"}
        if len(_guessed) > 50:
            _guessed.clear()
        _guessed[key] = v
    return _guessed[key]


def ensure(st, b) -> dict:
    """这本书的读法，没有就定下来存表（中文书问一次模型）；模型不可用时返回猜测、不存，下次再问。"""
    db, bid = st.db, b["id"]
    v = stored(db, bid)
    if v:
        return v
    with _guard:
        lk = _locks.setdefault(bid, threading.Lock())
    with lk:                                  # 阅读页打开和预生成可能同时要：只问一次
        v = stored(db, bid)
        if v:
            return v
        g = guess(db, b)
        if g["mode"] == "en":
            v = g
        else:
            sents = sample(db, bid, b["n_paras"] or 0)
            try:
                r = _ask(st, b, sents, [s for s in sents if is_wenyan(s)])
            except Exception as ex:  # noqa: BLE001
                log.info("读法：模型没答上 %s：%s", bid, ex)
                return g
            style = str(r.get("style") or "").strip().lower()
            m = str(r.get("map") or "").strip().lower()
            if g["share"] >= WY_SURE or g["share"] < ZH_SURE or style not in ("wy", "zh"):
                style = g["mode"]             # 比例够明显就按比例，模型只管中间地带
            v = {"mode": style, "map": m if m in ("people", "concepts") else "people", "share": g["share"], "by": "model",
                 "why": str(r.get("why") or "")[:80]}
        db.run("INSERT OR REPLACE INTO book_kind VALUES(?,?,?)", (bid, json.dumps(v, ensure_ascii=False), time.time()))
        _cache[bid] = v
        log.info("读法 %s《%s》：%s", bid, b["title"], v)
        return v


def mode(db, b) -> str:
    """阅读页、译文接口要的读法：定过的用定好的，没定的现猜（不问模型、不存）。"""
    v = stored(db, b["id"])
    return (v or guess(db, b))["mode"]


def forget(bid: str) -> None:
    _cache.pop(bid, None)
    for k in [k for k in _guessed if k[0] == bid]:
        _guessed.pop(k, None)
