"""线程本地连接：工作线程退出后连接要关掉，文件句柄不能越攒越多（服务跑几天顶到 256 个句柄上限会全站 500）。"""
from __future__ import annotations

import os
import subprocess
import threading

import pytest

from library.catalog import Catalog
from library.db import DB


def open_fds(tmp) -> int:
    out = subprocess.run(["lsof", "-p", str(os.getpid()), "-a", "-d", "0-65535"], capture_output=True, text=True).stdout
    return sum(1 for line in out.splitlines() if str(tmp) in line)


@pytest.mark.skipif(not os.path.exists("/usr/sbin/lsof"), reason="要用 lsof 数句柄")
def test_thread_exit_closes_connections(tmp_path):
    db, cat = DB(tmp_path / "library.db"), Catalog(tmp_path / "catalog.db")

    def work():
        db.one("SELECT 1")
        cat.q("SELECT 1")

    before = open_fds(tmp_path)
    for _ in range(10):
        t = threading.Thread(target=work)
        t.start()
        t.join()
    assert open_fds(tmp_path) <= before + 6        # 第一次用到 -wal / -shm 会多开几个；漏的话 10 个线程多 60 多个
