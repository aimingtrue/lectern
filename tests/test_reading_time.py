"""阅读时长：上报累加、夹值、今日 / 本周 / 累计的边界、书架和统计页。"""
from __future__ import annotations

import datetime as dt
import json

from test_library import lib, make_pdf  # noqa: F401  （lib 是 fixture）
from test_security import client


def test_time_accumulates_and_is_clamped(lib):
    c, db, bid = client(lib)
    url = f"/api/books/{bid}/time"
    assert c.post(url, json={"sec": 30, "listen": 10}).json()["today"] == 30
    assert c.post(url, json={"sec": 25, "listen": 0}).json()["today"] == 55
    assert c.post(url, json={"sec": 9999, "listen": 9999}).json()["today"] == 55 + 120, "一次最多 120 秒"
    assert c.post(url, json={"sec": -5, "listen": 3}).json()["today"] == 175, "负数不算"
    row = db.one("SELECT sec, listen FROM reading_log WHERE book=?", (bid,))
    assert (row["sec"], row["listen"]) == (175, 130), "朗读秒数不超过这次的总秒数"
    assert c.post(url, json={"sec": "x"}).status_code == 422
    assert c.post("/api/books/nope/time", json={"sec": 10}).status_code == 404


def test_time_rejects_cross_site(lib):
    c, db, bid = client(lib)
    r = c.post(f"/api/books/{bid}/time", content=json.dumps({"sec": 30}),
               headers={"origin": "http://evil.example", "content-type": "text/plain"})
    assert r.status_code == 403
    assert db.one("SELECT COUNT(*) n FROM reading_log")["n"] == 0


def test_summary_today_week_total(lib):
    from library.readtime import summary
    c, db, bid = client(lib)
    today = dt.date(2026, 10, 15)                  # 星期四：本周从 10-12（周一）算
    for day, sec, listen in (("2026-10-15", 600, 300), ("2026-10-12", 1200, 0), ("2026-10-11", 3600, 60),
                             ("2026-08-01", 7200, 0)):
        db.add_time(bid, sec, listen, day)
    db.add_time("gone", 60, 0, "2026-10-15")          # 书已经删了，时长还在
    s = summary(db, today)
    assert s["today"] == 660 and s["week"] == 660 + 1200 and s["total"] == 600 + 1200 + 3600 + 7200 + 60
    assert s["listen"] == 360
    assert len(s["days"]) == 30 and s["days"][-1] == {"day": "2026-10-15", "sec": 660, "listen": 300}
    assert s["days"][0]["day"] == "2026-09-16" and s["days"][0]["sec"] == 0
    assert [b["id"] for b in s["books"]] == [bid, "gone"] and s["books"][1]["title"] is None
    assert s["books"][0]["last_day"] == "2026-10-15"


def test_shelf_and_stats_pages(lib):
    from library.readtime import dur
    assert (dur(0), dur(59), dur(25 * 60), dur(3600), dur(8100)) == ("0 min", "0 min", "25 min", "1 h", "2 h 15 min")
    c, db, bid = client(lib)
    assert "No reading yet" in c.get("/shelf/stats").text
    c.post(f"/api/books/{bid}/progress", json={"para": 1, "sent": 0})
    assert "阅读时长统计" not in c.get("/shelf").text, "没有时长时书架和原来一样"
    for _ in range(3):
        c.post(f"/api/books/{bid}/time", json={"sec": 100, "listen": 40})
    shelf = c.get("/shelf").text
    assert "Today" in shelf and "5 min" in shelf and "5 min total" in shelf and "/shelf/stats" in shelf
    r = c.get("/shelf/stats")
    assert r.status_code == 200 and "The Test Novel" in r.text and "read aloud" in r.text and "100%" in r.text
    assert c.get("/api/stats").json()["total"] == 300
    page = c.get(f"/read/{bid}").text
    assert json.loads(page.split("window.BOOK=", 1)[1].split(";</script>", 1)[0])["today"] == 300
