"""书架：管理阅读进度。

  /shelf                    阅读时长（今日 / 本周 / 累计，详见 /shelf/stats，library/readtime.py）；
                            在读（按最近阅读排序）、已读完；继续阅读、目录、标为读完、移出书架；
                            外文书有中文书名（书城的 zh_titles，library/zh.py）时中文书名在上、原书名在下；
                            下面是「为你」（有阅读口味数据时，library/foryou.py，书架空着也出）
  /api/shelf/{id}/finish    标为读完（进度移到最后一段）
  /api/shelf/{id}/remove    移出书架（清掉阅读进度）
"""
from __future__ import annotations

import html
import logging
import time

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse

from .ui import book_names, same_origin, SCROLL_JS, short
from .readtime import dur, summary, tiles
from .web import page, read_pct, sv

e = html.escape
log = logging.getLogger(__name__)
DONE = 98                      # 读到这个百分比算读完


def ago(ts: float) -> str:
    d = time.time() - (ts or 0)
    if d < 3600:
        return "just now"
    if d < 86400:
        return f"{int(d // 3600)}h ago"
    if d < 86400 * 30:
        return f"{int(d // 86400)}d ago"
    return time.strftime("%Y-%m-%d", time.localtime(ts))


def item(b, done: bool, sec: int = 0, w=None) -> str:
    """w：这本书在书城里的作品（外文书有中文书名时中文书名在上、原书名小字在下，作者「中文名 · 原名」）。"""
    pct = read_pct(b)
    n = book_names(b, w)
    acts = (f'<a class="btn sm" href="/read/{b["id"]}?para=0">Read again</a>' if done else
            f'<a class="btn primary sm" href="/read/{b["id"]}">Continue</a>'
            f'<button class="btn sm" data-act="finish" data-id="{b["id"]}">Mark finished</button>')
    return (f'<div class="shelf-item" id="b{b["id"]}"><a class="cover" href="/read/{b["id"]}">'
            f'<img loading="lazy" src="/cover/{b["id"]}.jpg" alt=""></a><div class="info">'
            f'<a class="t" href="/book/{b["id"]}">{e(short(n["title"]))}</a>'
            + (f'<div class="tz">{e(short(n["orig"]))}</div>' if n["orig"] else "")
            + f'<div class="a">{e(n["author"])}</div>'
            f'<div class="progress"><i style="width:{pct}%"></i></div>'
            f'<div class="meta">{"Finished" if done else f"{pct}% read"} · {ago(b["last_read"])}'
            f'{" · " + dur(sec) + " total" if sec >= 60 else ""}</div>'
            f'<div class="acts">{acts}<a class="btn sm" href="/book/{b["id"]}">Contents</a>'
            f'<button class="btn sm ghost" data-act="remove" data-id="{b["id"]}">Remove</button></div></div></div>')


JS = """<script>
document.querySelectorAll('[data-act]').forEach(b=>b.onclick=async()=>{
  const act=b.dataset.act;
  if(act==='remove'&&!confirm('Removing it clears this book\u2019s reading progress. Continue?'))return;
  b.disabled=true;
  const r=await fetch('/api/shelf/'+b.dataset.id+'/'+act,{method:'POST',headers:{'content-type':'application/json'},body:'{}'});
  if(r.ok)location.reload();else{b.disabled=false;alert((await r.json()).detail||'Action failed')}
});
</script>"""


def register(app: FastAPI, st) -> None:
    db = st.db
    head = f'<link rel="stylesheet" href="{sv("cards.css")}">'          # shared card styles

    def need(bid: str):
        b = db.book(bid)
        if not b:
            raise HTTPException(404, "No such book")
        return b

    def works_of(bids: list[str]) -> dict:
        """书 id → 书城里的作品（中文书名等）；书城查不了时空，书架照常显示阅读库里的书名。"""
        try:
            return st.book_works(bids) if bids and getattr(st, "book_works", None) else {}
        except Exception as ex:  # noqa: BLE001
            log.info("shelf could not read store info: %s", ex)
            return {}

    @app.get("/shelf", response_class=HTMLResponse)
    def shelf():
        rows = db.q("SELECT * FROM books WHERE last_read IS NOT NULL AND status='ok' ORDER BY last_read DESC")
        reading = [b for b in rows if read_pct(b) < DONE]
        done = [b for b in rows if read_pct(b) >= DONE]
        secs = {r["book"]: r["sec"] for r in db.q("SELECT book, SUM(sec) sec FROM reading_log GROUP BY book")}
        ws = works_of([b["id"] for b in rows])
        if not rows:
            body = ('<div class="empty"><p style="font-size:17px;margin:0 0 8px">Your shelf is empty</p>'
                    '<p class="muted">Scan a folder of books, open one, and it will appear here.</p>'
                    '</div>')
        else:
            body = (f'<h2>Reading<small>{len(reading)}</small></h2>'
                    + ('<div class="shelf-list">' + "".join(item(b, False, secs.get(b["id"], 0), ws.get(b["id"]))
                                                            for b in reading) + "</div>"
                       if reading else '<p class="muted">Nothing in progress.</p>')
                    + (f'<h2>Finished<small>{len(done)}</small></h2><div class="shelf-list">'
                       + "".join(item(b, True, secs.get(b["id"], 0), ws.get(b["id"])) for b in done) + "</div>"
                       if done else ""))
        s = summary(db)
        stats = (f'<div class="stats rt-tiles"><div><b>{len(reading)}</b><span>reading</span></div>'
                 f'<div><b>{len(done)}</b><span>finished</span></div>'
                 + (f'{tiles(s)}<div><a class="btn sm" href="/shelf/stats">Reading time</a></div>' if s["total"] else "")
                 + '</div>') if rows else ""
        fy = st.for_you() if getattr(st, "for_you", None) else ""      # 没有口味数据时是空串，页面和原来一样
        return page("Shelf", f'<div class="wrap store"><div class="eyebrow">Shelf</div><h1>My reading</h1>{stats}'
                            f'{body}{fy}<div style="height:60px"></div></div>{JS}' + (SCROLL_JS if fy else ""), "shelf", head)

    @app.post("/api/shelf/{bid}/finish")
    def finish(bid: str, req: Request):
        same_origin(req)
        b = need(bid)
        n = b["n_paras"] or 0
        db.run("UPDATE books SET last_read=?, progress_para=?, progress_sent=0, progress_frac=1 WHERE id=?",
               (time.time(), max(0, n - 1), bid))
        return {"ok": True}

    @app.post("/api/shelf/{bid}/remove")
    def remove(bid: str, req: Request):
        same_origin(req)
        need(bid)
        db.run("UPDATE books SET last_read=NULL, progress_para=0, progress_sent=0, progress_page=0, progress_frac=0 "
               "WHERE id=?", (bid,))
        return {"ok": True}
