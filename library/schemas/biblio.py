"""biblio.db 的结构（书目层，以文件指纹 fid 为键，不改 catalog.db）。规则见 library/schema.py：只往 STEPS 末尾追加。

每张表只由一个模块写（V1 里每段前面注明）；书城网站只读。
"""
from __future__ import annotations

from ..schema import add_column

V1 = """
-- library.biblio（ISBN、CIP、上架日期、外部封面）、library.pubdate（pubdates、pub_tries）
-- covers 只经 library.biblio.set_cover 写：biblio.fetch_covers（Open Library）、sources.google_books
CREATE TABLE IF NOT EXISTS isbns(fid TEXT, isbn13 TEXT, src TEXT, PRIMARY KEY(fid, isbn13));
CREATE INDEX IF NOT EXISTS isbns_isbn ON isbns(isbn13);
CREATE TABLE IF NOT EXISTS cip(fid TEXT PRIMARY KEY, clc TEXT, category TEXT, subjects TEXT, series TEXT,
  orig_title TEXT, publisher TEXT, pub_year INT, isbn13 TEXT);
CREATE TABLE IF NOT EXISTS arrivals(fid TEXT PRIMARY KEY, date TEXT, src TEXT);
CREATE TABLE IF NOT EXISTS pubdates(fid TEXT PRIMARY KEY, date TEXT, prec TEXT, src TEXT, url TEXT, isbn13 TEXT, fetched REAL);
CREATE TABLE IF NOT EXISTS pub_tries(work TEXT PRIMARY KEY, status TEXT, at REAL);
CREATE TABLE IF NOT EXISTS covers(fid TEXT PRIMARY KEY, status TEXT, src TEXT, path TEXT, fetched REAL);
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS seen(fid TEXT PRIMARY KEY, size INT, mtime REAL, path TEXT, isbns TEXT, cip TEXT,
  cover INT, err TEXT, ver INT);
-- library.sources.*（外部来源、榜单）、library.classify（categories）
CREATE TABLE IF NOT EXISTS src_tries(source TEXT, fid TEXT, status TEXT, at REAL, PRIMARY KEY(source, fid));
CREATE TABLE IF NOT EXISTS descriptions(fid TEXT, source TEXT, lang TEXT, text TEXT, url TEXT, at REAL,
  PRIMARY KEY(fid, source));
CREATE TABLE IF NOT EXISTS subjects(fid TEXT, source TEXT, subject TEXT, PRIMARY KEY(fid, source, subject));
CREATE TABLE IF NOT EXISTS awards(fid TEXT, award TEXT, award_zh TEXT, year INT, kind TEXT, qid TEXT, source TEXT,
  PRIMARY KEY(fid, award, year, kind));
CREATE TABLE IF NOT EXISTS series(fid TEXT, series TEXT, series_zh TEXT, ordinal REAL, qid TEXT, source TEXT,
  PRIMARY KEY(fid, series));
CREATE TABLE IF NOT EXISTS wd_works(fid TEXT PRIMARY KEY, qid TEXT, title_en TEXT, title_zh TEXT, orig_title TEXT,
  orig_lang TEXT, author_qid TEXT, author_en TEXT, author_zh TEXT, genres TEXT, at REAL);
CREATE TABLE IF NOT EXISTS first_pub(fid TEXT, source TEXT, year INT, PRIMARY KEY(fid, source));
CREATE TABLE IF NOT EXISTS lists(list_id TEXT PRIMARY KEY, name TEXT, family TEXT, period TEXT, source TEXT,
  url TEXT, lang TEXT, fetched REAL);
CREATE TABLE IF NOT EXISTS list_entries(list_id TEXT, rank INT, weeks INT, title TEXT, author TEXT, isbn TEXT,
  fid TEXT, PRIMARY KEY(list_id, rank, title));
CREATE INDEX IF NOT EXISTS list_entries_fid ON list_entries(fid);
CREATE TABLE IF NOT EXISTS categories(fid TEXT PRIMARY KEY, top TEXT, sub TEXT, conf REAL, src TEXT, evidence TEXT, at REAL);
CREATE INDEX IF NOT EXISTS categories_top ON categories(top, sub);
-- library.ratings（评分、综合口碑分）
CREATE TABLE IF NOT EXISTS ratings(fid TEXT, src TEXT, score REAL, scale INT, n INT, ext_id TEXT, url TEXT, fetched REAL,
  PRIMARY KEY(fid, src));
CREATE TABLE IF NOT EXISTS tries(work TEXT, src TEXT, status TEXT, at REAL, PRIMARY KEY(work, src));
CREATE TABLE IF NOT EXISTS score(fid TEXT PRIMARY KEY, score REAL, n INT, src TEXT, shown REAL, shown_n INT);
-- library.translations（原版 ↔ 译本）
CREATE TABLE IF NOT EXISTS translations(tr_fid TEXT, orig_fid TEXT, method TEXT, score REAL, built REAL,
  PRIMARY KEY(tr_fid, orig_fid));
CREATE INDEX IF NOT EXISTS translations_orig ON translations(orig_fid);
CREATE TABLE IF NOT EXISTS translations_manual(tr_fid TEXT, orig_fid TEXT, note TEXT, PRIMARY KEY(tr_fid, orig_fid));
CREATE TABLE IF NOT EXISTS tr_llm(fid TEXT PRIMARY KEY, title TEXT, author TEXT, lang TEXT, asked REAL);
CREATE TABLE IF NOT EXISTS tr_llm_check(tr_fid TEXT, orig_fid TEXT, same INT, zh TEXT, asked REAL, PRIMARY KEY(tr_fid, orig_fid));
CREATE TABLE IF NOT EXISTS tr_llm_recheck(tr_fid TEXT, orig_fid TEXT, same INT, why TEXT, asked REAL, PRIMARY KEY(tr_fid, orig_fid));
CREATE TABLE IF NOT EXISTS translations_block(tr_fid TEXT, orig_fid TEXT, note TEXT, PRIMARY KEY(tr_fid, orig_fid));
-- library.zh（英文书的中文信息）
CREATE TABLE IF NOT EXISTS zh_titles(fid TEXT PRIMARY KEY, title TEXT, author TEXT, src TEXT, built REAL);
CREATE TABLE IF NOT EXISTS zh_text(kind TEXT, key TEXT, zh TEXT, model TEXT, at REAL, PRIMARY KEY(kind, key));
-- library.match（同一本书）、library.review（人工确认）
CREATE TABLE IF NOT EXISTS same_book(gid TEXT, fid TEXT, work TEXT, level TEXT, reason TEXT, score REAL, built REAL,
  PRIMARY KEY(gid, fid));
CREATE INDEX IF NOT EXISTS same_book_work ON same_book(work);
CREATE TABLE IF NOT EXISTS same_book_review(gid TEXT PRIMARY KEY, decision TEXT, decided_at REAL);
CREATE VIEW IF NOT EXISTS same_book_final AS SELECT s.gid, s.fid, s.work FROM same_book s
  LEFT JOIN same_book_review r ON r.gid=s.gid
  WHERE (s.level='auto' AND COALESCE(r.decision, '')<>'different') OR r.decision='same';
-- library.classify（模型分类的回答缓存）
CREATE TABLE IF NOT EXISTS cls_model(fid TEXT PRIMARY KEY, top TEXT, sub TEXT, model TEXT, at REAL);
-- library.taste（阅读口味）
CREATE TABLE IF NOT EXISTS me_books(fid TEXT PRIMARY KEY, work TEXT, status TEXT, rating INT, date TEXT, douban TEXT, how TEXT);
CREATE INDEX IF NOT EXISTS me_books_status ON me_books(status);
CREATE TABLE IF NOT EXISTS me_authors(akey TEXT PRIMARY KEY, name TEXT, weight REAL, src TEXT);
CREATE TABLE IF NOT EXISTS me_taste(top TEXT, sub TEXT, weight REAL, n INT, PRIMARY KEY(top, sub));
"""

STEPS = [
    (1, V1),                                              # 版本管理之前就有的表（原样）
    (2, add_column("ratings", "ext_title", "TEXT")),      # 外部来源的书名：核对、抽查用
    # library.sources.wikidata zhcn：中文维基按大陆用法显示的书名（Wikidata 的 zh 标签常是台版译名）
    (3, "CREATE TABLE IF NOT EXISTS wd_zh_cn(qid TEXT PRIMARY KEY, page TEXT, title TEXT, status TEXT, at REAL);"),
    # library.covers（只经 biblio.set_reject 写）：文件自带的封面不能用的作品。verdict：bad 人工确认是错图
    # （扉页、出版社 logo、生成的占位图）；suspect 机器初筛像扉页（可能是极简设计的真封面）。work 是标记时的作品编号
    (4, "CREATE TABLE IF NOT EXISTS cover_reject(fid TEXT PRIMARY KEY, work TEXT, verdict TEXT, why TEXT, at REAL);"),
    # library.translations：向量候选（similar.db cross_candidates）里核对不了的对，留给人工确认。
    # decision：NULL 待确认；same 是同一部（重建时连上）；different 不是（不再列出）。重建只刷新没确认的行
    (5, "CREATE TABLE IF NOT EXISTS tr_review(zh_fid TEXT, en_fid TEXT, cos REAL, reason TEXT, decision TEXT, at REAL, "
        "PRIMARY KEY(zh_fid, en_fid));"),
    # library.dedupe（只经 biblio.add_fid_alias 写）：去重移走的文件 old_fid → 同一本书保留下来的那份 new_fid。
    # 按 fid 记的人工决定（原版译本关联等）据此迁到保留的那份上
    (6, "CREATE TABLE IF NOT EXISTS fid_alias(old_fid TEXT PRIMARY KEY, new_fid TEXT, at REAL);"),
    # library.origin：中文正文、编目看不出是不是译本的书，本地模型判断的原著语言（zh＝中文原创）。
    # sure＝模型有把握；书城只认有把握且不是 zh 的，把它归进「译文」
    (7, "CREATE TABLE IF NOT EXISTS zh_origin(fid TEXT PRIMARY KEY, lang TEXT, sure INT, why TEXT, model TEXT, asked REAL);"),
]


def ensure(c) -> None:
    from ..schema import ensure as _ensure
    _ensure(c, STEPS)
