"""难词标注规则：虚词、缩写不标；模型补的关键表达要过服务端检查；截断的译文不进缓存；难点讲解单独取。"""
from __future__ import annotations

import pytest

from test_library import lib, make_pdf  # noqa: F401  （lib 是 fixture）
from test_narrate_fixes import HAS_DICT, site


class ScriptedLLM:
    """按顺序返回预设的 JSON，并记下每次的提示词。"""
    def __init__(self, *answers):
        self.answers, self.calls = list(answers), []

    def chat_json(self, model, system, user, **k):
        self.calls.append((system, user))
        return self.answers.pop(0) if self.answers else {"zh": "译文。", "gloss": []}


@pytest.mark.skipif(not HAS_DICT, reason="没有 data/dict.db")
def test_function_words_and_contractions_are_not_hard_words(lib):
    c, _, _ = site(lib)
    c.app.state.lib.llm = ScriptedLLM()
    words = lambda t: {w["lemma"].lower() for w in c.post("/api/translate", json={"text": t}).json()["words"]}
    got = words("Whereupon we've seen the butler's wan specimen, though it wasn't superior.")
    assert {"wan", "specimen"} <= got, got                     # 三个字母的冷僻词也标
    assert not {"whereupon", "though", "we've", "wasn't", "butler's"} & got, got
    assert "butler" in got or "butler's" not in got              # 所有格去掉 's 再查


@pytest.mark.skipif(not HAS_DICT, reason="没有 data/dict.db")
def test_model_extras_are_checked(lib):
    c, _, _ = site(lib)
    text = "He took down the names of the people who came, and it was beyond me why."
    llm = ScriptedLLM({"zh": "他把来的人的名字都记了下来，我实在想不通为什么。", "gloss": [
        {"en": "took down", "zh": "记了下来"},                    # 短语动词，译文里有 → 留
        {"en": "beyond me", "zh": "想不通"},                      # 习语 → 留
        {"en": "of the", "zh": "的"},                            # 全是虚词 → 去
        {"en": "who came", "zh": "来的人的名字都记了下来啊"},       # 不在译文里又超过 6 字 → 去
        {"en": "flew away", "zh": "飞走"},                        # 原句里没有 → 去
        {"en": "the names of the people who", "zh": "名字"},        # 超过 4 个词 → 去
    ]})
    c.app.state.lib.llm = llm
    r = c.post("/api/translate", json={"text": text}).json()
    assert [g["en"] for g in r["gloss"]] == ["took down", "beyond me"], r["gloss"]
    assert "notes" not in r                                      # 译文这一步不再做难点讲解
    system = llm.calls[0][0]
    assert "短语动词" in system and "虚词" in system


def test_truncated_translation_is_not_cached(lib):
    c, _, _ = site(lib)
    text = "I don't think so, said the man in the grey coat, turning slowly towards the window and the street."
    c.app.state.lib.llm = ScriptedLLM({"zh": "我不这么认为，", "gloss": []},
                                      {"zh": "“我不这么认为。”穿灰大衣的人说着，慢慢转向窗户和街道。", "gloss": []})
    assert c.post("/api/translate", json={"text": text}).json()["zh"] == "我不这么认为，"
    assert c.post("/api/translate", json={"text": text}).json()["zh"].endswith("街道。"), "截断的译文不该进缓存"


def test_notes_are_fetched_separately_and_cached(lib):
    c, _, _ = site(lib)
    llm = ScriptedLLM({"notes": ["took down 是「记下」。", "beyond me 是「我想不通」。", "第三条不要"]})
    c.app.state.lib.llm = llm
    text = "He took down the names, and it was beyond me why."
    r = c.post("/api/notes", json={"text": text}).json()
    assert r["notes"] == ["took down 是「记下」。", "beyond me 是「我想不通」。"]
    assert c.post("/api/notes", json={"text": text}).json() == r and len(llm.calls) == 1, "第二次走缓存"
    assert c.post("/api/notes", json={"text": "  "}).status_code == 400


@pytest.mark.skipif(not HAS_DICT, reason="没有 data/dict.db")
def test_gloss_levels_pick_more_words(lib):
    """Aa 的生词档位：标准 = 词频前 2500 名和中考词不标；多 = 前 1000 名和中考词不标；更多 = 只看前 1000 名。"""
    c, _, _ = site(lib)
    c.app.state.lib.llm = ScriptedLLM()
    t = "He pressed the orange against the specimen in the kitchen of the house."
    words = lambda lv: {w["word"].lower() for w in c.post("/api/translate", json={"text": t, "level": lv}).json()["words"]}
    assert words("std") == {"specimen"}
    assert words("more") == {"pressed", "specimen"}                    # press 排名 1178、高考词
    assert words("most") == {"pressed", "orange", "specimen", "kitchen"}   # orange、kitchen 是中考词
    assert "house" not in words("most")                                 # 前 1000 名哪一档都不标


@pytest.mark.skipif(not HAS_DICT, reason="没有 data/dict.db")
def test_dictionary_fallback_and_level_filter_on_cached_gloss(lib):
    """模型没给中文的难词用词典释义兜底（旧缓存也能标上）；模型只因「更多」档词表才标的单个词，低档不要。"""
    c, _, _ = site(lib)
    c.app.state.lib.llm = ScriptedLLM({"zh": "男管家拿来了一件标本。", "gloss": [{"en": "specimen", "zh": "标本"}]},
                                      {"zh": "她吃了一个桔子。", "gloss": [{"en": "orange", "zh": "桔子"}]})
    r = c.post("/api/translate", json={"text": "The butler brought a specimen."}).json()
    assert [(g["en"], g["zh"]) for g in r["gloss"]] == [("specimen", "标本"), ("butler", "男管家")], r["gloss"]
    t = "She ate an orange."
    assert c.post("/api/translate", json={"text": t, "level": "most"}).json()["gloss"] == [{"en": "orange", "zh": "桔子"}]
    assert c.post("/api/translate", json={"text": t, "level": "std"}).json()["gloss"] == []   # 同一份缓存，按档取舍


@pytest.mark.skipif(not HAS_DICT, reason="没有 data/dict.db")
def test_dictionary_fallback_picks_the_sense_matching_the_word_form(lib):
    """leaning 兜底要用 lean 的动词义（倚靠），不是名词义（瘦肉）。"""
    c, _, _ = site(lib)
    c.app.state.lib.llm = ScriptedLLM({"zh": "他靠在栏杆上。", "gloss": []})
    r = c.post("/api/translate", json={"text": "He was leaning on the rail, speaking contemptuously."}).json()
    gl = {g["en"]: g["zh"] for g in r["gloss"]}
    assert "leaning" in gl and "瘦肉" not in gl["leaning"], gl
    assert "contemptuously" in gl and gl["contemptuously"], gl
