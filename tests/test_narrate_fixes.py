"""朗读与译文（架构评审 P2）：同一句不会被并发生成两次、预生成的「检查—登记」不被并发请求插队、
句首人名不当难词、模型没给出译文时不进缓存。"""
from __future__ import annotations

import json
import socket
import threading
import time
from pathlib import Path

import pytest
from fastapi import Request  # noqa: F401  （假音色服务的注解要在模块级可见）

from test_library import lib, make_pdf  # noqa: F401  （lib 是 fixture）
from test_reliability import quiet_caption  # noqa: F401

ROOT = Path(__file__).resolve().parent.parent
HAS_DICT = (ROOT / "data" / "dict.db").exists()


def fake_wav(secs: float = 0.3) -> bytes:
    """一段真的 wav（24 kHz 单声道 16 位，正弦“讲话”）：流式接口要能被当成 PCM 转发、落盘。"""
    import io
    import math
    import wave
    n = int(24000 * secs)
    pcm = b"".join(int(8000 * math.sin(2 * math.pi * 220 * i / 24000)).to_bytes(2, "little", signed=True) for i in range(n))
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(24000); w.writeframes(pcm)
    return buf.getvalue()


FAKE_WAV = fake_wav()


def fake_tts(speak_plan):
    """起一个假的音色服务。speak_plan(n) → (秒数, 状态码)：第 n 次 /speak 先睡多久、返回什么。"""
    import uvicorn
    from fastapi import FastAPI
    from fastapi.responses import JSONResponse, Response
    app, calls = FastAPI(), []

    @app.get("/health")
    def health():
        return {"ok": True, "default": "narr", "voices": [{"id": "narr", "vid": "v1"}]}

    @app.post("/speak")
    async def speak(req: Request):
        n = len(calls)
        calls.append((await req.json())["text"])
        wait, code = speak_plan(n)
        time.sleep(wait)
        if code != 200:
            return JSONResponse({"detail": "坏了"}, status_code=code)
        return Response(b"RIFF....WAVEfake", media_type="audio/wav")

    @app.post("/speak/stream")
    async def speak_stream(req: Request):
        """流式：wav 头先回，PCM 每 2400 字节一块、每块隔 wait 秒。"""
        from fastapi.responses import StreamingResponse
        n = len(calls)
        calls.append("stream:" + (await req.json())["text"])
        wait, code = speak_plan(n)
        if code != 200:
            return JSONResponse({"detail": "坏了"}, status_code=code)

        def body():
            yield FAKE_WAV[:44]
            for i in range(44, len(FAKE_WAV), 2400):
                time.sleep(wait)
                yield FAKE_WAV[i:i + 2400]
        return StreamingResponse(body(), media_type="audio/wav", headers={"X-Sample-Rate": "24000"})

    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    threading.Thread(target=srv.run, daemon=True).start()
    for _ in range(50):
        if srv.started:
            break
        time.sleep(0.1)
    return f"http://127.0.0.1:{port}", calls, srv


def site(lib, tts_url="http://127.0.0.1:9"):
    from fastapi.testclient import TestClient
    from library.config import load
    from library.db import DB
    from library.scan import Scanner
    from library.web import create_app
    cfg = load()
    cfg.tts_url = tts_url
    db = DB(cfg.db_path)
    Scanner(db, cfg, set()).run([str(lib / "books")])
    bid = db.one("SELECT id FROM books")["id"]
    c = TestClient(create_app(cfg))
    c.get(f"/read/{bid}")
    return c, db, bid


def test_one_sentence_is_generated_once_even_after_a_failure(lib):
    # 第 1 次生成失败；失败时已有一个请求在等、又来一个新请求：两者合起来只能再生成 1 次
    url, calls, srv = fake_tts(lambda n: (0.3, 500) if n == 0 else (0.6, 200))
    c, db, bid = site(lib, url)
    codes = []

    def get():
        codes.append(c.get("/api/tts", params={"t": "Same sentence here."}).status_code)
    ts = [threading.Thread(target=get) for _ in range(3)]
    ts[0].start(); time.sleep(0.1); ts[1].start(); time.sleep(0.35); ts[2].start()
    for t in ts:
        t.join(10)
    srv.should_exit = True
    assert sorted(codes) == [200, 200, 503], codes
    assert len(calls) == 2, f"失败 1 次 + 成功 1 次；实际 {len(calls)} 次"


def test_prep_check_and_register_is_atomic(lib, quiet_caption):
    url, calls, srv = fake_tts(lambda n: (1.0, 200))
    c, db, bid = site(lib, url)

    class SlowLLM:
        def chat_json(self, *a, **k):
            time.sleep(1.0)
            return {"zh": "译文", "notes": [], "gloss": []}
    c.app.state.lib.llm = SlowLLM()
    codes = []
    ts = [threading.Thread(target=lambda r=r: codes.append(c.post(f"/api/books/{bid}/prep", json={"start": r[0], "end": r[1]}).status_code))
          for r in ((0, 6), (6, 12))]
    for t in ts:
        t.start()
    for t in ts:
        t.join(20)
    c.post("/api/prep/stop")
    srv.should_exit = True
    assert sorted(codes) == [200, 409], codes


def test_prep_audio_starts_before_attribution(lib, quiet_caption, monkeypatch):
    """预生成的音频线程不等对白归属：引号外的句子立刻生成，引号里的（含没有引号字符的中间句）等归属做完再做。"""
    from library import cast, speech
    from test_gloss_rules import ScriptedLLM
    url, calls, srv = fake_tts(lambda n: (0.05, 200))
    c, db, bid = site(lib, url)
    c.app.state.lib.llm = ScriptedLLM()
    gate = threading.Event()
    monkeypatch.setattr(cast, "ensure", lambda *a, **k: None)
    monkeypatch.setattr(speech, "ensure", lambda *a, **k: gate.wait(5))      # 归属「很慢」，直到测试放行
    s1, s2, s3, s4 = "“Let’s go back, Tom.", "Tomorrow is another day.", "Fine!” she said quickly.", "The night was quiet after that."
    rows = [(90, [s1, s2, s3, s4]), (91, ["No quotes here at all.", "Just narration."])]
    for n, ss in rows:
        text, sents, at = " ".join(ss), [], 0
        for x in ss:
            sents.append([at, at + len(x)]); at += len(x) + 1
        db.run("INSERT INTO paras(book, n, page, kind, text, sents) VALUES(?,?,?,?,?,?)", (bid, n, 7, "p", text, json.dumps(sents)))
    assert c.post(f"/api/books/{bid}/prep", json={"start": 90, "end": 92}).status_code == 200
    time.sleep(1.0)
    early = list(calls)
    assert {s4, "No quotes here at all.", "Just narration."} <= set(early), f"引号外的句子没等归属就做了：{early}"
    assert not {s1, s2, s3} & set(early), f"引号里的句子（含没有引号字符的 s2）要等归属：{early}"
    gate.set()
    for _ in range(100):
        st = c.get(f"/api/books/{bid}/prep", params={"start": 90, "end": 92}).json()
        if not st["running"]:
            break
        time.sleep(0.1)
    srv.should_exit = True
    assert st["error"] == "" and st["done"], st
    assert {s1, s2, s3} <= set(calls[len(early):]), f"归属做完后引号里的句子也做了：{calls}"


@pytest.mark.skipif(not HAS_DICT, reason="没有 data/dict.db")
def test_names_are_not_hard_words(lib):
    from library.text import sentences
    c, db, bid = site(lib)

    class FakeLLM:
        def chat_json(self, *a, **k):
            return {"zh": "译文", "notes": [], "gloss": []}
    c.app.state.lib.llm = FakeLLM()
    text = "He saw Daisy there. Then Daisy left the room. Later Daisy came back with Nick, and Nick smiled."
    n = db.book(bid)["n_paras"]
    db.run("INSERT INTO paras(book,n,page,kind,text,sents) VALUES(?,?,0,'p',?,?)", (bid, n, text, json.dumps(sentences(text))))
    db.run("UPDATE books SET n_paras=? WHERE id=?", (n + 1, bid))
    words = lambda t, **k: {w["lemma"].lower() for w in c.post("/api/translate", json={"text": t, **k}).json()["words"]}
    assert "daisy" not in words("Daisy picked a desperate daisy.", book=bid), "这本书里 Daisy 是人名"
    assert "nick" not in words("Nick hurtled down the stairs.", book=bid)
    assert "john" not in words("John hated school, and the trolley hurtled on."), "不知道是哪本书：句首大写的冷僻词当人名"
    assert "hurtle" in words("John hated school, and the trolley hurtled on.")


def test_empty_translation_is_not_cached(lib):
    c, db, bid = site(lib)
    answers = iter([{"zh": "", "notes": [], "gloss": []}, {"zh": "约翰讨厌上学。", "notes": [], "gloss": []}])

    class FlakyLLM:
        def chat_json(self, *a, **k):
            return next(answers)
    c.app.state.lib.llm = FlakyLLM()
    assert c.post("/api/translate", json={"text": "John hated school."}).json()["zh"] == ""
    assert db.one("SELECT COUNT(*) n FROM ai_cache")["n"] == 0, "没给出译文：不进缓存"
    assert c.post("/api/translate", json={"text": "John hated school."}).json()["zh"] == "约翰讨厌上学。"
    assert db.one("SELECT COUNT(*) n FROM ai_cache")["n"] == 1


def test_stream_plays_while_generating_and_caches_the_file(lib):
    """/api/tts/stream：没缓存时边生成边回（X-Tts: stream），收完落盘；同一句同时来的 /api/tts 等它做完拿同一个文件、不再生成；
    再来一次 /api/tts/stream 直接回文件（X-Tts: file）。"""
    url, calls, srv = fake_tts(lambda n: (0.05, 200))
    c, db, bid = site(lib, url)
    got = {}

    def plain():
        got["plain"] = c.get("/api/tts", params={"t": "Stream this sentence."})
    with c.stream("GET", "/api/tts/stream", params={"t": "Stream this sentence."}) as r:
        assert r.status_code == 200 and r.headers["x-tts"] == "stream"
        th = threading.Thread(target=plain)
        th.start()
        body = b"".join(r.iter_bytes())
    th.join(10)
    srv.should_exit = True
    assert body[:4] == b"RIFF" and body[36:40] == b"data" and body[44:] == FAKE_WAV[44:]
    assert got["plain"].status_code == 200 and got["plain"].content == FAKE_WAV, "等流式做完后拿到的应是同一个落盘文件"
    assert calls == ["stream:Stream this sentence."], calls
    r2 = c.get("/api/tts/stream", params={"t": "Stream this sentence."})
    assert r2.headers["x-tts"] == "file" and r2.content == FAKE_WAV


def test_stream_reports_service_errors_before_streaming(lib):
    """音色服务一开始就拒绝（503）：/api/tts/stream 回正常的 503 JSON，不落盘、不留 .part。"""
    url, calls, srv = fake_tts(lambda n: (0, 503))
    c, db, bid = site(lib, url)
    r = c.get("/api/tts/stream", params={"t": "Broken sentence."})
    srv.should_exit = True
    assert r.status_code == 503 and "坏了" in r.json()["detail"]
    assert not list((c.app.state.lib.cfg.data / "tts").rglob("*")) or not [p for p in (c.app.state.lib.cfg.data / "tts").rglob("*") if p.is_file()]


def test_cached_audio_is_revalidated_not_kept_for_a_year(lib):
    """同一个地址（文字 + 书 + 段号）的配音会随对白归属变：音频回 no-cache + 按配音计划算的 ETag，
    带同一个 ETag 来校验回 304；计划变了（这里换音色）ETag 跟着变，旧 ETag 拿到新文件。"""
    url, calls, srv = fake_tts(lambda n: (0, 200))
    c, db, bid = site(lib, url)
    q = {"t": "Check this sentence."}
    r = c.get("/api/tts", params=q)
    assert r.status_code == 200 and r.headers["cache-control"] == "no-cache"
    tag = r.headers["etag"]
    r304 = c.get("/api/tts", params=q, headers={"If-None-Match": tag})
    assert r304.status_code == 304 and not r304.content
    s = c.get("/api/tts/stream", params=q, headers={"If-None-Match": tag})
    assert s.status_code == 304 and s.headers["etag"] == tag
    other = c.get("/api/tts", params={**q, "voice": "freeman"}, headers={"If-None-Match": tag})
    srv.should_exit = True
    assert other.status_code == 200 and other.headers["etag"] != tag
