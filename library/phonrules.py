"""英语连读与句重音的文本规则（不听音频，只看句子和词典），给语调分析（library/prosody.py）当「教科书说法」，
和实测比对，并生成讲解栏的中文说明。依据：Ladefoged & Johnson《A Course in Phonetics》(ACiP) 第 5 章「英语的词与句」
（弱读式、重音等级、节奏、语调、ToBI 目标调）和台大史嘉琳老师语音学课讲义（调核重音、延续升调、复合名词重音、连读、闪音、
喉塞、Stop at stops），知识库在 NTU Caption 项目里。

  weak_form(词)                 常见虚词的弱读式 IPA（ACiP 表 5.1 节选）；不是虚词或没有弱读式时 None
  analyze(text, toks, lookup, accent, prev)  每个词的规则标注 + 词与词之间的连读点 + 规则调核 / 句调；见 Rules
  notes(rules, pr)              中文讲解行 [{k: 知识点 id, zh: 说明}]，pr 是实测结果（可为 None）

词的标注（都是「教科书上应该这样」，实测在 prosody 里）：
  fn 虚词 · weak 弱读式 · comp 复合名词的后一个词（不重读）· given 前几句出现过的实词（旧信息，不当调核）·
  contr 对比重音（not X but Y、X or Y）· flap 词内闪音（water）· glot 喉塞（button）· pos 词性（n/v/a/…）·
  readings 词典里按词性不同的读音（record n./v.）
连读点 links [k, kind]：第 k 词和第 k+1 词之间，kind = cv 辅音接元音 · vv 元音接元音（滑音 j/w 过渡）· r 连读 r（英音）·
  flap 词尾 t/d 接元音读闪音（美音）· cc 同一辅音相连（只发一次）
知识点 id 对应 NTU Caption 的 phonetics_topics.tsv：sentence-stress · continuation-rise · falling-tone · intonation ·
  reduced-vowel · elision · stress · rhythm · tone（ToBI 目标调归在 intonation）
"""
from __future__ import annotations

import re

TOK = re.compile(r"[A-Za-z0-9]+(?:['’][A-Za-z]+)*")
VOWEL_IPA = set("ɑæʌɔɛɝɚɪiʊuəaeoɒɜɐɨʉ")
CONS_LETTERS = set("bcdfghjklmnpqrstvwxz")

# ACiP 表 5.1「常见英语词的强式与弱式」节选 + 课堂常提的几个；只列弱读式（强式就是词典读音）
WEAK = {
    "a": "ə", "an": "ən", "the": "ðə", "and": "ən", "as": "əz", "at": "ət", "but": "bət", "can": "kən", "could": "kəd",
    "do": "də", "does": "dəz", "for": "fɚ", "from": "frəm", "had": "həd", "has": "həz", "have": "həv", "he": "i",
    "her": "ɚ", "him": "ɪm", "his": "ɪz", "is": "z", "must": "məs", "not": "nt", "of": "əv", "or": "ɚ", "shall": "ʃəl",
    "she": "ʃi", "should": "ʃəd", "some": "səm", "than": "ðən", "that": "ðət", "them": "ðəm", "there": "ðɚ", "to": "tə",
    "us": "əs", "was": "wəz", "we": "wi", "were": "wɚ", "will": "əl", "would": "wəd", "you": "jə", "your": "jɚ",
    "are": "ɚ", "am": "əm", "been": "bɪn", "me": "mi", "my": "mə", "our": "ɑr", "them": "ðəm", "who": "hu", "just": "dʒəs",
}
FUNC = set("""a an the this that these those some any each every no my your his her its our their whose
of to in on at by for from with about into onto over under up down out off through after before between
among against during without within upon across along around behind below beneath beside near toward towards
i me mine you yours he him she hers it we us ours they them theirs myself yourself himself herself itself
ourselves themselves one who whom which what there here
is am are was were be been being do does did have has had will would shall should can could may might must
and or but nor so yet if as than though although because while when where whether how why then
i'm i've i'd i'll you're you've you'd you'll he's he'd he'll she's she'd she'll it's it'd it'll we're we've we'd
we'll they're they've they'd they'll that's there's here's what's who's let's 's 'd 'll 're 've""".split())
WH = {"what", "when", "where", "why", "how", "who", "whom", "whose", "which"}
STOP = {"not", "no", "n't", "very", "so", "too", "just", "only", "even", "also", "still", "already", "ever", "never"}
ABBR = {"mr", "mrs", "ms", "dr", "st", "jr", "sr", "vs", "etc", "prof", "mt", "no"}


def _norm(w: str) -> str:
    return w.lower().replace("’", "'")


def weak_form(w: str) -> str | None:
    return WEAK.get(_norm(w))


def _readings(ipa: str | None) -> list[str]:
    """词典 us 字段里的几个读音（"/rəˈkɔrd/ / /ˈrɛkɚd/" → 两个），去掉斜线。"""
    if not ipa:
        return []
    return [x for x in (p.strip().strip("/").strip() for p in ipa.split(" / ")) if x]


def _pos(zh: str | None) -> str:
    """词典释义开头的词性：n. / vt. / vi. / a. / ad. / prep. …；只取第一个。"""
    m = re.match(r"\s*\[?([a-z]{1,4})\.", zh or "")
    if not m:
        return ""
    p = m.group(1)
    return "v" if p in ("vt", "vi", "v", "vbl") else "a" if p in ("a", "adj") else "ad" if p in ("ad", "adv") else p


def _first_last(ipa: str | None, w: str) -> tuple[str, str]:
    """词首、词尾是元音（v）还是辅音（c）；词尾的 r（美音 ɚ/ɹ、英音拼写 r/re）记 r。没词典按拼写猜。"""
    s = re.sub(r"[ˈˌ.ːˑ ()]", "", (_readings(ipa) or [""])[0])
    if s:
        f = "v" if s[0] in VOWEL_IPA else "c"
        last = s[-1]
        l = "r" if last in "ɚɝɹr" or (last in "ɑɔə" and _norm(w).rstrip("e").endswith("r")) else "v" if last in VOWEL_IPA else "c"
        return f, l
    lw = _norm(w)
    f = "v" if lw[:1] in "aeiou" else "c"
    if lw.rstrip("e").endswith("r"):
        l = "r"
    elif lw[-1:] in "aeiouy" or lw.endswith("ew") or lw.endswith("ow") or lw.endswith("ay"):
        l = "v"
    else:
        l = "c"
    return f, l


def _flap_in(ipa: str | None) -> bool:
    """词内闪音：t/d 在元音之后、非重读元音之前（water、better、city、ladder）；按第一个读音。"""
    r = _readings(ipa)
    if not r:
        return False
    s = r[0]
    return bool(re.search(r"[ɑæʌɔɛɝɚɪiʊuəaeoɒɜ][ːˑ]?[td](?![ˈˌ])[ɚəɪi]", s))


def _glottal(ipa: str | None, w: str) -> bool:
    """喉塞：t 后面接成音节 n（button、cotton、important、mountain、written）。"""
    r = _readings(ipa)
    s = r[0] if r else ""
    return bool(re.search(r"t[ə]?n(?![ɑæʌɔɛɪiʊuə])", s)) and bool(re.search(r"t[aeio]?n$|t[aeio]?nt$|t[aeio]?ns$", _norm(w)))


class Rules(dict):
    """analyze 的结果：{"words": [...], "links": [[k, kind]], "nuc": 调核下标, "tune": 规则句调, "accent": 口音, "lemmas": 本句实词}"""


def analyze(text: str, lookup=None, accent: str = "us", prev: list[str] | None = None) -> Rules:
    """text 里的词（和 prosody / 前端同一个正则）逐个标注。lookup(词) → 词典行（us、zh 字段）或 None；prev = 前面几句的原文（判断旧信息）。"""
    toks = [(m.group(), m.start(), m.end()) for m in TOK.finditer(text)]
    rows = []
    for w, a, b in toks:
        try:
            r = lookup(w) if lookup else None
        except Exception:  # noqa: BLE001
            r = None
        ipa = (r.get("us") if isinstance(r, dict) else None) or None
        zh = (r.get("zh") if isinstance(r, dict) else None) or ""
        zh = zh[0] if isinstance(zh, list) and zh else zh if isinstance(zh, str) else ""
        lw = _norm(w)
        d = {"w": w, "a": a, "b": b, "fn": lw in FUNC, "pos": _pos(zh), "ipa": ipa, "readings": _readings(ipa),
             "weak": WEAK.get(lw) if lw in FUNC else None, "first": _first_last(ipa, w)[0], "last": _first_last(ipa, w)[1]}
        if accent == "us" and _glottal(ipa, w):
            d["glot"] = True
        elif accent == "us" and _flap_in(ipa):
            d["flap"] = True
        rows.append(d)
    n = len(rows)
    # 标点：词后到下一词之间的符号
    for k, d in enumerate(rows):
        nxt = rows[k + 1]["a"] if k + 1 < n else len(text)
        d["p"] = re.sub(r"[\s\"“”‘’'()\[\]]", "", text[d["b"]:nxt])
    # 旧信息：前几句出现过的实词（按去掉常见词尾的词干比）
    stem = lambda s: re.sub(r"(ies|ied|ing|ed|es|s|ly)$", "", _norm(s)) if len(s) > 4 else _norm(s)   # noqa: E731
    old = {stem(w) for s in (prev or []) for w in TOK.findall(s) if _norm(w) not in FUNC and len(w) > 2}
    for d in rows:
        if not d["fn"] and stem(d["w"]) in old:
            d["given"] = True
    # 复合名词：名词 + 名词，重音在前一个（*vocal folds、*air pressure）
    for k in range(n - 1):
        a, b = rows[k], rows[k + 1]
        if a["pos"] == "n" and b["pos"] == "n" and not a["fn"] and not b["fn"] and not a["p"] and a["w"][0].islower() and b["w"][0].islower():
            b["comp"] = True
    # 对比：not X but Y · X, not Y · either X or Y · X rather than Y · X or Y
    lws = [_norm(d["w"]) for d in rows]
    for k, lw in enumerate(lws):
        if lw in ("not", "but", "either", "or", "rather", "instead") or lw.endswith("n't"):
            j = k + 1
            if lw == "rather" and j < n and lws[j] == "than":
                j += 1
            while j < n and rows[j]["fn"]:
                j += 1
            if j < n and j - k <= 3:
                rows[j]["contr"] = True
        if lw in ("but", "or") or lw == "not" and k and rows[k - 1]["p"]:   # 前一个对比项：but / or / 「, not」前面最近的实词
            j = k - 1
            while j >= 0 and rows[j]["fn"]:
                j -= 1
            if j >= 0 and k - j <= 4 and not rows[j].get("given"):
                rows[j]["contr"] = True
    # 连读点
    links = []
    for k in range(n - 1):
        a, b = rows[k], rows[k + 1]
        if a["p"]:                                     # 标点隔开的不连
            continue
        if a["last"] == "c" and b["first"] == "v":
            la = _norm(a["w"])
            if accent == "us" and len(la) > 1 and la[-1] in "td" and la[-2] in "aeiouynrl" and not b["w"][0].isupper():   # best of：st 后不闪
                links.append([k, "flap"])
            else:
                links.append([k, "cv"])
        elif a["last"] == "r" and b["first"] == "v":
            links.append([k, "r" if accent == "uk" else "cv"])
        elif a["last"] == "v" and b["first"] == "v":
            links.append([k, "vv"])
        elif a["last"] == "c" and b["first"] == "c" and _norm(a["w"])[-1] == _norm(b["w"])[0] and _norm(a["w"])[-1] not in "aeiou":
            links.append([k, "cc"])
    # 规则调核：短语（按标点分）里最后一个「新」实词，不是复合名词后半；全是旧信息时取最后实词；对比项也要重读
    phrases, cur = [], []
    for k, d in enumerate(rows):
        cur.append(k)
        brk = bool(re.search(r"[,.;:?!…—–]|--", d["p"])) and not (d["p"] == "." and _norm(d["w"]).rstrip(".") in ABBR)
        if brk and k + 1 < n:
            phrases.append(cur); cur = []
    if cur:
        phrases.append(cur)
    for ph in phrases:
        content = [k for k in ph if not rows[k]["fn"] and not rows[k].get("comp") and (_norm(rows[k]["w"]) not in STOP or rows[k].get("contr"))]
        new = [k for k in content if not rows[k].get("given")] or content or ph
        rows[new[-1]]["rule_nuc"] = True
        for k in ph:
            if rows[k].get("contr"):
                rows[k]["rule_acc"] = True
    end = rows[-1]["p"] if rows else ""
    if "?" in end:
        first = lws[0] if lws else ""
        tag = n >= 3 and rows[-3]["p"].endswith(",") and lws[-1] in ("it", "you", "he", "she", "they", "we", "i", "there")
        tune = "fall" if first in WH else "rise"
        if tag:
            tune = "rise"
    elif "!" in end:
        tune = "fall"
    else:
        tune = "fall"
    nuc = next((k for k in reversed(range(n)) if rows[k].get("rule_nuc")), n - 1)
    out = Rules(words=rows, links=links, nuc=nuc, tune=tune, accent=accent,
                phrases=[{"a": ph[0], "b": ph[-1], "rule_tune": tune if i == len(phrases) - 1 else "cont"} for i, ph in enumerate(phrases)])
    return out


# ---------------------------------------------------------------- 讲解
TUNE_ZH = {"fall": ("降调 ↘", "陈述、特殊疑问、命令：说完了"), "rise": ("高升调 ↗", "一般疑问句，幅度大"),
           "lowrise": ("低升调 ↗", "延续升调：后面还有话，或列举中间"), "fallrise": ("降升调 ↘↗", "有保留、对比、或礼貌地不同意"),
           "risefall": ("升降调 ↗↘", "强调、惊讶"), "level": ("平调 →", "念读、列举、未完")}
ACC_ZH = {"H*": "高目标调", "!H*": "降阶高调（比前一个重音低一级）", "L+H*": "低起高落的尖升", "L*": "低目标调", "L*+H": "低后上翘"}
LINK_ZH = {"cv": "辅音接元音连读", "vv": "元音接元音（中间带 j / w 过渡）", "r": "连读 r（英音）", "flap": "词尾 t/d 接元音读闪音 [ɾ]", "cc": "相同辅音只发一次"}


def notes(rules: Rules, pr: dict | None) -> list[dict]:
    """讲解栏的「语调」卡：调核、句调、连读、弱读、闪音、喉塞、复合名词、对比、节奏、词重音。每行带知识点 id 和出处。"""
    ws = rules["words"]
    out: list[dict] = []
    pw = (pr or {}).get("words") or []
    phs = (pr or {}).get("phrases") or []
    same = len(pw) == len(ws)
    if not ws:
        return out
    # 调核
    rk = rules["nuc"]
    why = "短语最后一个新信息实词" if not ws[rk].get("given") else "最后一个实词（后面的都是旧信息）"
    if same and phs:
        mk = phs[-1]["nuc"]
        if mk != rk:
            out.append({"k": "sentence-stress", "zh": f"调核：朗读者落在 *{pw[mk]['w']}（规则是 *{ws[rk]['w']}：{why}）", "ref": "ACiP §5 句重音"})
        else:
            out.append({"k": "sentence-stress", "zh": f"调核：*{ws[rk]['w']} — {why}", "ref": "ACiP §5 句重音"})
    else:
        out.append({"k": "sentence-stress", "zh": f"调核：*{ws[rk]['w']} — {why}", "ref": "ACiP §5 句重音"})
    contr = [w["w"] for w in ws if w.get("contr")]
    if contr:
        out.append({"k": "sentence-stress", "zh": "对比重音：" + "、".join(contr) + "（对比的两项都要重读，ACiP §5 / 课堂「重读对比项」）", "ref": "ACiP §5"})
    given = [w["w"] for w in ws if w.get("given") and not w.get("contr")]
    if given:
        out.append({"k": "sentence-stress", "zh": "旧信息不重读：" + "、".join(given[:6]) + "（前文刚出现过）", "ref": "ACiP §5"})
    comps = [(ws[k - 1]["w"], ws[k]["w"]) for k, w in enumerate(ws) if w.get("comp")]
    if comps:
        out.append({"k": "stress", "zh": "复合名词重音在前：" + "、".join(f"*{a} {b}" for a, b in comps), "ref": "课堂：複合名詞重音"})
    # 句调
    if same and phs:
        last = phs[-1]
        t = last.get("tune")
        if t in TUNE_ZH:
            name, mean = TUNE_ZH[t]
            rt = rules["tune"]
            exp = {"fall": "降调", "rise": "高升调"}.get(rt, rt)
            tail = "" if (t == rt or (t in ("fallrise", "risefall", "level") and rt == "fall")) else f"（教科书预期{exp}）"
            bt = last.get("bt")
            out.append({"k": "falling-tone" if t in ("fall", "rise") else "intonation", "zh": f"句调：{name} — {mean}{tail}" + (f"，边界调 {bt}" if bt else ""), "ref": "ACiP §5 语调"})
        for ph in phs[:-1]:
            if ph.get("tune") in ("lowrise", "rise", "fallrise", "level"):
                out.append({"k": "continuation-rise", "zh": f"延续升调：{pw[ph['b']]['w']} 之后不降到底，表示话还没说完", "ref": "ACiP §5 / 课堂：continuation rise"})
                break
        accs = [(pw[k]["w"], pw[k]["tone"]) for k in range(len(pw)) if pw[k].get("tone") and not pw[k].get("nuc")]
        if any(t == "!H*" for _, t in accs):
            out.append({"k": "intonation", "zh": "逐级下降（downdrift）：" + " ".join(f"{w} {t}" for w, t in accs) + " — 每个重音比前一个低一点", "ref": "ACiP §5 目标调"})
    # 弱读
    weak = list(dict.fromkeys((w["w"], w["weak"]) for k, w in enumerate(ws) if w.get("weak") and k != rk and not w.get("contr")))
    strong = [pw[k]["w"] for k, w in enumerate(ws) if same and w.get("weak") and pw[k].get("strong")]
    if weak:
        out.append({"k": "reduced-vowel", "zh": "弱读：" + "、".join(f"{w} [{ipa}]" for w, ipa in weak[:6]) + "（虚词平时用弱式，ACiP 表 5.1）"
                    + (f"；这里 {'、'.join(strong)} 读的是强式" if strong else ""), "ref": "ACiP §5 表 5.1"})
    # 连读 / 闪音 / 喉塞
    ls = rules["links"]
    cv = [f"{ws[k]['w']}‿{ws[k + 1]['w']}" for k, kind in ls if kind in ("cv", "r", "cc")]
    vv = [f"{ws[k]['w']}‿{ws[k + 1]['w']}" for k, kind in ls if kind == "vv"]
    fl = [f"{ws[k]['w']}‿{ws[k + 1]['w']}" for k, kind in ls if kind == "flap"] + [w["w"] for w in ws if w.get("flap")]
    if cv:
        out.append({"k": "elision", "zh": "连读：" + "、".join(cv[:6]) + "（词尾辅音接下一个词的元音，一口气读过去）", "ref": "课堂：linking"})
    if vv:
        out.append({"k": "elision", "zh": "元音相连：" + "、".join(vv[:4]) + "（中间带一个轻轻的 j 或 w 过渡，不要停顿）", "ref": "课堂：linking"})
    if fl:
        out.append({"k": "elision", "zh": "闪音 [ɾ]：" + "、".join(fl[:6]) + "（美音里元音之间的 t/d 轻轻一弹）", "ref": "课堂：flap"})
    gl = [w["w"] for w in ws if w.get("glot")]
    if gl:
        out.append({"k": "elision", "zh": "喉塞 [ʔ]：" + "、".join(gl) + "（t 不除阻，直接接成音节 n）", "ref": "课堂：Stop at stops"})
    # 词重音
    if same:
        odd = [f"{pw[k]['w']}（朗读者在第 {pw[k]['stress_at'] + 1} 音节，词典第 {next((i for i, s in enumerate(pw[k].get('sy') or []) if s[2] == 1), 0) + 1} 音节）"
               for k in range(len(pw)) if pw[k].get("stress_at") is not None]
        if odd:
            out.append({"k": "stress", "zh": "词重音和词典不同：" + "、".join(odd[:4]), "ref": "ACiP §5 词重音"})
        rd = [f"{pw[k]['w']} 读作 {pw[k]['ipa']}" for k in range(len(pw)) if pw[k].get("ipa")]
        if rd:
            out.append({"k": "stress", "zh": "名动转换的重音：" + "、".join(rd[:3]), "ref": "ACiP §5 表 5.2"})
    # 节奏
    beats = (pr or {}).get("beats") or []
    if len(beats) >= 3:
        gaps = [round(b - a, 2) for a, b in zip(beats, beats[1:])]
        out.append({"k": "rhythm", "zh": f"节奏：{len(beats)} 个重音，间隔 {min(gaps):.2f}–{max(gaps):.2f} s" + ("，大致等时（重音计时）" if max(gaps) <= 2 * min(gaps) else ""), "ref": "ACiP §5 节奏"})
    return out
