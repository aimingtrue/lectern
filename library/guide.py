"""章节导读：本机大模型按章写情节、人物、难词与表达（长任务，严格串行）。"""
from __future__ import annotations

import logging
import threading
import time

from fastapi import FastAPI, HTTPException

from . import busy

from .web import toc4

log = logging.getLogger(__name__)
_start = threading.Lock()              # 「检查有没有在跑」和「登记任务」之间不让别的请求插进来


def register(app: FastAPI, st) -> None:
    # ---- 章节导读（长任务：严格串行）
    @app.post("/api/books/{bid}/guide/{chapter}")
    def start_guide(bid: str, chapter: int):
        b = st.fresh(bid)
        chaps = [(i, t, pn) for i, (lv, t, pg, pn) in enumerate(toc4(b)) if lv == 1]
        idx = next((k for k, c in enumerate(chaps) if c[0] == chapter), None)
        if idx is None:
            raise HTTPException(404, "no such chapter")
        _, title, start = chaps[idx]          # 按段落序号取这一章：[本章首段, 下一章首段)
        end = chaps[idx + 1][2] if idx + 1 < len(chaps) else (b["n_paras"] or 0)
        with _start:
            why = st.busy_reason()
            if why:
                raise HTTPException(409, f"{why}，为避免同时跑两件重活，稍后再试")
            st.guide_job = {"running": True, "book": bid, "chapter": chapter, "step": ""}
        threading.Thread(target=_guide, args=(st, b, chapter, title, start, max(end, start + 1)), daemon=True).start()
        return {"ok": True}

    @app.get("/api/guide")
    def guide_status():
        return st.guide_job or {"running": False}


GUIDE_MAP = """你在帮中国的英语学习者读英文原著《{title}》。下面是其中「{chapter}」的一部分原文（可能有 PDF 换行瑕疵）。
用简体中文写这部分的要点笔记：发生了什么（按顺序）、出场人物及关系、值得注意的细节或伏笔。只写原文里有的，不要编造。"""
GUIDE_REDUCE = """你在帮中国的英语学习者读英文原著《{title}》。根据下面「{chapter}」各部分的笔记和原文摘录，写一份章节导读（Markdown，简体中文）：

### 一句话
### 情节梗概（按顺序，5–8 条）
### 人物
### 值得细读的段落（2–3 段：先引用英文原句，再说明好在哪里）
### 难词与表达（8–12 个：英文 — 中文意思 — 在本章里的用法）
### 读完可以想一想（2–3 个问题）

只依据给出的内容，不要编造情节。"""


def render_guide(md: str) -> str:
    """模型写的 Markdown → HTML。关掉原始 HTML（块级和行内）和链接、图片（Markdown 不过滤 javascript: 这类协议）：
    模型输出里的 &lt;script&gt;、&lt;img onerror&gt;、[x](javascript:…) 都只当文字显示。导读用不到链接和图片。"""
    from markdown import Markdown  # noqa: WPS433
    m = Markdown()
    m.preprocessors.deregister("html_block")
    for name in ("html", "link", "image_link", "reference", "image_reference", "short_reference", "short_image_ref",
                 "autolink", "automail"):
        m.inlinePatterns.deregister(name)
    return m.convert(md)


def _guide(st, b, chapter: int, title: str, start: int, end: int) -> None:
    cfg, db = st.cfg, st.db
    job = st.guide_job
    try:
        with busy.hold("chapter guide"):
            _write_guide(st, b, chapter, title, start, end, job)
    except busy.Busy as ex:
        job["error"] = str(ex)
    finally:
        job["running"] = False


def _write_guide(st, b, chapter: int, title: str, start: int, end: int, job: dict) -> None:
    cfg, db = st.cfg, st.db
    try:
        text = "\n\n".join(p["text"] for p in db.paras_range(b["id"], start, end))
        chunks = [text[i:i + 12000] for i in range(0, len(text), 12000)] or [""]
        notes = []
        for k, c in enumerate(chunks):
            job["step"] = f"{k + 1}/{len(chunks) + 1}"
            notes.append(st.llm.chat(cfg.guide_model, GUIDE_MAP.format(title=b["title"], chapter=title), c,
                                     num_ctx=16384))
        job["step"] = f"{len(chunks) + 1}/{len(chunks) + 1}"
        excerpt = text[:6000]
        md = st.llm.chat(cfg.guide_model, GUIDE_REDUCE.format(title=b["title"], chapter=title),
                         "\n\n---\n\n".join(notes) + "\n\n【原文开头摘录】\n" + excerpt, num_ctx=24576)
        db.run("INSERT OR REPLACE INTO guides VALUES(?,?,?,?,?)",
               (b["id"], chapter, title, render_guide(md), time.time()))
    except Exception as ex:  # noqa: BLE001
        log.exception("导读失败")
        job["error"] = str(ex)
    finally:
        job["running"] = False
