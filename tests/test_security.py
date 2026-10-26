"""网站安全：书名进页面脚本不能闭合 &lt;script&gt;；只接受本站页面发来的写请求；只认本机主机名；导读里的 HTML 不执行。"""
from __future__ import annotations

import json

from test_library import lib, make_pdf  # noqa: F401  （lib 是 fixture）

EVIL = 'Evil</script><script>alert(1)</script><!--'


def client(lib):
    from fastapi.testclient import TestClient
    from library.config import load
    from library.db import DB
    from library.scan import Scanner
    from library.web import create_app
    cfg = load()
    db = DB(cfg.db_path)
    Scanner(db, cfg, set()).run([str(lib / "books")])
    bid = db.one("SELECT id FROM books")["id"]
    return TestClient(create_app(cfg)), db, bid


def test_book_title_cannot_close_the_script(lib):
    c, db, bid = client(lib)
    assert c.get(f"/read/{bid}").status_code == 200          # 第一次打开会解析存全文（书名取自文件）
    db.run("UPDATE books SET title=?, author=? WHERE id=?", (EVIL, EVIL, bid))
    r = c.get(f"/read/{bid}")
    assert r.status_code == 200
    script = r.text.split("window.BOOK=", 1)[1].split(";</script>", 1)[0]
    assert "</script" not in script.lower() and "<!--" not in script, "书名里的 </script> 闭合了脚本"
    assert json.loads(script)["title"] == EVIL, "转义后仍是同一个书名"


def test_cross_site_writes_are_rejected(lib):
    c, db, bid = client(lib)
    url = f"/api/books/{bid}/progress"
    body = json.dumps({"para": 3, "sent": 1})
    # 别的网页发来的「简单请求」（text/plain 不触发预检）
    r = c.post(url, content=body, headers={"origin": "http://evil.example", "content-type": "text/plain"})
    assert r.status_code == 403
    r = c.post(url, content=body, headers={"sec-fetch-site": "cross-site", "content-type": "application/json"})
    assert r.status_code == 403
    r = c.post("/api/translate", json={"text": "Hello there."}, headers={"origin": "null"})
    assert r.status_code == 403, "沙箱页面 / file:// 的 Origin 是 null"
    assert db.book(bid)["progress_para"] == 0
    # 本站页面、命令行工具（没有 Origin）照常
    r = c.post(url, content=body, headers={"origin": "http://testserver", "content-type": "application/json",
                                         "sec-fetch-site": "same-origin"})
    assert r.status_code == 200 and db.book(bid)["progress_para"] == 3
    assert c.post(url, json={"para": 4, "sent": 0}).status_code == 200


def test_only_local_host_names(lib):
    """DNS 重绑定：攻击者的域名解析到 127.0.0.1 时，浏览器认为是同源；靠 Host 头拒绝。"""
    c, db, bid = client(lib)
    assert c.get("/api/health", headers={"host": "evil.example:8791"}).status_code == 403
    r = c.post(f"/api/books/{bid}/progress", json={"para": 2, "sent": 0},
               headers={"host": "evil.example:8791", "origin": "http://evil.example:8791"})
    assert r.status_code == 403 and db.book(bid)["progress_para"] == 0
    for h in ("127.0.0.1:8791", "localhost:8791", "[::1]:8791", "127.0.0.1"):
        assert c.get("/api/health", headers={"host": h}).status_code == 200, h


def test_guide_html_is_not_executed():
    from library.guide import render_guide
    out = render_guide("### 一句话\n\n<script>alert(1)</script>\n\n**粗体** <img src=x onerror=alert(1)> & 更多\n\n"
                       "[点我](javascript:alert(1)) ![图](x.png) <http://evil.example>")
    assert "<script" not in out and "<img" not in out and "<a " not in out, out
    assert "&lt;script&gt;" in out and "&lt;img src=x onerror=alert(1)&gt;" in out, "只当文字显示"
    assert "<h3>一句话</h3>" in out and "<strong>粗体</strong>" in out, "Markdown 本身照常渲染"


def test_mini_button_follows_config_and_only_takes_book_ids(lib):
    """「投到 mini」：没设置 mini_ssh 不显示按钮、接口 404；设置了也只接受书库里有的书 id（拼进 ssh 命令的只有它）。"""
    c, db, bid = client(lib)
    meta = json.loads(c.get(f"/read/{bid}").text.split("window.BOOK=", 1)[1].split(";</script>", 1)[0])
    assert meta["mini"] is False
    assert c.post("/api/mini/open", json={"book": bid}).status_code == 404

    from fastapi.testclient import TestClient
    from library.config import load
    from library.web import create_app
    cfg = load()
    cfg.mini_ssh = "macmini-test"
    c2 = TestClient(create_app(cfg))
    calls = []
    c2.app.state.lib.mini.ensure = lambda: "已通"
    c2.app.state.lib.mini.open = lambda path: calls.append(path) or "reused"
    assert c2.post("/api/mini/open", json={"book": "'; rm -rf ~; '"}).status_code == 404
    r = c2.post("/api/mini/open", json={"book": bid})
    assert r.status_code == 200 and r.json()["tab"] == "reused"
    assert calls == [f"/read/{bid}"]
    assert c2.post("/api/mini/open", json={"book": bid}, headers={"origin": "http://evil.example"}).status_code == 403
