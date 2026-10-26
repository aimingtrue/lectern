"""catalog.db 的结构（编目，与阅读用的 library.db 分开）。规则见 library/schema.py：只往 STEPS 末尾追加。

表 → 写入方
  files works shelves ghosts works_fts   library.catalog（编目）、library.dedupe（去重时记 ghosts）
  files.gate                             只经 Catalog.set_gate 写：tools/quality_gate（扫描版→flip）、library.scan（入库失败→unreadable）
  enrich                                 library.enrich（联网书目、难度、类别）；library.dedupe 迁移作品编号
  files（删行）works                      library.exclude：排除的书移到废纸篓后删掉那些文件、重新归并
"""
from __future__ import annotations

from ..schema import add_column

V1 = """
CREATE TABLE IF NOT EXISTS files(
  path TEXT PRIMARY KEY, size INT, mtime REAL, fid TEXT, fmt TEXT, kind TEXT,
  title TEXT, author TEXT, meta_title TEXT, meta_author TEXT, isbn TEXT, lang TEXT, pages INT, has_text INT,
  mag TEXT, issue TEXT, issue_label TEXT, tags TEXT, error TEXT, ver INT, amb TEXT,
  work TEXT, role TEXT
);
CREATE INDEX IF NOT EXISTS files_fid ON files(fid);
CREATE INDEX IF NOT EXISTS files_work ON files(work);
CREATE TABLE IF NOT EXISTS works(
  id TEXT PRIMARY KEY, kind TEXT, title TEXT, author TEXT, lang TEXT, isbn TEXT,
  mag TEXT, issue TEXT, issue_label TEXT, formats TEXT, text_src TEXT, layout_src TEXT,
  n_files INT, bytes INT, pages INT, has_text INT, tags TEXT, sort_title TEXT, added REAL
);
CREATE INDEX IF NOT EXISTS works_kind ON works(kind, sort_title);
CREATE INDEX IF NOT EXISTS works_mag ON works(mag, issue);
CREATE TABLE IF NOT EXISTS shelves(name TEXT, work TEXT, rank INT, PRIMARY KEY(name, work)) WITHOUT ROWID;
-- 去重时移走的文件：它所在目录对应的货架/榜单记在这里，挂到保留下来的那一份（keep_fid）上，重新编目时并回作品
CREATE TABLE IF NOT EXISTS ghosts(path TEXT PRIMARY KEY, keep_fid TEXT, tags TEXT, moved REAL);
CREATE INDEX IF NOT EXISTS ghosts_keep ON ghosts(keep_fid);
CREATE VIRTUAL TABLE IF NOT EXISTS works_fts USING fts5(id UNINDEXED, title, author, mag, tags, tokenize='unicode61');
CREATE TABLE IF NOT EXISTS enrich(
  work TEXT PRIMARY KEY, ol_status TEXT, ol_key TEXT, year INT, subjects TEXT, rating REAL, rating_n INT,
  pages INT, description TEXT, ol_at REAL,
  level TEXT, v95 INT, v98 INT, sent_len REAL, rare REAL, level_at REAL,
  category TEXT, cat_src TEXT, summary TEXT, label_at REAL
);
CREATE INDEX IF NOT EXISTS enrich_cat ON enrich(category);
CREATE INDEX IF NOT EXISTS enrich_level ON enrich(level);
"""

STEPS = [
    (1, V1),                                              # 版本管理之前就有的表（原样）
    (2, add_column("enrich", "ol_author", "TEXT")),       # 编目里没有作者时，用 Open Library 的作者补上
    # 质量关卡/入库对文件的结论：flip＝只能翻阅（扫描版、图像书），unreadable＝读不出正文。重新编目时保留（文件内容没变的话）
    (3, add_column("files", "gate", "TEXT")),
    # 很早的版本把书城视图 wv 建成了持久视图；现在每个连接各建一个临时视图，同名的旧视图去掉（书城的连接是只读的，删不了）
    (4, "DROP VIEW IF EXISTS wv;"),
    # 中文正文的书看得出是外国作品译本时的依据（国籍 / 音译人名 / 译者 / 版权页 / 译丛 / 外文作者名）：书城单列「译文」
    (5, lambda c: (add_column("files", "trans", "TEXT")(c), add_column("works", "trans", "TEXT")(c))),
]


def ensure(c) -> None:
    from ..schema import ensure as _ensure
    _ensure(c, STEPS)
