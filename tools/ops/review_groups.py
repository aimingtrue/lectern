"""同一本书合并前的检查（只读）：多少组、移走多少文件、跨语言的组、保留的比移走的小很多的、涉及正在读的书。"""
import sqlite3, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


def main():
    from library.catalog import Catalog
    from library.dedupe import plan_groups
    from library.config import load
    cfg = load()
    cat = Catalog(cfg.data / "catalog.db")
    reading = {r[0] for r in sqlite3.connect(cfg.db_path).execute("SELECT id FROM books WHERE last_read IS NOT NULL")}
    items = plan_groups(cat, cfg.data / "biblio.db", reading)
    lang = {r["id"]: r["lang"] for r in cat.q("SELECT id, lang FROM works")}
    moved = sum(len(fs) - 1 for _, fs in items)
    print(f"{len(items)} 组，移走 {moved} 个文件")
    cross = [w for w, fs in items if len({'zh' if lang.get(x) == 'zh' else 'en' for x in w['merged']}) > 1]
    print("跨语言的组：", len(cross), [w["title"][:20] for w in cross[:10]])
    small = []
    for w, fs in items:
        keep, others = fs[0], fs[1:]
        if keep["fmt"] in ("epub", "mobi") and any(o["fmt"] in ("epub", "mobi") and o["size"] > keep["size"] * 3 for o in others):
            small.append((w["title"][:24], keep["size"] // 1024, [o["size"] // 1024 for o in others]))
    print("保留的电子书比被移走的小 3 倍以上：", len(small))
    for x in small[:15]: print("   ", x)
    rd = [w["title"][:20] for w, fs in items if any(f["fid"] in reading for f in fs)]
    print("涉及正在读的书：", rd)
    from collections import Counter
    print("保留格式：", Counter(fs[0]["fmt"] for _, fs in items), " 移走格式：", Counter(f["fmt"] for _, fs in items for f in fs[1:]))


if __name__ == "__main__":
    main()

