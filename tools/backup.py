"""备份不可再生的数据：阅读进度、章节导读、译文缓存（从 library.db 摘出来）、编目库、书目库、相似书向量、配置。

  .venv/bin/python tools/backup.py [--dest 目录] [--keep 7]     做一份快照，只保留最近 keep 份（更早的移到废纸篓）
  .venv/bin/python tools/backup.py --list [--dest 目录]          列出已有的快照
  .venv/bin/python tools/backup.py --push 主机:目录               快照做完后用 rsync 推到另一台机器（环境变量 LECTERN_BACKUP_PUSH 可设默认）

默认目录 ~/Backups/english-library（环境变量 LECTERN_BACKUP_DIR 可改；最好指到外接盘或另一台机器）。
每份快照是一个「时间」命名的文件夹：
  user.db      library.db 里的书目与阅读进度（books，不含全文）、导读（guides）、译文缓存（ai_cache）、人物（cast）、
               对白归属（speech、speech_done、speakers）、阅读时长（reading_log）
  catalog.db   编目库全量          biblio.db   书目库全量          similar.db   相似书向量（算一次要几个小时）
  exclude.db   排除名单（不想要的书是谁、复核时留下的书）
  config.json  放书的文件夹、模型、外部来源的 key
不备份的：段落全文与摘要（打开时、扫描时都能重建）、search.db（几分钟重建）、朗读音频缓存（data/tts，可重生成）。
SQLite 用在线备份接口拷贝，后台任务正在写也能得到一致的快照。

恢复：停掉书库服务和后台任务，把 catalog.db、biblio.db、similar.db、exclude.db、config.json 拷回 data/；阅读进度和导读用
  .venv/bin/python tools/backup.py --restore-user 快照目录/user.db
写回 library.db（只更新进度列，按主键补回导读、译文缓存、人物、对白归属、阅读时长，不动全文）。
异地：--push 主机:目录 把刚做好的快照 rsync 到另一台机器（走 ssh，主机名用 ~/.ssh/config 里的别名）；远端上一份快照里内容相同的
文件只做硬链接不重传，远端同样只保留最近 keep 份。推送失败不影响本地快照。
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

FULL = ("catalog.db", "biblio.db", "similar.db", "exclude.db")                   # 整库备份
USER_TABLES = ("guides", "ai_cache", "cast", "speech", "speech_done", "speakers", "reading_log")   # library.db 里整表备份的
BOOK_COLS = ("id", "path", "size", "mtime", "title", "author", "added", "last_read", "progress_page", "progress_frac",
             "progress_para", "progress_sent", "parser_ver", "n_paras", "mode")
PROGRESS = ("last_read", "progress_page", "progress_frac", "progress_para", "progress_sent", "mode")


def default_dest() -> Path:
    return Path(os.environ.get("LECTERN_BACKUP_DIR", "~/Backups/english-library")).expanduser()


def copy_db(src: Path, dest: Path) -> None:
    s = sqlite3.connect(f"file:{src}?mode=ro", uri=True, timeout=60)
    d = sqlite3.connect(dest)
    try:
        s.backup(d)
    finally:
        d.close()
        s.close()


def user_db(lib: Path, dest: Path) -> dict:
    """从 library.db 摘出不可再生的部分（几十 MB 以内），不碰 33 GB 的段落全文。"""
    d = sqlite3.connect(dest)
    try:
        d.execute("ATTACH DATABASE ? AS l", (f"file:{lib}?mode=ro",))
        have = {r[0] for r in d.execute("SELECT name FROM l.sqlite_master WHERE type='table'")}
        cols = [r[1] for r in d.execute("PRAGMA l.table_info(books)")]
        keep = [c for c in BOOK_COLS if c in cols]
        d.execute(f"CREATE TABLE books AS SELECT {','.join(keep)} FROM l.books")
        n = {"books": d.execute("SELECT COUNT(*) FROM books").fetchone()[0]}
        for t in USER_TABLES:
            if t in have:
                sql = d.execute("SELECT sql FROM l.sqlite_master WHERE type='table' AND name=?", (t,)).fetchone()[0]
                d.execute(sql)
                d.execute(f"INSERT INTO main.{t} SELECT * FROM l.{t}")
                n[t] = d.execute(f"SELECT COUNT(*) FROM main.{t}").fetchone()[0]
        d.commit()
        d.execute("DETACH DATABASE l")
        return n
    finally:
        d.close()


def snapshot(data: Path, dest_root: Path, keep: int = 7, trash: Path | None = None, log=print) -> Path:
    if keep < 1:
        raise ValueError("--keep 至少是 1")
    dest_root.mkdir(parents=True, exist_ok=True)
    snap = dest_root / time.strftime("%Y%m%d-%H%M%S")
    part = snap.with_name(snap.name + ".part")
    part.mkdir()
    report = {"at": time.strftime("%F %T"), "files": {}}
    if (data / "library.db").exists():
        report["files"]["user.db"] = user_db(data / "library.db", part / "user.db")
    for name in FULL:
        if (data / name).exists():
            copy_db(data / name, part / name)
            report["files"][name] = (part / name).stat().st_size
    if (data / "config.json").exists():
        shutil.copy2(data / "config.json", part / "config.json")
        os.chmod(part / "config.json", 0o600)                         # 里面有外部来源的 key
        report["files"]["config.json"] = True
    for f in part.glob("*.db"):
        c = sqlite3.connect(f)
        ok = c.execute("PRAGMA quick_check").fetchone()[0]
        c.close()
        if ok != "ok":
            raise RuntimeError(f"{f.name} 校验没通过：{ok}")
    (part / "manifest.json").write_text(json.dumps(report, ensure_ascii=False, indent=1))
    part.rename(snap)                                                 # 全部写完才去掉 .part：半份快照不会被当成完整的
    old = sorted(p for p in dest_root.iterdir() if p.is_dir() and not p.name.endswith(".part"))[:-keep]
    trash = trash if trash is not None else Path.home() / ".Trash"
    for p in old:                                                     # 更早的快照移到废纸篓，不永久删除
        trash.mkdir(parents=True, exist_ok=True)
        shutil.move(str(p), str(trash / f"english-library-backup-{p.name}"))
    log(f"快照：{snap}（{', '.join(report['files'])}）；保留最近 {keep} 份，移到废纸篓 {len(old)} 份")
    return snap


def push(snap: Path, remote: str, keep: int = 7, log=print) -> None:
    """把一份快照 rsync 到另一台机器的 remote（主机:目录）。以远端上一份为增量基准只传有变化的（--checksum 比内容，
    因为在线备份每次重写 mtime）；传完在远端把内容相同的文件换成上一份的硬链接；先写到 .part，传完改名；远端也只留最近 keep 份。"""
    host, _, rdir = remote.partition(":")
    if not host or not rdir:
        raise ValueError("--push 要写成 主机:目录")
    prev = subprocess.run(["ssh", host, f"mkdir -p {rdir} && ls -d {rdir}/2*/ 2>/dev/null | tail -1"],
                          capture_output=True, text=True, check=True).stdout.strip()
    cmd = ["rsync", "-a", "--checksum"] + ([f"--link-dest={prev}"] if prev else []) + [f"{snap}/", f"{remote}/{snap.name}.part/"]
    subprocess.run(cmd, check=True)
    # macOS 自带的 openrsync 只拿 --link-dest 做增量基准、不做硬链接，所以传完在远端逐个比对，相同的换成上一份的硬链接
    dedup = f'for f in {snap.name}.part/*; do b=$(basename "$f"); cmp -s "{prev}$b" "$f" && ln -f "{prev}$b" "$f"; done; ' if prev else ""
    subprocess.run(["ssh", host, f"cd {rdir} && {dedup}mv {snap.name}.part {snap.name} && ls -d 2*/ | grep -v '[.]part' | head -n -{keep} | xargs -r rm -rf"],
                   check=True)
    log(f"已推到 {remote}/{snap.name}（{'基于上一份 ' + prev.rstrip('/').rsplit('/', 1)[-1] + ' 做硬链接' if prev else '首次，全量'}）")


def restore_user(user: Path, lib: Path) -> dict:
    """把快照里的阅读进度、导读、译文缓存、人物、对白归属、阅读时长写回 library.db：进度只更新 id 对得上的书，导读和缓存按主键补回。"""
    c = sqlite3.connect(lib, timeout=60)
    try:
        c.execute("ATTACH DATABASE ? AS u", (f"file:{user}?mode=ro",))
        with c:
            sets = ", ".join(f"{k}=(SELECT u.books.{k} FROM u.books WHERE u.books.id=books.id)" for k in PROGRESS)
            n = c.execute(f"UPDATE books SET {sets} WHERE id IN (SELECT id FROM u.books WHERE last_read IS NOT NULL)").rowcount
            out = {"books": n}
            for t in USER_TABLES:
                out[t] = c.execute(f"INSERT OR IGNORE INTO main.{t} SELECT * FROM u.{t}").rowcount
        c.execute("DETACH DATABASE u")
        return out
    finally:
        c.close()


def main(argv: list[str]) -> int:
    from library.config import load
    cfg = load()
    opt = lambda k, d=None: argv[argv.index(k) + 1] if k in argv else d   # noqa: E731
    dest = Path(opt("--dest", str(default_dest()))).expanduser()
    if "--list" in argv:
        for p in sorted(dest.glob("*")) if dest.exists() else []:
            m = p / "manifest.json"
            print(p.name, json.loads(m.read_text())["files"] if m.exists() else "（不完整）")
        return 0
    if "--restore-user" in argv:
        print(restore_user(Path(opt("--restore-user")), cfg.db_path))
        return 0
    keep = int(opt("--keep", 7))
    snap = snapshot(cfg.data, dest, keep)
    remote = opt("--push", os.environ.get("LECTERN_BACKUP_PUSH"))
    if remote:
        push(snap, remote, keep)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
