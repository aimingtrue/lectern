"""精简阅读库：几万本书的全文（paras）不再常驻，只留读过的书的全文，其余只留书目和摘要（见 library/digest.py），
第一次打开时再解析存全文。

  .venv/bin/python tools/slim_library.py digest          给已入库、还没有摘要的书算摘要（只读全文，可随时中断、接着跑）
  .venv/bin/python tools/slim_library.py slim            看会怎样（不动库）
  .venv/bin/python tools/slim_library.py slim --apply    另建精简库并换上；旧库（连同 -wal / -shm）移到废纸篓

留全文的书：有阅读进度或有章节导读的。换库前要停掉所有打开 library.db 的进程（书库网站、扫描、模型分类…），
否则不执行并列出这些进程。旧库在废纸篓里，放回 data/ 就能恢复。
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from library import digest  # noqa: E402
from library.db import DB, SCHEMA, VERSION  # noqa: E402

KEEP = "last_read IS NOT NULL OR id IN (SELECT book FROM {g}guides)"


def build_digests(db_path: Path, log=print) -> int:
    """没有摘要的书：从全文算摘要。返回算了多少本。"""
    db = DB(db_path)                                   # 2 → 3：加 digest 表和 stored 列（不备份）
    c = db.conn
    todo = [r[0] for r in c.execute("SELECT id FROM books WHERE status='ok' "
                                    "AND id NOT IN (SELECT book FROM digest) ORDER BY id")]
    log(f"要算摘要的书：{len(todo):,} 本")
    t0 = time.time()
    for i, bid in enumerate(todo, 1):
        paras = [{"kind": k, "text": t} for k, t in c.execute("SELECT kind, text FROM paras WHERE book=? ORDER BY n", (bid,))]
        d = digest.compute(paras)                       # 慢的部分（读全文、算指纹）在事务外做
        with db.lock:                                   # 每本写完就提交：写锁只占几毫秒，网站记进度、入库都不用等
            c.execute("INSERT OR REPLACE INTO digest(book,head,sketch,gate) VALUES(?,?,?,?)",
                      (bid, d["head"], d["sketch"], d["gate"]))
            c.commit()
        if i % 1000 == 0 or i == len(todo):
            log(f"{i:,}/{len(todo):,}  {time.time() - t0:.0f} 秒")
    c.commit()
    return len(todo)


def holders(db_path: Path) -> list[str]:
    """除本进程外打开着 library.db（含 -wal）的进程。"""
    files = [str(p) for p in (db_path, db_path.with_name(db_path.name + "-wal")) if p.exists()]
    try:
        out = subprocess.run(["lsof", "-t", *files], capture_output=True, text=True, timeout=30).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ["（lsof 不可用，无法确认没有别的进程在用）"]
    pids = sorted({int(x) for x in out.split()} - {os.getpid()})
    if not pids:
        return []
    ps = subprocess.run(["ps", "-o", "pid=,command=", "-p", ",".join(map(str, pids))], capture_output=True, text=True)
    return [l.strip() for l in ps.stdout.splitlines() if l.strip()] or [str(p) for p in pids]


def plan(db_path: Path) -> dict:
    c = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        digest.attach(c)
        books = c.execute("SELECT COUNT(*) FROM books").fetchone()[0]
        missing = c.execute("SELECT COUNT(*) FROM books WHERE status='ok' "
                            "AND id NOT IN (SELECT book FROM digest)").fetchone()[0]
        keep = c.execute(f"SELECT COUNT(*) FROM books WHERE stored=1 AND ({KEEP.format(g='')})").fetchone()[0]
    finally:
        c.close()
    return {"books": books, "missing": missing, "keep": keep, "bytes": db_path.stat().st_size}


def slim(db_path: Path, trash_root: Path = Path.home() / ".Trash", log=print) -> Path:
    """另建精简库换上，返回旧库在废纸篓里的目录。"""
    new = db_path.with_name(db_path.name + ".slim")
    for p in (new, new.with_name(new.name + "-wal"), new.with_name(new.name + "-shm")):
        p.unlink(missing_ok=True)
    c = sqlite3.connect(f"file:{new}", uri=True)          # uri=True：ATTACH 才认 file:…?mode=ro
    try:
        c.executescript(SCHEMA)
        c.execute(f"PRAGMA user_version={VERSION}")
        c.execute("ATTACH DATABASE ? AS o", (f"file:{db_path}?mode=ro",))
        digest.attach(c)                                       # 摘要在旁边的 digest.db 里，新旧库共用，不用搬
        new_cols = [r[1] for r in c.execute("PRAGMA main.table_info(books)")]
        old_cols = {r[1] for r in c.execute("PRAGMA o.table_info(books)")}
        cols = ",".join(x for x in new_cols if x in old_cols)
        c.execute("BEGIN")
        c.execute(f"INSERT INTO books({cols}) SELECT {cols} FROM o.books")
        c.execute(f"UPDATE books SET stored = (stored=1 AND ({KEEP.format(g='o.')}))")
        for t in ("ai_cache", "guides"):
            c.execute(f"INSERT INTO {t} SELECT * FROM o.{t}")
        c.execute("INSERT INTO paras SELECT * FROM o.paras WHERE book IN (SELECT id FROM books WHERE stored=1)")
        c.execute("COMMIT")
        same = [(t, c.execute(f"SELECT COUNT(*) FROM main.{t}").fetchone()[0], c.execute(f"SELECT COUNT(*) FROM o.{t}").fetchone()[0])
                for t in ("books", "ai_cache", "guides")]
        bad = [x for x in same if x[1] != x[2]]
        if bad:
            raise RuntimeError(f"条数对不上：{bad}")
        c.execute("DETACH DATABASE o")
        ok = c.execute("PRAGMA quick_check").fetchone()[0]
        if ok != "ok":
            raise RuntimeError(f"新库检查没通过：{ok}")
        c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        c.close()
    trash = trash_root / f"英文书库-旧阅读库-{time.strftime('%Y%m%d-%H%M%S')}"
    trash.mkdir(parents=True)
    for suffix in ("", "-wal", "-shm"):
        p = db_path.with_name(db_path.name + suffix)
        if p.exists():
            shutil.move(str(p), str(trash / p.name))
    for suffix in ("-wal", "-shm"):
        new.with_name(new.name + suffix).unlink(missing_ok=True)
    new.rename(db_path)
    log(f"已换上精简库（{db_path.stat().st_size / 1e9:.2f} GB）；旧库在废纸篓：{trash}")
    return trash


def main(argv: list[str]) -> int:
    from library.config import load
    cfg = load()
    cmd = argv[0] if argv else ""
    if cmd == "digest":
        from library import busy
        with busy.hold("补摘要", wait=True, kind="io"):         # 本机长任务一次只跑一个：有别的在跑就等
            build_digests(cfg.db_path)
        return 0
    if cmd == "slim":
        DB(cfg.db_path).conn.close()                     # 确保已升级到有 digest 表的版本
        p = plan(cfg.db_path)
        print(json.dumps(p, ensure_ascii=False))
        print(f"书 {p['books']:,} 本，留全文 {p['keep']:,} 本，其余只留摘要；现在库 {p['bytes'] / 1e9:.1f} GB")
        if p["missing"]:
            print(f"还有 {p['missing']:,} 本没有摘要，先跑：tools/slim_library.py digest")
            return 1
        if "--apply" not in argv:
            print("加 --apply 执行")
            return 0
        who = holders(cfg.db_path)
        if who:
            print("这些进程还开着 library.db，先停掉再执行：")
            for w in who:
                print("  ", w)
            return 2
        slim(cfg.db_path)
        return 0
    print(__doc__)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
