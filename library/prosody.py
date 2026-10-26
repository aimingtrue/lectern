"""句子的语调与重音：从朗读缓存的音频和逐词对齐算出整句的音高、重读和句调，给朗读页画「重音点 + 语调符号」
（● 调核重音、• 重读词的重读音节、∙ 弱音节；ˈ 重读、↘ ↗ → 句调、| 短停顿、‖ 句末）或「平滑音高曲线」。只用 numpy。

  输入    data/tts/<k[:2]>/<k>.wav（24 kHz 单声道 16 位）+ 同名 .json 的逐词对齐 [[词, 起, 止], ...]（80 ms 网格，
          约 3% 的词长度为 0）；lookup_ipa(词) → 美式 IPA（带 ˈ ˌ，词典 us 字段）或 None，由调用方传入，本模块不碰词典
  音高    先降到 12 kHz（频域截断），YIN：30 ms 窗、10 ms 步长、60–400 Hz，阈值 0.15；非周期度 < 0.25 且响度够才算浊音。倍频 / 半频错误按
          「局部」中位数（前后各 15 个浊音帧）纠正两遍——不用全句中位数：句末低沉的嘎裂音会被错翻倍；再 5 点中值平滑
  音节    IPA 里数元音（双元音算一个），音节边界：重音符号的位置；没有符号的按 phon.ONSETS 取最长合法词首。
          词的时长按音素权重（元音 1.7、辅音 1）分给各音节，边界吸到 ±50 ms 内的能量最低点（每个音节至少 40 ms）；
          查不到的词整词一个音节。单音节词的词重音记 1；多音节词没有主重音时，次重音升为主重音，都没有时取声学上最突出的音节
  突显    每个词：音高峰（先去掉整句的下倾趋势）、响度、按音素数归一的时长、音高起伏，各取句内 z 分数，
          加权 1.0 / 0.8 / 0.8 / 0.5 → prom；prom ≥ 0.6 算重读（acc）
  停顿    词与词之间最长的一段低能量（低于全句 95 分位 30 dB）≥ 0.1 s 记为停顿（pause，秒）
  短语    标点（, . ; : ? ! … — –）或 ≥ 0.15 s 的停顿处断开；短语里最后一个重读词是调核（nuc），
          从调核到短语末的音高走向是句调（ACiP §5 六类，见 _tune）：fall 降 · rise 高升（一般疑问句）· lowrise 低升（延续升调）·
          fallrise 降升（保留、对比）· risefall 升降（强调）· level 平；tune_st = 末端 − 峰值（半音）；bt = 边界调
          （L-L% / H-H% / L-H% / H-L%）。句末落进嘎裂声（有声但不周期）的当作低边界
  目标调  每个重读词的重读音节按 ToBI 记目标调 tone：L+H*（从低处尖升 ≥ 4 半音）· L*（低于短语中位 2 半音）·
          !H*（比前一个重音峰低 ≥ 1.5 半音：downdrift）· H*；beats = 重读音节中点（节奏）
  突显    按重读音节算音高峰和响度（不是整词），时长按整词归一；虚词突显够高的记 strong（读了强式）；
          多音节词实测重音不在词典标的音节上记 stress_at；词典有几个读音（record n./v.）时按实测重音挑一个，ipa 记所选
  文本规则  library/phonrules.py：弱读 weak、闪音 flap、喉塞 glot、复合名词 comp、旧信息 given、对比 contr、连读点 links、
          规则调核 rule_nuc（短语最后一个新信息实词，对比项优先）、规则句调（特殊疑问句 fall、一般疑问句 / 反意疑问 rise、
          其余 fall，句中短语 cont）。实测和规则矛盾时调核词上标 odd。notes = 讲解栏的中文说明
  不做    中文句子（含汉字）、对齐对不上 60% 的词（前端那时也不逐词点亮）、读不了的音频：返回 None

输出（数字已取整，一句 2–3 KB）：
  {"v": 2, "ref_hz": 全句浊音中位数 Hz（全句无浊音时 null）, "dur": 音频秒数, "accent": "us" / "uk",
   "words": [  # 和前端 tokensOf(text) 一一对应（同一个正则，对不上的词按前后词插值时间，和 reader.js wordTimes 一致）
     {"w", "s", "e", "st": 词内中位音高（相对 ref_hz 的半音）或 null, "prom": 突显分,
      "sy": [[起, 止, 词重音 0/1/2, 音节峰值半音或 null], ...],
      为真 / 非 0 时才有（省字节，没有就当 false / 0）："acc" 重读, "nuc" 调核, "tune" + "tune_st"（只在调核上）,
      "odd" 实测和规则矛盾（只在调核上）, "rule_nuc" 规则调核, "pause" 词后停顿秒数, "tone" 目标调, "strong" 虚词强式,
      "creak" 词尾嘎裂, "stress_at" 实测重音音节, "ipa" 所选读音, "weak" 弱读式, "flap" / "glot" / "comp" / "given" / "contr"}],
   "curve": [[t, 十分之一半音 或 null], ...]  # 20 ms 取点后按 0.25 半音容差抽稀（点距不等）；null 表示断开（清音 / 停顿）
   "phrases": [{"a": 首词下标, "b": 末词下标, "nuc": 调核下标, "tune", "tune_st", "rule_nuc", "rule_tune", "brk": "|" 或 "‖", "bt": 边界调}],
   "links": [[词下标, cv / vv / r / flap / cc]], "beats": [重读音节中点秒数], "notes": [{"k": 知识点, "zh": 中文说明, "ref": 出处}]}
"""
from __future__ import annotations

import logging
import re
import wave
from pathlib import Path
from typing import Callable

import numpy as np

from library.phon import CONS, ONSETS

log = logging.getLogger(__name__)

VERSION = 2                     # 输出格式版本：变了旧的 .pros.json 重算
HOP = 0.01                      # 10 ms 一帧
FMIN, FMAX = 60.0, 400.0
YIN_THR, AP_MAX = 0.15, 0.25
BLOCK = 256                     # 一次算多少帧（长句不一次占太多内存）
ACC, PAUSE, BREAK = 0.6, 0.1, 0.15

TOK = re.compile(r"[A-Za-z0-9]+(?:['’][A-Za-z]+)*")       # 和 static/reader/util.js 的 tokensOf 一致（英文部分）
HAN = re.compile(r"[㐀-鿿豈-﫿]")
BRK = re.compile(r"[,.;:?!…—–]|--")
ABBR = {"mr", "mrs", "ms", "dr", "st", "jr", "sr", "vs", "etc", "prof", "mt", "no"}

# 虚词（规则侧用）：限定词、介词、代词、助动词 / 情态动词、连词，外加不定式 to、存在句 there
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
AUX = {"is", "am", "are", "was", "were", "do", "does", "did", "have", "has", "had", "can", "could", "will",
       "would", "shall", "should", "may", "might", "must", "isn't", "aren't", "wasn't", "weren't", "don't",
       "doesn't", "didn't", "haven't", "hasn't", "hadn't", "can't", "couldn't", "won't", "wouldn't",
       "shouldn't", "mustn't"}

# IPA 切音节：双字符先认；元音（含双元音）是音节核
_IPA2 = ("aʊ", "aɪ", "eɪ", "oʊ", "ɔɪ", "tʃ", "dʒ")
_VOW = set("ɑæʌɔɛɝɚɪiʊuəaeoɒɜɐɨʉ") | {"aʊ", "aɪ", "eɪ", "oʊ", "ɔɪ"}
_ARPA = {v: k for k, v in CONS.items()} | {"g": "G", "ɹ": "R"}   # IPA 辅音 → ARPAbet（查 ONSETS 用）


def _norm(w: str) -> str:
    return w.lower().replace("’", "'")


def _key(w: str) -> str:
    return re.sub(r"[^a-z0-9]", "", _norm(w))


# ---------------------------------------------------------------- 音频与音高
def read_wav(path) -> tuple[np.ndarray, int] | None:
    try:
        with wave.open(str(path)) as w:
            sr, ch, sw, n = w.getframerate(), w.getnchannels(), w.getsampwidth(), w.getnframes()
            raw = w.readframes(n)
    except (OSError, EOFError, wave.Error):
        return None
    if sw != 2 or sr < 8000:
        return None
    x = np.frombuffer(raw[: len(raw) // (2 * ch) * 2 * ch], dtype="<i2").astype(np.float32) / 32768
    if ch > 1:
        x = x.reshape(-1, ch).mean(axis=1)
    return x, sr


def downsample(x: np.ndarray, sr: int, target: int = 12000) -> tuple[np.ndarray, int]:
    """整数倍降采样（频域截断 = 理想低通）：24 kHz → 12 kHz，YIN 快一倍多、精度不变（降到 8 kHz 粗差翻倍，不用）。"""
    q = sr // target
    if q < 2 or len(x) < 2 * q:
        return x, sr
    n = q * (1 << int(np.ceil(np.log2(len(x) / q))))
    m = n // q
    y = np.fft.irfft(np.fft.rfft(x, n)[: m // 2 + 1], m) / q
    return y[: len(x) // q].astype(np.float32), sr // q


def _zcr(x: np.ndarray, sr: int, nf: int, win: float = 0.03) -> np.ndarray:
    """每帧（10 ms 步长、30 ms 窗）的过零率（0–1）：嘎裂声很低，清擦音很高。"""
    W, H = int(sr * win), int(sr * HOP)
    sgn = np.signbit(x).astype(np.int8)
    ch = np.concatenate([[0], np.abs(np.diff(sgn))])
    c = np.concatenate([[0], np.cumsum(ch)])
    out = np.zeros(nf)
    for i in range(nf):
        a, b = max(0, i * H - W // 2), min(len(x), i * H + W // 2)
        out[i] = (c[b] - c[a]) / max(1, b - a)
    return out


def _slide(a: np.ndarray, k: int, fn) -> np.ndarray:
    """长度为 2k+1 的滑动窗口统计量，边上窗口截短（用 NaN 填充 + nan 统计）。"""
    p = np.concatenate([np.full(k, np.nan), a, np.full(k, np.nan)])
    return fn(np.lib.stride_tricks.sliding_window_view(p, 2 * k + 1), axis=1)


def yin(x: np.ndarray, sr: int, fmin=FMIN, fmax=FMAX, hop=HOP, thr=YIN_THR):
    """向量化 YIN。返回 (帧时刻, f0（0 = 清音）, 非周期度, 帧能量 dB)；第 i 帧的窗口以 i·hop 为中心。"""
    W, H = int(sr * 0.03), int(sr * hop)
    tmin, tmax = int(sr / fmax), int(sr / fmin)
    N = W + tmax
    nf = 1 + len(x) // H
    pad = np.concatenate([np.zeros(W // 2, np.float32), x, np.zeros(N + H, np.float32)])
    nfft = 1 << int(np.ceil(np.log2(N)))
    lag = np.arange(1, tmax + 1)
    f0, ap, db = np.zeros(nf), np.ones(nf), np.zeros(nf)
    for a in range(0, nf, BLOCK):
        fr = np.arange(a, min(nf, a + BLOCK))
        F = pad[fr[:, None] * H + np.arange(N)[None, :]].astype(np.float64)
        # r(τ) = Σ_{j<W} x[j]·x[j+τ]：窗口头部和整帧的互相关（FFT，nfft ≥ N 不会绕回）
        r = np.fft.irfft(np.fft.rfft(F, nfft) * np.conj(np.fft.rfft(F[:, :W], nfft)), nfft)[:, : tmax + 1]
        c = np.concatenate([np.zeros((len(fr), 1)), np.cumsum(F * F, axis=1)], axis=1)
        e0 = c[:, W]
        d = e0[:, None] + (c[:, W:W + tmax + 1] - c[:, : tmax + 1]) - 2 * r
        d[:, 0] = 0
        dn = np.ones_like(d)
        dn[:, 1:] = d[:, 1:] * lag / np.maximum(np.cumsum(d[:, 1:], axis=1), 1e-12)
        # 第一个低于阈值的谷，顺坡走到谷底；没有低于阈值的取全局最小
        seg = dn[:, tmin:tmax]
        below = seg < thr
        has = below.any(axis=1)
        t = np.where(has, below.argmax(axis=1), seg.argmin(axis=1)) + tmin
        up = np.diff(dn[:, : tmax + 1], axis=1) >= 0              # up[k]: dn[k+1] ≥ dn[k]
        cols = np.arange(tmax)
        m = up & (cols[None, :] >= t[:, None])
        t = np.where(has, np.where(m.any(axis=1), m.argmax(axis=1), tmax - 1), t)
        t = np.clip(t, 1, tmax - 1)
        ix = np.arange(len(fr))
        y0, y1, y2 = dn[ix, t - 1], dn[ix, t], dn[ix, t + 1]
        den = y0 - 2 * y1 + y2
        tt = t + np.where(np.abs(den) > 1e-12, 0.5 * (y0 - y2) / np.where(den == 0, 1, den), 0.0)
        f0[fr] = sr / np.clip(tt, tmin * 0.5, tmax + 1)
        ap[fr] = y1
        db[fr] = 10 * np.log10(np.maximum(e0 / W, 1e-12))
    voiced = (ap < AP_MAX) & (db > max(db.max() - 35, -60.0)) & (f0 >= fmin * 0.9) & (f0 <= fmax * 1.1)
    f0 = np.where(voiced, f0, 0.0)
    v = f0 > 0
    if v.sum() > 5:
        fv = f0[v]
        for _ in range(2):                                        # 局部中位数纠正倍频 / 半频
            loc = _slide(fv, 15, np.nanmedian)
            r2 = fv / loc
            fv = np.where(r2 > 1.8, fv / 2, np.where(r2 < 0.55, fv * 2, fv))
        f0[v] = _slide(fv, 2, np.nanmedian)
    return np.arange(nf) * hop, f0, ap, db


# ---------------------------------------------------------------- 音节
def ipa_syllables(ipa: str | None) -> list[tuple[list[str], int]] | None:
    """IPA → [(音素列表, 词重音 0/1/2)]；没有元音或解析不了时 None。多个读音（"/ə/ / /eɪ/"）取第一个。"""
    if not ipa:
        return None
    m = re.search(r"/([^/]+)/", ipa)
    s = (m.group(1) if m else ipa).strip().split(",")[0]
    ph: list[str] = []
    marks: dict[int, int] = {}                                    # 音素下标 → 这个音素前的重音
    i = 0
    while i < len(s):
        c = s[i]
        if c in "ˈˌ'":
            marks[len(ph)] = 1 if c in "ˈ'" else 2
            i += 1
        elif s[i:i + 2] in _IPA2:
            ph.append(s[i:i + 2]); i += 2
        elif c.isalpha() or c in _VOW:
            ph.append(c); i += 1
        else:
            i += 1                                                # ː . 空格 连字符等
    vi = [k for k, p in enumerate(ph) if p in _VOW]
    if not vi:
        return None
    cuts = [0]
    for a, b in zip(vi, vi[1:]):
        mk = [k for k in range(a + 1, b + 1) if k in marks]
        if mk:
            cuts.append(mk[-1]); continue
        cons = [_ARPA.get(p, p.upper()) for p in ph[a + 1:b]]
        on = 0
        for n in range(len(cons), 0, -1):
            if n == 1 or tuple(cons[-n:]) in ONSETS:
                on = n
                break
        cuts.append(b - on)
    cuts.append(len(ph))
    out = []
    for k, (a, b) in enumerate(zip(cuts, cuts[1:])):
        lo = 0 if k == 0 else a                                   # 词首的重音符号在第一个辅音前
        st = next((marks[j] for j in range(lo, vi[k] + 1) if j in marks), 0)
        out.append((ph[a:b], st))
    if len(out) == 1:
        out = [(out[0][0], 1)]                                    # 单音节词不标重音：词重音记 1
    return out


def _split(s: float, e: float, syl, t: np.ndarray, db: np.ndarray) -> list[float]:
    """把 [s, e] 按音素权重分给各音节，内部边界吸到 ±50 ms 内能量最低的帧（每个音节至少 40 ms）。"""
    wts = [sum(1.7 if p in _VOW else 1.0 for p in seg) for seg, _ in syl]
    bnd = list(s + np.cumsum([0] + wts) / sum(wts) * (e - s))
    for k in range(1, len(bnd) - 1):
        m = (t >= max(bnd[k] - 0.05, bnd[k - 1] + 0.04)) & (t <= min(bnd[k] + 0.05, bnd[k + 1] - 0.04))
        if m.sum() >= 3:
            bnd[k] = float(t[m][np.argmin(db[m])])
    return bnd


# ---------------------------------------------------------------- 工具
def _z(a) -> np.ndarray:
    a = np.asarray(a, float)
    m = np.isfinite(a)
    if m.sum() < 2:
        return np.zeros_like(a)
    a = np.where(m, a, a[m].mean())
    sd = a.std()
    return (a - a.mean()) / sd if sd > 1e-6 else np.zeros_like(a)


def _r(v, n=2):
    return None if v is None or not np.isfinite(v) else round(float(v), n) + 0.0     # + 0.0：不出 -0.0


def _runs(mask: np.ndarray) -> list[tuple[int, int]]:
    """True 的连续段 [(起, 止)]，止不含。"""
    d = np.diff(np.concatenate([[0], mask.astype(np.int8), [0]]))
    return list(zip(np.flatnonzero(d == 1), np.flatnonzero(d == -1)))


def _rdp(t: np.ndarray, y: np.ndarray, tol: float) -> list[int]:
    """Ramer–Douglas–Peucker 抽稀：留下的点连成折线和原曲线的差 ≤ tol。"""
    keep, stack = {0, len(y) - 1}, [(0, len(y) - 1)]
    while stack:
        a, b = stack.pop()
        if b - a < 2:
            continue
        line = y[a] + (y[b] - y[a]) * (t[a + 1:b] - t[a]) / max(t[b] - t[a], 1e-9)
        k = int(np.argmax(np.abs(y[a + 1:b] - line)))
        if abs(y[a + 1 + k] - line[k]) > tol:
            keep.add(a + 1 + k)
            stack += [(a, a + 1 + k), (a + 1 + k, b)]
    return sorted(keep)


def _curve(t: np.ndarray, st: np.ndarray) -> list:
    """平滑音高曲线：补上 ≤ 50 ms 的清音空隙、去掉 < 30 ms 的浊音碎片、3 点平均，20 ms 取点后抽稀。"""
    y = st.copy()
    ok = np.isfinite(y)
    for a, b in _runs(ok):
        if b - a < 3:
            y[a:b] = np.nan
    ok = np.isfinite(y)
    for a, b in _runs(~ok):
        if a > 0 and b < len(y) and b - a <= 5:
            y[a:b] = np.interp(np.arange(a, b), [a - 1, b], [y[a - 1], y[b]])
    ok = np.isfinite(y)
    sm = y.copy()
    for a, b in _runs(ok):
        if b - a >= 3:
            seg = y[a:b]
            sm[a:b] = np.convolve(np.concatenate([seg[:1], seg, seg[-1:]]), np.ones(3) / 3, "valid")
    out: list = []
    for a, b in _runs(np.isfinite(sm)):
        idx = np.arange(a, b, 2)
        if idx[-1] != b - 1:
            idx = np.append(idx, b - 1)
        keep = _rdp(t[idx], sm[idx], 0.25)
        if out:
            out.append([round(float(t[a - 1]), 2), None])         # 断开：空隙里放一个 null
        out += [[round(float(t[idx[k]]), 2), int(round(sm[idx[k]] * 10))] for k in keep]
    return out


# ---------------------------------------------------------------- 整句分析
def analyze(wav_path, words, text: str, lookup_ipa: Callable[[str], str | None], lookup=None, accent: str = "us", prev=None) -> dict | None:
    """一句话的语调数据（见模块说明的输出格式）；中文、对齐太差、音频读不了时 None。不抛异常。
    lookup(词) → 词典行（文本规则用：词性、读音），accent = 朗读音色的口音（us / uk），prev = 前几句原文（判断旧信息）。"""
    try:
        return _analyze(Path(wav_path), words, text or "", lookup_ipa, lookup, accent, prev)
    except Exception:                                             # noqa: BLE001  语调只是附加显示，出错不能影响朗读
        log.exception("prosody: %s", wav_path)
        return None


def _align(text: str, words) -> list[dict] | None:
    """句子里的词（和前端 tokensOf 同一个正则）对上对齐结果；对不上的按前后词插值（同 reader.js wordTimes）。"""
    toks = [(m.group(), m.start(), m.end()) for m in TOK.finditer(text)]
    al = []
    for w in words or []:
        try:
            s, e = float(w[1]), float(w[2])
        except (TypeError, ValueError, IndexError):
            continue
        if np.isfinite(s) and np.isfinite(e):
            al.append((str(w[0]), max(0.0, s), max(0.0, s, e)))
    if not toks or not al:
        return None
    rows, j = [], 0
    for k, (w, a, b) in enumerate(toks):
        nxt = toks[k + 1][1] if k + 1 < len(toks) else len(text)
        p = re.sub(r"[\s\"“”‘’'()\[\]]", "", text[b:nxt])
        hit = None
        for d in range(4):
            if j + d < len(al) and _key(al[j + d][0]) == _key(w):
                hit = al[j + d][1:]; j += d + 1
                break
        rows.append({"w": w, "p": p, "t": hit})
    if sum(r["t"] is not None for r in rows) < len(rows) * 0.6:
        return None
    for k, r in enumerate(rows):
        if r["t"] is None:
            prev = next((x["t"] for x in reversed(rows[:k]) if x["t"]), None)
            nxt = next((x["t"] for x in rows[k + 1:] if x["t"]), None)
            s = prev[1] if prev else 0.0
            r["t"] = (s, max(s, nxt[0] if nxt else s + 0.3))
    return rows


def _rule_tune(rows: list[dict]) -> str:
    end = rows[-1]["p"]
    if "?" in end:
        first = _norm(rows[0]["w"])
        return "fall" if first in WH else "rise"
    return "fall"


BT = {"fall": "L-L%", "rise": "H-H%", "lowrise": "L-H%", "fallrise": "L-H%", "risefall": "L-L%", "level": "H-L%"}


def _tune(sv: np.ndarray) -> tuple[str | None, float | None]:
    """调核到短语末的音高走向 → 六类句调（ACiP §5）和 tune_st（末端 − 峰值，半音）：
      risefall 先升 ≥ 3 再降 ≥ 3（强调、惊讶）· fallrise 降 ≥ 3 后回升 ≥ 2（保留、对比）·
      rise 从低点升 ≥ 5（一般疑问句的高升）· lowrise 升 2–5（延续升调）· fall 峰比末端高 ≥ 3 · level 其余"""
    if sv.size < 6:
        return None, None
    n = len(sv)
    half = sv[: max(3, n // 2)]
    pk_i, pk = int(np.argmax(half)), float(np.max(half))
    end = float(np.median(sv[-4:]))
    start = float(np.median(sv[:3]))
    lo_i, lo = int(np.argmin(sv)), float(np.min(sv))
    mx_i, mx = int(np.argmax(sv)), float(np.max(sv))
    tst = round(end - pk, 1)
    if mx - start >= 3 and 3 <= mx_i < n - 3 and mx - end >= 3:
        return "risefall", tst
    if pk - lo >= 3 and lo_i > pk_i and lo_i < n - 3 and end - lo >= 2:
        return "fallrise", tst
    if end - lo >= 5 and lo_i < n - 4:
        return "rise", tst
    if end - lo >= 2 and lo_i < n - 4:
        return "lowrise", tst
    if pk - end >= 3:
        return "fall", tst
    if end - start >= 1.5:
        return "lowrise", tst
    return "level", tst


def _analyze(wav: Path, words, text: str, lookup_ipa, lookup=None, accent: str = "us", prev=None) -> dict | None:
    if HAN.search(text) or not re.search(r"[A-Za-z]", text):
        return None
    rows = _align(text, words)
    got = read_wav(wav) if rows else None
    if got is None:
        return None
    x, sr = got
    t, f0, ap, db = yin(*downsample(x, sr))
    v = f0 > 0
    ref = float(np.median(f0[v])) if v.any() else None
    st = np.where(v, 12 * np.log2(np.maximum(f0, 1) / (ref or 100.0)), np.nan)
    dbr = db - db.max()
    loud = float(np.percentile(db, 95))
    sil = db < max(loud - 30, -60.0) if loud > -60 else np.zeros(len(db), bool)   # 整段没有声音时不谈停顿
    creaky = (~v) & (dbr > -25) & (_zcr(x, sr, len(t)) < 0.06)                      # 有声、不周期、过零率低：嘎裂声（句末常见；清擦音过零率高，不算）

    from . import phonrules
    rules = phonrules.analyze(text, lookup, accent, prev)
    rw = rules["words"] if len(rules["words"]) == len(rows) else [{} for _ in rows]

    out = []
    for r, rr in zip(rows, rw):
        s, e = r["t"]
        m = (t >= s) & (t < e)
        sv = st[m & v]
        w = _norm(r["w"])
        try:
            ipa = lookup_ipa(r["w"].replace("’", "'"))
        except Exception:                                         # noqa: BLE001  词典出错按查不到处理
            ipa = None
        readings = phonrules._readings(ipa if isinstance(ipa, str) else None)
        syls = [(rd, ipa_syllables(rd)) for rd in readings]
        syls = [(rd, sy) for rd, sy in syls if sy] or [("", [(list(r["w"]), 1)])]
        out.append({"w": r["w"], "p": r["p"], "s": s, "e": e, "fn": w in FUNC, "rr": rr, "readings": syls,
                    "st": float(np.median(sv)) if sv.size else np.nan,
                    "hi": float(np.percentile(sv, 90)) if sv.size >= 3 else np.nan,
                    "lo": float(np.percentile(sv, 10)) if sv.size >= 3 else np.nan,
                    "db": float(np.percentile(dbr[m], 80)) if m.any() else np.nan,
                    "creak": bool(m.sum() >= 3 and creaky[m][-min(10, int(m.sum())):].mean() > 0.6)})

    # 停顿：词与词之间最长的一段低能量
    for k, o in enumerate(out):
        o["pause"] = 0.0
        if k + 1 < len(out):
            nx = out[k + 1]
            lo, hi = max(o["e"] - 0.1, (o["s"] + o["e"]) / 2), min(nx["s"] + 0.1, (nx["s"] + nx["e"]) / 2)
            m = (t >= lo) & (t < hi)
            if m.sum() >= 2:
                run = max((b - a for a, b in _runs(sil[m])), default=0) * HOP
                o["pause"] = round(run, 2) if run >= PAUSE - 1e-9 else 0.0

    # 音节：先按第一个读音切；每个音节的音高峰、响度、时长
    def measure(o, syl):
        bnd = _split(o["s"], o["e"], syl, t, db) if len(syl) > 1 and o["e"] > o["s"] else [o["s"]] + [o["e"]] * len(syl)
        sy = []
        for k, (_, x_) in enumerate(syl):
            m = (t >= bnd[k]) & (t < bnd[k + 1])
            sv = st[m & v]
            sy.append([bnd[k], bnd[k + 1], x_, float(np.max(sv)) if sv.size else np.nan,
                       float(np.percentile(dbr[m], 80)) if m.any() else np.nan])
        return sy

    def peak_idx(sy):
        """声学上最突出的音节：音高峰 + 响度 + 时长的 z 分数之和。"""
        sc = _z([q[3] for q in sy]) + _z([q[4] for q in sy]) + _z([q[1] - q[0] for q in sy])
        return int(np.argmax(sc))

    for o in out:
        rd, syl = o["readings"][0]
        o["sy"] = measure(o, syl)
        o["nph"] = max(1, sum(len(seg) for seg, _ in syl))
        o["dict_main"] = next((i for i, q in enumerate(o["sy"]) if q[2] == 1), None) if rd else None
        o["ipa"] = rd
        # 词典给了几个读音（record n./v.）：重音位置不同时，按实测重音挑一个
        alts = [(r2, s2) for r2, s2 in o["readings"][1:] if [x for _, x in s2] != [x for _, x in syl] and len(s2) == len(syl)]
        if alts and len(syl) > 1:
            pk = peak_idx(o["sy"])
            for r2, s2 in alts:
                if next((i for i, (_, x_) in enumerate(s2) if x_ == 1), None) == pk and o["dict_main"] != pk:
                    o["ipa"], o["dict_main"] = r2, pk
                    for q, (_, x_) in zip(o["sy"], s2):
                        q[2] = x_
                    o["alt"] = True
                    break
        if not any(q[2] == 1 for q in o["sy"]):
            sec = [q for q in o["sy"] if q[2] == 2]
            if sec:
                sec[0][2] = 1
            else:
                o["sy"][peak_idx(o["sy"])][2] = 1

    # 突显：按重读音节算音高峰和响度（ACiP §5：重音 = 呼吸加强 + 音段完整 + 音高范围），时长按整词、按音素数归一
    per = np.array([(o["e"] - o["s"]) / o["nph"] for o in out])
    pos = per[per > 0]
    dn = per / (np.median(pos) if pos.size else 1.0)
    main = []
    for o in out:
        q = next((q for q in o["sy"] if q[2] == 1), o["sy"][0])
        main.append(q)
    hh = np.array([q[3] if np.isfinite(q[3]) else o["hi"] for q, o in zip(main, out)])
    dd = np.array([q[4] if np.isfinite(q[4]) else o["db"] for q, o in zip(main, out)])
    tc = np.array([(o["s"] + o["e"]) / 2 for o in out])
    fin = np.isfinite(hh)
    if fin.sum() >= 3 and np.ptp(tc[fin]) > 0:
        k1, b1 = np.polyfit(tc[fin], hh[fin], 1)                  # 去掉整句的下倾趋势
        hh = hh - (k1 * tc + b1)
    prom = 1.0 * _z(hh) + 0.8 * _z(dd) + 0.8 * _z(dn) + 0.5 * _z([o["hi"] - o["lo"] for o in out])
    for o, p in zip(out, prom):
        o["prom"], o["acc"] = float(p), bool(p >= ACC)
        if o["fn"] and p >= 0.3 and o["rr"].get("weak"):
            o["strong"] = True                                    # 虚词读了强式
        if o["acc"] and o["dict_main"] is not None and len(o["sy"]) > 1 and not o.get("alt"):
            pk = peak_idx(o["sy"])
            a_, b_ = o["sy"][pk][3], o["sy"][o["dict_main"]][3]
            if pk != o["dict_main"] and np.isfinite(a_) and np.isfinite(b_) and a_ - b_ >= 3 and o["sy"][pk][4] >= o["sy"][o["dict_main"]][4]:
                o["stress_at"] = pk                               # 朗读者的重音不在词典标的音节上（音高高 3 半音且不更轻）

    # 短语、调核、句调、目标调；规则侧来自 phonrules（新旧信息、对比、复合名词）
    phrases, cur = [], []
    for k, o in enumerate(out):
        cur.append(k)
        brk = bool(BRK.search(o["p"])) and not (o["p"] == "." and _key(o["w"]) in ABBR)
        if k + 1 < len(out) and (brk or o["pause"] >= BREAK):
            phrases.append(cur); cur = []
    if cur:
        phrases.append(cur)
    ph_out, beats = [], []
    for n, ph in enumerate(phrases):
        for k in ph:                                              # 对比项教科书上也要重读
            if out[k]["rr"].get("contr") and out[k]["prom"] >= 0.0:
                out[k]["acc"] = True
        acc = [k for k in ph if out[k]["acc"]]
        nk = acc[-1] if acc else max(ph, key=lambda k: out[k]["prom"])
        nuc = out[nk]
        nuc["acc"] = nuc["nuc"] = True
        m = (t >= nuc["s"]) & (t < out[ph[-1]]["e"]) & v
        tune, tst = _tune(st[m])
        if tune is None and out[ph[-1]]["creak"]:                # 句末落进嘎裂声：当作低边界
            tune, tst = "fall", None
        if tune:
            nuc["tune"] = tune
            if tst is not None:
                nuc["tune_st"] = tst
        # 目标调（ToBI）：重读音节的峰相对短语中位；比前一个峰低 ≥ 1.5 半音的记降阶 !H*
        pm = st[(t >= out[ph[0]]["s"]) & (t < out[ph[-1]]["e"]) & v]
        med = float(np.median(pm)) if pm.size else 0.0
        prev_pk = None
        for k in ph:
            o = out[k]
            if not o["acc"]:
                continue
            q = next((q for q in o["sy"] if q[2] == 1), o["sy"][0])
            pk = q[3] if np.isfinite(q[3]) else o["hi"]
            if not np.isfinite(pk):
                continue
            before = st[(t >= o["s"]) & (t < q[0]) & v]
            valley = float(np.min(before)) if before.size >= 2 else None
            if valley is not None and pk - valley >= 4:
                tone = "L+H*"
            elif pk < med - 2:
                tone = "L*"
            elif prev_pk is not None and pk <= prev_pk - 1.5:
                tone = "!H*"
            else:
                tone = "H*"
            if k == nk and tune == "fallrise" and tone == "H*":
                tone = "L+H*"
            o["tone"] = tone
            prev_pk = pk
            beats.append(round((q[0] + q[1]) / 2, 2))
        rk = next((k for k in reversed(ph) if out[k]["rr"].get("rule_nuc")), None)
        if rk is None:
            cw = [k for k in ph if not out[k]["fn"]]
            rk = cw[-1] if cw else ph[-1]
        out[rk]["rule_nuc"] = True
        last = n == len(phrases) - 1
        rt = (rules["tune"] if rw[0] else _rule_tune(out)) if last else "cont"
        if (nuc["fn"] and rk != nk and not nuc["rr"].get("contr")) or (tune == "rise" and rt == "fall") or (tune == "fall" and rt == "rise"):
            nuc["odd"] = True
        ph_out.append({"a": ph[0], "b": ph[-1], "nuc": nk, "tune": tune, "tune_st": nuc.get("tune_st"), "rule_nuc": rk,
                       "rule_tune": rt, "brk": "‖" if last else "|", "bt": BT.get(tune) if tune else None})

    res = []
    for o in out:
        d = {"w": o["w"], "s": _r(o["s"]), "e": _r(o["e"]), "st": _r(o["st"], 1), "prom": _r(o["prom"], 1),
             "sy": [[_r(a), _r(b), x_, _r(p, 1)] for a, b, x_, p, _ in o["sy"]]}
        for k in ("acc", "nuc", "tune", "tune_st", "odd", "rule_nuc", "pause", "tone", "strong", "creak", "stress_at"):
            if o.get(k) or (k in ("tune_st", "stress_at") and o.get(k) is not None):   # 假 / 0 / 没有的键省掉（前端按 false / 0 处理）
                d[k] = o[k]
        for k in ("weak", "flap", "glot", "comp", "given", "contr"):             # 文本规则（phonrules）
            if o["rr"].get(k):
                d[k] = o["rr"][k]
        if o.get("alt"):                                                          # 按实测重音选了另一个读音（record n./v.）
            d["ipa"] = f"/{o['ipa']}/"
        res.append(d)
    pr = {"v": 2, "ref_hz": _r(ref, 1), "dur": _r(len(x) / sr), "accent": accent, "words": res, "curve": _curve(t, st),
          "phrases": ph_out, "links": rules["links"], "beats": beats}
    pr["notes"] = phonrules.notes(rules, pr)
    return pr
