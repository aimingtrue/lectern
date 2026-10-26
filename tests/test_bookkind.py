"""读法（library/bookkind.py）与概念导图（library/concepts.py）：文言 / 白话逐句判定，白话句不译不注、文言句译成白话，
模型只管比例夹在中间的书，概念导图的校验，以及阅读页 /map 接口按读法给人物图或概念图。"""
from __future__ import annotations

import json
import time

from library import bookkind, concepts
from test_gloss_rules import ScriptedLLM
from test_library import lib, make_pdf  # noqa: F401  （lib 是 fixture）
from test_narrate_fixes import site

WENYAN = [
    "禹之父曰鲧，鲧之父曰帝颛顼，颛顼之父曰昌意，昌意之父曰黄帝。",
    "寡人恨不用公叔座之言也。",
    "张仪，天下贤士，吾殆弗如也。",
    "臣窃以天下之地图案之，诸侯之五倍于秦，料度诸侯之卒十倍于秦六国为一，并力西乡而攻秦，秦破矣。",
    "吾之有意于句读及符号之学也久矣。",
]
BAIHUA = [
    "答：理解静坐的真谛，其重要性远甚于单纯地掌握技巧本身。",
    "问：所以，就算是直觉到了真理实相，我想，还是无法以言语表达的，对吧？",
    "其他一切，自然会随之而来。",
    "这位太子就是秦孝公死后即位的秦惠文王。",
    "秦王政二十四年，王翦、蒙武攻打楚国，大破楚军，昌平君战死，于是项燕自杀。",
    "十二月，增派军队驻扎在汾城附近。",
    "I spent 6 years in Shanghai.",
    "冬至。",
]


def test_wenyan_sentences_are_told_from_vernacular_ones():
    assert all(bookkind.is_wenyan(s) for s in WENYAN), [s for s in WENYAN if not bookkind.is_wenyan(s)]
    assert not any(bookkind.is_wenyan(s) for s in BAIHUA), [s for s in BAIHUA if bookkind.is_wenyan(s)]
    assert bookkind.share(WENYAN + BAIHUA[:5]) == 0.5 and bookkind.share([]) == 0.0


def test_vernacular_chinese_is_skipped_without_asking_the_model_and_wenyan_is_translated(lib):
    c, _, _ = site(lib)
    llm = c.app.state.lib.llm = ScriptedLLM(
        {"zh": "张仪是天下的贤能之士，我大概比不上他。", "gloss": [
            {"en": "殆", "zh": "大概"},              # 译文里有 → 留
            {"en": "弗如", "zh": "比不上"},          # 留
            {"en": "张仪", "zh": "人名"},            # 合格，但短句最多 2 条 → 去
            {"en": "楚王", "zh": "楚国国君"},         # 原句里没有 → 去
            {"en": "天下贤士也", "zh": "这是一个非常非常长的释义"},   # 释义太长 → 去
        ], "notes": ["张仪：战国纵横家，与苏秦齐名。", "第二条", "第三条不要"]})
    r = c.post("/api/translate", json={"text": BAIHUA[0]}).json()
    assert r["skip"] is True and r["zh"] == "" and r["gloss"] == [] and r["words"] == [] and not llm.calls, "白话句不问模型"
    r = c.post("/api/translate", json={"text": WENYAN[2]}).json()
    assert r["lang"] == "wy" and r["zh"].startswith("张仪是天下")
    assert [g["en"] for g in r["gloss"]] == ["殆", "弗如"] and len(r["notes"]) == 2
    assert "文言" in llm.calls[0][0] and len(llm.calls) == 1
    assert c.post("/api/translate", json={"text": WENYAN[2]}).json()["zh"] == r["zh"] and len(llm.calls) == 1, "进了缓存"
    assert c.post("/api/notes", json={"text": WENYAN[2]}).json()["notes"] == r["notes"], "讲解和译文一起出"
    assert c.post("/api/notes", json={"text": BAIHUA[0]}).json()["notes"] == []


def test_wenyan_translation_cut_short_is_not_cached(lib):
    c, _, _ = site(lib)
    llm = c.app.state.lib.llm = ScriptedLLM({"zh": "张仪，", "gloss": []}, {"zh": "张仪是贤士，我大概不如他。", "gloss": []})
    assert c.post("/api/translate", json={"text": WENYAN[2]}).json()["zh"] == "张仪，"
    assert c.post("/api/translate", json={"text": WENYAN[2]}).json()["zh"] == "张仪是贤士，我大概不如他。" and len(llm.calls) == 2


class FakeDB:
    def __init__(self, paras: dict):
        self.paras, self.rows = paras, {}

    def paras_range(self, bid, a, b):
        return [{"n": n, "text": t, "sents": [[0, len(t)]]} for n, t in sorted(self.paras.items()) if a <= n < b]

    def one(self, sql, args=()):
        return {"json": self.rows[args[0]]} if "book_kind" in sql and args[0] in self.rows else None

    def run(self, sql, args=()):
        self.rows[args[0]] = args[1]


class St:
    def __init__(self, db, llm):
        from library.config import Config
        self.db, self.llm, self.cfg = db, llm, Config()


def test_kind_is_decided_once_and_the_model_only_settles_the_middle_ground():
    bookkind._cache.clear(); bookkind._guessed.clear()
    b = {"id": "b1", "title": "某书", "author": "某人", "toc": "[]", "n_paras": 200}
    # 全是白话：比例 < 4%，模型说文言也不算；导图听模型的
    db = FakeDB({n: BAIHUA[n % 4] for n in range(200)})
    llm = ScriptedLLM({"style": "wy", "map": "concepts", "why": "论述"})
    v = bookkind.ensure(St(db, llm), b)
    assert v["mode"] == "zh" and v["map"] == "concepts" and v["by"] == "model" and len(llm.calls) == 1
    assert bookkind.ensure(St(db, llm), b) is v and len(llm.calls) == 1, "定过的不再问"
    assert bookkind.mode(db, b) == "zh"
    # 文言句约占一成：中间地带，听模型的
    bookkind.forget("b2")
    db2 = FakeDB({n: (WENYAN[n % 5] if n % 10 == 0 else BAIHUA[n % 4]) for n in range(200)})
    b2 = {**b, "id": "b2"}
    assert bookkind.mode(db2, b2) == "zh", "没问模型前先当白话"
    v2 = bookkind.ensure(St(db2, ScriptedLLM({"style": "wy", "map": "people"})), b2)
    assert v2["mode"] == "wy" and v2["map"] == "people" and 0.04 <= v2["share"] < 0.25
    # 模型不在：返回猜测、不存，下次再问
    class Down:
        def chat_json(self, *a, **k):
            raise RuntimeError("Ollama 没开")
    bookkind.forget("b3")
    b3 = {**b, "id": "b3"}
    assert bookkind.ensure(St(db, Down()), b3)["by"] == "guess" and "b3" not in db.rows
    # 外文书不问模型
    db4 = FakeDB({n: "It was a bright cold day in April, and the clocks were striking thirteen." for n in range(200)})
    assert bookkind.ensure(St(db4, Down()), {**b, "id": "b4"})["mode"] == "en"


TEXT = "静坐不是为了得到什么。注意力回到呼吸上，念头自然会落下。观呼吸是最基本的方法，也是止观的入门。放下念头，就是静坐。"


def test_concept_map_keeps_only_what_the_text_says():
    out = concepts.clean({"thesis": "静坐的要领是放下，而不是追求体验。" * 3, "concepts": [
        {"name": "静坐", "aliases": ["打坐"], "gist": "让心安静下来的练习", "kind": "method"},
        {"name": "观呼吸", "aliases": ["呼吸"], "gist": "把注意力放在呼吸上", "kind": "method"},
        {"name": "放下念头", "aliases": ["放下"], "gist": "不跟着念头走", "kind": "claim"},
        {"name": "心灵觉醒", "aliases": ["灵性"], "gist": "原文里没有这个说法：画不出来", "kind": "concept"},
        {"name": "止观", "aliases": [], "gist": "止和观两种修法", "kind": "怪类型"},
        {"name": "静坐", "aliases": [], "gist": "重复的不要"},
    ], "links": [
        {"a": "观呼吸", "b": "止观", "rel": "入门"},
        {"a": "呼吸", "b": "静坐", "rel": "方法"},               # 用别名指也行
        {"a": "静坐", "b": "心灵觉醒", "rel": "导致"},          # 没这个概念 → 去
        {"a": "放下", "b": "放下念头", "rel": "自己"},          # 自己连自己 → 去
    ]}, TEXT)
    by = {p["name"]: p for p in out["people"]}
    assert list(by) == ["静坐", "观呼吸", "放下念头", "止观"]
    assert by["静坐"]["aliases"] == ["静坐"] and by["观呼吸"]["aliases"] == ["观呼吸", "呼吸"], "原文里没有的说法（打坐）不要"
    assert by["止观"]["kind"] == "concept" and by["放下念头"]["kind"] == "claim"
    assert by["静坐"]["n"] == 2 and by["观呼吸"]["n"] == 2, "按原文里出现的次数（观呼吸先配，剩下的呼吸算一次）"
    assert out["links"] == [{"a": "观呼吸", "b": "止观", "rel": "入门"}, {"a": "观呼吸", "b": "静坐", "rel": "方法"}]
    assert len(out["thesis"]) <= 60


def test_sections_use_toc_entries_down_to_level_two():
    b = {"title": "书", "n_paras": 100, "toc": json.dumps([[1, "书", 0, 0], [1, "卷一", 1, 10], [2, "第一章", 1, 10], [3, "一、小节", 2, 12],
                                                          [2, "第二章", 3, 40], [1, "卷二", 5, 70], [2, "第三章", 6, 75]])}
    secs = concepts.sections(b)
    assert [(t, s, e) for _, t, s, e in secs] == [("第一章", 10, 40), ("第二章", 40, 70), ("卷二", 70, 75), ("第三章", 75, 100)], \
        "卷一和第一章同一段起：卷名没有正文；三级目录不单算"
    assert concepts.section_at(b, 50)[1] == "第二章" and concepts.section_at(b, 5) is None


def test_map_api_follows_the_book_kind(lib):
    c, db, bid = site(lib)
    b = db.book(bid)
    r = c.get(f"/api/books/{bid}/map", params={"para": 0}).json()
    assert r["kind"] == "people" and r["mode"] == "en" and "span" in r
    secs = concepts.sections(b)
    assert secs, b["toc"]
    i, _, s, e = secs[0]
    db.run("INSERT OR REPLACE INTO book_kind VALUES(?,?,?)", (bid, json.dumps({"mode": "zh", "map": "concepts"}), time.time()))
    bookkind.forget(bid)
    r = c.get(f"/api/books/{bid}/map", params={"para": s}).json()
    assert r["kind"] == "concepts" and r["mode"] == "zh" and r["pending"] is True and r["span"] == [s, e]
    db.run("INSERT OR REPLACE INTO maps VALUES(?,?,?,?)", (bid, i, json.dumps({"people": [{"name": "Alpha", "aliases": ["Alpha"], "role": "x", "n": 1}],
                                                                                "links": [], "thesis": "主旨"}), time.time()))
    r = c.get(f"/api/books/{bid}/map", params={"para": s}).json()
    assert r["pending"] is False and r["thesis"] == "主旨" and r["people"][0]["name"] == "Alpha"
    page = c.get(f"/read/{bid}").text
    assert '"mode": "zh"' in page or '"mode":"zh"' in page
    bookkind.forget(bid)


def test_parts_of_a_long_section_merge_locally():
    p1 = concepts.clean({"thesis": "甲", "concepts": [{"name": "静坐", "aliases": [], "gist": "练习"}, {"name": "观呼吸", "aliases": ["呼吸"], "gist": "方法"}],
                         "links": [{"a": "观呼吸", "b": "静坐", "rel": "方法"}]}, TEXT)
    p2 = concepts.clean({"thesis": "乙", "concepts": [{"name": "呼吸", "aliases": [], "gist": "另一块的叫法"}, {"name": "止观", "aliases": [], "gist": "修法"}],
                         "links": [{"a": "呼吸", "b": "止观", "rel": "入门"}]}, TEXT)
    m = concepts.merge([p1, p2])
    assert [p["name"] for p in m["people"]] == ["静坐", "观呼吸", "止观"], "呼吸是观呼吸的说法：并成一个"
    assert m["links"] == [{"a": "观呼吸", "b": "静坐", "rel": "方法"}, {"a": "观呼吸", "b": "止观", "rel": "入门"}]
    text = "\n\n".join("段" * 900 for _ in range(30))
    cs = concepts.chunks_of(text)
    assert len(cs) == concepts.MAX_CHUNKS and all(len(c) <= concepts.CHUNK for c in cs)
    assert concepts.chunks_of("短。\n\n短短。") == ["短。\n\n短短。"]


def test_inside_a_wenyan_book_plain_narrative_counts_and_notes_do_not():
    narrative = ["项梁起东阿，西，（北）[比]至定陶①，再破秦军，项羽等又斩李由，益轻秦，有骄色。",
                 "故立沛公为汉王，王巴、蜀、汉中，都南郑。", "于是项梁然其言⑦，乃求楚怀王孙心民间⑧，为人牧羊。"]
    notes = ["④穷：困窘，走投无路。", "(17)伏：同“服”，敬服，佩服。", "大名：指称王之名。", "255.猾──侵扰。",
             "项王答应了。", "这是表示尊贵。"]
    assert not any(bookkind.is_wenyan(s) for s in narrative), "一般书里不算（没有虚词）"
    assert all(bookkind.is_wenyan(s, loose=True) for s in narrative), [s for s in narrative if not bookkind.is_wenyan(s, loose=True)]
    assert not any(bookkind.is_wenyan(s, loose=True) for s in notes), [s for s in notes if bookkind.is_wenyan(s, loose=True)]
    assert not bookkind.is_wenyan("项王许诺。", loose=True), "太短"
    assert bookkind.is_wenyan("项王曰：“沛公安在？”", loose=True)


def test_whole_book_prep_status_counts_every_sentence(lib):
    c, db, bid = site(lib)
    b = db.book(bid)
    r = c.get(f"/api/books/{bid}/prep-all").json()
    n = sum(len(p["sents"]) for p in db.paras_range(bid, 0, b["n_paras"]))
    assert 0 < r["total"] <= n and r["audio"] == 0 and r["running"] is False and r["done"] is False
    assert c.post(f"/api/books/{bid}/prep-all", json={"para": 0}).status_code == 503, "朗读服务不在：不登记任务"
