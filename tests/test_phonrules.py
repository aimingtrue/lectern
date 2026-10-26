"""连读与句重音的文本规则（library/phonrules.py）：弱读、连读点、闪音、喉塞、复合名词、新旧信息、对比重音、规则调核与句调、讲解行。"""
from __future__ import annotations

from library import phonrules as R

DICT = {
    "water": {"us": "/ˈwɔtɚ/", "zh": ["n. 水"]}, "button": {"us": "/ˈbʌtən/", "zh": ["n. 纽扣"]}, "important": {"us": "/ˌɪmˈpɔrtənt/", "zh": ["a. 重要的"]},
    "record": {"us": "/rəˈkɔrd/ / /ˈrɛkɚd/", "zh": ["n. 记录", "vt. 记录"]}, "air": {"us": "/ɛr/", "zh": ["n. 空气"]}, "pressure": {"us": "/ˈprɛʃɚ/", "zh": ["n. 压力"]},
    "vocal": {"us": "/ˈvoʊkəl/", "zh": ["a. 声音的"]}, "folds": {"us": "/foʊldz/", "zh": ["n. 褶皱"]}, "red": {"us": "/rɛd/", "zh": ["a. 红的"]}, "pen": {"us": "/pɛn/", "zh": ["n. 笔"]},
    "black": {"us": "/blæk/", "zh": ["a. 黑的"]}, "better": {"us": "/ˈbɛtɚ/", "zh": ["a. 更好的"]}, "go": {"us": "/goʊ/", "zh": ["vi. 去"]}, "end": {"us": "/ɛnd/", "zh": ["n. 末端"]},
    "car": {"us": "/kɑr/", "zh": ["n. 车"]}, "is": {"us": "/ɪz/", "zh": ["v. 是"]}, "money": {"us": "/ˈmʌni/", "zh": ["n. 钱"]}, "mail": {"us": "/meɪl/", "zh": ["vt. 寄"]},
    "big": {"us": "/bɪg/", "zh": ["a. 大"]}, "game": {"us": "/geɪm/", "zh": ["n. 比赛"]},
}
look = lambda w: DICT.get(w.lower().replace("’", "'"))   # noqa: E731


def words(r):
    return r["words"]


def test_weak_forms_and_function_words():
    r = R.analyze("Will you mail me my money or not?", look)
    w = words(r)
    assert w[0]["fn"] and w[0]["weak"] == "əl" and w[1]["weak"] == "jə" and w[5]["weak"] is None
    assert R.weak_form("to") == "tə" and R.weak_form("money") is None
    assert r["tune"] == "rise" and w[r["nuc"]]["w"] == "not" and w[5].get("contr") and w[7].get("contr")   # ACiP (11)：*not


def test_links_flap_glottal_and_accent():
    r = R.analyze("The water got a better car is at the end.", look, "us")
    kinds = {(words(r)[k]["w"], kind) for k, kind in r["links"]}
    assert ("got", "flap") in kinds and ("car", "cv") in kinds and ("the", "vv") in kinds and ("is", "cv") in kinds
    assert words(r)[1].get("flap") and words(r)[4].get("flap")                                     # water、better 词内闪音
    uk = R.analyze("The water got a better car is at the end.", look, "uk")
    assert ("car", "r") in {(words(uk)[k]["w"], kind) for k, kind in uk["links"]} and not any(kind == "flap" for _, kind in uk["links"])
    assert not words(uk)[1].get("flap")
    b = R.analyze("Push the button, it is important.", look, "us")
    assert words(b)[2].get("glot") and not words(b)[2].get("flap") and words(b)[5].get("glot")
    assert R.analyze("It was the best of times.", look, "us")["links"] == [[3, "cv"]], "best of：st 后不闪音"
    assert ("big", "cc") in {(words(x)[k]["w"], kind) for x in [R.analyze("A big game.", look)] for k, kind in x["links"]}


def test_compound_given_and_contrast():
    r = R.analyze("The air pressure got higher.", look, "us", prev=["The pressure was high."])
    w = words(r)
    assert w[2].get("comp") and w[2].get("given") and w[4].get("rule_nuc")
    r = R.analyze("I want a red pen, not a black one.", look)
    w = words(r)
    assert w[4].get("contr") and w[7].get("contr") and w[7].get("rule_nuc") and [p["rule_tune"] for p in r["phrases"]] == ["cont", "fall"]
    r = R.analyze("Either the car or the pen.", look)
    assert words(r)[2].get("contr") and words(r)[5].get("contr")


def test_rule_tune_wh_yesno_tag_and_exclamation():
    assert R.analyze("Where were you last night?", look)["tune"] == "fall"
    assert R.analyze("Must it be printed?", look)["tune"] == "rise"
    assert R.analyze("He put it on the button, isn't it?", look)["tune"] == "rise"
    assert R.analyze("Go on!", look)["tune"] == "fall"
    assert R.analyze("Mr. Smith is well-known.", look)["phrases"][0]["rule_tune"] == "fall", "Mr. 不断句"


def test_readings_and_notes():
    r = R.analyze("Play the record.", look)
    assert words(r)[2]["readings"] == ["rəˈkɔrd", "ˈrɛkɚd"] and words(r)[2]["pos"] == "n"
    ns = R.notes(R.analyze("Will you mail me my money or not?", look), None)
    ks = [n["k"] for n in ns]
    assert ks[0] == "sentence-stress" and "reduced-vowel" in ks and all(n.get("ref") for n in ns)
    assert "*not" in ns[0]["zh"] and "money" in next(n["zh"] for n in ns if "对比" in n["zh"])
    assert R.notes(R.analyze("", look), None) == []
