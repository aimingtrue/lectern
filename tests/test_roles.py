"""角色配音的全书准备（library/roles.py）：每章状态、假「做完」记录的修复、处理顺序、按章扫全书，以及归属失败不记做完。"""
from __future__ import annotations

import json

from library import roles, speech
from library.llm import LLMError
from test_gloss_rules import ScriptedLLM
from test_library import lib, make_pdf  # noqa: F401  （lib 是 fixture）
from test_narrate_fixes import site

PARA = '“Never heard of them,” he remarked decisively. “You will,” I answered.'
CAST = {"people": [{"name": "Tom Buchanan", "aliases": ["Tom"], "role": "丈夫", "gender": "m"},
                   {"name": "Nick", "aliases": [], "role": "第一人称叙述者", "gender": "m"}],
        "links": [{"a": "Tom", "b": "Nick", "rel": "同学"}], "narrator": "Nick"}
QUOTES = {"quotes": [{"i": 1, "who": "Tom", "g": "m"}, {"i": 2, "who": "Nick", "g": "m"}]}


class FailingLLM(ScriptedLLM):
    def chat_json(self, model, system, user, **k):
        self.calls.append((system, user))
        raise LLMError("本地模型不可用：timed out")


def test_status_repairs_bogus_done_rows_and_orders_from_the_current_chapter(lib):
    c, db, bid = site(lib)
    b = db.book(bid)
    db.run("UPDATE paras SET text=? WHERE book=? AND n=?", (PARA, bid, 2))
    (i0, _, s0, e0), (i1, _, s1, e1) = [(r["chapter"], r["title"], r["start"], r["end"]) for r in roles.status(db, b)]
    db.run("INSERT INTO speech_done VALUES(?,?,?,0)", (bid, s0, e0))       # 早先版本：整章归属都失败也记了做完
    rows = roles.status(db, b)
    assert rows[0]["quoted"] == 1 and rows[0]["attributed"] == 0 and rows[0]["done"] is False, "一段归属都没有却记了做完：删掉"
    assert db.one("SELECT 1 FROM speech_done WHERE book=?", (bid,)) is None
    assert [roles.todo(r) for r in rows] == [True, True], "两章都没抽人物"
    assert [r["chapter"] for r in roles.order(rows, s1)] == [i1, i0], "正在读第二章：先做它，再往前补"
    assert [r["chapter"] for r in roles.order(rows, 0)] == [i0, i1]


def test_sweep_prepares_every_chapter_and_a_failed_chunk_is_retried_next_time(lib):
    c, db, bid = site(lib)
    st = c.app.state.lib
    b = db.book(bid)
    db.run("UPDATE paras SET text=? WHERE book=? AND n=?", (PARA, bid, 2))
    pool = [{"id": "narr", "lang": "en", "gender": "m", "pick": 0}, {"id": "v-m1", "lang": "en", "gender": "m", "pick": 1}]
    st.llm = FailingLLM()
    r = roles.sweep(st, b, 0, pool=pool, exclude={"narr"})
    assert r == {"chapters": 2, "stopped": False} and len(st.llm.calls) >= 2, "每章都试了：人物（失败）、归属（失败）"
    rows = roles.status(db, b)
    assert all(roles.todo(x) for x in rows) and db.one("SELECT 1 FROM speech_done WHERE book=?", (bid,)) is None, "失败的不记做完、不记人物"
    st.llm = ScriptedLLM(CAST, QUOTES, CAST)                              # 第一章：人物 + 归属；第二章：只有人物（没有引号）
    r = roles.sweep(st, b, 0, pool=pool, exclude={"narr"})
    assert r["chapters"] == 2 and len(st.llm.calls) == 3
    rows = roles.status(db, b)
    assert not any(roles.todo(x) for x in rows) and rows[0]["attributed"] == 1 and rows[0]["done"] is True
    sp = {x["name"]: x["voice"] for x in db.q("SELECT name, voice FROM speakers WHERE book=?", (bid,))}
    assert sp["Tom Buchanan"] == "v-m1" and sp["Nick"] is None, "Tom 配到偏好表第一个非旁白的声音；叙述者 Nick 的对白用旁白"
    assert roles.sweep(st, b, 0, pool=pool, exclude={"narr"}) == {"chapters": 0, "stopped": False}, "都做完了：什么都不做"
    j = c.get(f"/api/books/{bid}/text", params={"from": 2, "count": 1}).json()
    assert j["narrator"] == "Nick" and [q[2] for q in j["paras"][0]["speech"]] == ["Tom Buchanan", "Nick"]


def test_ensure_does_not_mark_done_when_a_chunk_fails(lib):
    c, db, bid = site(lib)
    st = c.app.state.lib
    b = db.book(bid)
    db.run("UPDATE paras SET text=? WHERE book=? AND n=?", (PARA, bid, 2))
    st.llm = ScriptedLLM({"quotes": []})                                   # 模型一条都没认出：当失败
    speech.ensure(st, b, 0, 6)
    assert db.one("SELECT 1 FROM speech_done WHERE book=?", (bid,)) is None and db.one("SELECT 1 FROM speech WHERE book=?", (bid,)) is None
    st.llm = ScriptedLLM(QUOTES)
    speech.ensure(st, b, 0, 6)
    assert db.one("SELECT 1 FROM speech_done WHERE book=? AND start=0 AND end=6", (bid,))
    assert json.loads(db.one("SELECT json FROM speech WHERE book=? AND para=2", (bid,))["json"])[0]["who"] == "Tom"


def test_prep_replaces_a_running_sweep_job(lib):
    """后台全书准备在跑时按播放：预生成叫停它并登记，不是 409。"""
    import threading
    c, db, bid = site(lib)
    st = c.app.state.lib
    st.prep_job = {"running": True, "book": bid, "start": 0, "end": 0, "voice": ("narr", "narr-zh"), "total": 0, "audio": 0, "tr": 0,
                   "stop": False, "error": "", "done": False, "speech": threading.Event(), "sweep": True}
    old = st.prep_job
    r = c.post(f"/api/books/{bid}/prep", json={"start": 0, "end": 6, "voice": "narr"})
    assert old["stop"] is True
    assert r.status_code in (200, 503), r.text                             # 503 = 测试里没有朗读服务；登记已经发生
    assert st.prep_job is not old
