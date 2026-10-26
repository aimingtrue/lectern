"""人物关系图（library/cast.py）：模型输出的校验、几章累计合并、句子里认人，以及阅读页的接口。"""
from __future__ import annotations

import json

from library import cast
from test_gloss_rules import ScriptedLLM
from test_library import lib, make_pdf  # noqa: F401  （lib 是 fixture）
from test_narrate_fixes import site

TEXT = ("Daisy was my second cousin once removed, and I’d known Tom in college. Tom’s got some woman in New York, "
        "said Miss Baker. The wife, Mrs. Wilson, was in the middle thirties. Wilson was a blond, spiritless man. "
        "Jordan Baker nodded at Daisy.")
RAW = {"people": [
    {"name": "Nick Carraway", "aliases": ["Nick", "I", "my"], "role": "叙述者，黛西的远房表兄"},
    {"name": "Daisy Buchanan", "aliases": ["Daisy"], "role": "尼克的表妹，汤姆的妻子，住在东卵的大房子里，说话声音很动人"},
    {"name": "Tom Buchanan", "aliases": ["Tom", "Buchanan", "Tom's"], "role": "黛西的丈夫"},
    {"name": "Gatsby's", "aliases": ["A stout, middle-aged man"], "role": "所有格和描述都不是叫法：去掉所有格后是 Gatsby"},
    {"name": "Jordan Baker", "aliases": ["Jordan", "Miss Baker", "Baker"], "role": "黛西的闺蜜"},
    {"name": "Myrtle Wilson", "aliases": ["Mrs. Wilson", "Myrtle"], "role": "汤姆的情妇"},
    {"name": "George Wilson", "aliases": ["Wilson", "George"], "role": "默特尔的丈夫"},
    {"name": "", "aliases": ["x"], "role": "没有名字的不要"},
    "不是对象的也不要",
], "links": [
    {"a": "Daisy", "b": "Tom", "rel": "夫妻"},                       # 用别名指人也行
    {"a": "Tom Buchanan", "b": "Daisy Buchanan", "rel": "妻子（重复的一对，不要）"},
    {"a": "Nick Carraway", "b": "Nick", "rel": "自己和自己，不要"},
    {"a": "Tom Buchanan", "b": "Myrtle Wilson", "rel": "情人"},
    {"a": "Tom Buchanan", "b": "Somebody Else", "rel": "没这个人，不要"},
]}


def test_clean_validates_people_links_and_counts_mentions():
    out = cast.clean(RAW, TEXT)
    names = [p["name"] for p in out["people"]]
    assert names == ["Nick Carraway", "Daisy Buchanan", "Tom Buchanan", "Gatsby", "Jordan Baker", "Myrtle Wilson", "George Wilson"]
    assert out["people"][2]["aliases"] == ["Tom Buchanan", "Tom", "Buchanan"] and out["people"][3]["aliases"] == ["Gatsby"]
    nick = out["people"][0]
    assert nick["aliases"] == ["Nick Carraway", "Nick"], "I / my 不能当叫法"
    assert len(out["people"][1]["role"]) <= 30
    by = {p["name"]: p["n"] for p in out["people"]}
    assert by["Daisy Buchanan"] == 2 and by["Tom Buchanan"] == 2 and by["Jordan Baker"] == 2   # Tom’s 的所有格也算；Miss Baker、Jordan Baker 各一次
    assert by["Myrtle Wilson"] == 1 and by["George Wilson"] == 1, "Mrs. Wilson 先配，剩下的 Wilson 才是乔治"
    assert [(l["a"], l["b"], l["rel"]) for l in out["links"]] == [("Daisy Buchanan", "Tom Buchanan", "夫妻"), ("Tom Buchanan", "Myrtle Wilson", "情人")]


def test_merge_accumulates_chapters_without_mixing_shared_aliases():
    c1 = cast.clean({"people": [{"name": "Gatsby", "aliases": ["Mr. Gatsby"], "role": "邻居"},
                                {"name": "Nick", "aliases": [], "role": "叙述者"},
                                {"name": "George Wilson", "aliases": ["Wilson"], "role": "修车行老板"}],
                     "links": [{"a": "Nick", "b": "Gatsby", "rel": "邻居"}]}, "Gatsby and Nick. Gatsby. Wilson.")
    c2 = cast.clean({"people": [{"name": "Jay Gatsby", "aliases": ["Gatsby", "Jay"], "role": "西卵豪宅主人"},
                                {"name": "Myrtle Wilson", "aliases": ["Wilson", "Mrs. Wilson"], "role": "情妇"}],
                     "links": [{"a": "Jay Gatsby", "b": "Nick", "rel": "朋友"}, {"a": "Myrtle Wilson", "b": "George Wilson", "rel": "夫妻"}]},
                    "Jay Gatsby. Mrs. Wilson.")
    m = cast.merge([c1, c2])
    by = {p["name"]: p for p in m["people"]}
    assert set(by) == {"Jay Gatsby", "Nick", "George Wilson", "Myrtle Wilson"}, "Jay Gatsby 并进 Gatsby 并成为称呼；Myrtle 和 George 只是同姓，是两个人"
    g = by["Jay Gatsby"]
    assert set(g["aliases"]) == {"Gatsby", "Mr. Gatsby", "Jay Gatsby", "Jay"} and g["role"] == "西卵豪宅主人" and g["n"] == 3
    assert by["George Wilson"]["aliases"] == ["George Wilson"] and by["Myrtle Wilson"]["aliases"] == ["Myrtle Wilson", "Mrs. Wilson"], \
        "两个人都叫 Wilson：这个叫法不再用来认人"
    rels = {frozenset((l["a"], l["b"])): l["rel"] for l in m["links"]}
    assert rels == {frozenset(("Nick", "Jay Gatsby")): "邻居"}, "c2 里没有 Nick、George Wilson 这两个人：那两条关系在校验时就不进，不覆盖第一章的"
    assert cast.merge([]) == {"people": [], "links": [], "narrator": None}


def test_narrator_comes_from_the_model_or_a_narrator_role_and_is_kept_without_mentions():
    """叙述者：模型的 narrator 字段对得上人物表就用；早先抽的章看谁的身份是「叙述者」（「叙述者的表妹」不算）；
    几章累计取最近一章说了的；叙述者只用「我」、正文里从没按名字出现也画。"""
    c1 = cast.clean({"people": [{"name": "Nick", "aliases": [], "role": "第一人称叙述者，债券从业者"},
                                {"name": "Daisy", "aliases": [], "role": "叙述者的表妹"}], "links": []}, "Daisy. Daisy.")
    assert c1["narrator"] == "Nick" and [p["name"] for p in c1["people"]] == ["Nick", "Daisy"]
    c2 = cast.clean({"people": [{"name": "Nick Carraway", "aliases": ["Nick"], "role": "故事讲述者"}, {"name": "Gatsby", "aliases": [], "role": "邻居"}],
                     "links": [], "narrator": "nick"}, "Gatsby.")
    assert c2["narrator"] == "Nick Carraway", "narrator 按别名认、不分大小写"
    c3 = cast.clean({"people": [{"name": "Gatsby", "aliases": [], "role": "富豪"}], "links": [], "narrator": "Somebody"}, "Gatsby.")
    assert c3["narrator"] is None, "对不上人物表的不要"
    m = cast.merge([c1, c2, c3])
    assert m["narrator"] == "Nick Carraway" and "Nick Carraway" in [p["name"] for p in m["people"]], "没按名字出现过（n = 0）、没关系的叙述者也画"
    assert cast.merge([{"people": [{"name": "Daisy", "aliases": ["Daisy"], "role": "叙述者的表妹", "n": 2}], "links": []}])["narrator"] is None


def test_cast_api_accumulates_up_to_current_chapter_and_marks_pending(lib):
    c, db, bid = site(lib)
    b = db.book(bid)
    chaps = cast.chapters(b)
    assert len(chaps) >= 2, chaps
    (i0, _, s0, _), (i1, _, s1, _) = chaps[0], chaps[1]
    c.app.state.lib.llm = ScriptedLLM({"people": [{"name": "Alpha Reader", "aliases": ["Alpha"], "role": "主角"},
                                                  {"name": "Beta Friend", "aliases": [], "role": "朋友"},
                                                  {"name": "Nobody Here", "aliases": [], "role": "正文里没出现、也没关系：不画"}],
                                       "links": [{"a": "Alpha", "b": "Beta Friend", "rel": "朋友"}]})
    assert c.post(f"/api/books/{bid}/cast/{i0}").json() == {"ok": True}
    import time
    for _ in range(50):
        if db.one("SELECT 1 FROM cast WHERE book=? AND chapter=?", (bid, i0)):
            break
        time.sleep(0.1)
    r = c.get(f"/api/books/{bid}/cast", params={"para": s0}).json()
    assert [p["name"] for p in r["people"]] == ["Alpha Reader", "Beta Friend"] and r["chapter"] == i0 and r["pending"] is False
    assert r["links"] == [{"a": "Alpha Reader", "b": "Beta Friend", "rel": "朋友"}]
    r = c.get(f"/api/books/{bid}/cast", params={"para": s1}).json()
    assert [p["name"] for p in r["people"]] == ["Alpha Reader", "Beta Friend"] and r["chapter"] == i1 and r["pending"] is True
    assert c.post(f"/api/books/{bid}/cast/999").status_code == 404
    assert json.loads(db.one("SELECT json FROM cast WHERE book=?", (bid,))["json"])["people"][0]["aliases"] == ["Alpha Reader", "Alpha"]


def test_merge_joins_a_short_name_to_the_one_full_name_it_belongs_to():
    c1 = cast.clean({"people": [{"name": "Nick Carraway", "aliases": ["Carraway"], "role": "叙述者"},
                                {"name": "George Wilson", "aliases": [], "role": "修车行老板"},
                                {"name": "Myrtle Wilson", "aliases": [], "role": "情妇"}], "links": []},
                    "Nick Carraway. George Wilson. Myrtle Wilson.")
    c2 = cast.clean({"people": [{"name": "Nick", "aliases": [], "role": "第一人称叙述者，故事讲述者，Tom和Daisy的表亲，给予Nick人生建议"},
                                {"name": "Wilson", "aliases": [], "role": "灰烬谷的人"}], "links": []}, "Nick. Wilson.")
    m = cast.merge([c1, c2])
    assert [p["name"] for p in m["people"]] == ["Nick Carraway", "George Wilson", "Myrtle Wilson", "Wilson"]
    nick = m["people"][0]
    assert nick["aliases"] == ["Nick Carraway", "Carraway", "Nick"] and nick["n"] == 2
    assert nick["role"] == "第一人称叙述者，故事讲述者，Tom和Daisy的表亲", "超过 30 字在最后一个逗号处截断"


def test_multiword_aliases_must_share_a_word_with_the_name():
    out = cast.clean({"people": [{"name": "Nick", "aliases": ["Mr. Carraway", "Finnish woman", "Carraway", "Demaine"], "role": "叙述者"},
                                 {"name": "Jordan Baker", "aliases": ["Miss Baker", "Mrs. Sigourney Howard"], "role": "朋友"}], "links": []},
                     "Nick Carraway. Jordan Baker.")
    assert out["people"][0]["aliases"] == ["Nick", "Mr. Carraway", "Carraway", "Demaine"], "Mr. Carraway 靠单词叫法 Carraway 对上"
    assert out["people"][1]["aliases"] == ["Jordan Baker", "Miss Baker"]
    old = {"people": [{"name": "Jordan Baker", "aliases": ["Jordan Baker", "Mrs. Sigourney Howard"], "role": "朋友", "n": 2}], "links": []}
    assert cast.merge([old])["people"][0]["aliases"] == ["Jordan Baker"], "早先存的章在合并时也按现在的规则过一遍"
    g = cast.clean({"people": [{"name": "Gatsby", "aliases": [], "role": "邻居"}], "links": []}, "Gatsby.")
    bt = cast.clean({"people": [{"name": "Gatsby’s butler", "aliases": [], "role": "管家"}], "links": [{"a": "Gatsby’s butler", "b": "Gatsby’s butler", "rel": "x"}]}, "Gatsby’s butler.")
    assert [p["name"] for p in cast.merge([g, bt])["people"]] == ["Gatsby", "Gatsby’s butler"], "所有格称呼说的是别人：不并进 Gatsby"
    w1 = cast.clean({"people": [{"name": "Mrs. Wilson", "aliases": ["Myrtle"], "role": "情妇"}], "links": []}, "Mrs. Wilson.")
    w2 = cast.clean({"people": [{"name": "George B. Wilson", "aliases": ["Wilson", "Mr. Wilson"], "role": "老板"},
                                {"name": "Myrtle Wilson", "aliases": [], "role": "妻子"}], "links": []}, "George B. Wilson. Myrtle Wilson.")
    m = cast.merge([w1, w2])
    assert [p["name"] for p in m["people"]] == ["Myrtle Wilson", "George B. Wilson"], "只有姓的 Mrs. Wilson 不能凭「是全名的一部分」并进 George；Myrtle Wilson 靠 Myrtle 对上"
