"""多角色配音（library/speech.py）：引号识别、归属结果校验、音色分配、按句切段、/text 的 speech 字段、多段音频拼接。"""
from __future__ import annotations

import io
import json
import socket
import threading
import time
import wave

import numpy as np
from fastapi import Request  # noqa: F401

from library import speech
from test_gloss_rules import ScriptedLLM
from test_library import lib, make_pdf  # noqa: F401  （lib 是 fixture）
from test_narrate_fixes import site

POOL = [{"id": "narr", "lang": "en", "gender": "m"}, {"id": "v-m1", "lang": "en", "gender": "m"}, {"id": "v-m2", "lang": "en", "gender": "m"},
        {"id": "v-f1", "lang": "en", "gender": "f"}, {"id": "v-f2", "lang": "en", "gender": "f"}, {"id": "v-zh-f1", "lang": "zh", "gender": "f"}]


def test_quotes_english_chinese_and_edge_cases():
    t = '“You will,” I answered shortly. “You will if you stay in the East.” He didn’t say ‘no’ to it. "Oh, I’ll stay," he said.'
    assert speech.quotes(t) == [(0, 11), (32, 67), (82, 86), (94, 110)]
    assert speech.quotes("他说：「走吧。」她答“好”。") == [(3, 8), (10, 13)]
    assert speech.quotes('"unterminated quote here') == [(0, 24)], "没闭合的到段末"
    assert speech.quotes("It’s the boys’ room. ‘Tis true.") == [], "撇号不是引号；’Tis 这种不成对的不算"
    assert speech.quotes("“…” and “!!”") == [], "引号里没有可读文字的不算"
    assert speech.quotes("He said “the ‘inner’ one” then.") == [(8, 25)], "嵌套只取最外层"


def test_numbering_and_clean():
    paras = [{"n": 3, "text": '“Hi,” said Tom. “Bye.”'}, {"n": 4, "text": "No quotes here."}, {"n": 5, "text": "She said “ok”."}]
    text, index = speech.numbered_text(paras)
    assert text == '【1】“Hi,” said Tom. 【2】“Bye.”\n\nNo quotes here.\n\nShe said 【3】“ok”.'
    assert index == [(1, 3, 0), (2, 3, 1), (3, 5, 0)]
    raw = {"quotes": [{"i": 1, "who": "Tom", "g": "male"}, {"i": "2", "who": " Daisy ", "g": "F"}, {"i": 3, "who": "?", "g": "m"},
                      {"i": 9, "who": "Nobody", "g": "m"}, {"i": "x", "who": "Bad"}, "junk", {"i": 1, "who": "叙述者"}]}
    got = speech.clean(raw, index, {"tom": "Tom Buchanan"})
    assert got == {1: ("Tom Buchanan", "m"), 2: ("Daisy", "f")}, got     # 别名归到称呼；? / 叙述者 / 没有的编号都不要
    assert speech.chunks([{"n": i, "text": "x" * 6000} for i in range(3)]) == [[{"n": 0, "text": "x" * 6000}], [{"n": 1, "text": "x" * 6000}], [{"n": 2, "text": "x" * 6000}]]


def test_segments_split_a_sentence_by_speaker():
    t = '“You will,” I answered shortly. “You will if you stay in the East.” (He smiled.)'
    spans = [{"a": 0, "b": 11, "who": "Nick"}, {"a": 32, "b": 67, "who": "Nick"}]
    vo = lambda who, x: {"Nick": "v-m3"}.get(who)   # noqa: E731
    narr = lambda x: "narr"                      # noqa: E731
    assert speech.segments(t, 0, 31, spans, vo, narr) == [("“You will,”", "v-m3"), ("I answered shortly.", "narr")]
    assert speech.segments(t, 32, 67, spans, vo, narr) == [("“You will if you stay in the East.”", "v-m3")]
    assert speech.segments(t, 68, 80, spans, vo, narr) == [("(He smiled.)", "narr")], "没有对白的句子一段"
    assert speech.segments(t, 0, 31, spans, lambda w, x: None, narr) == [("“You will,” I answered shortly.", "narr")], "说话人没音色：整句旁白"
    t2 = '“Go,” she said, “now.”'
    sp2 = [{"a": 0, "b": 5, "who": "D"}, {"a": 16, "b": 22, "who": "D"}]
    assert speech.segments(t2, 0, len(t2), sp2, lambda w, x: "v-f1", narr) == [("“Go,”", "v-f1"), ("she said,", "narr"), ("“now.”", "v-f1")]
    assert speech.segments("“Hm.” ok", 0, 5, [{"a": 0, "b": 5, "who": "D"}], lambda w, x: "v-f1", narr) == [("“Hm.”", "v-f1")]


def test_quoted_sents_includes_lines_inside_a_multi_sentence_quote():
    """等归属的句子 = 落在所在段落引号区间里的，包括自己没有引号字符的中间句；引号外的句子不等。"""
    s1, s2, s3, s4 = "“Let’s go back, Tom.", "Tomorrow is another day.", "Fine!” she said quickly.", "The night was quiet after that."
    t = " ".join((s1, s2, s3, s4))
    texts = {7: t, 8: "No quotes here at all. Just narration."}

    class FakeDB:
        def one(self, sql, args):
            return {"text": texts[args[1]]} if args[1] in texts else None
    sents = [(7, s1), (7, s2), (7, s3), (7, s4), (8, "No quotes here at all."), (8, "Just narration."), (9, "Missing paragraph.")]
    assert speech.quoted_sents(FakeDB(), "b", sents) == {(7, s1), (7, s2), (7, s3)}


def test_voice_assignment_is_stable_and_avoids_the_narrator(lib):
    c, db, bid = site(lib)
    db.run("INSERT INTO speakers(book, name, gender, voice, created) VALUES(?,?,?,NULL,0)", (bid, "Daisy", "f"))
    db.run("INSERT INTO speakers(book, name, gender, voice, created) VALUES(?,?,?,NULL,0)", (bid, "Jordan", "f"))
    db.run("INSERT INTO speakers(book, name, gender, voice, created) VALUES(?,?,?,NULL,0)", (bid, "Myrtle", "f"))
    db.run("INSERT INTO speakers(book, name, gender, voice, created) VALUES(?,?,?,NULL,0)", (bid, "Tom", "m"))
    db.run("INSERT INTO speakers(book, name, gender, voice, created) VALUES(?,?,?,NULL,0)", (bid, "Someone", "?"))
    d = speech.voice_for(db, bid, "Daisy", "en", POOL, "narr")
    assert d in ("v-f1", "v-f2") and speech.voice_for(db, bid, "Daisy", "en", POOL, "narr") == d, "第二次还是同一个"
    j = speech.voice_for(db, bid, "Jordan", "en", POOL, "narr")
    assert j in ("v-f1", "v-f2") and j != d, "另一个女声"
    assert speech.voice_for(db, bid, "Myrtle", "en", POOL, "narr") in ("v-f1", "v-f2"), "池子用完了：复用"
    t = speech.voice_for(db, bid, "Tom", "en", POOL, "narr")
    assert t in ("v-m1", "v-m2"), "不用旁白 narr"
    t2 = speech.voice_for(db, bid, "Tom", "en", POOL, t)
    assert t2 in ("narr", "v-m1", "v-m2") and t2 != t and speech.voice_for(db, bid, "Tom", "en", POOL, t) == t2, "旁白换成了他的声音：重新分一个"
    assert speech.voice_for(db, bid, "Someone", "en", POOL, "narr") is None, "性别不明：旁白"
    assert speech.voice_for(db, bid, "Nobody", "en", POOL, "narr") is None
    assert speech.voice_for(db, bid, "Daisy", "zh", POOL, "narr-zh") == "v-zh-f1", "中文对白走中文池"


def test_ranked_follows_cast_order_and_pick_prefers_unused():
    pool = [{"id": "a", "lang": "en", "gender": "m"}, {"id": "b", "lang": "en", "gender": "m", "pick": 5}, {"id": "c", "lang": "en", "gender": "m", "pick": 1},
            {"id": "d", "lang": "en", "gender": "f", "pick": 0}, {"id": "e", "lang": "zh", "gender": "m", "pick": 0}]
    assert speech.ranked(pool, "en", "m", set()) == ["c", "b", "a"], "cast_order 里的按 pick 从前往后，没排的排最后"
    assert speech.ranked(pool, "en", "m", {"c"}) == ["b", "a"]
    assert speech.ranked(pool + [{"id": "x", "lang": "en", "gender": "m", "pick": 0, "cast": False}], "en", "m", set()) == ["c", "b", "a"], "cast: false 不配给角色"
    assert speech.pick(["c", "b", "a"], {}) == "c" and speech.pick(["c", "b", "a"], {"c": 1}) == "b"
    assert speech.pick(["c", "b", "a"], {"c": 2, "b": 1, "a": 1}) == "b", "都用过了：用得最少的，并列取靠前的"
    assert speech.pick([], {}) is None
    assert speech.resolve("Nick’s", [{"name": "Nick Carraway", "aliases": ["Nick"]}])["name"] == "Nick Carraway"
    assert speech.resolve("Gatsby", [{"name": "Nick Carraway", "aliases": ["Nick"]}]) is None


def test_assign_all_unifies_aliases_ranks_by_importance_and_leaves_the_narrator_to_narration(lib):
    """同一个人的几种叫法一种声音；叙述者自己的对白不配音色；戏份重的先挑偏好表靠前的；/text 用全书称呼、标出叙述者。"""
    c, db, bid = site(lib)
    db.run("INSERT INTO cast VALUES(?,?,?,0)", (bid, 0, json.dumps({"people": [
        {"name": "Nick Carraway", "aliases": ["Nick Carraway", "Nick"], "role": "第一人称叙述者", "n": 3, "g": "m"},
        {"name": "Daisy Fay", "aliases": ["Daisy Fay", "Daisy", "Daisy Buchanan"], "role": "表妹", "n": 9, "g": "f"},
        {"name": "Tom Buchanan", "aliases": ["Tom Buchanan", "Tom"], "role": "丈夫", "n": 20, "g": "m"},
        {"name": "Jordan Baker", "aliases": ["Jordan Baker", "Jordan"], "role": "朋友", "n": 4}], "links": []})))
    para = db.one("SELECT text FROM paras WHERE book=? AND n=2", (bid,))["text"]
    db.run("INSERT INTO speech VALUES(?,?,?)", (bid, 2, json.dumps([{"a": 0, "b": 5, "who": "Nick"}, {"a": 6, "b": 12, "who": "Daisy"}, {"a": 13, "b": 20, "who": "policeman"}])))
    db.run("INSERT INTO speech VALUES(?,?,?)", (bid, 3, json.dumps([{"a": 0, "b": 5, "who": "Nick Carraway"}, {"a": 6, "b": 12, "who": "Daisy Buchanan"}, {"a": 13, "b": 20, "who": "Jordan"}])))
    for name, g, v, t in [("Nick", "m", "v-m2", 1), ("Nick Carraway", "m", "gaiman", 2), ("Daisy", "f", "v-f2", 1), ("Daisy Buchanan", "f", None, 2),
                          ("Tom Buchanan", "m", None, 1), ("policeman", "m", None, 1), ("Jordan", "f", None, 1)]:
        db.run("INSERT INTO speakers(book, name, gender, voice, created) VALUES(?,?,?,?,?)", (bid, name, g, v, t))
    pool = [{"id": "narr", "lang": "en", "gender": "m", "pick": 9}, {"id": "v-m1", "lang": "en", "gender": "m", "pick": 1}, {"id": "v-m2", "lang": "en", "gender": "m", "pick": 0},
            {"id": "gaiman", "lang": "en", "gender": "m"}, {"id": "v-f1", "lang": "en", "gender": "f", "pick": 0}, {"id": "v-f2", "lang": "en", "gender": "f", "pick": 4}]
    got = speech.assign_all(db, bid, pool, {"narr"})
    assert got["Nick"] is None and got["Nick Carraway"] is None, "叙述者的对白 = 旁白的声音"
    assert got["Daisy"] == "v-f2" and got["Daisy Buchanan"] == "v-f2", "Daisy 已有的声音沿用，另一种叫法跟着一样"
    assert got["Tom Buchanan"] == "v-m2", "戏份最重的先挑：偏好表第一个（叙述者没占着它）"
    assert got["policeman"] == "v-m1" and got["Jordan"] == "v-f1"
    sp = {r["name"]: r["voice"] for r in db.q("SELECT name, voice FROM speakers WHERE book=?", (bid,))}
    assert sp["Nick"] is None and sp["Daisy Buchanan"] == "v-f2" and sp["Tom Buchanan"] == "v-m2"
    assert speech.voice_for(db, bid, "Nick", "en", pool, "narr") is None and speech.voice_for(db, bid, "Daisy Buchanan", "en", pool, "narr") == "v-f2"
    pool2 = pool + [{"id": "vance", "lang": "en", "gender": "m", "pick": 3}]
    assert speech.voice_for(db, bid, "Tom", "en", pool2, "v-m2") == "vance", "旁白换成了 Tom 的声音：Tom 重新分（别人没用的、偏好表最靠前的；v-m1 已是 policeman 的）"
    assert speech.assign_all(db, bid, pool2, {"v-m2"})["Tom Buchanan"] == "vance", "再跑一遍不动已分好的"
    db.run("INSERT INTO speech VALUES(?,?,?)", (bid, 4, json.dumps([{"a": 0, "b": 5, "who": "Ferdie"}] * 3)))
    db.run("INSERT INTO speakers(book, name, gender, voice, created) VALUES(?,?,?,?,?)", (bid, "Ferdie", "m", None, 3))
    db.run("INSERT INTO speakers(book, name, gender, voice, created) VALUES(?,?,?,?,?)", (bid, "Chester", "m", "gaiman", 3))
    got = speech.assign_all(db, bid, pool + [{"id": "irons", "lang": "en", "gender": "m", "pick": 2}], {"narr"})
    assert got["Ferdie"] == "irons" and got["Chester"] == "gaiman", "戏份排在前面的新人不能拿走后面那位已经分到的声音"
    paras = db.paras_range(bid, 2, 4)
    assert speech.for_text(db, bid, paras, [{"id": "v-f2", "name": "Judi Dench"}, {"id": "v-m1", "name": "Tom Hanks"}]) == "Nick Carraway"
    assert [q[2:] for q in paras[0]["speech"]] == [["Nick Carraway", "m", "", 1], ["Daisy Fay", "f", "Judi Dench", 0], ["policeman", "m", "Tom Hanks", 0]]
    assert [q[2:] for q in paras[1]["speech"]][2] == ["Jordan Baker", "f", "", 0], "音色名表里没有 v-f1：留空"
    j = c.get(f"/api/books/{bid}/text", params={"from": 2, "count": 1}).json()
    assert j["narrator"] == "Nick Carraway"


def test_locked_book_keeps_its_voices(lib):
    """锁定的书：已分好的音色即使后来标了 cast: false 也不换，新人物照常分（不拿 cast: false 的）；解锁后 cast: false 的会被换掉。"""
    c, db, bid = site(lib)
    db.run("INSERT INTO speech VALUES(?,?,?)", (bid, 2, json.dumps([{"a": 0, "b": 5, "who": "Daisy"}, {"a": 6, "b": 12, "who": "Jordan"}])))
    db.run("INSERT INTO speakers(book, name, gender, voice, created) VALUES(?,?,?,?,?)", (bid, "Daisy", "f", "sawyer", 1))
    db.run("INSERT INTO speakers(book, name, gender, voice, created) VALUES(?,?,?,?,?)", (bid, "Jordan", "f", None, 2))
    pool = [{"id": "narr", "lang": "en", "gender": "m"}, {"id": "sawyer", "lang": "en", "gender": "f", "pick": 0, "cast": False},
            {"id": "close", "lang": "en", "gender": "f", "pick": 1, "cast": False}, {"id": "stevenson", "lang": "en", "gender": "f", "pick": 2}]
    db.run("INSERT INTO voice_lock VALUES(?, 0)", (bid,))
    assert speech.locked(db, bid)
    got = speech.assign_all(db, bid, pool, {"narr"})
    assert got == {"Daisy": "sawyer", "Jordan": "stevenson"}, got
    assert speech.voice_for(db, bid, "Daisy", "en", pool, "narr") == "sawyer"
    db.run("DELETE FROM voice_lock WHERE book=?", (bid,))
    assert speech.assign_all(db, bid, pool, {"narr"})["Daisy"] == "stevenson", "没锁：cast: false 的音色换掉"


def fake_tts():
    """假音色服务：/speak 返回 0.5 秒 24 kHz 正弦 wav，按音色记下每次请求；/health 的音色带性别。"""
    import uvicorn
    from fastapi import FastAPI
    from fastapi.responses import Response
    app, calls = FastAPI(), []

    @app.get("/health")
    def health():
        return {"ok": True, "default": "narr", "cats": [],
                "voices": [{"id": "narr", "vid": "v1", "lang": "en", "gender": "m", "name": "Example Voice"},
                           {"id": "v-f1", "vid": "v2", "lang": "en", "gender": "f", "name": "Meryl Streep"},
                           {"id": "v-m1", "vid": "v3", "lang": "en", "gender": "m", "name": "Tom Hanks"}]}

    @app.post("/speak")
    async def speak(req: Request):
        d = await req.json()
        calls.append((d["voice"], d["text"]))
        x = (0.3 * np.sin(2 * np.pi * 220 * np.arange(12000) / 24000)).astype(np.float32)
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(24000); w.writeframes((x * 32767).astype("<i2").tobytes())
        return Response(buf.getvalue(), media_type="audio/wav")

    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    threading.Thread(target=srv.run, daemon=True).start()
    for _ in range(50):
        if srv.started:
            break
        time.sleep(0.1)
    return f"http://127.0.0.1:{port}", calls


def test_attribution_text_api_and_multi_voice_audio(lib):
    url, calls = fake_tts()
    c, db, bid = site(lib, url)
    st = c.app.state.lib
    b = db.book(bid)
    para = '“Never heard of them,” he remarked decisively. “You will,” I answered. 😀 Daisy said “Absolutely!” and laughed.'
    sents = json.dumps([[0, 46], [47, 70], [71, len(para)]])
    db.run("UPDATE paras SET text=?, sents=? WHERE book=? AND n=?", (para, sents, bid, 2))
    db.run("INSERT INTO cast VALUES(?,?,?,0)", (bid, 0, json.dumps({"people": [
        {"name": "Tom Buchanan", "aliases": ["Tom Buchanan", "Tom"], "role": "", "n": 3, "g": "m"},
        {"name": "Nick", "aliases": ["Nick"], "role": "", "n": 3}, {"name": "Daisy", "aliases": ["Daisy"], "role": "", "n": 2, "g": "f"}], "links": []})))
    st.llm = ScriptedLLM({"quotes": [{"i": 1, "who": "Tom", "g": "m"}, {"i": 2, "who": "Nick", "g": "m"}, {"i": 3, "who": "Daisy", "g": "f"}]})
    speech.ensure(st, b, 0, 6, pool=st.tts_voices(), exclude={"narr"})
    assert "【1】“Never heard of them,”" in st.llm.calls[0][1] and "Tom Buchanan" in st.llm.calls[0][0]
    rows = {r["para"]: json.loads(r["json"]) for r in db.q("SELECT para, json FROM speech WHERE book=?", (bid,))}
    assert rows == {2: [{"a": 0, "b": 22, "who": "Tom Buchanan"}, {"a": 47, "b": 58, "who": "Nick"}, {"a": 84, "b": 97, "who": "Daisy"}]}
    assert db.one("SELECT 1 FROM speech_done WHERE book=? AND start=0 AND end=6", (bid,))
    sp = {r["name"]: r["gender"] for r in db.q("SELECT name, gender FROM speakers WHERE book=?", (bid,))}
    assert sp == {"Tom Buchanan": "m", "Nick": "m", "Daisy": "f"}
    speech.ensure(st, b, 0, 6, pool=st.tts_voices(), exclude={"narr"})
    assert len(st.llm.calls) == 1, "做过的范围不重做"

    j = c.get(f"/api/books/{bid}/text", params={"from": 2, "count": 1}).json()["paras"][0]
    assert j["speech_ok"] is True and j["speech"][0][:4] == [0, 22, "Tom Buchanan", "m"]
    assert j["speech"][2][:2] == [85, 98], "emoji 之后的偏移按 UTF-16 算"
    assert j["speech"][0][4] == "Tom Hanks" and j["speech"][2][4] == "Meryl Streep", "分到的音色名（不是旁白 narr）"
    assert c.get(f"/api/books/{bid}/text", params={"from": 7, "count": 1}).json()["paras"][0]["speech_ok"] is False

    r = c.get("/api/tts", params={"t": para[0:46], "voice": "narr", "b": bid, "n": 2})
    assert r.status_code == 200
    assert [v for v, _ in calls] == ["v-m1", "narr"] and calls[0][1] == "“Never heard of them,”" and calls[1][1] == "he remarked decisively."
    with wave.open(io.BytesIO(r.content)) as w:
        assert w.getframerate() == 24000 and abs(w.getnframes() - (12000 * 2 + int(24000 * 0.12))) < 5, "两段 + 120 ms 静音"
    r2 = c.get("/api/tts", params={"t": para[0:46], "voice": "narr", "b": bid, "n": 2})
    assert r2.status_code == 200 and len(calls) == 2, "第二次走缓存"
    assert c.get("/api/tts", params={"t": para[0:46], "voice": "narr"}).status_code == 200 and len(calls) == 3 and calls[2][0] == "narr", "不带段号：整句旁白"
    assert c.get("/api/tts", params={"t": para[47:70], "voice": "narr", "b": bid, "n": 2}).status_code == 200
    assert calls[3][0] != "narr" and calls[3][1] == "“You will,”" and calls[4] == ("narr", "I answered.")
    assert c.get("/api/tts/words", params={"t": para[0:46], "voice": "narr", "b": bid, "n": 2}).status_code in (200, 503)


def test_children_get_child_voices_and_adults_never_do(lib):
    """孩子配同性别的童声（age: child），大人不配童声；已锁定的书里配了成人音色的孩子换成童声，大人不动；童声没装时孩子退回成人音色。"""
    c, db, bid = site(lib)
    db.run("INSERT INTO speech VALUES(?,?,?)", (bid, 2, json.dumps([{"a": 0, "b": 5, "who": "Daisy"}, {"a": 6, "b": 12, "who": "Pammy"},
                                                                    {"a": 13, "b": 20, "who": "Tom"}, {"a": 21, "b": 25, "who": "Ned"}])))
    for name, g, v, age in [("Daisy", "f", "v-f1", "adult"), ("Pammy", "f", "v-f2", "child"), ("Tom", "m", "v-m1", "adult"), ("Ned", "m", None, "child")]:
        db.run("INSERT INTO speakers(book, name, gender, voice, created, age) VALUES(?,?,?,?,0,?)", (bid, name, g, v, age))
    db.run("INSERT INTO voice_lock VALUES(?, 0)", (bid,))
    pool = [{"id": "narr", "lang": "en", "gender": "m", "cast": False}, {"id": "v-m1", "lang": "en", "gender": "m", "pick": 0},
            {"id": "v-f1", "lang": "en", "gender": "f", "pick": 0}, {"id": "v-f2", "lang": "en", "gender": "f", "pick": 1},
            {"id": "girl-a", "lang": "en", "gender": "f", "age": "child", "pick": 5}, {"id": "boy-a", "lang": "en", "gender": "m", "age": "child", "pick": 6}]
    got = speech.assign_all(db, bid, pool, {"narr"})
    assert got == {"Daisy": "v-f1", "Pammy": "girl-a", "Tom": "v-m1", "Ned": "boy-a"}, got
    assert speech.voice_for(db, bid, "Pammy", "en", pool, "narr") == "girl-a", "固定下来：再问还是这个童声"
    assert speech.ranked(pool, "en", "f", set()) == ["v-f1", "v-f2"], "大人不配童声"
    assert speech.ranked(pool, "en", "m", set(), "child") == ["boy-a"]
    adults_only = [v for v in pool if v.get("age") != "child"]
    assert speech.ranked(adults_only, "en", "f", set(), "child") == ["v-f1", "v-f2"], "童声没装：孩子退回成人音色"
    assert speech.voice_for(db, bid, "Pammy", "en", adults_only, "narr") in ("v-f1", "v-f2")


def test_classify_ages_asks_once_and_defaults_to_adult(lib):
    """还没定年龄的说话人一次问模型；c → child，其他（含判断不了、漏答）→ adult；定了的不再问。"""
    c, db, bid = site(lib)
    db.run("INSERT INTO speech VALUES(?,?,?)", (bid, 2, json.dumps([{"a": 0, "b": 5, "who": "Pammy"}, {"a": 6, "b": 12, "who": "Tom"}])))
    for name, g in [("Pammy", "f"), ("Tom", "m"), ("Owl Eyes", "m"), ("someone", "?")]:
        db.run("INSERT INTO speakers(book, name, gender, voice, created) VALUES(?,?,?,NULL,0)", (bid, name, g))
    asked = []

    class LLM:
        def chat_json(self, model, prompt, text, **kw):
            asked.append(text)
            return {"ages": [{"who": "Pammy", "a": "c"}, {"who": "Tom", "a": "a"}]}
    st = type("St", (), {"db": db, "llm": LLM(), "cfg": type("Cfg", (), {"model": "m"})()})()
    b = db.book(bid)
    assert speech.classify_ages(st, b) == 3
    ages = {r["name"]: r["age"] for r in db.q("SELECT name, age FROM speakers WHERE book=?", (bid,))}
    assert ages == {"Pammy": "child", "Tom": "adult", "Owl Eyes": "adult", "someone": None}, ages
    assert "Pammy" in asked[0] and "someone" not in asked[0], "性别不明的用旁白，不用问"
    assert speech.classify_ages(st, b) == 0 and len(asked) == 1, "定了的不再问"


def test_dialogue_language_falls_back_to_the_paragraph_when_offsets_miss(lib):
    """对白区间对不上正文（切出空串）时按整段判断语言：中文书的人物不会被当成英文对白、换掉中文音色。"""
    c, db, bid = site(lib)
    db.run("INSERT INTO paras(book, n, page, kind, text, sents) VALUES(?,?,?,?,?,?)", (bid, 50, 9, "p", "“你来了。”她笑着说。", "[]"))
    db.run("INSERT INTO speech VALUES(?,?,?)", (bid, 50, json.dumps([{"a": 400, "b": 420, "who": "女人"}])))
    db.run("INSERT INTO speakers(book, name, gender, voice, created) VALUES(?,?,?,?,0)", (bid, "女人", "f", "v-zh-f1"))
    pool = [{"id": "narr", "lang": "en", "gender": "m", "cast": False}, {"id": "v-f1", "lang": "en", "gender": "f"},
            {"id": "narr-zh", "lang": "zh", "gender": "m"}, {"id": "v-zh-f1", "lang": "zh", "gender": "f"}]
    assert speech.assign_all(db, bid, pool, {"narr", "narr-zh"})["女人"] == "v-zh-f1"
