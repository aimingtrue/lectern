"""可靠性（架构评审 P2）：长任务互斥锁、查不清时按忙处理、导读跟着章节标题走、删书连导读一起删、
导读「检查—登记」不被并发请求插队、解析子进程在主进程意外退出后自己退出。"""
from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from test_library import lib, make_pdf  # noqa: F401  （lib 是 fixture）

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture()
def quiet_caption(monkeypatch):
    """这些测试不看字幕项目的进程（本机可能正好在跑）。"""
    from library import busy
    real = subprocess.run

    def fake(cmd, *a, **k):
        if cmd[:2] == ["pgrep", "-f"]:
            return subprocess.CompletedProcess(cmd, 1, "", "")
        return real(cmd, *a, **k)
    monkeypatch.setattr(busy.subprocess, "run", fake)


def test_job_lock_between_processes(tmp_path, monkeypatch, quiet_caption):
    from library import busy
    monkeypatch.setenv("LIBRARY_DATA", str(tmp_path))
    assert busy.other_jobs() is None
    with busy.hold("模型分类"):
        assert busy.holder() is None and busy.other_jobs() is None, "本进程拿着锁：不算别人在忙"
        with busy.hold("嵌套"):                      # 同一进程嵌套不会自己等自己
            pass
    code = ("import sys, time; sys.path.insert(0, %r); from library import busy\n"
            "with busy.hold('a library scan is running'):\n    print('locked', flush=True); time.sleep(3)") % str(ROOT)
    p = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True)
    assert p.stdout.readline().strip() == "locked"
    assert "library scan" in (busy.holder() or "") and "is running" in busy.other_jobs()
    with pytest.raises(busy.Busy):
        with busy.hold("dedupe"):
            pass
    p.wait(10)
    assert busy.holder() is None and busy.other_jobs() is None, "进程退出，锁自动释放"


def test_unknown_state_counts_as_busy(tmp_path, monkeypatch):
    from library import busy
    monkeypatch.setenv("LIBRARY_DATA", str(tmp_path))

    def boom(*a, **k):
        raise OSError("no pgrep")
    monkeypatch.setattr(busy.subprocess, "run", boom)
    assert busy.other_jobs(), "查不清时按忙处理"
    with pytest.raises(busy.Busy):
        with busy.hold("扫描"):
            pass


def test_guide_follows_chapter_title_and_goes_with_the_book(lib):
    from fastapi.testclient import TestClient
    from library.config import load
    from library.db import DB
    from library.scan import Scanner
    from library.web import create_app
    cfg = load()
    db = DB(cfg.db_path)
    Scanner(db, cfg, set()).run([str(lib / "books")])
    bid = db.one("SELECT id FROM books")["id"]
    c = TestClient(create_app(cfg))
    c.get(f"/read/{bid}")
    # 旧版本解析时 Chapter II 是目录第 0 项；重新解析后挪到了第 1 项
    db.run("INSERT INTO guides VALUES(?, 0, 'Chapter II', '<p>第二章导读</p>', 1)", (bid,))
    page = c.get(f"/book/{bid}").text
    assert "第二章导读" in page and 'id="g1"' in page
    db.remove_book(bid)
    assert db.one("SELECT COUNT(*) n FROM guides")["n"] == 0, "删书连导读一起删"


def test_guide_check_and_register_is_atomic(lib, monkeypatch, quiet_caption):
    from fastapi.testclient import TestClient
    import library.guide as G
    from library.config import load
    from library.db import DB
    from library.scan import Scanner
    from library.web import create_app
    cfg = load()
    db = DB(cfg.db_path)
    Scanner(db, cfg, set()).run([str(lib / "books")])
    bid = db.one("SELECT id FROM books")["id"]
    app = create_app(cfg)
    c = TestClient(app)
    c.get(f"/read/{bid}")
    real_reason = app.state.lib.busy_reason

    def slow_reason():                            # 把「检查」拉长，放大并发窗口
        r = real_reason()
        time.sleep(0.3)
        return r
    monkeypatch.setattr(app.state.lib, "busy_reason", slow_reason)
    monkeypatch.setattr(G, "_guide", lambda st, *a: time.sleep(1.0) or st.guide_job.update(running=False))
    codes = []
    ts = [threading.Thread(target=lambda: codes.append(c.post(f"/api/books/{bid}/guide/0").status_code)) for _ in range(2)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert sorted(codes) == [200, 409], codes


def test_isolated_child_exits_with_its_parent(tmp_path):
    code = ("import os, sys; sys.path.insert(0, %r)\n"
            "from library.isolate import Isolated\n"
            "iso = Isolated(30)\n"
            "print(iso.call(os.getpid), flush=True)\n"
            "os._exit(0)\n") % str(ROOT)               # 主进程不收尾直接退出（像被 kill）
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60).stdout
    child = int(out.strip().splitlines()[-1])
    for _ in range(40):
        try:
            os.kill(child, 0)
        except ProcessLookupError:
            return
        time.sleep(0.25)
    os.kill(child, 9)
    pytest.fail("解析子进程在主进程退出 10 秒后还在")
