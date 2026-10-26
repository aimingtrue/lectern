"""命令行：
  python -m library serve              启动网站（默认 http://127.0.0.1:8791）
  python -m library add 书.pdf ...      入库指定的书
  python -m library scan [文件夹 ...]   扫描文件夹（不写参数则扫描「管理」里保存的文件夹；
                                       解析器升级后，旧版本解析的书也会在这时重新解析；--workers=N 并行解析数，默认 4）
  python -m library sources 文件夹 ...  设置放书的文件夹
  python -m library covers-prune        删掉从没打开读过的书的阅读页封面（显示时会现画；省磁盘）
  python -m library vacuum              整理 library.db、digest.db、catalog.db（删过大量行后把空闲页还给文件系统）
  python -m library recast 书的id        重新给这本书的角色分音色（按 tts/voices.json 的 cast_order；需要朗读服务在运行）；
                                       不带 id 列出每本书的角色与音色；锁定了音色的书要加 --force
  python -m library lock-voices [书的id] [--off]  锁定这本书现在的角色音色（以后改 cast_order、新装音色都不影响它）；
                                       --off 解锁；不带 id 列出锁定的书
  python -m library voices [书的id|--all] 角色配音的全书准备（人物 → 对白归属 → 音色，按章）：不带参数列出读过的书每章的
                                       准备情况；带 id 把这本书没准备的章做完（正在读的章先）；--all 给读过的书都做
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

from . import busy
from .config import load
from .db import DB
from .dictionary import Dictionary
from .scan import Scanner


def main(argv: list[str]) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname).1s %(message)s", datefmt="%m-%d %H:%M:%S")
    logging.getLogger("httpx").setLevel(logging.WARNING)   # 每次调模型 / 朗读服务一行 INFO 把 server.log 刷满，业务日志自己记
    cfg = load()
    cmd = argv[0] if argv else "serve"
    if cmd == "serve":
        import resource
        import uvicorn
        # launchd 给的句柄软上限只有 256：40 个工作线程各开几个库（每个库连 -wal 两个句柄）就贴着上限，
        # 一阵并发顶满后接不了新请求、库也打不开（500）
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        if soft < 4096:
            resource.setrlimit(resource.RLIMIT_NOFILE, (4096 if hard == resource.RLIM_INFINITY else min(4096, hard), hard))
        from .web import create_app
        app = create_app(cfg)
        if app.state.lib.mini:
            app.state.lib.mini.start()                    # 投到 mini 的隧道：一启动就接上
        uvicorn.run(app, host=cfg.host, port=cfg.port, log_level="warning")
        return 0
    if cmd == "sources":
        cfg.sources = [str(Path(a).expanduser()) for a in argv[1:]]
        cfg.save()
        print("放书的文件夹：", cfg.sources)
        return 0
    if cmd == "vacuum":
        import sqlite3
        for name in ("library.db", "digest.db", "catalog.db"):
            p = cfg.data / name
            if not p.exists():
                continue
            before = p.stat().st_size
            c = sqlite3.connect(p, timeout=60)
            try:
                c.execute("VACUUM")
            except sqlite3.OperationalError as ex:
                print(f"{name}: 没做成（{ex}）")
                continue
            finally:
                c.close()
            print(f"{name}: {before / 1e6:.0f} MB → {p.stat().st_size / 1e6:.0f} MB")
        return 0
    if cmd == "voices":
        return voices(cfg, argv[1] if len(argv) > 1 else "")
    if cmd == "recast":
        return recast(cfg, next((a for a in argv[1:] if not a.startswith("--")), ""), "--force" in argv)
    if cmd == "lock-voices":
        return lock_voices(cfg, next((a for a in argv[1:] if not a.startswith("--")), ""), "--off" in argv)
    if cmd == "covers-prune":
        db = DB(cfg.db_path)
        keep = {r["id"] for r in db.q("SELECT id FROM books WHERE stored=1 OR last_read IS NOT NULL")}
        n = freed = 0
        for p in cfg.covers.glob("*.jpg"):
            if p.stem not in keep:
                freed += p.stat().st_size
                p.unlink()
                n += 1
        print(f"删掉 {n} 张封面，释放 {freed / 1e6:.0f} MB；留下 {len(keep)} 本读过的书的")
        return 0
    if cmd in ("add", "scan"):
        workers = next((int(a.split("=", 1)[1]) for a in argv if a.startswith("--workers=")), None)
        args = [a for a in argv[1:] if a != "--force" and not a.startswith("--workers=")]
        if workers:
            global_workers(workers)
        if "--force" in argv:
            return _scan(cmd, cfg, args)
        try:
            with busy.hold("library scan", kind="io"):
                return _scan(cmd, cfg, args)
        except busy.Busy as ex:
            print(f"{ex}。扫描是长任务，为避免两件重活同时跑，请等它结束（或加 --force）。")
            return 2
    print(__doc__)
    return 1


def voices(cfg, arg: str) -> int:
    """角色配音的全书准备（library/roles.py）：列出 / 补做。和网站用同一把长任务锁，网站正在预生成时等它。"""
    import types
    import httpx
    from . import roles
    from .llm import Ollama
    db = DB(cfg.db_path)
    st = types.SimpleNamespace(cfg=cfg, db=db, llm=Ollama(cfg.ollama))
    if arg == "--all":
        books = [r["id"] for r in db.q("SELECT id FROM books WHERE last_read IS NOT NULL ORDER BY last_read DESC")]
    elif arg:
        books = [arg]
    else:
        books = [r["id"] for r in db.q("SELECT id FROM books WHERE last_read IS NOT NULL ORDER BY last_read DESC LIMIT 30")]
    pool, narr = [], set()
    if arg:
        try:
            h = httpx.get(cfg.tts_url + "/health", timeout=5).json()
            pool = h.get("voices") or []
            narr = {h.get("default") or "mine", next((v["id"] for v in pool if v.get("lang") == "zh"), "mine")}
        except Exception as ex:  # noqa: BLE001
            print(f"朗读服务没在运行（{cfg.tts_url}），只做人物和归属、不分音色：{ex}")
    for bid in books:
        b = db.book(bid)
        if not b:
            print(f"没有这本书：{bid}")
            return 1
        rows = roles.status(db, b)
        left = [r for r in rows if roles.todo(r)]
        print(f"{b['title']}（{bid}）：{len(rows)} 章，{len(left)} 章待准备")
        if not arg:
            for r in rows:
                flag = "✓" if not roles.todo(r) else "…"
                print(f"  {flag} 第 {r['chapter']:>3} 项 {r['title'][:24]:<24} 人物 {'有' if r['cast'] else '无'}  对白段 {r['attributed']}/{r['quoted']}")
            continue
        if not left:
            continue

        def show(r):
            now = roles.status(db, b, repair=False)
            x = next(q for q in now if q["chapter"] == r["chapter"])
            print(f"  第 {x['chapter']:>3} 项 {x['title'][:24]:<24} 人物 {'有' if x['cast'] else '无'}  对白段 {x['attributed']}/{x['quoted']}", flush=True)
        with busy.hold("character voice prep", wait=True):
            r = roles.sweep(st, b, b["progress_para"] or 0, pool=pool, exclude=narr, progress=show)
        print(f"  做了 {r['chapters']} 章")
    return 0


def lock_voices(cfg, bid: str, off: bool = False) -> int:
    """锁定 / 解锁一本书的角色音色；不带 id 列出锁定的书。"""
    import time
    db = DB(cfg.db_path)
    if not bid:
        for r in db.q("SELECT l.book, b.title FROM voice_lock l LEFT JOIN books b ON b.id=l.book ORDER BY l.created"):
            print(f"{r['book']}  {r['title'] or '—'}")
        return 0
    bk = db.book(bid)
    if not bk:
        print(f"没有这本书：{bid}")
        return 1
    if off:
        db.run("DELETE FROM voice_lock WHERE book=?", (bid,))
        print(f"已解锁：{bk['title']}")
    else:
        db.run("INSERT OR IGNORE INTO voice_lock(book, created) VALUES (?, ?)", (bid, time.time()))
        n = db.one("SELECT COUNT(*) n FROM speakers WHERE book=? AND voice IS NOT NULL", (bid,))["n"]
        print(f"已锁定：{bk['title']}（{n} 个角色的音色）")
    return 0


def recast(cfg, bid: str, force: bool = False) -> int:
    """角色音色：不带书的 id 列出已配音的书和各角色的声音；带 id 把这本书的角色音色清掉按 cast_order 重分（旁白按音色服务的默认音色）。"""
    import httpx
    from . import speech
    db = DB(cfg.db_path)
    try:
        h = httpx.get(cfg.tts_url + "/health", timeout=5).json()
    except Exception as ex:  # noqa: BLE001
        print(f"朗读服务没在运行（{cfg.tts_url}）：{ex}")
        return 1
    pool = h.get("voices") or []
    names = {v["id"]: v["name"] for v in pool}
    books = [bid] if bid else [r["book"] for r in db.q("SELECT DISTINCT book FROM speakers ORDER BY book")]
    for b in books:
        bk = db.book(b)
        if not bk:
            print(f"没有这本书：{b}")
            return 1
        if bid:
            from .speech import locked
            if locked(db, b) and not force:
                print(f"《{bk['title']}》的角色音色已锁定，不重分（真要重分加 --force，或先 lock-voices {b} --off）")
                return 1
            db.run("UPDATE speakers SET voice=NULL WHERE book=?", (b,))
            narr = {h.get("default") or "mine", next((v["id"] for v in pool if v.get("lang") == "zh"), "mine")}
            speech.assign_all(db, b, pool, narr)
        from . import cast
        allc = cast.people_all(db, b)
        print(f"{bk['title']}（{b}）  叙述者：{allc['narrator'] or '—'}")
        for r in db.q("SELECT name, gender, voice FROM speakers WHERE book=? ORDER BY voice IS NULL, name", (b,)):
            who = speech.resolve(r["name"], allc["people"])
            tag = "叙述者 → 旁白" if who and who["name"] == allc["narrator"] else (names.get(r["voice"], r["voice"]) or "旁白")
            print(f"  {r['name']:<28} {r['gender'] or '?'}  {tag}" + (f"  （= {who['name']}）" if who and who["name"] != r["name"] else ""))
    return 0


def global_workers(n: int) -> None:
    from . import scan
    scan.WORKERS = max(1, n)


def _scan(cmd: str, cfg, args: list[str]) -> int:
    db = DB(cfg.db_path)
    words = Dictionary(cfg.dict_path).words()
    sc = Scanner(db, cfg, words)
    if cmd == "add":
        for a in args:
            print(a, "→", sc.add_file(Path(a).expanduser(), force=True, full=True))
        return 0
    dirs = args or cfg.sources
    if not dirs:
        print("没有要扫描的文件夹。用法：python -m library scan ~/Books")
        return 1

    def prog(s):
        if s.done % 10 == 0 or s.done == s.total:
            print(f"\r{s.done}/{s.total}  新入库 {s.added}  跳过 {s.skipped}  失败 {s.failed}", end="", flush=True)
    s = sc.run(dirs, prog)
    print(f"\n完成：新入库 {s.added}，跳过 {s.skipped}，失败 {s.failed}")
    for e in s.errors[-20:]:   # 目录不存在（外接盘没插）、解析失败等
        print(f"⚠️ {e['path']}：{e['error']}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
