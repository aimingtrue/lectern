"""阅读器：书页（目录、章节导读入口）、阅读页、封面，取正文和记进度的接口。"""
from __future__ import annotations

import logging
import random
import re
import threading
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response

from . import bookkind, roles, speech
from .ui import book_names
from .text import is_zh, utf16_spans
from .web import cover_html, e, json_script, page, read_pct, sv, toc4

log = logging.getLogger(__name__)


def register(app: FastAPI, st) -> None:
    cfg, db = st.cfg, st.db
    need, fresh_or_old = st.need, st.fresh_or_old

    # ============================================================ 页面
    @app.get("/")
    def home():
        return RedirectResponse("/shelf")

    @app.get("/book/{bid}", response_class=HTMLResponse)
    def book_page(bid: str):
        b = fresh_or_old(bid)
        toc = toc4(b)
        rows = db.q("SELECT * FROM guides WHERE book=?", (bid,))
        # 导读按「目录第几项」存：同一项、标题也对得上的直接用；重新解析后目录挪了位置的，按章节标题找回来
        guides = {r["chapter"]: r for r in rows if r["chapter"] < len(toc) and (r["title"] or "") == toc[r["chapter"]][1]}
        left = [r for r in rows if guides.get(r["chapter"]) is not r]
        for i, (lv, t, pg, pn) in enumerate(toc):
            if i not in guides and (r := next((r for r in left if (r["title"] or "") == t), None)):
                guides[i] = r
                left.remove(r)
        pct = read_pct(b) if b["last_read"] else 0
        start = f"/read/{bid}"
        toc_html = "".join(
            f'<li style="padding-left:{(lv - 1) * 16}px"><a href="/read/{bid}?para={pn}">{e(t)}</a>'
            f'{"<a class=tag href=#g" + str(i) + ">Guide</a>" if i in guides else ""}<span class="pg">{pg + 1}</span></li>'
            for i, (lv, t, pg, pn) in enumerate(toc))
        chapters = [(i, t, pg) for i, (lv, t, pg, pn) in enumerate(toc)
                    if lv == 1 and t.strip().lower() != (b["title"] or "").strip().lower()]
        g_rows = "".join(
            f'<li><span>{e(t)}</span><span class="pg">from p. {pg + 1}</span>'
            + (f'<a class="btn sm" href="#g{i}">View guide</a>' if i in guides else
               f'<button class="btn sm" data-guide="{i}">Generate guide</button>') + "</li>"
            for i, t, pg in chapters[:80])
        g_html = f'<ul class="toc-list guide-list">{g_rows}</ul>' + "".join(
            f'<div class="panel" id="g{i}"><b>{e(t)}</b><div class="md">{guides[i]["md"]}</div></div>'
            for i, t, pg in chapters if i in guides)
        try:                                     # 书城里的中文书名、分类、简介、主题词；查不了也照常打开
            info = st.book_info(bid) if getattr(st, "book_info", None) else None
        except Exception as ex:  # noqa: BLE001
            log.info("store lookup failed %s: %s", bid, ex)
            info = None
        n = book_names(b, info["w"] if info else None)
        zh_line = f'<div class="zh-name">{e(n["orig"])}<span class="muted sm">{n["how"]}</span></div>' if n["orig"] else ""
        about = "".join(f'<div class="{k}">{info[k]}</div>' for k in ("desc", "subj") if info and info[k])
        body = f"""<div class="wrap">
<div class="book-head"><div>{cover_html(b, False)}</div><div>
<div class="eyebrow" style="margin-top:0">{e(n["author"] or "Unknown author")}</div><h1>{e(n["title"])}</h1>{zh_line}{info["crumb"] if info else ""}
<p class="muted">{b["pages"]} p.{" · scanned, cannot be read aloud" if not b["has_text"] else ""}</p>
<div style="max-width:420px;margin:18px 0 8px" class="progress"><i style="width:{pct}%"></i></div>
<p class="muted" style="margin:0 0 20px">{f"{pct}% read (p. {b["progress_page"] + 1})" if b["last_read"] else "Not started"}</p>
{f'<a class="btn primary" href="{start}">{"Continue" if b["last_read"] else "Start reading"}</a>' if b["has_text"] else '<span class="btn" style="opacity:.5;cursor:default">Scanned — cannot be read</span>'}

</div></div>{f'<div class="about">{about}</div>' if about else ""}
<h2>Contents<small>{len(toc)}</small></h2>{"<ul class=toc-list>" + toc_html + "</ul>" if toc else "<p class=muted>This PDF has no table of contents and no chapter headings were detected.</p>"}
{"<h2>Chapter guide<small>Written by the local model: plot, characters, hard words and expressions</small></h2>" + g_html if chapters else ""}
<div style="height:70px"></div></div>
<script>
document.querySelectorAll('[data-guide]').forEach(b=>b.onclick=async()=>{{
  b.disabled=true;b.textContent='Generating…';
  const r=await fetch('/api/books/{bid}/guide/'+b.dataset.guide,{{method:'POST'}});const j=await r.json();
  if(!r.ok){{b.disabled=false;b.textContent='Generate guide';alert(j.detail||'Failed');return}}
  const t=setInterval(async()=>{{const s=await (await fetch('/api/guide')).json();
    if(!s.running){{clearInterval(t);if(s.error)alert('Could not generate the guide: '+s.error);location.reload()}} else b.textContent='Generating… '+(s.step||'')}},4000);
}});
</script>"""
        return page(n["title"], body, "shelf", f'<link rel="stylesheet" href="{sv("cards.css")}">')     # shared card and summary styles

    @app.get("/read/{bid}", response_class=HTMLResponse)
    def read_page(bid: str):
        try:
            b = st.fresh(bid)
        except HTTPException as ex:
            if ex.status_code not in (410, 422):
                raise
            r = page("Cannot open this book", f'<div class="wrap"><div class="empty">{e(ex.detail)}。'
                     f'<p><a class="btn" href="/book/{bid}">Back</a></p></div></div>')
            r.status_code = ex.status_code
            return r
        meta = {"id": bid, "title": b["title"], "author": b["author"], "pages": b["pages"],
                "n_paras": b["n_paras"] or 0, "has_text": bool(b["has_text"]), "toc": toc4(b),
                "para": b["progress_para"] or 0, "sent": b["progress_sent"] or 0, "rev": b["last_read"] or 0,
                "tab": where_now(bid)["tab"], "lang": bookkind.lang_of(db, bid), "today": db.time_since(time.strftime("%Y-%m-%d")),
                "spent": db.time_since("0000-00-00", bid),
                "mode": bookkind.mode(db, b),    # 读法：en 外文 · wy 文言 · zh 白话（library/bookkind.py）
                "mini": bool(cfg.mini_ssh), "ver": sv("reader/main.js").rsplit("v=", 1)[-1]}   # 页面脚本版本：/text 里带着，变了页面自己重载
        try:                                     # 原版 ↔ 译本（书城登记的）；查不了也照常打开
            pairs = st.pair_links(bid) if getattr(st, "pair_links", None) else []
        except Exception as ex:
            log.info("original/translation link lookup failed %s: %s", bid, ex)
            pairs = []
        meta["pair"] = pairs[0] if pairs else None
        try:                                     # 这本书还有章没做人物 / 对白归属：后台先准备着（按播放时的预生成会接手）
            if meta["has_text"] and meta["toc"]:
                roles.start(st, b, meta["para"])
        except Exception as ex:  # noqa: BLE001
            log.info("character voice prep did not start %s: %s", bid, ex)
        body = f"""<div id="app"></div>
<script>window.BOOK={json_script(meta)};</script>
<script type="module" src="{sv("reader/main.js")}"></script>"""
        return page(b["title"], body, full=True, extra_head=f'<link rel="stylesheet" href="{sv("reader.css")}">')

    # ============================================================ 资源
    cover_locks: dict = {}
    cover_guard = threading.Lock()

    @app.get("/cover/{bid}.jpg")
    def cover(bid: str):
        """封面图：扫描时只给读过的书画，其余的第一次要显示时在这里现画（3.4 万本书只有几本会被打开，
        不必预先画 1.4 GB）；画不出的（自带的判过是错图、又没有外部封面、文件不在了）给文字封面。"""
        p = cfg.covers / f"{bid}.jpg"
        if not p.exists():
            b = db.one("SELECT id, title, author, path FROM books WHERE id=?", (bid,))
            if b is None:
                raise HTTPException(404)
            with cover_guard:
                lk = cover_locks.setdefault(bid, threading.Lock())
            with lk:                                # 同一本书同时来几个请求只画一次
                if not p.exists() and b["path"] and Path(b["path"]).exists():
                    try:
                        st.scanner.draw_cover(bid, Path(b["path"]))
                    except Exception as ex:  # noqa: BLE001
                        log.info("cover render failed %s: %s", bid, ex)
            with cover_guard:
                cover_locks.pop(bid, None)
        if not p.exists():
            b = db.one("SELECT id, title, author FROM books WHERE id=?", (bid,))
            from .ui import typo_svg
            return Response(typo_svg({"id": b["id"], "kind": "book", "title": b["title"], "author": b["author"]}),
                            media_type="image/svg+xml", headers={"Cache-Control": "no-cache"})
        return FileResponse(p, headers={"Cache-Control": "public, max-age=604800"})

    @app.get("/api/health")
    def health():
        return {"ok": True, "books": db.one("SELECT COUNT(*) n FROM books")["n"], "dict": st.dict.ok}

    # ============================================================ 阅读 API
    @app.get("/api/books/{bid}/text")
    def book_text(bid: str, start: int = Query(0, alias="from"), count: int = 40):
        """第 from 段起的 count 段（1..200）。sents 是 UTF-16 偏移：前端 text.slice(a, b) 就是这一句。"""
        b = st.fresh(bid)
        count = max(1, min(count, 200))
        start = max(0, start)
        rows = db.paras_range(bid, start, start + count)
        narrator = speech.for_text(db, bid, rows, getattr(st, "tts_voices", lambda: [])())   # 对白：[[a, b, 人物, 性别, 音色名, 是否叙述者]]
        paras = [{"n": p["n"], "page": p["page"], "kind": p["kind"], "text": p["text"],
                  "sents": utf16_spans(p["text"], p["sents"]), "speech_ok": p["speech_ok"],
                  "speech": [utf16_spans(p["text"], [(q[0], q[1])])[0] + q[2:] for q in p["speech"]]} for p in rows]
        return {"total": b["n_paras"] or 0, "paras": paras, "narrator": narrator, "ver": sv("reader/main.js").rsplit("v=", 1)[-1]}

    def where_now(bid: str) -> dict:
        b = db.one("SELECT progress_para, progress_sent, last_read FROM books WHERE id=?", (bid,))
        w = db.one("SELECT tab FROM progress_log WHERE book=? AND at=? AND ok=1", (bid, b["last_read"])) if b["last_read"] else None
        return {"para": b["progress_para"] or 0, "sent": b["progress_sent"] or 0, "rev": b["last_read"] or 0,
                "tab": w["tab"] if w else None}

    @app.get("/api/books/{bid}/progress")
    def progress_now(bid: str):
        """服务端记的进度、版本（rev = last_read）和写它的页面（tab）：页面回到前台时比一比，别的页面读到别处去了就接上。"""
        need(bid)
        return where_now(bid)

    lengths: dict = {}
    WORD = re.compile(r"[A-Za-z0-9]+(?:['’][A-Za-z]+)*|[\u3400-\u9fff\uf900-\ufaff]")   # 和 util.js 的 tokensOf 一致

    @app.get("/api/books/{bid}/length")
    def book_length(bid: str):
        """每段的朗读量，阅读页按现在的语速、句间间隔算本章 / 全书要读多久：a = 原速音频秒数 ×10（按字数估，
        英文 14 字 / 秒、中文 4.5 字 / 秒，和流式播放的估算一致；页面再按实测校准），s = 句数，
        w = 各句词数之和（每句封顶 40：停顿按每词 0.1 s、最多加 4 s 算；中文约两字一词）。"""
        b = st.fresh(bid)
        n = b["n_paras"] or 0
        key = (bid, n, b["parser_ver"])
        if lengths.get(bid, (None,))[0] != key:
            a_, s_, w_ = [], [], []
            for p in db.paras_range(bid, 0, n):
                t, sec, words = p["text"], 0.0, 0
                for x, y in p["sents"]:
                    seg = t[x:y]
                    zh = is_zh(seg)
                    sec += len(seg) / (4.5 if zh else 14)
                    k = len(WORD.findall(seg))
                    words += min(40, round(k / 2) if zh else k)
                a_.append(round(sec * 10))
                s_.append(len(p["sents"]))
                w_.append(words)
            while len(lengths) >= 20:
                lengths.pop(next(iter(lengths)))
            lengths[bid] = (key, {"a": a_, "s": s_, "w": w_})
        return lengths[bid][1]

    @app.post("/api/books/{bid}/progress")
    async def progress(bid: str, req: Request):
        """记进度。rev 是页面上次和服务端对上时的 last_read，tab 是页面自己的编号：之后别的页面（mini、另一个标签）
        写过，这个页面停在旧位置，就不许它盖掉（409，带回服务端的进度）——开着没关的旧标签页一切走、一重载，
        会把 mini 上读到的第六章写回第五章。服务端那条是这个页面自己写的（隐藏时用 sendBeacon 存，看不到回应，
        rev 没跟上）照样能写。force：这个页面正在朗读，用户就在这儿，以它为准。
        不带 rev 的（升级前打开的页面）照旧直接写。每次都记进 progress_log，被拒的也记（ok=0）。"""
        b = need(bid)
        d = await req.json()
        n = b["n_paras"] or 0
        para = max(0, min(int(d.get("para", 0) or 0), max(0, n - 1)))
        sent = max(0, int(d.get("sent", 0) or 0))
        rev, tab = d.get("rev"), d.get("tab")
        tab = str(tab)[:40] if tab else None
        now = time.time()
        sql = "UPDATE books SET last_read=?, progress_para=?, progress_sent=?, progress_page=?, progress_frac=? WHERE id=?"
        args = [now, para, sent, db.para_page(bid, para), para / n if n else 0, bid]
        if rev is not None and not d.get("force"):
            try:
                rev = float(rev)
            except (TypeError, ValueError):
                raise HTTPException(422, "malformed rev")
            sql += (" AND (last_read IS NULL OR last_read <= ? OR (? IS NOT NULL AND EXISTS("
                    "SELECT 1 FROM progress_log l WHERE l.book=books.id AND l.at=books.last_read AND l.tab=? AND l.ok=1)))")
            args += [rev, tab, tab]
        ok = db.run(sql, args).rowcount > 0
        db.run("INSERT INTO progress_log(book, at, para, sent, tab, ok) VALUES(?,?,?,?,?,?)", (bid, now, para, sent, tab, int(ok)))
        if random.random() < 0.002:
            db.run("DELETE FROM progress_log WHERE at < ?", (now - 60 * 86400,))
        if not ok:
            log.info("progress not saved: %s tab %s at %s/%s, server already has %s from elsewhere", bid, tab, para, sent, where_now(bid))
            return JSONResponse(where_now(bid), status_code=409)
        return {"ok": True, "rev": now}
