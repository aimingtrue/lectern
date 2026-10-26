"""新书入库：收件箱（config 的 inbox，默认 ~/Downloads/下）里的书清洗文件名、去重，再移进书库（不是复制）。

  python -m library.ingest            先看会怎么处理（不动文件），清单写到 logs/ingest-时间.txt
  python -m library.ingest --apply    执行

每个文件依次：
  留在收件箱 · 不是电子书格式（mp3、apk、xlsx、还在下载的 .downloading…）、几分钟内还在变的文件、
              广告/教程/说明文件（Kindle 推送教程、详细使用介绍、格式转换…），或放在这类文件夹里的
  废纸篓     · 和书库里已有的文件内容相同，或和收件箱里排在前面的一份相同：指纹 book_id 一样，再逐字节比对确认
              （指纹只看大小和头尾各 1MB）
  留在收件箱 · 指纹相同而字节不同的（下载残缺或改过的版本）：书库按指纹认书，两份会撞号，人工看过再处理
  移进书库   · 文件名和各级文件夹名去掉广告（catalog.strip_ads）；重名不覆盖，加「 (2)」
同一本书的不同版本（epub / pdf / mobi）不在这里比：移进书库、入库体检之后由去重（library.dedupe）比质量，按作品留最好的一份。

放到书库的哪里（书库 = config 的第一个 sources）：
  收件箱/杂志/…、收件箱/儿童/…（书库顶层的类别目录）  → 书库/杂志/…
  收件箱/<文件夹>/…                                   → 书库/电子书/<文件夹>/…（子目录结构不变）
  收件箱根目录的单个文件                               → 书库/电子书/新书-年-月/
同一块盘上移动只是改名；跨盘时先复制、核对指纹一致再删原文件。不永久删除任何文件。
"""
from __future__ import annotations

import filecmp
import os
import re
import shutil
import sqlite3
import sys
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from .catalog import EXTS, JUNK_FILE, KIND_DIRS, strip_ads
from .extract import book_id

SETTLE = 120                 # 这么多秒内改过的文件当作还在下载/写入，这次不动
# 广告、教程、说明：文本/Word 按 JUNK_FILE 加这些词；任何格式按强广告词；文件夹名按 JUNK_DIR
DOC_JUNK = re.compile(r"使用介绍|格式转换|免费代找|推送|破解版|仔细看", re.I)
ANY_JUNK = re.compile(r"使用介绍|格式转换|免费代找|仔细看几遍|推送教程", re.I)
JUNK_DIR = re.compile(r"必看|免费代找|使用介绍|推送教程|格式转换|^提示[:：]", re.I)
BOOK_DIR = "电子书"


@dataclass
class Item:
    src: Path
    action: str              # move / trash / keep
    dest: Path | None = None
    why: str = ""


SYSTEM = {".DS_Store", ".Trashes", ".Spotlight-V100", ".fseventsd", ".TemporaryItems", ".localized", ".git", ".svn"}


def system_file(name: str) -> bool:
    """系统和工具生成的隐藏文件；其余以「.」开头的照常当书处理（下载来的书偶尔被藏成 .金粉世家）。"""
    return name in SYSTEM or name.startswith("._")


def clean_part(name: str) -> str:
    """一段文件夹名或不带扩展名的文件名去掉广告和开头的「.」；清成空的保留原名。"""
    name = name.lstrip(".") or name
    return strip_ads(name, tags=False) or name


def junk_reason(rel: Path) -> str | None:
    """rel：相对收件箱的路径。不该当书移走的返回原因。"""
    ext = rel.suffix.lower()
    if ext not in EXTS:
        return "不是电子书格式"
    if any(JUNK_DIR.search(seg) for seg in rel.parts[:-1]):
        return "在广告/教程文件夹里"
    if ext in (".txt", ".doc", ".docx") and (JUNK_FILE.search(rel.name) or DOC_JUNK.search(rel.name)):
        return "广告/说明文件"
    if ANY_JUNK.search(rel.stem):
        return "广告/说明文件"
    return None


def target(rel: Path, root: Path, month: str) -> Path:
    """收件箱里的相对路径 → 书库里的路径（文件夹名、文件名都已清洗）。"""
    dirs = list(rel.parts[:-1])
    if not dirs:
        base = root / BOOK_DIR / f"新书-{month}"
    elif dirs[0] in KIND_DIRS:
        base = root / dirs[0]
        dirs = dirs[1:]
    else:
        base = root / BOOK_DIR
    for d in dirs:
        base = base / clean_part(d)
    return base / (clean_part(rel.stem) + rel.suffix.lower())


def unique(p: Path, taken: set) -> Path:
    """已存在或这一批里已经有人要用的名字：加「 (2)」「 (3)」…"""
    if not p.exists() and p not in taken:
        return p
    for i in range(2, 10000):
        q = p.with_name(f"{p.stem} ({i}){p.suffix}")
        if not q.exists() and q not in taken:
            return q
    raise RuntimeError(f"重名太多：{p}")


def library_fids(catalog_db: Path) -> dict[str, str]:
    """书库里已有文件的指纹 → 路径（编目库只读打开）。"""
    if not catalog_db.exists():
        return {}
    c = sqlite3.connect(f"file:{catalog_db}?mode=ro", uri=True)
    try:
        return {fid: path for fid, path in c.execute("SELECT fid, path FROM files WHERE fid IS NOT NULL")}
    finally:
        c.close()


def same_bytes(a: Path, b: Path) -> bool:
    """逐字节相同才算重复：指纹相同而中段不同的（下载残缺、改过的版本）不能不比就扔掉。"""
    try:
        return filecmp.cmp(a, b, shallow=False)
    except OSError:
        return False


def plan(inbox: Path, root: Path, known: dict[str, str], now: float | None = None) -> list[Item]:
    now = time.time() if now is None else now
    month = time.strftime("%Y-%m", time.localtime(now))
    files = []
    for dp, dn, fn in os.walk(inbox):
        dn[:] = sorted(x for x in dn if not system_file(x))
        files += [Path(dp) / f for f in sorted(fn) if not system_file(f)]
    items, seen, taken = [], {}, set()
    for p in files:
        rel = p.relative_to(inbox)
        why = junk_reason(rel)
        if why:
            items.append(Item(p, "keep", why=why))
            continue
        try:
            st = p.stat()
            if now - st.st_mtime < SETTLE:
                items.append(Item(p, "keep", why="刚改过，可能还在下载"))
                continue
            fid = book_id(p)
        except OSError as ex:
            items.append(Item(p, "keep", why=f"读不了：{ex}"))
            continue
        other = Path(known[fid]) if fid in known and Path(known[fid]).exists() else seen.get(fid)
        if other is not None:
            where = "书库里已有" if fid in known and other == Path(known[fid]) else "收件箱里重复"
            if same_bytes(p, other):
                items.append(Item(p, "trash", why=f"{where}：{other}"))
            else:
                items.append(Item(p, "keep", why=f"指纹相同但内容不同（{where}：{other}），要人工看"))
            continue
        seen[fid] = p
        dest = unique(target(rel, root, month), taken)
        taken.add(dest)
        items.append(Item(p, "move", dest))
    return items


def move(src: Path, dest: Path) -> None:
    """同盘直接改名；跨盘复制后核对指纹，一致才删原文件。"""
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        raise FileExistsError(dest)
    if os.stat(src).st_dev == os.stat(dest.parent).st_dev:
        os.rename(src, dest)
        return
    shutil.copy2(src, dest)
    if book_id(dest) != book_id(src):
        dest.unlink()
        raise OSError(f"复制后内容不一致：{src}")
    src.unlink()


def prune(inbox: Path) -> int:
    """移走以后空下来的文件夹删掉（只含 .DS_Store 的也算空）；收件箱本身留着。"""
    n = 0
    for dp, dn, fn in os.walk(inbox, topdown=False):
        d = Path(dp)
        if d == inbox:
            continue
        if all(f == ".DS_Store" for f in fn) and not any((d / x).exists() for x in dn):
            for f in fn:
                (d / f).unlink()
            d.rmdir()
            n += 1
    return n


def apply(items: list[Item], inbox: Path, trash: Path, log) -> Counter:
    done: Counter = Counter()
    for it in items:
        try:
            if it.action == "move":
                move(it.src, it.dest)
                log.write(f"移入\t{it.src}\t{it.dest}\n")
            elif it.action == "trash":
                dest = unique(trash / it.src.relative_to(inbox), set())
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(it.src), str(dest))
                log.write(f"废纸篓\t{it.src}\t{dest}\t{it.why}\n")
            else:
                log.write(f"留下\t{it.src}\t\t{it.why}\n")
            done[it.action] += 1
        except OSError as ex:
            done["failed"] += 1
            log.write(f"失败\t{it.src}\t{it.dest or ''}\t{ex}\n")
    done["pruned"] = prune(inbox)
    return done


def main(argv: list[str]) -> int:
    from .config import ROOT, load
    cfg = load()
    inbox, root = Path(cfg.inbox).expanduser(), cfg.library_root()
    if not inbox.is_dir():
        print(f"收件箱不存在：{inbox}")
        return 1
    if not root.is_dir():
        print(f"书库文件夹不存在（外接盘没插？）：{root}")
        return 1
    stamp = time.strftime("%Y%m%d-%H%M%S")
    log_path = ROOT / "logs" / f"ingest-{stamp}.txt"
    log_path.parent.mkdir(exist_ok=True)
    items = plan(inbox, root, library_fids(cfg.data / "catalog.db"))
    n = Counter(it.action for it in items)
    print(f"收件箱 {inbox}：{len(items):,} 个文件 → 移进书库 {n['move']:,}、重复放废纸篓 {n['trash']:,}、留在收件箱 {n['keep']:,}")
    for why, k in Counter(it.why.split("（")[0] for it in items if it.action == "keep").most_common():
        print(f"  留下 · {why}：{k:,}")
    renamed = sum(1 for it in items if it.action == "move" and it.dest.name != it.src.name)
    if renamed:
        print(f"  文件名清洗：{renamed:,} 个")
    if "--apply" not in argv:
        with open(log_path, "w") as log:
            log.write("# 预演（没有移动任何文件）：处理\t原路径\t新路径\t原因\n")
            for it in items:
                log.write(f"{dict(move='移入', trash='废纸篓', keep='留下')[it.action]}\t{it.src}\t{it.dest or ''}\t{it.why}\n")
        for it in [x for x in items if x.action == "move" and x.dest.name != x.src.name][:10]:
            print(f"  {it.src.name}\n    → {it.dest.relative_to(root)}")
        print(f"清单：{log_path}\n加 --apply 执行")
        return 0
    if not n["move"] and not n["trash"]:
        print("没有要入库的新书")
        return 0
    from . import busy
    trash = Path.home() / ".Trash" / f"英文书库-收件箱重复-{stamp}"
    try:
        with busy.hold("新书入库", kind="io"), open(log_path, "w") as log:
            log.write(f"# 处理\t原路径\t新路径\t原因（重复文件在 {trash}）\n")
            done = apply(items, inbox, trash, log)
    except busy.Busy as ex:
        print(f"{ex}，入库要移动文件，请等它结束再执行。")
        return 2
    print(f"完成：移进书库 {done['move']:,}、重复放废纸篓 {done['trash']:,}、留在收件箱 {done['keep']:,}、"
          f"失败 {done['failed']:,}；清掉空文件夹 {done['pruned']:,} 个\n清单：{log_path}")
    return 1 if done["failed"] else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
