"""长任务互斥：长任务分两类，每类同时只跑一个，两类之间互不阻塞。

  gpu  用本机模型 / GPU 的：朗读预生成、章节导读、人物抽取、模型分类、相似书向量、中文翻译、原版译本关联
       锁文件 data/.jobs/heavy.lock；另外还要让开别的项目（字幕项目 ntu-caption）的长任务进程（按进程名找）
  io   只读写文件和库的：书库扫描、新书入库、去重、排除、封面检查、补摘要、同一本书判定
       锁文件 data/.jobs/io.lock；不看字幕项目
长任务用 `with busy.hold("model classification"):`（默认 gpu）或 `busy.hold("library scan", kind="io")` 包起来，进程退出（包括崩溃）
时锁自动释放，不会留下死锁；锁文件里写着谁拿着，other_jobs(kind) 报出来。查不清（pgrep 出错）时按「忙」处理。
other_jobs() 里的 pgrep 结果缓存 5 秒（网站每次登记任务都会问一次，pgrep 要 20 多毫秒）；文件锁每次现看。

  python -m library.busy        看现在谁在跑
"""
from __future__ import annotations

import fcntl
import logging
import os
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path

log = logging.getLogger(__name__)

# 字幕项目（ntu-caption）的长任务进程
OTHER = r"server (batch|kb|prep|summarize)|run_queue[.]py"

KINDS = {"gpu": "heavy.lock", "io": "io.lock"}
_held: dict[str, object] = {}         # 本进程拿着的锁（自己拿着时 other_jobs 不算忙）：锁名 → 文件
_held_kind: dict[str, str] = {}       # 锁名 → 类别
_cache: dict[str, tuple[float, str | None]] = {}   # other_jobs 的结果：类别 → (时间, 结果)
CACHE_TTL = 5.0


class Busy(RuntimeError):
    pass


def lock_path(kind: str = "gpu") -> Path:
    from .config import load
    p = load().data / ".jobs" / KINDS[kind]
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def holder(kind: str = "gpu") -> str | None:
    """别的进程拿着这一类的长任务锁时，返回它是谁（「模型分类（PID 123，10:05 起）」）；没人拿或是本进程拿着返回 None。"""
    if any(k == kind for k in _held_kind.values()):
        return None
    p = lock_path(kind)
    with open(p, "a+") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            f.seek(0)
            return f.read().strip() or "another long job"
        fcntl.flock(f, fcntl.LOCK_UN)
    return None


def _captions_busy() -> str | None:
    try:
        r = subprocess.run(["pgrep", "-f", OTHER], capture_output=True, text=True, timeout=5)
    except Exception as ex:  # noqa: BLE001  查不清就当忙
        log.warning("could not check the caption queue: %s", ex)
        return "could not tell whether the caption/summary queue is running"
    return "the caption/summary queue is running" if any(l.strip() for l in r.stdout.splitlines()) else None


def other_jobs(kind: str = "gpu", ttl: float | None = None) -> str | None:
    """这一类有别的长任务在跑吗：有就返回原因。字幕项目的 pgrep（20 多毫秒）缓存 ttl 秒（默认 CACHE_TTL；0 = 现查），
    本项目的文件锁每次现看（不到 1 毫秒，进程一退出就能看到释放）。"""
    ttl = CACHE_TTL if ttl is None else ttl
    why = None
    if kind == "gpu":
        hit = _cache.get("captions")
        if hit and time.monotonic() - hit[0] < ttl:
            why = hit[1]
        else:
            why = _captions_busy()
            _cache["captions"] = (time.monotonic(), why)
    if not why:
        h = holder(kind)
        why = f"{h} is running" if h else None
    return why


@contextmanager
def hold(name: str, wait: bool = False, every: float = 60.0, log_wait=print, kind: str = "gpu"):
    """拿到这一类的长任务锁再执行；拿不到时 wait=False 抛 Busy，wait=True 每 every 秒重试。
    同一进程里嵌套 hold 同一类不会自己等自己。"""
    if any(k == kind for k in _held_kind.values()):
        yield
        return
    while True:
        why = _captions_busy() if kind == "gpu" else None
        f = None
        if not why:
            f = open(lock_path(kind), "a+")
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                f.seek(0)
                why = f"{f.read().strip() or 'another long job'} is running"
                f.close()
                f = None
        if f is not None:
            break
        if not wait:
            raise Busy(why)
        log_wait(f"{why}; retrying in {int(every)}s…")
        time.sleep(every)
    try:
        f.seek(0)
        f.truncate()
        f.write(f"{name} (PID {os.getpid()}, since {time.strftime('%m-%d %H:%M')})")
        f.flush()
        _held[name], _held_kind[name] = f, kind
        _cache.clear()
        yield
    finally:
        _held.pop(name, None)
        _held_kind.pop(name, None)
        _cache.clear()
        try:
            f.seek(0)
            f.truncate()
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)
            f.close()


if __name__ == "__main__":
    for k in KINDS:
        print(f"{k}: {other_jobs(k, ttl=0) or 'idle'}")
    sys.exit(0)
