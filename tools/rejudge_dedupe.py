"""重审一次去重：那次移到废纸篓的版本和留下的那份按现在的规则（先比质量再看格式，library.dedupe.quality_order）
重新比一遍：废纸篓里那份量得出字数、而且更好的，或者其实不是同一本书（语言、卷册数字不同，dedupe.same_book）的，移回原处，并撤掉那次为它写下的 ghosts / fid_alias；最后重新编目、把增强标签和
「查过」记录迁到编目后的作品编号。真正挑哪份留下交给下一次去重（新书入库流水线会跑）。

  .venv/bin/python tools/rejudge_dedupe.py logs/dedupe-时间.txt            先看（不动文件），报告写到 logs/
  .venv/bin/python tools/rejudge_dedupe.py logs/dedupe-时间.txt --apply    执行
"""
from __future__ import annotations

import shutil
import sqlite3
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from library import busy, dedupe  # noqa: E402
from library.catalog import Catalog, describe  # noqa: E402
from library.config import load  # noqa: E402
from library.extract import book_id  # noqa: E402
from library.isolate import Isolated, IsolatedError  # noqa: E402


def main(argv: list[str]) -> int:
    log_path, apply = Path(argv[0]), "--apply" in argv
    cfg = load()
    cat = Catalog(cfg.data / "catalog.db")
    rows = [ln.rstrip("\n").split("\t") for ln in open(log_path, encoding="utf-8") if not ln.startswith("#")]
    keep_fid = dict(cat.conn.execute(f"SELECT path, keep_fid FROM ghosts WHERE path IN ({','.join('?' * len(rows))})",
                                     [r[0] for r in rows]).fetchall())
    files = {r["fid"]: dict(r) for r in cat.q("SELECT * FROM files")}
    kinds = {r["id"]: r["kind"] for r in cat.q("SELECT id, kind FROM works")}
    reading = set()
    if cfg.db_path.exists():
        reading = {r[0] for r in sqlite3.connect(cfg.db_path).execute("SELECT id FROM books WHERE last_read IS NOT NULL")}
    groups: dict = {}
    for src, dest in rows:
        k = keep_fid.get(src)
        if k in files and Path(dest).exists() and kinds.get(files[k]["work"]) == "book":
            groups.setdefault(k, []).append((src, dest))
    print(f"那次移走 {len(rows):,} 个；按书比的 {sum(map(len, groups.values())):,} 个，分属 {len(groups):,} 本书", flush=True)

    local, isos = threading.local(), []

    def judge(item):
        k, trashed = item
        iso = getattr(local, "iso", None)
        if iso is None:
            iso = local.iso = Isolated(120)
            isos.append(iso)
        keep = files[k]
        fs = [keep]
        for src, dest in trashed:
            f = dict(path=dest, src=src, fid=book_id(Path(dest)), fmt=Path(dest).suffix[1:].lower(),
                     size=Path(dest).stat().st_size, has_text=None, error=None, gate=None, meta_title="", title="", lang="")
            try:
                fi = describe(Path(dest), Path(dest).parent)
                f.update(title=fi.title, lang=fi.lang, meta_title=fi.meta_title)
            except Exception:                      # noqa: BLE001 读不出元数据：书名语言留空，不拦
                pass
            fs.append(f)
        others = [f for f in fs[1:] if not dedupe.same_book(keep, f)]
        fs = [f for f in fs if f not in others]
        chars, usable = {}, {}
        for f in fs:
            try:
                usable[f["path"]], chars[f["path"]] = iso.call(dedupe._measure, f["path"], f["fmt"])
            except (IsolatedError, Exception):     # noqa: BLE001
                chars[f["path"]] = None
            if f["has_text"] is None:
                f["has_text"] = int(bool(chars.get(f["path"])))
        order = dedupe.quality_order(dedupe.candidates("book", fs), chars, reading)
        best = next((f for f in order if usable.get(f["path"]) is not False), None)
        if best is not None and best is not keep:
            b, k = chars.get(best["path"]), chars.get(keep["path"])
            flip = b == 0 and best["fmt"] == "pdf" and (k or 0) < dedupe.PICTURE_TEXT
            clearly = usable.get(keep["path"]) is False or (b and k is not None and k < 0.87 * b) or flip
            if b is None or not clearly:
                best = keep                        # 量不出字数、或只是格式偏好/打平的：不翻案，免得来回搬
        return keep, best, chars, others

    restore, report = [], []
    with ThreadPoolExecutor(4) as ex:
        for i, (keep, best, chars, others) in enumerate(ex.map(judge, groups.items()), 1):
            if best is not None and best is not keep:
                restore.append((keep, best))
                report.append(f"更好\t{best['src']}\t{chars.get(best['path'])}\t留着的\t{keep['path']}\t{chars.get(keep['path'])}")
            for f in others:
                restore.append((keep, f))
                report.append(f"不是同一本\t{f['src']}\t{f['lang']} {f['title']}\t留着的\t{keep['path']}\t{keep['lang']} {keep['title']}")
            if i % 500 == 0:
                print(f"{i:,}/{len(groups):,}  要移回 {len(restore):,}", flush=True)
    for iso in isos:
        iso.close()
    out = ROOT / "logs" / f"rejudge-{log_path.stem}.tsv"
    out.write_text("# 处理\t废纸篓那份的原路径\t字数\t\t那次留下的\t字数\n" + "\n".join(report) + "\n")
    print(f"废纸篓里那份更好、要移回的：{len(restore):,} 本\n报告：{out}", flush=True)
    if not apply or not restore:
        return 0
    with busy.hold("去重", kind="io"):
        before = {keep["path"]: keep["work"] for keep, _ in restore}
        moved = []
        for keep, best in restore:
            src = Path(best["src"])
            if src.exists():
                continue
            src.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(best["path"], src)
            moved.append(best)
        with cat.lock:
            cat.conn.executemany("DELETE FROM ghosts WHERE path=?", [(b["src"],) for b in moved])
            cat.conn.commit()
        b = sqlite3.connect(cfg.data / "biblio.db")
        with b:
            b.executemany("DELETE FROM fid_alias WHERE old_fid=?", [(f["fid"],) for f in moved])
        b.close()
        print(f"移回 {len(moved):,} 个；重新编目…", flush=True)
        cat.scan(cfg.sources)
        remap = list(before.items())
        remap = [(work, path) for path, work in remap]
        print(f"增强标签迁到新编号：{dedupe.migrate_enrich(cat, remap):,} 条；"
              f"「查过」记录：{dedupe.migrate_work_marks(cat, remap, cfg.data / 'biblio.db'):,} 条", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
