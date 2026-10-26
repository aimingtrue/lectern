"""exclude.db 的结构（排除书库：不想要、不会读的书）。规则见 library/schema.py：只往 STEPS 末尾追加。

和 catalog.db 分开放：编目可以重建，排除是不可再生的个人决定，不能跟着编目一起丢。
全部表只由 library.exclude 写；library.catalog 编目时只读 blocked，命中的文件经 exclude.hold 记进 held。

  blocked  排除的书：记的是「这本书是谁」而不是路径，以后再导入同一本（别的副本、格式、版本）都认得出
           kind：fid（文件指纹）/ isbn / ta（规范化书名 + 作者，和编目归并作品用的同一个键）/ author（match.author_key，只能手动加）
           / mag（catalog.mag_key：按 "mag:" 规则排除的刊，以后的新一期也拦下）
  kept     复核时说「留着」的书（键同上，没有 author）：规则以后不再把它报出来
  held     编目时命中 blocked 的新文件：没进编目，等 sweep 移到废纸篓
  runs     每次移动文件：动作、数量、清单
  txt_layout  「质量」规则查过的 txt 排版结论（exclude.txt_flaw，空串 = 没毛病或正文里有版权页）：按指纹缓存，
           ver 不等于 exclude.LAYOUT_VER 的重查
"""
from __future__ import annotations

V1 = """
CREATE TABLE IF NOT EXISTS blocked(kind TEXT, key TEXT, why TEXT, rule TEXT, title TEXT, author TEXT, run TEXT, at REAL,
  PRIMARY KEY(kind, key)) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS blocked_run ON blocked(run);
CREATE TABLE IF NOT EXISTS kept(kind TEXT, key TEXT, why TEXT, title TEXT, author TEXT, at REAL,
  PRIMARY KEY(kind, key)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS held(path TEXT PRIMARY KEY, fid TEXT, kind TEXT, key TEXT, title TEXT, at REAL);
CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY, action TEXT, works INT, files INT, bytes INT, manifest TEXT, at REAL);
"""

V2 = """
CREATE TABLE IF NOT EXISTS txt_layout(fid TEXT PRIMARY KEY, flaw TEXT, ver INT, at REAL) WITHOUT ROWID;
"""

STEPS = [
    (1, V1),
    (2, V2),
]


def ensure(c) -> None:
    from ..schema import ensure as _ensure
    _ensure(c, STEPS)
