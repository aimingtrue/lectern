"""句子语调（library/prosody.py）：合成的浊音信号（已知频率、句末滑降 / 滑升、静音间隔）检查音高精度、句调、
停顿、调核选择；IPA 切音节；各种边界情况不抛异常。"""
from __future__ import annotations

import json
import wave

import numpy as np
import pytest

from library import prosody as P

SR = 24000
IPA = {"important": "/ˌɪmˈpɔrtənt/", "daisy": "/ˈdeɪzi/", "the": "/ðə/ / /ðʌ/", "everything": "/ˈɛvriˌθɪŋ/",
       "created": "/kriˈeɪtəd/ / /kriˈeɪtɪd/", "instrument": "/ˈɪnstrəmənt/", "button": "/ˈbʌtən/"}


def voiced(dur, f0, f1=None, amp=0.3, sr=SR, phase=0.0):
    """谐波丰富的「元音」：基频从 f0 线性滑到 f1，8 个谐波按 1/k 衰减。返回 (信号, 结束相位)。"""
    n = int(round(dur * sr))
    f = np.linspace(f0, f1 if f1 else f0, n, endpoint=False)
    ph = phase + 2 * np.pi * np.cumsum(f) / sr
    x = sum(np.sin(k * ph) / k for k in range(1, 9)) * amp / 1.5
    return x.astype(np.float32), float(ph[-1]) if n else phase


def build(tmp_path, spec, name="s.wav", lead=0.15, tail=0.15):
    """spec: [(词, 时长, f0, f1, 音量, 词后静音)]；f0 = 0 表示这段是清音（静音）。写 wav，返回 (路径, 对齐词表)。"""
    parts, words, t, ph = [np.zeros(int(lead * SR), np.float32)], [], lead, 0.0
    for w, dur, f0, f1, amp, gap in spec:
        if f0:
            x, ph = voiced(dur, f0, f1, amp, phase=ph)
        else:
            x = np.zeros(int(round(dur * SR)), np.float32)
        parts.append(x)
        words.append([w, round(t, 3), round(t + dur, 3)])
        t += dur
        if gap:
            parts.append(np.zeros(int(round(gap * SR)), np.float32)); t += gap
    parts.append(np.zeros(int(tail * SR), np.float32))
    sig = np.concatenate(parts)
    sig = sig + np.random.default_rng(0).normal(0, 1e-4, len(sig)).astype(np.float32)    # 一点底噪
    path = tmp_path / name
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(SR)
        w.writeframes((np.clip(sig, -1, 1) * 32767).astype("<i2").tobytes())
    return path, words


def no_ipa(_w):
    return None


def ipa(w):
    return IPA.get(w.lower())


def flat(words, f=120.0, amp=0.3, per=0.07):
    """每个词时长和字母数成正比（按音素归一后时长一样），平调。"""
    return [(w, per * len(w), f, f, amp, 0.0) for w in words]


# ---------------------------------------------------------------- 音高
def test_f0_steady_and_step():
    x = np.concatenate([voiced(0.6, 100)[0], voiced(0.6, 200)[0]])
    for xx, sr in ((x, SR), P.downsample(x, SR)):
        t, f0, ap, db = P.yin(xx, sr)
        a, b = f0[(t > 0.1) & (t < 0.5)], f0[(t > 0.7) & (t < 1.1)]
        assert (a > 0).all() and (b > 0).all()
        assert abs(np.median(a) - 100) < 1.0 and abs(np.median(b) - 200) < 2.0
        assert np.abs(12 * np.log2(a / 100)).max() < 0.3 and np.abs(12 * np.log2(b / 200)).max() < 0.3


def test_f0_glide_tracks_and_silence_unvoiced():
    x = np.concatenate([voiced(0.8, 180, 100)[0], np.zeros(SR // 2, np.float32)])
    t, f0, _, _ = P.yin(*P.downsample(x, SR))
    m = (t > 0.1) & (t < 0.7)
    want = 180 + (100 - 180) * t[m] / 0.8
    assert np.median(np.abs(12 * np.log2(f0[m] / want))) < 0.2
    assert (f0[t > 0.9] == 0).all()                                # 静音段是清音


def test_downsample_keeps_length_and_band():
    x = voiced(0.5, 150)[0]
    y, sr = P.downsample(x, SR)
    assert sr == 12000 and len(y) == len(x) // 2
    assert P.downsample(x, 11025) == (x, 11025)                   # 不是整数倍就不降


# ---------------------------------------------------------------- 句调、调核、停顿
def test_nucleus_and_fall(tmp_path):
    ws = "we saw the big red house".split()
    spec = flat(ws)
    spec[3] = ("big", 0.07 * 3 * 1.6, 190, 190, 0.6, 0.0)          # 高、响、长
    spec[-1] = ("house", 0.35, 120, 85, 0.3, 0.0)                  # 句末滑降
    path, words = build(tmp_path, spec)
    r = P.analyze(path, words, "We saw the big red house.", no_ipa)
    w = r["words"]
    assert [x["w"] for x in w] == ["We", "saw", "the", "big", "red", "house"]
    assert abs(r["ref_hz"] - 120) < 3
    assert w[3].get("acc") and w[3].get("nuc") and w[3]["st"] > 7
    assert sum(1 for x in w if x.get("nuc")) == 1 and not any(x.get("acc") for x in w[:3])
    assert w[3]["tune"] == "fall" and w[3]["tune_st"] < -3
    assert w[-1].get("rule_nuc") and not w[3].get("odd")          # 规则调核在 house；big 是实词，不算矛盾
    assert r["phrases"] == [{"a": 0, "b": 5, "nuc": 3, "tune": "fall", "tune_st": w[3]["tune_st"],
                             "rule_nuc": 5, "rule_tune": "fall", "brk": "‖", "bt": "L-L%"}]
    assert w[3]["tone"] in ("H*", "L+H*") and r["beats"] and r["notes"][0]["k"] == "sentence-stress" and "house" in r["notes"][0]["zh"]


def test_rise_on_statement_is_odd_and_rise_on_question_is_not(tmp_path):
    spec = flat(["it", "is"]) + [("red", 0.4, 110, 190, 0.35, 0.0)]
    path, words = build(tmp_path, spec)
    r = P.analyze(path, words, "It is red.", no_ipa)
    nuc = r["words"][-1]
    assert nuc.get("nuc") and nuc["tune"] == "rise" and nuc["tune_st"] > 3
    assert nuc.get("odd")                                          # 句号却升调
    q = P.analyze(path, words, "Is it red?", no_ipa)
    assert q["words"][-1]["tune"] == "rise" and not q["words"][-1].get("odd")
    assert q["phrases"][-1]["rule_tune"] == "rise"
    wh = P.analyze(path, words, "Why is red?", no_ipa)
    assert wh["phrases"][-1]["rule_tune"] == "fall" and wh["words"][-1].get("odd")


def test_level(tmp_path):
    path, words = build(tmp_path, flat(["so", "it", "goes", "on"]))
    r = P.analyze(path, words, "So it goes on.", no_ipa)
    assert r["phrases"][-1]["tune"] == "level"


def test_pause_and_phrases(tmp_path):
    spec = [("well", 0.3, 130, 110, 0.3, 0.3), ("I", 0.15, 120, 120, 0.3, 0.0), ("think", 0.3, 125, 125, 0.3, 0.25),
            ("we", 0.15, 120, 120, 0.3, 0.0), ("should", 0.25, 120, 120, 0.3, 0.0), ("go", 0.3, 125, 95, 0.3, 0.0)]
    path, words = build(tmp_path, spec)
    r = P.analyze(path, words, "Well, I think we should go.", no_ipa)
    w = r["words"]
    assert abs(w[0]["pause"] - 0.3) <= 0.05 and abs(w[2]["pause"] - 0.25) <= 0.05
    assert all("pause" not in x for x in (w[1], w[3], w[4], w[5]))
    ph = [(p["a"], p["b"], p["brk"]) for p in r["phrases"]]
    assert ph == [(0, 0, "|"), (1, 2, "|"), (3, 5, "‖")]          # 逗号断开；没有标点的 0.25 s 停顿也断开
    assert [p["rule_tune"] for p in r["phrases"]] == ["cont", "cont", "fall"]


def test_no_break_inside_compounds_and_abbreviations(tmp_path):
    path, words = build(tmp_path, flat(["Mr", "Smith", "is", "well", "known"]))
    r = P.analyze(path, words, "Mr. Smith is well-known.", no_ipa)
    assert len(r["phrases"]) == 1


def test_function_word_nucleus_is_odd(tmp_path):
    spec = flat(["she", "went", "to", "the", "park"])
    spec[3] = ("the", 0.07 * 3 * 1.6, 200, 200, 0.6, 0.0)
    path, words = build(tmp_path, spec)
    r = P.analyze(path, words, "She went to the park.", no_ipa)
    w = r["words"]
    assert w[3].get("nuc") and w[3].get("odd") and w[4].get("rule_nuc")


# ---------------------------------------------------------------- 音节
def test_ipa_syllables():
    st = lambda s: [x for _, x in P.ipa_syllables(s)]
    assert st("/ˌɪmˈpɔrtənt/") == [2, 1, 0]
    assert st("/ˈdeɪzi/") == [1, 0] and P.ipa_syllables("/ˈdeɪzi/")[0][0] == ["d", "eɪ"]
    assert st("/ðə/ / /ðʌ/") == [1]                               # 单音节记 1；多个读音取第一个
    assert st("/ˈɛvriˌθɪŋ/") == [1, 0, 2]
    assert st("/kriˈeɪtəd/") == [0, 1, 0]
    assert [p for p, _ in P.ipa_syllables("/ˈɪnstrəmənt/")] == [["ɪ", "n"], ["s", "t", "r", "ə"], ["m", "ə", "n", "t"]]
    assert [p for p, _ in P.ipa_syllables("/ˈbʌtən/")] == [["b", "ʌ"], ["t", "ə", "n"]]
    assert len(P.ipa_syllables("/ˈfaɪɚ/")) == 2 and len(P.ipa_syllables("/aʊr/")) == 1   # 双元音算一个
    for bad in (None, "", "/ /", "/ʃh/", "abc"):
        assert P.ipa_syllables(bad) is None or len(P.ipa_syllables(bad)) == 1


def test_syllable_spans(tmp_path):
    # important：三个音节，中间一段是更高的音
    path, words = build(tmp_path, [("it's", 0.2, 120, 120, 0.3, 0.0), ("important", 0.6, 120, 120, 0.3, 0.0)])
    r = P.analyze(path, words, "It's important.", ipa)
    sy = r["words"][1]["sy"]
    assert [s[2] for s in sy] == [2, 1, 0]
    assert sy[0][0] == r["words"][1]["s"] and sy[-1][1] == r["words"][1]["e"]
    assert all(b[0] - a[1] == 0 for a, b in zip(sy, sy[1:])) and all(s[1] - s[0] >= 0.04 for s in sy)
    assert r["words"][0]["sy"] == [[r["words"][0]["s"], r["words"][0]["e"], 1, r["words"][0]["sy"][0][3]]]


def test_syllable_boundary_snaps_to_energy_dip(tmp_path):
    # button：两个音节中间放 40 ms 静音（塞音闭塞）；按音素权重分的边界（约 0.19 s 处）应吸到静音里
    path, words = build(tmp_path, [("a", 0.15, 120, 120, 0.3, 0.0), ("bu", 0.2, 130, 130, 0.3, 0.04),
                                   ("tton", 0.2, 125, 125, 0.3, 0.0)])
    words = [words[0], ["button", words[1][1], words[2][2]]]
    r = P.analyze(path, words, "A button.", ipa)
    s = words[1][1]
    cut = r["words"][1]["sy"][0][1]
    assert s + 0.2 <= cut <= s + 0.24 + 0.005


# ---------------------------------------------------------------- 输出格式、边界情况
def test_payload_and_curve(tmp_path):
    ws = "in my younger and more vulnerable years my father gave me some advice".split()
    spec = [(w, 0.06 * len(w), 140 - 3 * k, 135 - 3 * k, 0.3, 0.0) for k, w in enumerate(ws)]
    path, words = build(tmp_path, spec)
    r = P.analyze(path, words, "In my younger and more vulnerable years my father gave me some advice.", ipa)
    s = json.dumps(r, ensure_ascii=False, separators=(",", ":"))
    assert r["v"] == 2 and len(s.encode()) < 4000
    c = r["curve"]
    assert c and all(len(p) == 2 for p in c) and c[0][1] is not None and c[-1][1] is not None
    ts = [p[0] for p in c]
    assert ts == sorted(ts)
    vals = [p[1] for p in c if p[1] is not None]
    assert max(vals) - min(vals) > 20                              # 下倾约 3.5 半音，十分之一半音为单位


def test_curve_breaks_on_pauses(tmp_path):
    path, words = build(tmp_path, [("yes", 0.3, 130, 130, 0.3, 0.4), ("no", 0.3, 110, 110, 0.3, 0.0)])
    r = P.analyze(path, words, "Yes, no.", no_ipa)
    nulls = [p for p in r["curve"] if p[1] is None]
    assert len(nulls) == 1 and words[0][2] - 0.05 <= nulls[0][0] <= words[1][1] + 0.05


def test_silent_audio(tmp_path):
    path, words = build(tmp_path, [("hello", 0.4, 0, 0, 0, 0.0), ("world", 0.4, 0, 0, 0, 0.0)])
    r = P.analyze(path, words, "Hello world.", no_ipa)
    assert r["ref_hz"] is None and r["curve"] == []
    assert all(w["st"] is None for w in r["words"]) and len(r["phrases"]) == 1
    assert r["phrases"][0]["tune"] is None and sum(1 for w in r["words"] if w.get("nuc")) == 1


def test_zero_length_and_unmatched_words(tmp_path):
    spec = flat(["i", "forgot", "to", "ask", "you"])
    path, words = build(tmp_path, spec)
    words[0][2] = words[0][1]                                      # 长度为 0 的词
    words = words[:2] + words[3:]                                  # 对齐结果漏了 to
    r = P.analyze(path, words, "I forgot to ask you.", no_ipa)
    w = r["words"]
    assert [x["w"] for x in w] == ["I", "forgot", "to", "ask", "you"]
    assert w[0]["s"] == w[0]["e"] and w[0]["sy"][0][:3] == [w[0]["s"], w[0]["e"], 1]
    assert w[2]["s"] == w[1]["e"] and w[2]["e"] == w[3]["s"]     # 漏掉的词按前后词插值


def test_short_sentence_and_lookup_errors(tmp_path):
    path, words = build(tmp_path, [("yes", 0.35, 140, 100, 0.3, 0.0)])

    def boom(_w):
        raise RuntimeError("dict down")
    for fn in (no_ipa, boom, lambda w: 42, lambda w: "/ /"):
        r = P.analyze(path, words, "Yes.", fn)
        assert len(r["words"]) == 1 and r["words"][0].get("nuc") and r["words"][0]["tune"] == "fall"


@pytest.mark.parametrize("text,words", [
    ("我忘了问你一件事。", [["我", 0.1, 0.3]]),                    # 中文
    ("Hello 世界", [["Hello", 0.1, 0.3]]),                         # 夹了汉字
    ("", [["x", 0.1, 0.2]]),
    ("…", [["x", 0.1, 0.2]]),
    ("Hello there.", []),                                          # 没有对齐结果
    ("Hello there my old friend.", [["completely", 0.1, 0.2], ["different", 0.2, 0.3]]),   # 对不上
    ("Hello there.", [["Hello"], None, ["there", "a", 2], ["there", float("nan"), 1.0]]),  # 坏数据
])
def test_returns_none(tmp_path, text, words):
    path, _ = build(tmp_path, flat(["hello", "there"]))
    assert P.analyze(path, words, text, no_ipa) is None


def test_bad_audio(tmp_path):
    words = [["Hello", 0.1, 0.3], ["there", 0.3, 0.6]]
    assert P.analyze(tmp_path / "missing.wav", words, "Hello there.", no_ipa) is None
    (tmp_path / "junk.wav").write_bytes(b"not a wav")
    assert P.analyze(tmp_path / "junk.wav", words, "Hello there.", no_ipa) is None
    with wave.open(str(tmp_path / "empty.wav"), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(SR); w.writeframes(b"")
    r = P.analyze(tmp_path / "empty.wav", words, "Hello there.", no_ipa)
    assert r is not None and r["ref_hz"] is None and len(r["words"]) == 2


def test_words_past_audio_end(tmp_path):
    path, words = build(tmp_path, flat(["hello", "there"]))
    words.append(["friend", 50.0, 51.0])                           # 对齐时间超出音频
    r = P.analyze(path, words, "Hello there friend.", no_ipa)
    assert r["words"][2]["st"] is None and len(r["words"][2]["sy"]) == 1


# ---------------------------------------------------------------- v2：六类句调、目标调、嘎裂、节拍、文本规则
def test_fallrise_lowrise_and_risefall(tmp_path):
    spec = flat(["i", "think"]) + [("so", 0.6, 170, 100, 0.35, 0.0), ("far", 0.4, 100, 135, 0.35, 0.0)]   # 降到低点再回升 ≈ 5 半音
    path, words = build(tmp_path, spec)
    r = P.analyze(path, words, "I think so far.", no_ipa)
    assert r["phrases"][-1]["tune"] == "fallrise" and r["phrases"][-1]["bt"] == "L-H%" and not r["words"][-1].get("odd")
    spec = flat(["when", "you", "are"]) + [("winning", 0.5, 120, 138, 0.35, 0.0)]                     # 小幅上扬 ≈ 2.4 半音：延续升调
    path, words = build(tmp_path, spec)
    r = P.analyze(path, words, "When you are winning,", no_ipa)
    assert r["phrases"][-1]["tune"] == "lowrise" and r["phrases"][-1]["bt"] == "L-H%"
    spec = flat(["is", "it"]) + [("true", 0.5, 110, 200, 0.35, 0.0)]                                   # 大幅上升：高升
    path, words = build(tmp_path, spec)
    assert P.analyze(path, words, "Is it true?", no_ipa)["phrases"][-1]["tune"] == "rise"
    spec = flat(["oh"]) + [("really", 0.3, 110, 190, 0.4, 0.0), ("now", 0.4, 190, 95, 0.3, 0.0)]       # 先升后降
    path, words = build(tmp_path, spec)
    r = P.analyze(path, words, "Oh really now!", no_ipa)
    assert r["phrases"][-1]["tune"] == "risefall"


def test_downstep_targets_and_beats(tmp_path):
    ws = "mary's younger brother wanted fifty chocolate peanuts".split()
    spec = []
    for k, w in enumerate(ws):
        hi = k in (0, 2, 4, 6)
        f = {0: 180, 2: 165, 4: 150, 6: 135}.get(k, 115)
        spec.append((w, 0.07 * len(w) * (1.5 if hi else 1), f, f, 0.55 if hi else 0.25, 0.0))
    path, words = build(tmp_path, spec)
    r = P.analyze(path, words, "Mary's younger brother wanted fifty chocolate peanuts.", no_ipa)
    w = r["words"]
    tones = [w[k].get("tone") for k in (0, 2, 4, 6)]
    assert tones[0] == "H*" and tones[1:] == ["!H*", "!H*", "!H*"], tones
    assert len(r["beats"]) == 4 and all(b < c for b, c in zip(r["beats"], r["beats"][1:]))
    assert any(n["k"] == "intonation" and "downdrift" in n["zh"] for n in r["notes"])
    assert any(n["k"] == "rhythm" for n in r["notes"])


def test_creaky_ending_counts_as_low_boundary(tmp_path):
    rng = np.random.default_rng(1)
    spec = flat(["it", "was"]) + [("late", 0.25, 120, 100, 0.3, 0.0)]
    path, words = build(tmp_path, spec)
    with wave.open(str(path)) as f:
        sr, n = f.getframerate(), f.getnframes(); raw = np.frombuffer(f.readframes(n), "<i2").astype(np.float32) / 32768
    s, e = int(words[-1][1] * sr), int(words[-1][2] * sr)
    pulses = np.zeros(e - s, np.float32)                                                      # 20–35 ms 不等的脉冲：嘎裂声
    k = 0
    while k < len(pulses):
        pulses[k] = rng.uniform(0.5, 1.0); k += int(sr * rng.uniform(0.02, 0.035))
    raw[s + (e - s) // 2:e] = 0.4 * np.convolve(pulses, np.hanning(40), "same")[(e - s) // 2:]
    with wave.open(str(path), "wb") as f:
        f.setnchannels(1); f.setsampwidth(2); f.setframerate(sr); f.writeframes((np.clip(raw, -1, 1) * 32767).astype("<i2").tobytes())
    r = P.analyze(path, words, "It was late.", no_ipa)
    assert r["words"][-1].get("creak") and r["phrases"][-1]["tune"] in ("fall", "level") and r["phrases"][-1]["bt"] in ("L-L%", "H-L%")


def test_text_rules_reach_the_payload(tmp_path):
    ws = "i want a red pen not a black one".split()
    spec = flat(ws)
    spec[3] = ("red", 0.25, 170, 170, 0.6, 0.0); spec[7] = ("black", 0.3, 160, 100, 0.6, 0.0)
    path, words = build(tmp_path, spec)
    r = P.analyze(path, words, "I want a red pen, not a black one.", no_ipa, accent="us")
    w = r["words"]
    assert w[2].get("weak") == "ə" and w[4].get("contr") and w[7].get("contr") and w[7].get("rule_nuc")
    assert [k for k, kind in r["links"] if kind == "flap"] == [1, 5]                     # want‿a、not‿a
    assert r["accent"] == "us" and any(n["k"] == "elision" and "闪音" in n["zh"] for n in r["notes"])
    r2 = P.analyze(path, words, "I want a red pen, not a black one.", no_ipa, accent="uk")
    assert not any(kind == "flap" for _, kind in r2["links"])


def test_given_information_moves_the_rule_nucleus(tmp_path):
    ws = "the pressure got higher".split()
    path, words = build(tmp_path, flat(ws))
    r = P.analyze(path, words, "The pressure got higher.", no_ipa, prev=["The pressure was high."])
    w = r["words"]
    assert w[1].get("given") and w[3].get("rule_nuc")
    r = P.analyze(path, words, "The pressure got higher.", no_ipa, prev=["Then it got higher."])
    assert r["words"][1].get("rule_nuc") and r["words"][3].get("given")


def test_strong_form_of_a_function_word(tmp_path):
    spec = flat(["what", "are", "you", "looking"]) + [("at", 0.4, 170, 120, 0.6, 0.0)]
    path, words = build(tmp_path, spec)
    r = P.analyze(path, words, "What are you looking at?", no_ipa)
    assert r["words"][-1].get("strong") and r["words"][-1].get("weak") == "ət"
    assert any("强式" in n["zh"] for n in r["notes"])
