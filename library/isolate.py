"""在独立子进程里跑可能崩溃或卡死的解析（PyMuPDF 遇到个别坏文件会段错误或死循环）。

子进程常驻、重复使用；超时或崩溃就杀掉，下次调用自动重开，主进程（网站、扫描）不受影响。
主进程意外退出（被 kill、重启脚本只杀主进程）时，子进程发现父进程没了就自己退出，不会成为孤儿。
"""
from __future__ import annotations

import multiprocessing as mp
import os
import threading
import time
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from concurrent.futures.process import BrokenProcessPool


class IsolatedError(RuntimeError):
    pass


class IsolatedTimeout(IsolatedError):
    """The child did not finish in time and was killed (as opposed to crashing)."""



def _watch_parent(parent: int, every: float = 2.0) -> None:
    while True:
        time.sleep(every)
        if os.getppid() != parent:               # 父进程没了（被收养给 launchd / init）：跟着退出
            os._exit(0)


def _child_init(parent: int, initializer, initargs: tuple) -> None:
    threading.Thread(target=_watch_parent, args=(parent,), daemon=True).start()
    if initializer is not None:
        initializer(*initargs)


class Isolated:
    def __init__(self, timeout: float = 300, initializer=None, initargs: tuple = ()):
        self.timeout, self.initializer, self.initargs = timeout, initializer, initargs
        self.pool: ProcessPoolExecutor | None = None

    def call(self, fn, *args, timeout: float | None = None):
        """跑一次；timeout 给了就用它代替构造时的默认上限（大文件给短一点）。"""
        if self.pool is None:
            self.pool = ProcessPoolExecutor(1, mp_context=mp.get_context("spawn"), initializer=_child_init,
                                            initargs=(os.getpid(), self.initializer, self.initargs))
        fut = self.pool.submit(fn, *args)
        limit = self.timeout if timeout is None else timeout
        try:
            return fut.result(timeout=limit)
        except FutureTimeout:
            self.close(kill=True)
            raise IsolatedTimeout(f"did not finish within {limit:.0f}s and was killed") from None
        except BrokenProcessPool:
            self.close(kill=True)
            raise IsolatedError("the parse process crashed (the file may be corrupt)") from None

    def close(self, kill: bool = False) -> None:
        pool, self.pool = self.pool, None
        if pool is None:
            return
        if kill:
            for p in list(getattr(pool, "_processes", {}).values()):
                p.kill()
        pool.shutdown(wait=not kill, cancel_futures=True)
