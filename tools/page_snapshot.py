"""重构前后对比网页输出：把数据库冻结成一份快照，用同一组 URL 请求网站，保存每个响应，再逐字节比对。

  .venv/bin/python tools/page_snapshot.py freeze DIR             把 data/ 的库拷成快照（library.db 只拷书目，不拷全文）
  .venv/bin/python tools/page_snapshot.py urls DIR               按快照里的数据挑一组 URL，写到 DIR/urls.json
  .venv/bin/python tools/page_snapshot.py take DIR OUT [CODE]    用 CODE 目录下的代码（默认本仓库）请求 urls.json，响应存到 OUT
  .venv/bin/python tools/page_snapshot.py diff OUT1 OUT2         比对两次的结果

只发 GET 请求；快照目录之外不写任何东西（封面、翻页图的缓存也写在快照目录里）。
后台任务一直在写真实的书目库，直接拿真实库比对，两次之间数据会变，所以先冻结快照。
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def freeze(dest: Path) -> None:
    src = ROOT / "data"
    data = dest / "data"
    data.mkdir(parents=True, exist_ok=True)
    for name in ("catalog.db", "biblio.db", "search.db", "similar.db"):
        if (src / name).exists():
            s = sqlite3.connect(f"file:{src / name}?mode=ro", uri=True)
            d = sqlite3.connect(data / name)
            s.backup(d)
            d.close()
            s.close()
    lib = sqlite3.connect(data / "library.db")
    lib.execute("ATTACH DATABASE ? AS o", (f"file:{src / 'library.db'}?mode=ro",))
    for (sql,) in lib.execute("SELECT sql FROM o.sqlite_master WHERE type='table' AND name IN "
                              "('books','ai_cache','guides','digest')").fetchall():
        lib.execute(sql)
    for t in ("books", "ai_cache", "guides", "digest"):
        lib.execute(f"INSERT INTO {t} SELECT * FROM o.{t}")
    keep = "SELECT id FROM books WHERE last_read IS NOT NULL"
    lib.execute("CREATE TABLE paras AS SELECT * FROM o.paras WHERE book IN (" + keep + ")")
    lib.execute(f"UPDATE books SET stored = id IN ({keep})")
    from library.db import VERSION
    lib.execute(f"PRAGMA user_version = {VERSION}")
    lib.commit()
    lib.execute("DETACH DATABASE o")
    lib.close()
    from library.db import DB
    db = DB(data / "library.db")                       # 补上索引、全文索引表
    db.conn.commit()
    for name in ("dict.db",):
        if (src / name).exists() and not (data / name).exists():
            os.symlink(src / name, data / name)
    cfg = json.loads((src / "config.json").read_text()) if (src / "config.json").exists() else {}
    (data / "config.json").write_text(json.dumps({k: cfg[k] for k in ("sources", "model", "guide_model") if k in cfg}))
    print("快照：", data)


def urls(dest: Path) -> list[str]:
    data = dest / "data"
    cat = sqlite3.connect(data / "catalog.db")
    bib = sqlite3.connect(data / "biblio.db")
    lib = sqlite3.connect(data / "library.db")
    one = lambda c, sql, *a: (c.execute(sql, a).fetchone() or [None])[0]
    many = lambda c, sql, *a: [r[0] for r in c.execute(sql, a).fetchall()]
    u = ["/", "/store", "/store?tab=zh", "/store?tab=magazine", "/store?tab=newspaper", "/store?tab=kids",
         "/shelf", "/admin/same-book", "/admin/same-book?tab=done", "/admin/same-book?tab=auto&page=2",
         "/api/health", "/api/guide"]
    read = one(lib, "SELECT id FROM books WHERE last_read IS NOT NULL")
    if read:
        u += [f"/book/{read}", f"/read/{read}", f"/cover/{read}.jpg", f"/api/books/{read}/text?from=0&count=5",
              f"/api/books/{read}/text?from=100&count=40", f"/api/books/{read}/prep?start=0&end=8"]
    for bid in many(lib, "SELECT id FROM books WHERE last_read IS NULL AND status='ok' ORDER BY id LIMIT 3"):
        u += [f"/book/{bid}", f"/cover/{bid}.jpg"]
    # 列表：各种筛选、排序、翻页
    for q in ["sort=rating", "sort=new", "sort=title&p=2", "lang=zh", "lang=en&rated=1", "new=1", "kind=magazine",
              "level=B2", "tab=zh&sort=rating", "q=gatsby", "q=%E7%BA%A2%E6%A5%BC", "q=hongloumeng", "q=orwell&p=2"]:
        u.append(f"/store/list?{q}")
    for top, sub in bib.execute("SELECT top, sub FROM categories WHERE sub IS NOT NULL GROUP BY top, sub "
                                "ORDER BY COUNT(*) DESC LIMIT 3"):
        u += [f"/store/list?top={top}", f"/store/list?top={top}&sub={sub}&sort=rating"]
    from urllib.parse import quote
    for name in many(cat, "SELECT name FROM shelves GROUP BY name ORDER BY COUNT(*) DESC LIMIT 3"):
        u.append(f"/store/list?shelf={quote(name)}")
    for fam in many(bib, "SELECT family FROM lists WHERE family IS NOT NULL GROUP BY family ORDER BY family LIMIT 3"):
        u += [f"/store/list?fam={quote(fam)}", f"/store/list?listfam={quote(fam)}"]
    for fam, per in bib.execute("SELECT family, period FROM lists WHERE family IS NOT NULL AND period IS NOT NULL "
                                "ORDER BY family, period DESC LIMIT 2"):
        u.append(f"/store/list?listfam={quote(fam)}&period={quote(per)}")
    for lid in many(bib, "SELECT list_id FROM lists ORDER BY list_id LIMIT 2"):
        u.append(f"/store/list?list={quote(lid)}")
    for q in ["g", "gats", "红楼", "hlm", "orwell", "三体", ""]:
        u.append(f"/store/suggest?q={quote(q)}")
    # 详情：英文书、中文书、有评分、有奖项、有译本、有系列、没有文字、杂志、套装
    picks = [
        ("SELECT id FROM works WHERE kind='book' AND lang='en' ORDER BY id LIMIT 2", cat),
        ("SELECT id FROM works WHERE kind='book' AND lang='zh' ORDER BY id LIMIT 2", cat),
        ("SELECT id FROM works WHERE kind='book' AND text_src IS NULL AND layout_src IS NOT NULL ORDER BY id LIMIT 1", cat),
        ("SELECT id FROM works WHERE kind='magazine' ORDER BY id LIMIT 1", cat),
        ("SELECT id FROM works WHERE kind='kids' ORDER BY id LIMIT 1", cat),
        ("SELECT id FROM works WHERE kind='book' AND title LIKE '%套装%' ORDER BY id LIMIT 1", cat),
    ]
    wids = [w for sql, c in picks for w in many(c, sql)]
    for sql in ["SELECT fid FROM score ORDER BY score DESC, fid LIMIT 2", "SELECT fid FROM awards ORDER BY fid LIMIT 2",
                "SELECT tr_fid FROM translations ORDER BY tr_fid LIMIT 1", "SELECT orig_fid FROM translations ORDER BY orig_fid LIMIT 1",
                "SELECT fid FROM series ORDER BY fid LIMIT 1", "SELECT fid FROM descriptions ORDER BY fid LIMIT 1"]:
        for f in many(bib, sql):
            w = one(cat, "SELECT work FROM files WHERE fid=?", f)
            if w:
                wids.append(w)
    wids = list(dict.fromkeys(wids))
    u += [f"/store/w/{w}" for w in wids] + [f"/store/cover/{w}" for w in wids[:4]]
    mag = cat.execute("SELECT kind, mag FROM works WHERE kind IN ('magazine','newspaper') AND mag IS NOT NULL "
                      "GROUP BY kind ORDER BY kind").fetchall()
    u += [f"/store/mag?kind={k}&name={quote(m)}" for k, m in mag]
    flip = one(cat, "SELECT id FROM works WHERE layout_src IS NOT NULL AND kind='magazine' ORDER BY id LIMIT 1")
    if flip:
        u += [f"/store/read/{flip}", f"/store/read/{flip}?p=2", f"/store/page/{flip}/0.jpg"]
    (dest / "urls.json").write_text(json.dumps(u, ensure_ascii=False, indent=0))
    print(len(u), "个 URL →", dest / "urls.json")
    return u


TAKE = r"""
import hashlib, json, os, sys
from pathlib import Path
code, dest, out = sys.argv[1], Path(sys.argv[2]), Path(sys.argv[3])
sys.path.insert(0, code)
os.environ["LIBRARY_DATA"] = str(dest / "data")
from fastapi.testclient import TestClient
from library.config import load
from library.web import create_app
import library.web
assert library.web.__file__.startswith(code), library.web.__file__
c = TestClient(create_app(load()))
out.mkdir(parents=True, exist_ok=True)
man = {}
for i, u in enumerate(json.loads((dest / "urls.json").read_text())):
    r = c.get(u, follow_redirects=False)
    body = r.content
    (out / f"{i:03d}.bin").write_bytes(body)
    man[u] = {"i": i, "status": r.status_code, "type": r.headers.get("content-type", ""),
              "location": r.headers.get("location", ""), "len": len(body), "sha": hashlib.sha256(body).hexdigest()}
(out / "manifest.json").write_text(json.dumps(man, ensure_ascii=False, indent=1))
print(len(man), "个响应 →", out, "；状态", sorted({m["status"] for m in man.values()}))
"""


def take(dest: Path, out: Path, code: Path) -> None:
    subprocess.run([sys.executable, "-c", TAKE, str(code), str(dest), str(out)], check=True, cwd=dest)


def diff(a: Path, b: Path) -> int:
    ma, mb = (json.loads((x / "manifest.json").read_text()) for x in (a, b))
    bad = [u for u in ma if ma[u] != mb.get(u)] + [u for u in mb if u not in ma]
    for u in bad:
        print("不同：", u, ma.get(u), mb.get(u))
    print(f"{len(ma)} 个 URL，不同 {len(bad)} 个")
    return 1 if bad else 0


def main(argv: list[str]) -> int:
    cmd = argv[0] if argv else ""
    if cmd == "freeze":
        freeze(Path(argv[1]))
    elif cmd == "urls":
        urls(Path(argv[1]))
    elif cmd == "take":
        take(Path(argv[1]), Path(argv[2]), Path(argv[3]) if len(argv) > 3 else ROOT)
    elif cmd == "diff":
        return diff(Path(argv[1]), Path(argv[2]))
    else:
        print(__doc__)
        return 1
    return 0


if __name__ == "__main__":
    sys.path.insert(0, str(ROOT))
    sys.exit(main(sys.argv[1:]))
