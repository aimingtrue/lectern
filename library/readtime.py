"""阅读时长：阅读页按「页面可见且 3 分钟内有操作，或正在朗读」计秒，攒一会儿报上来，按（本地日期, 书）累计。

  POST /api/books/{id}/time   {sec, listen}：加上这段时长（listen 是其中朗读的秒数），返回今天一共读了多久
  GET  /api/stats             今日 / 本周（周一起）/ 累计、最近 30 天每天、按书分布
  /shelf/stats                上面这些的页面（从书架进）
"""
from __future__ import annotations

import datetime as dt
import html

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse

from .web import page, sv

e = html.escape
MAX_SEC = 120                  # 一次最多报这么多秒：前端每 30 秒报一次，多出来的是异常数据
DAYS = 30


def dur(sec: int) -> str:
    """时长：25 分钟、2 小时 15 分；不到 1 分钟记 0 分钟。"""
    m = int(sec or 0) // 60
    if m < 60:
        return f"{m} min"
    return f"{m // 60} h" + (f" {m % 60} min" if m % 60 else "")


def summary(db, today: dt.date | None = None) -> dict:
    today = today or dt.date.today()
    monday = today - dt.timedelta(days=today.weekday())
    start = today - dt.timedelta(days=DAYS - 1)
    per = {r["day"]: r for r in db.q("SELECT day, SUM(sec) sec, SUM(listen) listen FROM reading_log "
                                     "WHERE day>=? GROUP BY day", (start.isoformat(),))}
    days = []
    for i in range(DAYS):
        d = (start + dt.timedelta(days=i)).isoformat()
        r = per.get(d)
        days.append({"day": d, "sec": r["sec"] if r else 0, "listen": r["listen"] if r else 0})
    tot = db.one("SELECT COALESCE(SUM(sec), 0) sec, COALESCE(SUM(listen), 0) listen FROM reading_log")
    books = [{"id": r["book"], "title": r["title"], "author": r["author"] or "", "sec": r["sec"],
              "listen": r["listen"], "last_day": r["last_day"]}
             for r in db.q("SELECT l.book, b.title, b.author, SUM(l.sec) sec, SUM(l.listen) listen, MAX(l.day) last_day "
                           "FROM reading_log l LEFT JOIN books b ON b.id=l.book GROUP BY l.book "
                           "HAVING SUM(l.sec) > 0 ORDER BY sec DESC")]
    return {"today": db.time_since(today.isoformat()), "week": db.time_since(monday.isoformat()),
            "total": tot["sec"], "listen": tot["listen"], "days": days, "books": books}


def tiles(s: dict) -> str:
    """书架和统计页顶上的格子：今日 / 本周 / 累计。"""
    return "".join(f'<div><b>{dur(s[k])}</b><span>{label}</span></div>'
                   for k, label in (("today", "Today"), ("week", "This week"), ("total", "All time")))


def name(b: dict) -> str:
    if b["title"] is None:                     # 书已经从库里删了，时长还留着
        return '<span class="muted">removed book</span>'
    return f'<a href="/book/{e(b["id"])}">{e(b["title"])}</a>'


def stats_body(s: dict) -> str:
    top = max([d["sec"] for d in s["days"]] + [60])
    bars = "".join(
        f'<div class="rt-day" title="{d["day"]}：{dur(d["sec"])}'
        f'{" (" + dur(d["listen"]) + " read aloud)" if d["listen"] >= 60 else ""}">'
        f'<i style="height:{100 * d["sec"] / top:.1f}%"><u style="height:{100 * d["listen"] / max(d["sec"], 1):.1f}%"></u></i>'
        f'<span>{int(d["day"][8:]) if i % 5 == 0 or i == len(s["days"]) - 1 else ""}</span></div>'
        for i, d in enumerate(s["days"]))
    total = max(s["total"], 1)
    rows = "".join(
        f'<tr><td>{name(b)}'
        f'<div class="muted">{e(b["author"])}</div></td><td>{dur(b["sec"])}</td><td>{dur(b["listen"])}</td>'
        f'<td><span class="rt-share"><i style="width:{100 * b["sec"] / total:.1f}%"></i></span>{round(100 * b["sec"] / total)}%</td>'
        f'<td class="muted">{b["last_day"]}</td></tr>'
        for b in s["books"])
    table = (f'<table class="rt-books"><tr><th>Book</th><th>Total</th><th>Read aloud</th><th>Share</th><th>Last</th></tr>{rows}</table>'
             if rows else '<p class="muted">No reading yet. Open a book and read for a while — with or without the voice — and it will show up here.</p>')
    return (f'<div class="stats rt-tiles">{tiles(s)}<div><b>{dur(s["listen"])}</b><span>read aloud</span></div></div>'
            f'<h2>Last {DAYS} days<small>the lower part of each bar was read aloud</small></h2><div class="rt-chart">{bars}</div>'
            f'<h2>By book<small>{len(s["books"])}</small></h2>{table}')


def register(app: FastAPI, st) -> None:
    db = st.db

    @app.post("/api/books/{bid}/time")
    async def add_time(bid: str, req: Request):
        if not db.one("SELECT 1 FROM books WHERE id=?", (bid,)):
            raise HTTPException(404, "No such book")
        try:
            d = await req.json()
            sec = max(0, min(int(d.get("sec", 0) or 0), MAX_SEC))
            listen = max(0, min(int(d.get("listen", 0) or 0), sec))
        except (ValueError, TypeError, AttributeError):
            raise HTTPException(422, "malformed duration")
        if sec:
            db.add_time(bid, sec, listen)
        return {"ok": True, "today": db.time_since(dt.date.today().isoformat())}

    @app.get("/api/stats")
    def stats():
        return summary(db)

    @app.get("/shelf/stats", response_class=HTMLResponse)
    def stats_page():
        return page("Reading time", f'<div class="wrap store"><div class="eyebrow"><a href="/shelf">Shelf</a> · Reading time</div>'
                                f'<h1>Reading time</h1>{stats_body(summary(db))}<div style="height:60px"></div></div>', "shelf",
                    f'<link rel="stylesheet" href="{sv("cards.css")}">')
