"""视频图层（library/overlay.py）：英英注释落到句子里的位置、人物网换成英文说明、每句的说话人和提到的人，结果进缓存。"""
from __future__ import annotations

import json
import time

from library import cast
from test_library import lib, make_pdf  # noqa: F401  （lib 是 fixture）
from test_narrate_fixes import site

CAST = {"people": [{"name": "Alpha Reader", "aliases": ["Alpha Reader", "Alpha"], "role": "主角，第一人称叙述者", "n": 3},
                   {"name": "Beta Friend", "aliases": ["Beta Friend", "Beta"], "role": "主角的朋友", "n": 1}],
        "links": [{"a": "Alpha Reader", "b": "Beta Friend", "rel": "朋友"}], "narrator": "Alpha Reader"}


class ByPromptLLM:
    """按系统提示词分辨是哪种请求：人物表翻译、还是一句的注释。"""
    def __init__(self):
        self.calls = []

    def chat_json(self, model, system, user, **k):
        self.calls.append(user)
        if "cast list" in system:
            return {"roles": {"Alpha Reader": "the narrator", "Beta Friend": "Alpha's old friend"},
                    "rels": [["Alpha Reader", "Beta Friend", "Friends"]]}
        if "quarrel" in user:
            return {"notes": [{"term": "Quarrel", "def": "an angry argument."}, {"term": "not in the sentence", "def": "dropped."}]}
        return {"notes": []}


def test_overlay_data_glosses_cast_and_speakers(lib):
    c, db, bid = site(lib)
    b = db.book(bid)
    i0, _, s0, _ = cast.chapters(b)[0]
    db.run("INSERT INTO cast VALUES(?,?,?,?)", (bid, i0, json.dumps(CAST), time.time()))
    para = s0 + 1
    quote = "“We should not quarrel,” Beta said."
    db.run("INSERT OR REPLACE INTO paras(book, n, page, kind, text, sents) VALUES(?,?,?,?,?,?)",
           (bid, para, 1, "p", "Alpha walked home. " + quote, json.dumps([[0, 18], [19, 19 + len(quote)]])))
    db.run("INSERT OR REPLACE INTO speech VALUES(?,?,?)", (bid, para, json.dumps([{"a": 19, "b": 45, "who": "Beta"}])))
    llm = c.app.state.lib.llm = ByPromptLLM()
    body = {"book": bid, "sents": [{"para": para, "text": "Alpha walked home."}, {"para": para, "text": quote}]}
    r = c.post("/api/overlay/data", json=body).json()
    assert r["cast"]["people"][0]["role"] == "the narrator" and r["cast"]["links"][0]["rel"] == "Friends", "说明换成英文"
    s1, s2 = r["sents"]
    assert s1["who"] == "Alpha Reader" and [h["name"] for h in s1["hits"]] == ["Alpha Reader"] and s1["gloss"] == []
    assert s2["who"] == "Beta Friend", "句子大半在对白里：说话人按人物表归到 Beta Friend"
    assert s1["quotes"] == [] and s2["quotes"] == [[0, 26, "Beta Friend"]], "对白区间换成句内位置，称呼归到人物表里的人"
    assert s2["gloss"] == [{"term": "quarrel", "a": quote.index("quarrel"), "b": quote.index("quarrel") + 7, "def": "An angry argument."}], \
        "按句子里的原样定位（不分大小写），句子里没有的词丢掉，释义首字母大写"
    n = len(llm.calls)
    assert c.post("/api/overlay/data", json=body).json() == r and len(llm.calls) == n, "第二次全走缓存"
    assert c.post("/api/overlay/data", json={"book": bid, "sents": []}).status_code == 400


def test_overlay_page_loads_its_script(lib):
    c, _, _ = site(lib)
    html = c.get("/overlay").text
    assert "overlay/main.js" in html and "overlay/overlay.css" in html


def test_overlay_prosody_only_reads_tmp_lecture(lib, tmp_path):
    c, _, _ = site(lib)
    outside = tmp_path / "x.wav"
    outside.write_bytes(b"RIFF")
    for path in (str(outside), "../../etc/passwd", ""):
        r = c.post("/api/overlay/prosody", json={"path": path, "text": "Hi.", "words": [["Hi", 0, 0.3]]})
        assert r.status_code == 400
