"""朗读与译文：整句译文和难词（本机 Ollama + 离线词典）、多音色朗读和逐词对齐（tts/server.py），
按章预生成音频和译文。中文句不翻译，改为讲解难词、成语、典故；每句按自己的语言用对应的音色。"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
import time
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, Response, StreamingResponse

from . import bookkind, busy, cast, concepts, prosody, roles, speech
from .llm import LLMError
from .text import is_zh
from .web import run_in_thread


log = logging.getLogger(__name__)
AUDIO_PAR, TR_PAR, AUDIO_WINDOW = 3, 2, 12   # 预生成：同时生成的句数、同时翻译的句数、按句长排序的窗口
SPEAKABLE = re.compile(r"[A-Za-z0-9\u3400-\u9fff]")   # 有字可读（只有标点、符号的碎片不读）


def register(app: FastAPI, st) -> None:
    cfg, db = st.cfg, st.db

    # ---- AI
    def cached_ai(key: str, fn, ok=lambda v: True):
        """ok(v) 为假的结果（如模型没给出译文）照样返回，但不进缓存：下次再问，不会把坏结果永久留下。"""
        r = db.one("SELECT value FROM ai_cache WHERE key=?", (key,))
        if r:
            return json.loads(r["value"])
        v = fn()
        if ok(v):
            db.run("INSERT OR REPLACE INTO ai_cache VALUES(?,?,?)", (key, json.dumps(v, ensure_ascii=False), time.time()))
        return v

    SKIP = {"zh": "", "gloss": [], "notes": [], "lang": "zh", "skip": True}   # 白话句：母语，不译不注

    def zh_skip(text: str, mode: str) -> bool:
        """中文句要不要跳过：白话书（mode zh）整本不译；别的书里只有文言句译成白话，白话句（译文、注释、引言）跳过。"""
        return is_zh(text) and (mode == "zh" or not bookkind.is_wenyan(text, loose=mode == "wy"))

    def mode_of(bid: str) -> str:
        """书的读法（library/bookkind.py）：不知道是哪本书时按 en 处理（中文句只看本句是不是文言）。"""
        b = db.book(bid) if bid and re.fullmatch(r"[0-9a-f]{8,40}", bid) else None
        return bookkind.mode(db, b) if b else "en"

    def wenyan_ok(v, text: str) -> bool:
        """文言译文完整才进缓存：有内容、不停在逗号顿号上；文言译成白话通常更长，10 字以上的句子译文不该短于原文一半。"""
        zh = str(v.get("zh") or "").strip() if isinstance(v, dict) else ""
        return bool(zh) and zh[-1] not in "，、：；（“‘《—" and (len(text) < 10 or len(zh) >= len(text) * 0.5)

    def translate_wenyan(text: str):
        """文言句：译成现代白话，注古今异义、通假、活用和难懂的字词（浮在原文上方，和英文生词一样），
        外加 1–2 条典故、人物、制度背景（讲解栏）。形状同英文译文，lang = wy。"""
        def run():
            extra = max(2, min(5, len(text) // 12))
            r = st.llm.chat_json(cfg.model,
                "你是古文老师，帮现代读者读懂文言原文。只输出 JSON："
                '{"zh":"忠实、通顺的现代白话译文","gloss":[{"en":"原句里的文言字词，从原句原样照抄的连续片段，1–4 个字","zh":"它在这句里的意思"}],'
                '"notes":["典故、人物、制度或历史背景，每条一句；没有就给空数组"]}'
                f"。gloss 最多 {extra} 条，挑古今异义、通假字、词类活用、难懂的实词和虚词用法；人名、地名、官名不要，一看就懂的不要。"
                "gloss 的 zh 优先从译文 zh 中原样复制对应的连续片段（不超过 8 个字）；译文里没有明确对应的，写它在这句里的意思（不超过 6 个字）。"
                "notes 最多 2 条，简单的句子没有就给空数组。",
                f"原句：{text}", num_ctx=4096)
            zh = str(r.get("zh") or "").strip()
            gl, seen = [], set()
            for g in (r.get("gloss") or []):
                if not isinstance(g, dict):
                    continue
                w, z = str(g.get("en") or g.get("word") or "").strip(), str(g.get("zh", "")).strip()
                if w and z and w not in seen and len(w) <= 6 and w in text and len(z) <= (8 if z in zh else 6):
                    seen.add(w)
                    gl.append({"en": w, "zh": z})
            notes = [str(n).strip() for n in (r.get("notes") or []) if str(n).strip()][:2]
            return {"zh": zh, "gloss": gl[:extra], "notes": notes, "lang": "wy"}
        return cached_ai(tr_key(text), run, ok=lambda v: wenyan_ok(v, text))

    def gloss_ok(en: str, z: str, zh: str, text: str, names: frozenset) -> bool:
        """一条难词标注能不能用：英文是原句里原样的连续片段（最多 4 个词、整词匹配），不是虚词或人名；
        中文优先是译文里原样的片段（≤8 字，右栏能对上划线），否则是这个词在本句的意思（≤6 字）。"""
        toks = re.findall(r"[A-Za-z]+(?:['’][A-Za-z]+)*", en)
        if not toks or len(toks) > 4 or not z or not re.search(rf"(?<![A-Za-z]){re.escape(en)}(?![A-Za-z])", text, re.I):
            return False
        if all(t.lower() in FUNC or t.lower() in names or "'" in t or "’" in t for t in toks) and (len(toks) == 1 or len(z) < 3):
            return False   # 虚词、人名不标；全是虚词的多词习语（beyond me →「想不通」）中文得有 3 字以上才算数，「of the →的」不要
        cjk = any("\u4e00" <= c <= "\u9fff" for c in cfg.gloss_lang)
        cap = (8 if z in zh else 6) if cjk else (48 if z in zh else 36)   # 非 CJK 译文按字符放宽
        return len(z) <= cap

    def complete(v, text: str) -> bool:
        """译文完整才进缓存：有内容、不停在逗号顿号上（模型输出被截断时常见）、长句的译文不至于短得离谱
        （英文 80 字符以上时，中文至少是英文长度的 12%；正常译文约 30%–50%）。"""
        zh = str(v.get("zh") or "").strip() if isinstance(v, dict) else ""
        cjk = any("\u4e00" <= c <= "\u9fff" for c in cfg.gloss_lang)
        ratio = 0.12 if cjk else 0.4          # CJK 译文比原文短得多；拼音文字长度相当
        return bool(zh) and zh[-1] not in "，、：；（“‘《—,:;(" and (len(text) < 80 or len(zh) >= len(text) * ratio)

    def translate_text(text: str, names: frozenset = frozenset(), mode: str = "en"):
        """整句译文 + 难词标注（词典挑的难词 + 模型补的关键表达）。难点讲解另走 notes_text（只在讲解栏要时做），
        这样译文这一步输出短、快。中文句：白话不译不注（SKIP，不问模型），文言译成白话（translate_wenyan）。"""
        if is_zh(text):
            return SKIP if zh_skip(text, mode) else translate_wenyan(text)

        def run():
            words = [w["word"] for w in hard_words(text, names, "most")]   # 按最高档列词：译文缓存各档共用，返回时再按档取舍
            extra = max(2, -(-len(re.findall(r"[A-Za-z]+", text)) // 6))   # 每 6 个词最多补 1 条，至少 2 条
            lang = cfg.gloss_lang
            r = st.llm.chat_json(cfg.model,
                f"You translate and teach English literature in {lang}. Output JSON only: "
                '{"zh":"a faithful, natural rendering of the sentence","gloss":'
                '[{"en":"a word or expression copied verbatim from the sentence","zh":"what it means"}]}'
                f". In gloss, first give one entry for each word the user listed, in the order they appear; "
                f"then add at most {extra} more expressions that are key to understanding the sentence: "
                "phrasal verbs, idioms, set collocations, or a common word used here in an extended or unusual sense "
                "(4 words at most). Do not gloss function words, conjunctions, prepositions, pronouns, articles or "
                "auxiliaries, and do not gloss names of people or places; add nothing if nothing is worth adding. "
                f'Write the "zh" field in {lang}. Prefer copying the matching run verbatim out of the translation '
                "(keep it short); if the translation has no clear counterpart, state what the expression means here.",
                f"Sentence: {text}\nWords to gloss: {', '.join(words) or '(none)'}", num_ctx=6144)
            zh = str(r.get("zh") or "")
            gl, seen = [], set()
            for g in (r.get("gloss") or []):
                if not isinstance(g, dict):
                    continue
                en, z = str(g.get("en", "")).strip(), str(g.get("zh", "")).strip()
                if en.lower() not in seen and gloss_ok(en, z, zh, text, names):
                    seen.add(en.lower())
                    gl.append({"en": en, "zh": z})
            return {"zh": zh, "gloss": gl[:len(words) + extra]}
        return cached_ai(tr_key(text), run, ok=lambda v: complete(v, text))

    def notes_text(text: str, mode: str = "en") -> list[str]:
        """难点讲解（讲解栏用）：最多 2 条，每条一句；单独缓存，和译文互不影响。文言句的译文里本来就有，白话句没有。"""
        if is_zh(text):
            return [] if zh_skip(text, mode) else translate_wenyan(text).get("notes") or []

        def run():
            lang = cfg.gloss_lang
            r = st.llm.chat_json(cfg.model,
                f"You are an English teacher explaining, in {lang}, what is hard about this sentence: "
                "a phrase, the sentence structure, a figure of speech, or cultural background. Output JSON only: "
                '{"notes":["one note, one sentence"]}. At most 2; return an empty array when the sentence holds '
                "nothing difficult. Do not prefix a note with a label.",
                f"Sentence: {text}", num_ctx=6144)
            return {"notes": [str(n).strip() for n in (r.get("notes") or []) if str(n).strip()][:2]}
        return cached_ai("n1:" + hashlib.sha1(f"{cfg.model}|{text}".encode()).hexdigest(), run,
                         ok=lambda v: isinstance(v, dict) and isinstance(v.get("notes"), list)).get("notes") or []

    @app.post("/api/translate")
    async def translate(req: Request):
        d = await req.json()
        text = d.get("text", "").strip()[:3000]
        if not text:
            raise HTTPException(400, "no text")
        bid = str(d.get("book") or "")
        names = await run_in_thread(lambda: book_names(bid))   # 前端带上书的 id 时按全书认人名
        mode = await run_in_thread(lambda: mode_of(bid))
        level = str(d.get("level") or "more")
        try:
            r = await run_in_thread(lambda: translate_text(text, names, mode))
        except LLMError as ex:
            raise HTTPException(503, str(ex))
        if is_zh(text):
            return {**r, "words": []}
        words = await run_in_thread(lambda: hard_words(text, names, level))   # 难词：词典现查，不进缓存
        gloss = await run_in_thread(lambda: level_gloss(r.get("gloss") or [], text, names, level, words))
        return {**r, "gloss": gloss, "words": words}

    @app.post("/api/notes")
    async def notes(req: Request):
        d = await req.json()
        text = d.get("text", "").strip()[:3000]
        if not text:
            raise HTTPException(400, "no text")
        bid = str(d.get("book") or "")
        try:
            mode = await run_in_thread(lambda: mode_of(bid))
            return {"notes": await run_in_thread(lambda: notes_text(text, mode))}
        except LLMError as ex:
            raise HTTPException(503, str(ex))

    # ---- 朗读：多个音色（本机 tts/server.py，声音克隆），默认 所选音色；音色由前端每次请求带上
    tts_http = httpx.Client(base_url=cfg.tts_url, timeout=180, trust_env=False)
    tts_locks: dict = {}                      # 文件名 → [锁, 正在用它的线程数]
    tts_guard = threading.Lock()
    tts_meta: dict = {"h": {}, "t": 0.0}
    tts_meta_lock = threading.Lock()      # 音色表过期时只让一个线程去问（以前两个线程同时过期各问一次）
    DOWN = "the speech service is not running (start it with: bash tools/setup_tts.sh)"

    def tts_health() -> dict:
        try:
            h = tts_http.get("/health", timeout=2).json()
        except Exception:  # noqa: BLE001
            return {"ok": False, "down": True}
        tts_meta["h"], tts_meta["t"] = h, time.time()
        return h

    def voice_of(voice: str | None, zh: bool = False) -> str:
        """请求里的音色 id；没给就用默认音色（中文句用第一个装好的中文音色）。"""
        v = (voice or "").strip()
        if v and not re.fullmatch(r"[a-z0-9_-]{1,40}", v):
            raise HTTPException(400, "bad voice id")
        if v:
            return v
        if not tts_meta["h"]:                 # 还没问过音色服务：先问一次，免得同一句先按兜底音色、后按服务的默认音色各存一份
            tts_voices()
        if zh:
            return next((x["id"] for x in tts_meta["h"].get("voices", []) if x.get("lang") == "zh"), "mine")
        return tts_meta["h"].get("default") or "mine"

    def tts_voices() -> list[dict]:
        """装好的音色（音色服务 /health 给的：id、name、lang、gender、vid…）；30 秒内不重复问。"""
        if not tts_meta["h"] or time.time() - tts_meta["t"] > 30:
            with tts_meta_lock:
                if not tts_meta["h"] or time.time() - tts_meta["t"] > 30:
                    tts_health()
        return tts_meta["h"].get("voices", [])
    st.tts_voices = tts_voices
    st.narrator_voices = lambda: (voice_of(None), voice_of(None, True))   # 默认旁白（英文、中文）：全书准备时角色不配这两个

    def vid_of(v: str) -> str:
        return next((x["vid"] for x in tts_voices() if x["id"] == v), None) or f"v-{v}"

    def tts_file(text: str, voice: str | None = None) -> Path:
        """缓存文件名 = 音色指纹 + 句子：换音色另存一份，参考音频变了旧缓存自动作废。"""
        v = voice_of(voice, is_zh(text))
        key = hashlib.sha1(f"{vid_of(v)}|{text}".encode()).hexdigest()
        return cfg.data / "tts" / key[:2] / f"{key}.wav"

    # ---- 多角色：一句按对白切成几段（library/speech.py），各段各自的音色，拼成一个文件
    def plan(bid: str | None, n: int, text: str, voice: str | None = None, voice_zh: str | None = None) -> list[tuple[str, str]]:
        """一句的朗读计划 [(文字, 音色 id)]：这一段做过对白归属的按说话人切段，否则整句一段用旁白。"""
        text = re.sub(r"\s+", " ", text).strip()[:1500]
        narr = lambda t: voice_of(voice_zh if is_zh(t) else voice, is_zh(t))   # noqa: E731
        row = db.one("SELECT json FROM speech WHERE book=? AND para=?", (bid, n)) if bid and n is not None and n >= 0 else None
        if not row:
            return [(text, narr(text))]
        p = db.one("SELECT text FROM paras WHERE book=? AND n=?", (bid, n))
        m = re.search(r"\s+".join(re.escape(w) for w in text.split()), p["text"]) if p else None
        if not m:
            return [(text, narr(text))]
        pool = tts_voices()
        vo = lambda who, t: speech.voice_for(db, bid, who, "zh" if is_zh(t) else "en", pool, narr(t))   # noqa: E731
        return speech.segments(p["text"], m.start(), m.end(), json.loads(row["json"]), vo, narr)

    def plan_path(parts: list[tuple[str, str]]) -> Path:
        """计划对应的缓存文件：一段的就是 tts_file；多段的键 = 各段的音色指纹 + 文字。"""
        if len(parts) == 1:
            return tts_file(*parts[0])
        key = hashlib.sha1(("multi|" + "|".join(f"{vid_of(voice_of(v, is_zh(t)))}|{t}" for t, v in parts)).encode()).hexdigest()
        return cfg.data / "tts" / key[:2] / f"{key}.wav"

    def resolve(t: str, voice: str, b: str, n: int) -> tuple[str, Path, list]:
        text = re.sub(r"\s+", " ", t).strip()[:1500]
        bid = b if re.fullmatch(r"[0-9a-f]{8,40}", b or "") else None
        parts = plan(bid, n, text, voice)
        return text, plan_path(parts), parts

    @app.get("/api/tts/status")
    def tts_status():
        return tts_health()

    def once(path: Path, make) -> None:
        """同一个文件同时只生成一次：等着的线程拿到锁时文件已经在了，直接返回。make() 返回要写的字节。"""
        with tts_guard:
            ent = tts_locks.setdefault(path.name, [threading.Lock(), 0])
            ent[1] += 1
        try:
            with ent[0]:
                if path.exists():
                    return
                data = make()
                path.parent.mkdir(parents=True, exist_ok=True)
                tmp = path.with_suffix(".part")
                tmp.write_bytes(data)
                tmp.replace(path)
        finally:
            with tts_guard:
                ent[1] -= 1
                if ent[1] == 0:
                    tts_locks.pop(path.name, None)

    def tts_generate(path: Path, text: str, voice: str | None = None) -> None:
        def make() -> bytes:
            try:
                r = tts_http.post("/speak", json={"text": text, "voice": voice_of(voice, is_zh(text))})
            except httpx.HTTPError:
                raise HTTPException(503, DOWN)
            if r.status_code != 200:
                raise HTTPException(503, (r.json().get("detail") if r.headers.get("content-type", "").startswith("application/json") else r.text[:200]) or "生成失败")
            return r.content
        once(path, make)

    GAP = 0.12                                # 多角色拼接：段与段之间的静音（秒）

    def generate_plan(path: Path, parts: list[tuple[str, str]]) -> None:
        """按计划生成：一段的直接生成；多段的各段先各自生成（各段也是普通缓存，别的句子能复用），再拼成一个 wav。"""
        if len(parts) == 1:
            return tts_generate(path, *parts[0])
        for t, v in parts:
            tts_generate(tts_file(t, v), t, v)

        def make() -> bytes:
            import io
            import wave
            import numpy as np
            xs, sr = [], 0
            for t, v in parts:
                got = prosody.read_wav(tts_file(t, v))
                if got is None:
                    raise HTTPException(503, "join failed: one piece could not be read")
                x, r = got
                if sr and r != sr:
                    raise HTTPException(503, "join failed: pieces have different sample rates")
                sr = r
                if xs:
                    xs.append(np.zeros(int(sr * GAP), np.float32))
                xs.append(x)
            a = np.concatenate(xs)
            buf = io.BytesIO()
            with wave.open(buf, "wb") as w:
                w.setnchannels(1); w.setsampwidth(2); w.setframerate(sr)
                w.writeframes((np.clip(a, -1, 1) * 32767).astype("<i2").tobytes())
            return buf.getvalue()
        once(path, make)

    # ---- 流式：没缓存的句子边生成边播（/api/tts/stream），收完照旧落盘，之后逐词对齐、语调、重听都走文件
    def wav_header(sr: int, nbytes: int) -> bytes:
        """单声道 16 位 wav 头（44 字节）；长度未知（流式）时填 0xFFFFFFFF。"""
        import struct
        riff = 0xFFFFFFFF if nbytes == 0xFFFFFFFF else nbytes + 36
        return b"RIFF" + struct.pack("<I", riff) + b"WAVEfmt " + struct.pack("<IHHIIHH", 16, 1, 1, sr, sr * 2, 2, 16) + b"data" + struct.pack("<I", nbytes)

    def claim(path: Path):
        """试着成为这个文件的生成者（不等）：拿到了返回释放函数；别人正在生成返回 None。和 once 共用同一把锁。"""
        with tts_guard:
            ent = tts_locks.setdefault(path.name, [threading.Lock(), 0])
            ent[1] += 1

        def drop():
            with tts_guard:
                ent[1] -= 1
                if ent[1] == 0:
                    tts_locks.pop(path.name, None)
        if ent[0].acquire(blocking=False):
            def release():
                ent[0].release()
                drop()
            return release
        drop()
        return None

    def stream_open(text: str, voice: str | None):
        """向音色服务开一路流式生成（POST /speak/stream）；连不上、没就绪时和 tts_generate 一样报 503。返回 (响应, 采样率)。"""
        try:
            r = tts_http.send(tts_http.build_request("POST", "/speak/stream", json={"text": text, "voice": voice_of(voice, is_zh(text))}), stream=True)
        except httpx.HTTPError:
            raise HTTPException(503, DOWN)
        if r.status_code != 200:
            body = r.read()
            r.close()
            detail = ""
            if r.headers.get("content-type", "").startswith("application/json"):
                try:
                    detail = json.loads(body).get("detail", "")
                except ValueError:
                    pass
            raise HTTPException(503, detail or body[:200].decode(errors="replace") or "生成失败")
        return r, int(r.headers.get("x-sample-rate") or 24000)

    def stream_plan(path: Path, parts: list[tuple[str, str]], release):
        """流式生成一句（已经 claim 到 path）：各段依次来自 /speak/stream（这一段的缓存已有就直接读文件），一边收一边送给浏览器，
        收完落盘（各段自己的缓存 + 多段拼成的文件，和 generate_plan 的结果一样）。返回 (采样率, 给浏览器的字节块生成器)。
        收和落盘在自己的线程里做完，浏览器中途断开不影响；生成出错时生成器抛出异常，响应中断、不落盘，前端会改走 /api/tts。"""
        import queue
        import wave
        t0, v0 = parts[0]
        first, sr = None, 0
        if len(parts) > 1 and tts_file(t0, v0).exists():
            with wave.open(str(tts_file(t0, v0))) as w:
                sr = w.getframerate()
        if not sr:
            try:
                first, sr = stream_open(t0, v0)
            except BaseException:
                release()
                raise
        q: "queue.Queue" = queue.Queue()

        def part_pcm(t: str, v: str, opened) -> bytes:
            """一段的 PCM：有缓存读文件；没有就流式生成（边收边 q.put）并写这一段的缓存。"""
            f = tts_file(t, v)
            rel = None if f == path else claim(f)              # 单段：path 就是这一段的缓存，锁已经在手上
            try:
                if f != path and rel is None:                  # 别人（预生成）正在做这一段：等它做完读文件
                    tts_generate(f, t, v)
                if f != path and f.exists():
                    with wave.open(str(f)) as w:
                        data = w.readframes(w.getnframes())
                    q.put(data)
                    return data
                r = opened or stream_open(t, v)[0]
                buf, head = [], b""
                try:
                    for chunk in r.iter_bytes():
                        if len(head) < 44:                     # 音色服务回的 wav 头固定 44 字节，去掉
                            head += chunk
                            if len(head) < 44:
                                continue
                            chunk, head = head[44:], head[:44]
                            if head[36:40] != b"data":
                                raise HTTPException(503, "the speech service did not return wav")
                        if chunk:
                            buf.append(chunk)
                            q.put(chunk)
                finally:
                    r.close()
                data = b"".join(buf)
                f.parent.mkdir(parents=True, exist_ok=True)
                tmp = f.with_suffix(".part")
                tmp.write_bytes(wav_header(sr, len(data)) + data)
                tmp.replace(f)
                return data
            finally:
                if rel:
                    rel()

        def work():
            try:
                pcm = []
                for i, (t, v) in enumerate(parts):
                    if i:
                        gap = bytes(int(sr * GAP) * 2)
                        q.put(gap)
                        pcm.append(gap)
                    pcm.append(part_pcm(t, v, first if i == 0 else None))
                if len(parts) > 1:
                    data = b"".join(pcm)
                    path.parent.mkdir(parents=True, exist_ok=True)
                    tmp = path.with_suffix(".part")
                    tmp.write_bytes(wav_header(sr, len(data)) + data)
                    tmp.replace(path)
                q.put(None)
            except Exception as ex:  # noqa: BLE001
                log.warning("流式生成失败 %s：%s", path.name, ex)
                q.put(ex)
            finally:
                release()
        threading.Thread(target=work, daemon=True).start()

        def body():
            yield wav_header(sr, 0xFFFFFFFF)
            while True:
                item = q.get()
                if item is None:
                    return
                if isinstance(item, Exception):
                    raise item
                yield item
        return sr, body()

    def wav_file(req: Request, path: Path, **headers) -> Response:
        """缓存好的一句音频。地址（文字 + 书 + 段号）不变时对白归属、角色音色可能已经变了，所以不让浏览器长期缓存：
        每次带 If-None-Match 来校验，ETag 是按配音计划算的文件名，没变回 304（mini 的 Chrome 曾把归属前的纯旁白音频缓存了一年）。"""
        tag = f'"{path.stem}"'
        h = {"Cache-Control": "no-cache", "ETag": tag, **headers}
        if req.headers.get("if-none-match") == tag:
            return Response(status_code=304, headers=h)
        return FileResponse(path, media_type="audio/wav", headers=h)

    @app.get("/api/tts/stream")
    async def tts_stream(req: Request, t: str, voice: str = "", b: str = "", n: int = -1):
        """同 /api/tts；没缓存时边生成边回（X-Tts: stream），有缓存时回文件（X-Tts: file）。别人正在生成这一句就等它做完回文件。"""
        text, path, parts = await run_in_thread(lambda: resolve(t, voice, b, n))
        if not SPEAKABLE.search(text):
            raise HTTPException(400, "nothing here can be read aloud")
        if not path.exists():
            release = claim(path)
            if release is None:
                await run_in_thread(lambda: generate_plan(path, parts))
            elif path.exists():
                release()
            else:
                _, body = await run_in_thread(lambda: stream_plan(path, parts, release))
                return StreamingResponse(body, media_type="audio/wav", headers={"X-Tts": "stream", "Cache-Control": "no-store"})
        return wav_file(req, path, **{"X-Tts": "file"})

    def tts_words(path: Path, text: str) -> list:
        """逐词时间 [[词, 起, 止], …]，和音频同名的 .json，只对齐一次。"""
        wf = path.with_suffix(".json")
        if wf.exists():
            return json.loads(wf.read_text())
        try:
            r = tts_http.post("/align", json={"path": str(path), "text": text}, timeout=120)
        except httpx.HTTPError:
            raise HTTPException(503, DOWN)
        if r.status_code != 200:
            raise HTTPException(503, "word alignment failed")
        words = r.json()["words"]
        wf.write_text(json.dumps(words, ensure_ascii=False))
        return words

    @app.get("/api/tts/words")
    async def tts_words_api(t: str, voice: str = "", b: str = "", n: int = -1):
        text, path, _ = await run_in_thread(lambda: resolve(t, voice, b, n))
        if not path.exists():
            raise HTTPException(404, "no audio generated for this sentence yet")
        try:
            return {"words": await run_in_thread(lambda: tts_words(path, text))}
        except HTTPException:
            return {"words": []}

    def ipa_of(w: str):
        r = st.dict.lookup(w) if st.dict.ok else None
        return ((r.get("form") or {}).get("us") or r.get("us")) if r else None

    def accent_of(voice: str | None, text: str) -> str:
        v = voice_of(voice, is_zh(text))
        return next((x.get("accent") for x in tts_voices() if x["id"] == v), None) or "us"

    def prev_sents(bid: str | None, n: int, text: str) -> list[str]:
        """这句前面的两句（同一段里的，或上一段末尾的）：文本规则靠它判断旧信息。"""
        if not bid or n is None or n < 0:
            return []
        out: list[str] = []
        for p in reversed(db.paras_range(bid, max(0, n - 1), n + 1)):
            ss = [p["text"][a:b] for a, b in p["sents"]]
            if p["n"] == n:
                k = next((i for i, s_ in enumerate(ss) if re.sub(r"\s+", " ", s_).strip() == text), len(ss))
                ss = ss[:k]
            out = ss[-2:] + out
            if len(out) >= 2:
                break
        return out[-2:]

    def tts_prosody(path: Path, text: str, voice: str | None = None, bid: str | None = None, n: int = -1):
        """这一句的语调：音高曲线、重读、句调、目标调、停顿 + 文本规则与中文讲解（library/prosody.py、phonrules.py）。
        存成和音频同名的 .pros.json；中文句、对不上的返回 None（也记下来，不再重算）。旧版本（v < 2）的下次用到时重算。"""
        pf = path.with_suffix(".pros.json")
        if pf.exists():
            old = json.loads(pf.read_text())
            if old is None or old.get("v", 0) >= prosody.VERSION:
                return old
        r = prosody.analyze(path, tts_words(path, text), text, ipa_of, st.dict.lookup if st.dict.ok else None,
                            accent_of(voice, text), prev_sents(bid, n, text))
        pf.write_text(json.dumps(r, ensure_ascii=False, separators=(",", ":")))
        return r

    @app.get("/api/tts/prosody")
    async def tts_prosody_api(t: str, voice: str = "", b: str = "", n: int = -1):
        text, path, _ = await run_in_thread(lambda: resolve(t, voice, b, n))
        if not path.exists():
            raise HTTPException(404, "no audio generated for this sentence yet")
        bid = b if re.fullmatch(r"[0-9a-f]{8,40}", b or "") else None
        try:
            return {"prosody": await run_in_thread(lambda: tts_prosody(path, text, voice, bid, n))}
        except HTTPException:
            return {"prosody": None}

    @app.get("/api/tts")
    async def tts(req: Request, t: str, voice: str = "", b: str = "", n: int = -1):
        """b + n（书、段号）给了就按对白归属多角色配音（library/speech.py）；没给整句旁白。"""
        text, path, parts = await run_in_thread(lambda: resolve(t, voice, b, n))
        if not SPEAKABLE.search(text):
            raise HTTPException(400, "nothing here can be read aloud")
        if not path.exists():
            await run_in_thread(lambda: generate_plan(path, parts))
        return wav_file(req, path)

    @app.get("/api/voice/{vid}/bed.m4a")
    def voice_bed(vid: str):
        """音色的背景音（tools/add_bed.py 从纪录片配乐里做的循环音轨），朗读时在底下轻轻放。"""
        path = cfg.data / "voice" / vid / "bed.m4a"
        if not re.fullmatch(r"[a-z0-9_-]{1,40}", vid) or not path.exists():
            raise HTTPException(404, "this voice has no background bed")
        return FileResponse(path, media_type="audio/mp4", headers={"Cache-Control": "public, max-age=86400"})

    # ---- 预生成：把一段范围（段落序号 [start, end)）的朗读音频和译文提前做好，播放时只读缓存
    def prep_sentences(bid: str, start: int, end: int, cap: int | None = 5000) -> list[tuple[int, str]]:
        """和前端一样取句子 [(段号, 句子)]：text[a:b]（存好的句子偏移），所以缓存键完全一致。按章最多 cap 段，整本不设上限。"""
        out, seen = [], set()
        start = max(0, start)
        for p in db.paras_range(bid, start, end if cap is None else min(end, start + cap)):
            for a, b in p["sents"]:
                sent = p["text"][a:b]
                if SPEAKABLE.search(sent) and sent not in seen:
                    seen.add(sent)
                    out.append((p["n"], sent))
        return out

    def tr_key(text: str) -> str:
        return ("w1:" if is_zh(text) else "t3:") + hashlib.sha1(f"{cfg.model}|{text}".encode()).hexdigest()

    def prep_cached(sents: list[tuple[int, str]], bid: str, voices: tuple) -> tuple[int, int]:
        """已经做好的句数：(音频, 译文)。不用译的句子（白话）算作译文已好。"""
        audio = sum(1 for n, s in sents if plan_path(plan(bid, n, s, *voices)).exists())
        mode = mode_of(bid)
        skip = sum(1 for _, s in sents if zh_skip(s, mode))
        sents = [x for x in sents if not zh_skip(x[1], mode)]
        keys = {tr_key(s[:3000]) for _, s in sents}
        found, ks = set(), list(keys)
        for i in range(0, len(ks), 500):
            part = ks[i:i + 500]
            found.update(r["key"] for r in db.q(f"SELECT key FROM ai_cache WHERE key IN ({','.join('?' * len(part))})", part))
        tr = sum(1 for _, s in sents if tr_key(s[:3000]) in found)
        return audio, tr + skip

    def _prep(job: dict, sents: list[str]) -> None:
        prev = job.pop("after", None)         # 顶掉的后台全书准备：等它当前这次模型调用做完、把锁放了再开始
        while prev and prev["running"] and not job["stop"]:
            time.sleep(0.5)
        if job["stop"]:
            job["running"] = False
            return
        try:
            while not job["stop"]:
                try:
                    with busy.hold("whole-book pre-generation" if job.get("whole") else "朗读预生成"):   # 本机长任务一次只跑一个
                        job["waiting"] = ""
                        _prep_run(job, sents)
                    return
                except busy.Busy as ex:
                    if not job.get("whole"):
                        job["error"], job["stop"] = str(ex), True
                        return
                    job["waiting"] = str(ex)       # 整本的：排队等别的长任务（如书名翻译）做完，随时可停
                    for _ in range(15):
                        if job["stop"]:
                            return
                        time.sleep(1)
        finally:
            job["running"] = False

    def _prep_run(job: dict, sents: list[str]) -> None:
        voices = job["voice"]
        names = book_names(job["book"])

        counters = threading.Lock()

        def bump(k: str) -> None:
            with counters:
                job[k] += 1

        def pool(items: list, fn, par: int) -> bool:
            """par 个线程一起做 items（每项 fn(*item) → 继续与否）；任一项返回 False 或任务被停就全停。"""
            it, take, ok = iter(items), threading.Lock(), [True]

            def worker():
                while ok[0] and not job["stop"]:
                    with take:
                        x = next(it, None)
                    if x is None:
                        return
                    if not fn(*x):
                        ok[0] = False
            ts = [threading.Thread(target=worker, daemon=True) for _ in range(max(1, par))]
            for t in ts:
                t.start()
            for t in ts:
                t.join()
            return ok[0] and not job["stop"]

        def by_length(items: list, k: int = AUDIO_WINDOW) -> list:
            """每 k 句一组，组内按句长排序：同时送到朗读服务的几句长短接近，攒成一批时不浪费补零；组间仍按阅读顺序。"""
            out = []
            for i in range(0, len(items), k):
                out += sorted(items[i:i + k], key=lambda x: len(x[1]))
            return out

        def audio_worker():
            def one(n: int, s: str) -> bool:    # 做一句：已有就补语调；停止或出错返回 False
                if job["stop"]:
                    return False
                text = re.sub(r"\s+", " ", s).strip()[:1500]
                parts = plan(job["book"], n, text, *voices)
                path = plan_path(parts)
                if path.exists():
                    if not path.with_suffix(".pros.json").exists():
                        try:
                            tts_prosody(path, text, parts[0][1], job["book"], n)   # 没有逐词时间时会先对齐
                        except Exception:  # noqa: BLE001
                            pass
                    return True
                try:
                    generate_plan(path, parts)
                except HTTPException as ex:
                    job["error"], job["stop"] = str(ex.detail), True
                    return False
                except Exception as ex:  # noqa: BLE001
                    job["error"], job["stop"] = str(ex), True
                    return False
                bump("audio")
                try:
                    tts_prosody(path, text, parts[0][1], job["book"], n)   # 先逐词对齐，再算语调
                except Exception:  # noqa: BLE001
                    pass   # 对齐失败不影响朗读，前端会按字符位置估算
                return True

            # 不等对白归属：引号外的句子（约一半）不管归属做没做，计划都是整句旁白、缓存键一样，立刻开始做；
            # 落在引号区间里的句子等归属做完（cast_worker 置位 job["speech"]）再按说话人配音。
            # AUDIO_PAR 句同时送朗读服务：它把同时到的句子攒成一批生成（实测批 ×3 每句 1.9 s，单句 2.7 s），
            # 对齐和语调在各自线程里接着做，不再和生成串行
            later = speech.quoted_sents(db, job["book"], sents)
            if not pool(by_length([x for x in sents if x not in later]), one, AUDIO_PAR):
                return
            job["speech"].wait(None if job.get("whole") else 600)   # 整本的：引号里的句子放在最后，等全书归属做完再按说话人配
            pool(by_length([x for x in sents if x in later]), one, AUDIO_PAR)

        def tr_worker():
            fails = [0]
            mode = ["en"]

            def one(_n: int, s: str) -> bool:
                text = s[:3000]
                if zh_skip(text, mode[0]) or db.one("SELECT 1 FROM ai_cache WHERE key=?", (tr_key(text),)):
                    return True
                try:
                    translate_text(text, names, mode[0])
                    fails[0] = 0
                except Exception as ex:  # noqa: BLE001
                    fails[0] += 1
                    if fails[0] >= 3:
                        job["error"], job["stop"] = f"译文生成失败：{ex}（需要本机 Ollama 在运行）", True
                        return False
                    return True
                bump("tr")
                return True

            def note(_n: int, s: str) -> bool:
                if is_zh(s):
                    return True                     # 文言句的讲解和译文一起出了，白话句没有
                try:
                    notes_text(s[:3000])
                except Exception:  # noqa: BLE001
                    pass
                return True
            # 先让人物抽取、对白归属用模型（约 8 次调用、几分钟；和译文抢时它们会超时，整章退化成旁白），再开始译文：
            # TR_PAR 句同时问模型（Ollama 的 NUM_PARALLEL=2）；译文都好了再顺带做讲解栏的难点讲解（打开讲解栏时不用等）
            job["speech"].wait(None if job.get("whole") else 900)
            mode[0] = mode_of(job["book"])          # 读法在 cast_worker 里先定好了（中文书问一次模型）
            if pool(list(sents), one, TR_PAR):
                pool(list(sents), note, TR_PAR)

        def cast_worker():                      # 先定读法（中文书问一次模型：文言 / 白话，导图画什么），再做人物关系图：
            kind = {"map": "people"}            #   这一章（和前面缺的章）的人物与关系，模型调用和译文排队共用 Ollama
            b = db.book(job["book"])            #   然后对白归属（谁说的、性别）：音频线程先做引号外的句子，引号里的等它做完再按说话人配音
            try:
                if b:
                    kind = bookkind.ensure(st, b)
                    cast.ensure(st, b, job.get("para", job["start"]) if job.get("whole") else job["start"], stop=lambda: job["stop"])
                    speech.ensure(st, b, job["start"], job["end"], stop=lambda: job["stop"], pool=tts_voices(), exclude=set(voices))
            except Exception as ex:  # noqa: BLE001
                log.warning("人物抽取 / 对白归属失败：%s", ex)
            finally:
                job["speech"].set()
            if b and kind.get("map") == "concepts":   # 概念导图：读到的这一节和后面两节（译文少的白话书，这时 Ollama 正空着）
                concepts.ensure(st, b, job.get("para", job["start"]), stop=lambda: job["stop"], ahead=10 ** 6 if job.get("whole") else 2)

        try:
            ts = [threading.Thread(target=audio_worker, daemon=True), threading.Thread(target=tr_worker, daemon=True),
                  threading.Thread(target=cast_worker, daemon=True)]
            for t in ts:
                t.start()
            for t in ts:
                t.join()
            job["done"] = not job["stop"]
            if not job["stop"]:                 # 这一章好了：接着把全书其余章的人物、归属、音色准备好（随时可被新任务叫停）
                job["sweep"] = True
                b = db.book(job["book"])
                if b:
                    r = roles.sweep(st, b, job["start"], stop=lambda: job["stop"], pool=tts_voices(), exclude=set(voices))
                    log.info("角色配音准备 %s：%d 章%s", job["book"], r["chapters"], "，被叫停" if r["stopped"] else "")
        finally:
            job["running"] = False

    @app.get("/api/books/{bid}/prep")
    def prep_status(bid: str, start: int, end: int, voice: str = "", voice_zh: str = ""):
        st.fresh(bid)
        voice = (voice_of(voice), voice_of(voice_zh, True))
        job = st.prep_job
        if job and job["book"] == bid and job["start"] == start and job["end"] == end and job["voice"] == voice:
            audio, tr = (job["audio"], job["tr"]) if job["running"] else prep_cached(prep_sentences(bid, start, end), bid, voice)
            return {"total": job["total"], "audio": audio, "tr": tr, "running": job["running"], "error": job["error"], "done": bool(job.get("done"))}
        sents = prep_sentences(bid, start, end)
        audio, tr = prep_cached(sents, bid, voice)
        return {"total": len(sents), "audio": audio, "tr": tr, "running": False, "error": "", "done": False}

    prep_lock = threading.Lock()   # 「检查有没有在跑」和「登记任务」一起做（锁里不 await）：并发请求不能同时通过检查

    @app.post("/api/books/{bid}/prep")
    async def prep_start(bid: str, req: Request):
        await run_in_thread(lambda: st.fresh(bid))
        d = await req.json()
        start, end = int(d.get("start", 0)), int(d.get("end", 0))
        para = int(d.get("para", start) or start)   # 读到的段：概念导图从这一节抽起
        voice = (voice_of(d.get("voice")), voice_of(d.get("voice_zh"), True))   # (英文句的音色, 中文句的音色)
        with prep_lock:
            job, after = st.prep_job, None
            if job and job["running"]:
                if job["book"] == bid and job["start"] == start and job["end"] == end and job["voice"] == voice and not job.get("sweep"):
                    return {"ok": True}
                if job.get("whole"):                 # 整本预生成在跑：同一本同一音色的章已包含在内；别的不顶掉它
                    if job["book"] == bid and job["voice"] == voice:
                        return {"ok": True, "whole": True}
                    raise HTTPException(409, f"正在整本预生成《{job.get('title', '')}》，这一章先边读边生成")
                if job.get("sweep"):                 # 在扫全书的后台准备：叫停，新任务等它放锁再开始（_prep）
                    job["stop"] = True
                    after = job
                elif job["book"] == bid and job["voice"] != voice:   # 同一本书换了音色：旧音色的预生成作废
                    job["stop"] = True
                    raise HTTPException(409, "stopping the previous voice's pre-generation — try again shortly")
                else:
                    raise HTTPException(409, "pre-generating another chapter — stop it or wait for it to finish")
            why = None if after else st.busy_reason()
            if why:
                raise HTTPException(409, f"{why}，为避免同时跑两件重活，稍后再试")
            job = st.prep_job = {"running": True, "book": bid, "start": start, "end": end, "para": para, "voice": voice, "total": 0,
                                 "audio": 0, "tr": 0, "stop": False, "error": "", "done": False, "speech": threading.Event(), "after": after}
        try:                                      # 登记好了再做慢的准备；失败就撤回登记
            if not (await run_in_thread(tts_health)).get("ok"):
                raise HTTPException(503, "the speech service is not running, or still loading")
            sents = await run_in_thread(lambda: prep_sentences(bid, start, end))
            audio, tr = await run_in_thread(lambda: prep_cached(sents, bid, voice))
        except BaseException:
            job["running"] = False
            raise
        job.update(total=len(sents), audio=audio, tr=tr)
        if job["stop"]:                           # 准备期间被停掉了
            job["running"] = False
            return {"ok": True}
        threading.Thread(target=_prep, args=(job, sents), daemon=True).start()
        return {"ok": True}

    # ---- 整本预生成：从读到的这一句起往后、再回头补前面，音频（和外文书的译文）全部做好存进缓存；
    #      别的长任务（如书名翻译）在跑就排队等，随时可停；做过的句子不重做，停了再点接着做
    whole_cache: dict = {}

    def whole_status(bid: str, voice: tuple) -> dict:
        job = st.prep_job
        names = {v["id"]: v["name"] for v in tts_voices()}
        vn = [names.get(v, v) for v in voice]
        if job and job.get("whole") and job["book"] == bid and (job["running"] or job["voice"] == voice):
            return {"running": job["running"], "waiting": job.get("waiting", ""), "total": job["total"], "audio": job["audio"],
                    "tr": job["tr"], "audio0": job.get("audio0", 0), "since": job.get("since", 0), "done": bool(job.get("done")),
                    "error": job["error"], "voices": [names.get(v, v) for v in job["voice"]], "same": job["voice"] == voice}
        key = (bid, voice)
        hit = whole_cache.get(key)
        if not hit or time.time() - hit[0] > 60:
            b = db.book(bid)
            sents = prep_sentences(bid, 0, b["n_paras"] or 0, cap=None)
            audio, tr = prep_cached(sents, bid, voice)
            hit = whole_cache[key] = (time.time(), {"total": len(sents), "audio": audio, "tr": tr})
            while len(whole_cache) > 20:
                whole_cache.pop(next(iter(whole_cache)))
        other = job and job.get("running") and job.get("whole") and job["book"] != bid
        return {**hit[1], "running": False, "waiting": "", "done": hit[1]["audio"] >= hit[1]["total"], "error": "",
                "voices": vn, "same": True, "busy": f"正在整本预生成《{job.get('title', '')}》" if other else ""}

    @app.get("/api/books/{bid}/prep-all")
    def prep_all_status(bid: str, voice: str = "", voice_zh: str = ""):
        st.fresh(bid)
        return whole_status(bid, (voice_of(voice), voice_of(voice_zh, True)))

    @app.post("/api/books/{bid}/prep-all")
    async def prep_all_start(bid: str, req: Request):
        b = await run_in_thread(lambda: st.fresh(bid))
        d = await req.json()
        para = max(0, int(d.get("para", 0) or 0))
        voice = (voice_of(d.get("voice")), voice_of(d.get("voice_zh"), True))
        if not (await run_in_thread(tts_health)).get("ok"):
            raise HTTPException(503, "the speech service is not running, or still loading")
        n = b["n_paras"] or 0
        sents = await run_in_thread(lambda: prep_sentences(bid, 0, n, cap=None))
        k = next((i for i, (pn, _) in enumerate(sents) if pn >= para), 0)
        sents = sents[k:] + sents[:k]           # 从读到的地方起，读到书尾再回头补前面
        audio, tr = await run_in_thread(lambda: prep_cached(sents, bid, voice))
        with prep_lock:
            job, after = st.prep_job, None
            if job and job["running"]:
                if job.get("whole") and job["book"] == bid and job["voice"] == voice:
                    return {"ok": True}
                if job.get("whole"):
                    raise HTTPException(409, f"正在整本预生成《{job.get('title', '')}》，先停掉它")
                job["stop"] = True                # 按章的预生成 / 后台角色准备：叫停，整本的等它放锁再开始（整本包含这一章）
                after = job
            job = st.prep_job = {"running": True, "whole": True, "title": b["title"], "book": bid, "start": 0, "end": n, "para": para,
                                 "voice": voice, "total": len(sents), "audio": audio, "audio0": audio, "tr": tr, "since": time.time(),
                                 "stop": False, "error": "", "waiting": "", "done": False, "speech": threading.Event(), "after": after}
        whole_cache.pop((bid, voice), None)
        threading.Thread(target=_prep, args=(job, sents), daemon=True).start()
        log.info("整本预生成《%s》%s：%d 句，已有音频 %d", b["title"], voice, len(sents), audio)
        return {"ok": True}

    @app.post("/api/prep/stop")
    def prep_stop():
        if st.prep_job:
            st.prep_job["stop"] = True
        return {"ok": True}

    STOP = set("""a an the and or but if of to in on at by for with from as is am are was were be been being it its
    this that these those he she they we you i me him her them us my your his our their not no so do does did have has had
    there here what which who whom whose when where why how all any some one two very just than then too can will would
    shall should may might must""".split())
    # 虚词（介词、连词、限定词、代词、助动词、数词、语气词）：不管多冷僻都不标，正文不会被它们铺满
    FUNC = STOP | set("""about above across after against along amid among amongst around before behind below beneath beside
    besides between beyond down during except inside into near off onto out outside over past since through throughout till
    toward towards under underneath unlike until unto up upon via within without although because though unless whereas
    whether while nor yet whereupon wherein whereby hence thus therefore however moreover furthermore nevertheless otherwise
    each every either neither another such both few many much more most less least several own same other others
    myself yourself himself herself itself ourselves yourselves themselves yours hers ours theirs mine thy thee thou
    anyone anybody anything everyone everybody everything someone somebody something nobody nothing none whatever whoever
    whichever whomever could ought cannot shan won also only even still ever never always often once again quite rather
    really oh ah yes yeah three four five six seven eight nine ten hundred thousand first second third""".split())
    FUNC_POS = {"prep", "conj", "pron", "art", "aux", "num", "int", "interj", "abbr"}

    names_cache: dict = {}

    def book_names(bid: str) -> frozenset:
        """一本书里的人名、地名（小写）：句中大写出现至少 2 次、且大写比小写多的词。
        词典不分大小写（John 查到的是 john「厕所」，Daisy 是「雏菊」），只能看这本书自己怎么写。"""
        if not bid or not re.fullmatch(r"[0-9a-f]{8,40}", bid):
            return frozenset()
        b = db.book(bid)
        key = (bid, b["n_paras"] if b else 0)
        if key not in names_cache:
            cap, low = {}, {}
            for p in db.paras_range(bid, 0, 1 << 30):
                for a, z in p["sents"]:
                    for k, m in enumerate(re.finditer(r"[A-Za-z][a-z'’-]+", p["text"][a:z])):
                        w = m.group(0)
                        d = cap if w[0].isupper() else low
                        if k > 0 or d is low:              # 句首的大写不算证据
                            d[w.lower()] = d.get(w.lower(), 0) + 1
            if len(names_cache) > 20:
                names_cache.clear()
            names_cache[key] = frozenset(w for w, n in cap.items() if n >= 2 and n > low.get(w, 0))
        return names_cache[key]

    # 生词标注档位（Aa 面板）：std 标准 = 词频前 2500 名和中考词不标；more 多 = 前 1000 名和中考词不标；most 更多 = 只看前 1000 名
    LEVELS = ("std", "more", "most")

    def word_level(frq: int, tag: str) -> str | None:
        """这个词从哪一档起算生词：None = 哪一档都不标（词频前 1000 名）。"""
        zk = "zk" in tag.split()
        if 0 < frq <= 1000:
            return None
        if zk:
            return "most"
        return "more" if 0 < frq <= 2500 else "std"

    def hard_words(text: str, names: frozenset = frozenset(), level: str = "more") -> list[dict]:
        """句子里值得讲的词：按档位看词频和考试词级别（word_level）；带美式音标和释义。人名地名不算（句中大写的、
        这本书里当人名用的；不知道是哪本书时，句首大写又不是常用词也不是考试词的）。
        lv 是这个词从哪一档起标；level 只取这一档及更低档的词。"""
        out, seen = [], set()
        if not st.dict.ok:
            return out
        rank = LEVELS.index(level if level in LEVELS else "more")
        for i, raw in enumerate(re.findall(r"[A-Za-z][A-Za-z'’-]+", text)):
            w = raw.replace("’", "'").strip("'-")
            if w.lower().endswith("'s"):
                w = w[:-2]                         # 所有格：butler's → butler
            if "'" in w:
                continue                           # 缩写（don't、we've、I'd）不标，免得白占名额
            if len(w) < 3 or w.lower() in FUNC or (i > 0 and w[0].isupper()) or w.lower() in names:
                continue   # 太短、虚词、句中大写（人名地名）、这本书里的人名
            lw = w.lower()
            f = st.dict.c.execute("SELECT lemma FROM forms WHERE form=? LIMIT 1", (lw,)).fetchone()
            row = st.dict._get(f["lemma"]) if f else None
            if row is None or not row["zh"]:
                row = st.dict._get(lw)
            if row is None or not row["zh"]:
                r = st.dict.lookup(w)
                row = st.dict._get(r["word"]) if r else None
            if row is None or not row["zh"] or row["word"].lower() in seen:
                continue
            frq, tag = (row["frq"] or 0), (row["tag"] or "")
            lv = word_level(frq, tag)
            if lv is None or LEVELS.index(lv) > rank:
                continue
            pos = {m for line in row["zh"].split("\n") for m in re.findall(r"^([a-z]+)\.", line.strip())}
            if pos and pos <= FUNC_POS:
                continue   # 词典里只有虚词词性（whereupon 只是 conj.）
            if i == 0 and w[0].isupper() and not tag and not (row["collins"] or 0):
                continue   # 句首大写、词典里既不常用也不是考试词：多半是人名（John →「厕所」）
            seen.add(row["word"].lower())
            form = st.dict._get(lw) if lw != row["word"].lower() else None
            ipa = (form["us"] if form and form["us"] else "") or row["us"] or ""
            senses = [l.strip() for l in row["zh"].split("\n") if l.strip()]
            out.append({"word": w, "lemma": row["word"], "ipa": ipa, "zh": senses[0] if senses else "", "lv": lv, "senses": senses[:6]})
            if len(out) >= 12:                     # 上限只防极端长句；短语、引申义由模型另补
                break
        return out

    def short_zh(word: str, senses: list[str]) -> str:
        """词典释义压成能浮在词上方的几个字：按词形先挑词性（-ing / -ed 先看动词行，-ly 先看副词行，否则按词典顺序），
        去掉词性、<口> 之类的标记，取第一个不超过 6 字的义项；都长就截断。"""
        lw = word.lower()
        want = "v" if re.search(r"(ing|ed)$", lw) else "adv" if lw.endswith("ly") else ""
        pos = lambda l: (re.match(r"^([a-z]+)\.", l) or [None, ""])[1]
        lines = sorted(senses, key=lambda l: 0 if want and pos(l).startswith(want) else 1) if want else list(senses)
        for line in lines:
            line = re.sub(r"^(?:[a-z]+\.\s*|<[^>]*>\s*|\[[^\]]*\]\s*|=\s*)+", "", line.strip())
            parts = [p.strip() for p in re.split(r"[,，;；]", line) if p.strip()]
            if parts:
                return next((p for p in parts if len(p) <= 6), parts[0][:6])
        return ""

    def level_gloss(gl: list, text: str, names: frozenset, level: str, words: list[dict]) -> list:
        """按档位整理浮在正文上的注释：模型因为「更多」档的词表才标的单个词，这一档不要；
        这一档选中、模型又没给出中文的词（旧缓存、模型漏标），用词典释义兜底，不等模型。"""
        want = {w["word"].lower() for w in words}
        drop = {w["word"].lower() for w in hard_words(text, names, "most")} - want
        one = lambda en: re.fullmatch(r"[A-Za-z]+(?:['’][A-Za-z]+)*", en.strip())
        out = [g for g in gl if not (one(g["en"]) and g["en"].strip().lower() in drop)]
        for w in words:
            if not any(re.search(rf"(?<![A-Za-z]){re.escape(w['word'])}(?![A-Za-z])", g["en"], re.I) for g in out):
                z = short_zh(w["word"], w.get("senses") or [w["zh"]])
                if z:
                    out.append({"en": w["word"], "zh": z})
        return out
