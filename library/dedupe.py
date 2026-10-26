"""每本书、每一期只留一份：其余文件移到废纸篓（不永久删除），并写下清单。
正在读的那一份（书架上有进度）总是优先保留。

  python -m library.dedupe            先看会删什么（不动文件）
  python -m library.dedupe --apply    执行：验证保留的那份能打开、能读出内容，再把其余文件移到废纸篓
  python -m library.dedupe --groups data/biblio.db [--apply]
                                      按书目库判定的「同一本书」（不同版本、译本、副本）合并，同样只留一份

留哪一份：先比质量再看格式。书的每个版本都打开量一遍正文字数：读不出、「�」超过两成的不留；比最长的少 13% 以上的
（删节版、残缺、解析到一半停下）排后面；内容差不多完整的版本之间按 epub > 有文字层的 pdf > mobi > txt；
文字版撑不起内容的绘本画册留翻阅版（没有文字层的 pdf）。杂志/报纸/儿童刊每期留有文字层、文件最大的 pdf。
入库体检（quality_gate）判过的文件（只能翻阅、读不出正文）不当文字版，所以新书入库时去重排在入库和体检之后。
保留的那份验证不过就换同一作品的下一个候选；一个都验证不过的作品整组不动。
套装、丛书、译本共用一个 ISBN：编目归并作品时已按 catalog.same_book 分开（一中一英、两边书名都写了卷册/级别/版次数字而数字不同
（Book 1 / Book 2、第3级 / 第4级、13th / 5th Edition；年份、重名加的 (2) 不算）、书名一个词都不沾）；按作品去重时再用同一条判断兜底，
不像同一本书的不当副本移走（--groups 按书目库判定，译本也合并，不受这条限制）。
废纸篓里按原目录结构放在「英文书库-去重-时间」文件夹下，清单在 logs/dedupe-时间.txt。
"""
from __future__ import annotations

import shutil
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .catalog import Catalog, same_book, text_rank, traditional
from .isolate import Isolated, IsolatedError

def candidates(kind: str, files: list[dict]) -> list[dict]:
    """同一作品的文件按「最适合留下」排序。"""
    if kind == "book":
        return sorted(files, key=lambda f: (text_rank(f)[0] >= 9 and f["fmt"] != "pdf", text_rank(f)[0], traditional(f),
                                            text_rank(f), -f["size"]))          # 同格式的简繁两版留简体
    return sorted(files, key=lambda f: (f["fmt"] != "pdf", bool(f["error"]), -(f["has_text"] or 0), -f["size"]))


def _check(path: str, fmt: str) -> bool:
    """能打开、有页、能读出文字（扫描版能渲染第一页）。在子进程里跑。"""
    p = Path(path)
    if fmt in ("txt",):
        return p.stat().st_size > 200
    if fmt in ("doc", "docx", "chm", "djvu"):
        return False
    import pymupdf
    with pymupdf.open(p) as d:
        if d.page_count < 1:
            return False
        n = d.page_count
        for i in sorted({0, n // 4, n // 2, (3 * n) // 4} & set(range(n))):
            if len(d[i].get_text().strip()) > 50:
                return True
        if fmt == "pdf":                            # 扫描版：能渲染出第一页就算可用
            d[0].get_pixmap(matrix=pymupdf.Matrix(0.2, 0.2))
            return True
        return False


BAD_CHARS = 0.2           # 「�」占正文的比例超过这个：读不好（和入库体检的乱码标准一致）


def _measure(path: str, fmt: str) -> tuple[bool, int]:
    """（能不能用, 正文字数）：打开全书数非空白字符，用来比同一本书各版本完整不完整。在子进程里跑。"""
    p = Path(path)
    if fmt in ("doc", "docx", "chm", "djvu"):
        return False, 0
    if fmt == "txt":
        raw = p.read_bytes()
        for enc in ("utf-8", "gb18030"):
            try:
                text = raw.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        else:
            text = raw.decode("utf-8", "replace")
    else:
        import pymupdf
        with pymupdf.open(p) as d:
            if d.page_count < 1:
                return False, 0
            text = "".join(pg.get_text() for pg in d)
            if fmt == "pdf" and len(text.strip()) <= 50:    # 扫描版：能渲染出第一页就算可用（翻阅）
                d[0].get_pixmap(matrix=pymupdf.Matrix(0.2, 0.2))
                return True, 0
    n = sum(1 for ch in text if not ch.isspace())
    if n <= 50 or text.count("\ufffd") > BAD_CHARS * n:
        return False, n
    return True, n


def quality_order(fs: list[dict], chars: dict, reading: set = frozenset()) -> list[dict]:
    """fs 已按格式偏好排好；chars：路径 → 量出的正文字数（量不出来的为 None，按完整算，不因超时吃亏）。
    明显不完整的排后面、绘本类留翻阅版（与按书目库合并同一规则）；正在读的那份仍排第一，阅读进度不丢。"""
    known = [c for c in chars.values() if c is not None]
    top = max(known, default=0)
    lengths = {f["fid"]: top if chars.get(f["path"]) is None else chars[f["path"]] for f in fs}
    return prefer_reading(prefer_flip_for_pictures(prefer_complete(fs, lengths), lengths), reading)


def prefer_reading(fs: list[dict], reading: set) -> list[dict]:
    """正在读的那一份（书架上有进度）排第一：去重不能让阅读进度丢掉。"""
    return sorted(fs, key=lambda f: f["fid"] not in reading) if reading else fs


def plan(cat: Catalog, reading: set = frozenset()) -> list[tuple[dict, list[dict]]]:
    """[(作品, 候选按优先级排好的文件)]；只含有多个文件的作品。"""
    works = {w["id"]: dict(w) for w in cat.q("SELECT id, kind, title, mag, issue FROM works")}
    by: dict = {}
    for f in cat.q("SELECT * FROM files WHERE work IS NOT NULL"):
        by.setdefault(f["work"], []).append(dict(f))
    out = []
    for wid, fs in by.items():
        if len(fs) > 1 and wid in works:
            out.append((works[wid], prefer_reading(candidates(works[wid]["kind"], fs), reading)))
    return out


def prefer_complete(fs: list[dict], lengths: dict) -> list[dict]:
    """同一本书的不同版本：正文明显短的（比最长的少 13% 以上，比如旧版、删节版）排后面；
    内容差不多完整的版本之间，才按格式偏好（epub 优先）。lengths：文件指纹 → 已入库正文的字数。"""
    longest = max((lengths.get(f["fid"], 0) for f in fs), default=0)
    if not longest:
        return fs
    return sorted(fs, key=lambda f: lengths.get(f["fid"], 0) < 0.87 * longest)


PICTURE_TEXT = 20000      # 文字版不到这么多字：内容主要在图上（绘本、画册、漫画），和判断文字书的标准一致


def prefer_flip_for_pictures(fs: list[dict], lengths: dict) -> list[dict]:
    """组里有翻阅版（没有文字层的 PDF），而文字版正文撑不起内容时，保留翻阅版——
    绘本、画册的文字版只有零散配字，留它会丢掉全部的图。扫描版而文字版完整的，照常留文字版。"""
    flips = [f for f in fs if f["fmt"] == "pdf" and not f.get("has_text")]
    texts = [f for f in fs if f not in flips]
    if not flips or not texts:
        return fs
    if max(lengths.get(f["fid"], 0) for f in texts) >= PICTURE_TEXT:
        return fs
    return flips + texts


def plan_groups(cat: Catalog, groups_db: Path, reading: set = frozenset(),
                lengths: dict | None = None) -> list[tuple[dict, list[dict]]]:
    """书目库判定的「同一本书」（视图 same_book_final(gid, fid, work)：同一作品的不同版本、译本、副本）：
    同一 gid 的作品合成一组，组里所有文件按同样的规则挑保留的那份。fid 是作品文字来源的指纹，以它找当前作品。"""
    import sqlite3
    g = sqlite3.connect(f"file:{groups_db}?mode=ro", uri=True)
    gids: dict = {}
    for gid, fid in g.execute("SELECT gid, fid FROM same_book_final"):
        gids.setdefault(gid, set()).add(fid)
    g.close()
    works = {w["id"]: dict(w) for w in cat.q("SELECT id, kind, title, mag, issue FROM works")}
    work_of = {r["fid"]: r["work"] for r in cat.q("SELECT fid, work FROM files WHERE work IS NOT NULL")}
    files_of: dict = {}
    for f in cat.q("SELECT * FROM files WHERE work IS NOT NULL"):
        files_of.setdefault(f["work"], []).append(dict(f))
    out = []
    for gid, fids in sorted(gids.items()):
        ws = sorted({work_of[f] for f in fids if f in work_of and work_of[f] in works})
        if len(ws) < 2:
            continue                                    # 已经是一个作品（或文件不在了）：普通去重会处理
        fs = [f for w in ws for f in files_of.get(w, [])]
        kind = works[ws[0]]["kind"]
        head = dict(works[ws[0]], merged=ws)           # merged：这一组原来的全部作品编号（迁移增强标签用）
        ordered = prefer_flip_for_pictures(prefer_complete(candidates(kind, fs), lengths or {}), lengths or {})
        out.append((head, prefer_reading(ordered, reading)))
    return out


def run(cat: Catalog, apply: bool, workers: int = 4, items: list | None = None,
        reading: set = frozenset()) -> list[tuple[str, str]]:
    """返回 [(旧作品编号, 保留的文件路径)]：重新编目后作品编号可能变（取组内最小指纹），据此迁移增强标签。"""
    measure = items is None                              # 按作品去重时逐个版本比质量（按书目库合并已按入库字数排过）
    items = plan(cat, reading) if items is None else items
    total = sum(len(fs) - 1 for _, fs in items)
    size = sum(sum(f["size"] for f in fs[1:]) for _, fs in items)
    print(f"有多个文件的作品 {len(items):,} 个；按首选保留，要移走 {total:,} 个文件，约 {size / 1e9:.1f} GB", flush=True)
    if not apply:
        for w, fs in items[:15]:
            print(f"  {w['title']}\n    留：{fs[0]['path']}\n    移：{len(fs) - 1} 个")
        print("加 --apply 执行（先验证保留的那份）")
        return []
    remap: list = []
    aliases: list = []                                   # (移走的 fid, 保留的 fid)：按 fid 记的人工决定据此迁移
    stamp = time.strftime("%Y%m%d-%H%M%S")
    trash = Path.home() / ".Trash" / f"英文书库-去重-{stamp}"
    log_path = cat.path.parent.parent / "logs" / f"dedupe-{stamp}.txt"
    log_path.parent.mkdir(exist_ok=True)
    local = threading.local()
    isos: list = []

    def keep_of(item):
        """第一个验证通过的候选；都不通过返回 None。"""
        iso = getattr(local, "iso", None)
        if iso is None:
            iso = local.iso = Isolated(120)
            isos.append(iso)
        w, fs = item
        fs = [f for f in fs if Path(f["path"]).exists()]
        usable: dict = {}
        if measure and w["kind"] == "book" and len(fs) > 1:
            chars: dict = {}
            for f in fs:
                try:
                    ok, n = iso.call(_measure, f["path"], f["fmt"])
                    usable[f["path"]], chars[f["path"]] = ok, n
                except (IsolatedError, Exception):     # noqa: BLE001 量不出来（超时、坏文件）：下面照常验证
                    chars[f["path"]] = None
            fs = quality_order(fs, chars, reading)
        for f in fs:
            if usable.get(f["path"]) is False:
                continue
            if f["path"] in usable:
                return w, fs, f
            try:
                if iso.call(_check, f["path"], f["fmt"]):
                    return w, fs, f
            except (IsolatedError, Exception):     # noqa: BLE001 坏文件：试下一个
                continue
        return w, fs, None

    moved, skipped, freed, n = 0, 0, 0, 0
    t0 = time.time()
    with open(log_path, "w") as log, ThreadPoolExecutor(workers) as ex:
        log.write(f"# 原路径\t废纸篓里的路径（{trash}）\n")
        for w, fs, keep in ex.map(keep_of, items):
            n += 1
            if keep is None:
                skipped += 1
                log.write(f"# 未处理（没有一份能验证通过）：{w['title']}\n")
                continue
            # 保留那份原来所在的作品排前面：迁移增强标签时优先用它自己的
            olds = w.get("merged") or [w["id"]]
            olds = sorted(olds, key=lambda o: o != keep.get("work"))
            remap += [(o, keep["path"]) for o in olds]
            for f in fs:
                if f is keep:
                    continue
                if measure and w["kind"] == "book" and not same_book(keep, f):
                    log.write(f"# 不像同一本书（语言或卷册不同），没动：{f['path']}（留的是 {keep['path']}）\n")
                    continue
                src = Path(f["path"])
                if not src.exists():
                    continue
                rel = src.relative_to(Path.home()) if str(src).startswith(str(Path.home())) else Path(src.name)
                dest = trash / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                # 先记下这个文件带来的货架/榜单，挂到保留的那一份上（作品的榜单、名次不因去重丢失）
                with cat.lock:
                    cat.conn.execute("INSERT OR REPLACE INTO ghosts(path, keep_fid, tags, moved) VALUES(?,?,?,?)",
                                     (f["path"], keep["fid"], f["tags"], time.time()))
                    cat.conn.commit()
                shutil.move(str(src), str(dest))
                aliases.append((f["fid"], keep["fid"]))
                log.write(f"{src}\t{dest}\n")
                moved += 1
                freed += f["size"]
            if n % 500 == 0:
                log.flush()
                print(f"{n:,}/{len(items):,}  已移走 {moved:,} 个（{freed / 1e9:.1f} GB）  未处理 {skipped}  "
                      f"{n / (time.time() - t0):.0f} 个/秒", flush=True)
    for iso in isos:
        iso.close()
    from .biblio import add_fid_alias
    add_fid_alias(cat.path.parent / "biblio.db", aliases)
    print(f"完成：移到废纸篓 {moved:,} 个文件（{freed / 1e9:.1f} GB），{skipped} 个作品没有可验证的版本、未处理。\n"
          f"废纸篓：{trash}\n清单：{log_path}", flush=True)
    return remap


def moved_works(cat: Catalog, remap: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """重新编目后编号变了的作品：[(旧编号, 新编号)]。remap 是 run() 返回的 (旧编号, 保留文件的路径)。"""
    out = []
    for old, path in remap:
        r = cat.conn.execute("SELECT work FROM files WHERE path=?", (path,)).fetchone()
        if r and r[0] and r[0] != old:
            out.append((old, r[0]))
    return out


def migrate_enrich(cat: Catalog, remap: list[tuple[str, str]]) -> int:
    """作品编号变了的，把评分、难度、简介等迁到新编号上。"""
    from .enrich import db as enrich_db
    c = enrich_db(cat)
    n = 0
    with cat.lock:
        for old, new in moved_works(cat, remap):
            if c.execute("SELECT 1 FROM enrich WHERE work=?", (new,)).fetchone():
                c.execute("DELETE FROM enrich WHERE work=?", (old,))
            else:
                c.execute("UPDATE enrich SET work=? WHERE work=?", (new, old))
                n += c.execute("SELECT changes()").fetchone()[0]
        c.commit()
    return n


# 书目库里按作品编号记「查过没有」的表：编号变了不迁，下一轮会对豆瓣等站点重复查询。
# same_book、me_books 也带作品编号，但每次都整表重建，不用迁。
WORK_MARKS = (("tries", "work"), ("pub_tries", "work"))


def migrate_work_marks(cat: Catalog, remap: list[tuple[str, str]], biblio_path: Path) -> int:
    """把书目库的「查过」记录迁到新编号；新编号已经有同一来源的记录时保留新的、删掉旧的。返回迁移的行数。"""
    pairs = moved_works(cat, remap)
    if not pairs or not biblio_path.exists():
        return 0
    from .biblio import open_db as biblio_db
    c = biblio_db(biblio_path)
    n = 0
    try:
        with c:
            for table, col in WORK_MARKS:
                for old, new in pairs:
                    n += c.execute(f"UPDATE OR IGNORE {table} SET {col}=? WHERE {col}=?", (new, old)).rowcount
                    c.execute(f"DELETE FROM {table} WHERE {col}=?", (old,))
    finally:
        c.close()
    return n


def main(argv: list[str]) -> int:
    from .config import load
    cfg = load()
    cat = Catalog(cfg.data / "catalog.db")
    if cfg.sources:
        print("先更新编目…", flush=True)
        cat.scan(cfg.sources)
    import sqlite3
    reading = set()
    if cfg.db_path.exists():                            # 正在读的书（有阅读进度）：去重时优先保留
        reading = {r[0] for r in sqlite3.connect(cfg.db_path).execute("SELECT id FROM books WHERE last_read IS NOT NULL")}
    items = None
    if "--groups" in argv:                               # 书目库判定的「同一本书」
        lengths = {}
        if cfg.db_path.exists():                        # 各版本已入库的正文字数：内容完整的版本优先
            from .digest import lengths as text_lengths
            lengths = text_lengths(sqlite3.connect(cfg.db_path))
        items = plan_groups(cat, Path(argv[argv.index("--groups") + 1]), reading, lengths)
        print(f"书目库判定的同一本书：{len(items)} 组要合并", flush=True)
    if "--apply" not in argv:
        run(cat, False, items=items, reading=reading)
        return 0
    from . import busy
    try:
        with busy.hold("dedupe", kind="io"):                       # 要移动文件、重新编目：和别的长任务错开
            return _apply(cat, cfg, items, reading)
    except busy.Busy as ex:
        print(f"{ex}，去重要移动文件和重新编目，请等它结束再执行。")
        return 2


def _apply(cat: Catalog, cfg, items, reading) -> int:
    remap = run(cat, True, items=items, reading=reading)
    if cfg.sources:
        print("重新编目…", flush=True)
        cat.scan(cfg.sources)
        print(f"增强标签迁到新编号：{migrate_enrich(cat, remap):,} 条", flush=True)
        print(f"书目库「查过」记录迁到新编号：{migrate_work_marks(cat, remap, cfg.data / 'biblio.db'):,} 条", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
