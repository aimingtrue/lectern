"""视频图层：把朗读页做成可以叠到 16:9 成片上的英文图层（给 ~/dev/lecture-hall 的讲堂视频用）。

  /overlay                    图层页面：透明背景、1920×1080，脚本 static/overlay/main.js；
                              外面（无头浏览器）调 OV.load(spec) 装一段，再逐帧 OV.seek(t) 截图
  POST /api/overlay/data      一段句子的图层数据：英英页边注释、英文人物网、每句的说话人、对白区间和提到的人
  POST /api/overlay/prosody   一句视频音频的语调（library/prosody.py，和朗读页的音高曲线同一份算法）：
                              {path: data/tmp-lecture/ 下的 wav, text, words: [[词, 起, 止]]}

注释和人物网都面向英语母语的观众：不给中文。模型结果进 ai_cache（ov1: 前缀），同一句、同一份人物表只问一次。
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse

from . import cast as castmod
from . import prosody
from .llm import LLMError
from .web import e, run_in_thread, sv

log = logging.getLogger(__name__)

GLOSS = """You annotate a classic English book for adult viewers of a read-aloud video (the book: {title}).
Pick at most 2 words or short phrases in the sentence that a general adult reader today may not know or may misread:
archaic or literary words, period slang, allusions, words used in an old sense. Skip common words and names of people or places.
For each, explain what it means as used in this sentence, in plain English, at most 14 words, no quotation marks.
Output JSON only: {{"notes":[{{"term":"the exact words as they appear in the sentence","def":"the meaning"}}]}}
Give an empty array when nothing needs explaining."""

CAST_EN = """Translate the descriptions in this cast list of the book {title} into short English for a video caption.
Input JSON has people (name, role in Chinese) and links (a, b, rel in Chinese).
Output JSON only: {{"roles":{{"<name>":"who this person is, at most 8 English words"}},"rels":[["<a>","<b>","relation, at most 3 English words"]]}}
Keep every name exactly as given. Do not add people or links."""


def register(app: FastAPI, st) -> None:
    db, cfg = st.db, st.cfg

    def cached(key: str, fn, ok=lambda v: True):
        r = db.one("SELECT value FROM ai_cache WHERE key=?", (key,))
        if r:
            return json.loads(r["value"])
        v = fn()
        if ok(v):
            db.run("INSERT OR REPLACE INTO ai_cache VALUES(?,?,?)", (key, json.dumps(v, ensure_ascii=False), time.time()))
        return v

    def gloss(title: str, text: str) -> list[dict]:
        """一句的页边注释 [{term, a, b, def}]：a、b 是 term 在句子里的位置；模型给的词在句子里找不到就丢掉。"""
        def run():
            r = st.llm.chat_json(cfg.model, GLOSS.format(title=title), f"Sentence: {text}", num_ctx=4096, temperature=0)
            return {"notes": [{"term": str(n.get("term", "")).strip(), "def": str(n.get("def", "")).strip()}
                              for n in (r.get("notes") or []) if isinstance(n, dict)][:2]}
        key = "ov1:g:" + hashlib.sha1(f"{cfg.model}|{title}|{text}".encode()).hexdigest()
        notes = cached(key, run, ok=lambda v: isinstance(v, dict) and isinstance(v.get("notes"), list)).get("notes") or []
        out = []
        for n in notes:
            t, d = n.get("term") or "", n.get("def") or ""
            m = re.search(rf"(?<![A-Za-z]){re.escape(t)}(?![A-Za-z])", text, re.I) if t and d else None
            if m:
                out.append({"term": m.group(0), "a": m.start(), "b": m.end(), "def": d[:1].upper() + d[1:].rstrip(".") + "."})
        return sorted(out, key=lambda x: x["a"])

    def cast_en(title: str, c: dict) -> dict:
        """人物网换成英文说明：role、rel 原来是中文（library/cast.py 为中国读者抽的），这里整份翻一次。"""
        people = [{"name": p["name"], "role": p.get("role", "")} for p in c["people"]]
        links = [{"a": l["a"], "b": l["b"], "rel": l.get("rel", "")} for l in c["links"]]
        src = json.dumps({"people": people, "links": links}, ensure_ascii=False)

        def run():
            return st.llm.chat_json(cfg.model, CAST_EN.format(title=title), src, num_ctx=6144, temperature=0)
        key = "ov1:c:" + hashlib.sha1(f"{cfg.model}|{title}|{src}".encode()).hexdigest()
        r = cached(key, run, ok=lambda v: isinstance(v, dict) and isinstance(v.get("roles"), dict))
        roles = r.get("roles") or {}
        rels = {(x[0], x[1]): x[2] for x in (r.get("rels") or []) if isinstance(x, list) and len(x) == 3}
        return {"people": [{"name": p["name"], "aliases": p["aliases"], "n": p.get("n", 1),
                            "role": str(roles.get(p["name"], "")).strip()} for p in c["people"]],
                "links": [{"a": l["a"], "b": l["b"], "rel": str(rels.get((l["a"], l["b"]), "")).strip()} for l in c["links"]],
                "narrator": c.get("narrator")}

    def speaker(bid: str, para: int, text: str, people: list[dict], narrator: str | None) -> tuple[str | None, list]:
        """这一句是谁在说，和句子里每段对白归谁 [[起, 止, 人物]]（句内位置）：句子大半落在某条对白里就是那条对白的说话人
        （library/speech.py 归属的），否则是叙述者。对白区间给讲堂旁白分音色用：引号里的部分换成角色的声音。"""
        p = db.one("SELECT text FROM paras WHERE book=? AND n=?", (bid, para))
        row = db.one("SELECT json FROM speech WHERE book=? AND para=?", (bid, para))
        if not p or not row:
            return narrator, []
        m = re.search(r"\s+".join(re.escape(w) for w in text.split()), p["text"])
        if not m:
            return narrator, []
        best, most, quotes = None, 0, []
        for q in json.loads(row["json"]):
            a, b = max(q["a"], m.start()), min(q["b"], m.end())
            if b <= a:
                continue
            who = castmod.mentions(q.get("who") or "", people)      # 归属写的是称呼：归到人物表里的那个人
            name = who[0]["name"] if who else q.get("who")
            quotes.append([min(a - m.start(), len(text)), min(b - m.start(), len(text)), name])   # 句子按单个空格规整过，位置可能差几个字
            if b - a > most:
                best, most = name, b - a
        if not best or most < 0.4 * (m.end() - m.start()):
            return narrator, quotes
        return best, quotes

    def data(bid: str, sents: list[dict]) -> dict:
        b = st.need(bid)
        title = b["title"] or ""
        paras = [int(s.get("para", 0)) for s in sents]
        cur = castmod.chapter_at(b, max(paras) if paras else 0)
        if cur:
            rows = db.q("SELECT json FROM cast WHERE book=? AND chapter<=? ORDER BY chapter", (bid, cur[0]))
            c = castmod.merge([json.loads(r["json"]) for r in rows])
        else:
            c = {"people": [], "links": [], "narrator": None}
        people = cast_en(title, c) if c["people"] else {"people": [], "links": [], "narrator": None}
        out = []
        for s, para in zip(sents, paras):
            text = re.sub(r"\s+", " ", str(s.get("text", ""))).strip()
            who, quotes = speaker(bid, para, text, people["people"], people["narrator"]) if people["people"] else (None, [])
            out.append({"gloss": gloss(title, text) if text else [],
                        "hits": castmod.mentions(text, people["people"]) if people["people"] else [],
                        "who": who, "quotes": quotes})
        return {"title": title, "author": b["author"] or "", "chapter": cur[1] if cur else "", "cast": people, "sents": out}

    @app.post("/api/overlay/data")
    async def overlay_data(req: Request):
        d = await req.json()
        bid, sents = str(d.get("book") or ""), d.get("sents") or []
        if not re.fullmatch(r"[0-9a-f]{8,40}", bid) or not isinstance(sents, list) or not sents:
            raise HTTPException(400, "book and sents are required")
        try:
            return await run_in_thread(lambda: data(bid, sents[:200]))
        except LLMError as ex:
            raise HTTPException(503, str(ex))

    @app.post("/api/overlay/prosody")
    async def overlay_prosody(req: Request):
        """讲堂视频的句子音频不在朗读缓存里，所以由调用方放到 data/tmp-lecture/ 再来算；只读那个目录下的文件。"""
        d = await req.json()
        root = (cfg.data / "tmp-lecture").resolve()
        path = Path(str(d.get("path") or "")).resolve()
        if root not in path.parents or path.suffix != ".wav" or not path.exists():
            raise HTTPException(400, "path must be an existing wav under data/tmp-lecture/")
        text, words = str(d.get("text") or ""), d.get("words") or []

        def ipa_of(w: str):
            r = st.dict.lookup(w) if st.dict.ok else None
            return ((r.get("form") or {}).get("us") or r.get("us")) if r else None
        r = await run_in_thread(lambda: prosody.analyze(path, words, text, ipa_of, st.dict.lookup if st.dict.ok else None, "us", None))
        return {"prosody": r}

    @app.get("/overlay", response_class=HTMLResponse)
    def overlay_page():
        return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><title>{e("Overlay")}</title>
<link rel="stylesheet" href="{sv("overlay/overlay.css")}"></head>
<body><div id="ov"></div><script type="module" src="{sv("overlay/main.js")}"></script></body></html>"""
