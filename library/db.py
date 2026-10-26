"""SQLite 存储。正文按段存（每段带起始页和句子偏移），一本书一个事务写入，适合上万本书。

全文（paras）只给打开阅读过的书存（books.stored=1）；扫描入库的书只存书目和摘要（digest 表，见 library/digest.py），
第一次打开时再解析存全文。摘要（每本 11 KB，3.4 万本 550 MB，占过 library.db 的 81%）从版本 4 起放在旁边的
data/digest.db 里，每个连接打开时 ATTACH 为 dg；SQL 里不带前缀的 digest 自动解析到它（main 里已没有这张表）。
library.db 只剩书目、进度、读过的书的全文和 AI 结果，备份、升级、VACUUM 都快得多。

数据库版本用 PRAGMA user_version 记录；会删表、删数据的升级前先把整个库备份成 library.db.bak-<时间>，
只加列、加表的升级（2 → 3）不备份；3 → 4 把 digest 搬到 digest.db、删掉没有读取方的 books_fts 和旧前缀的 ai_cache，
搬完 VACUUM。
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from pathlib import Path

from . import digest
from . import threadconn
from .text import is_zh, sentences

log = logging.getLogger(__name__)
VERSION = 5

SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS books(
  id TEXT PRIMARY KEY, path TEXT NOT NULL, size INT, mtime REAL,
  title TEXT, author TEXT, pages INT, has_text INT, lang TEXT, typo_cover INT DEFAULT 0,
  toc TEXT, added REAL, status TEXT DEFAULT 'ok', error TEXT,
  last_read REAL, progress_page INT DEFAULT 0, progress_frac REAL DEFAULT 0, mode TEXT,
  parser_ver INT DEFAULT 0, n_paras INT DEFAULT 0, progress_para INT DEFAULT 0, progress_sent INT DEFAULT 0,
  quality TEXT, stored INT DEFAULT 1
);
CREATE INDEX IF NOT EXISTS books_path ON books(path);
CREATE INDEX IF NOT EXISTS books_title ON books(title COLLATE NOCASE);
CREATE INDEX IF NOT EXISTS books_author ON books(author COLLATE NOCASE);
CREATE INDEX IF NOT EXISTS books_added ON books(added);
CREATE INDEX IF NOT EXISTS books_last ON books(last_read);
CREATE TABLE IF NOT EXISTS paras(
  book TEXT, n INT, page INT, kind TEXT, text TEXT, sents TEXT, PRIMARY KEY(book, n)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS ai_cache(key TEXT PRIMARY KEY, value TEXT, created REAL);
CREATE TABLE IF NOT EXISTS guides(book TEXT, chapter INT, title TEXT, md TEXT, created REAL, PRIMARY KEY(book, chapter));
CREATE TABLE IF NOT EXISTS cast(book TEXT, chapter INT, json TEXT, created REAL, PRIMARY KEY(book, chapter));
-- 读法（library/bookkind.py）：外文 en / 文言 wy / 白话 zh，右栏导图画人物关系还是概念结构；每本书定一次
CREATE TABLE IF NOT EXISTS book_kind(book TEXT PRIMARY KEY, json TEXT, created REAL) WITHOUT ROWID;
-- 概念导图（library/concepts.py）：按目录最细到二级的小节抽的概念、关系和这一节的主旨；sec = 目录序号
CREATE TABLE IF NOT EXISTS maps(book TEXT, sec INT, json TEXT, created REAL, PRIMARY KEY(book, sec)) WITHOUT ROWID;
-- 对白归属（library/speech.py）：speech = 每段里的对白区间 [{a, b, who}]（码点偏移，who 是人物称呼）；
-- speech_done = 已归属的段落范围；speakers = 人物的性别、年龄（child / adult）和分到的音色（第一次定下，以后不变）
CREATE TABLE IF NOT EXISTS speech(book TEXT, para INT, json TEXT, PRIMARY KEY(book, para)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS speech_done(book TEXT, start INT, end INT, created REAL, PRIMARY KEY(book, start)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS speakers(book TEXT, name TEXT, gender TEXT, voice TEXT, created REAL, age TEXT, PRIMARY KEY(book, name)) WITHOUT ROWID;
-- voice_lock = 角色音色锁定的书：已分好的音色不再变（改 cast_order、标 cast: false 都不影响，recast 要 --force），新出现的人物照常分
CREATE TABLE IF NOT EXISTS voice_lock(book TEXT PRIMARY KEY, created REAL) WITHOUT ROWID;
-- 阅读时长：按（本地日期 YYYY-MM-DD, 书）累计秒数，listen 是其中朗读的秒数。书删了、移出书架都不清
CREATE TABLE IF NOT EXISTS reading_log(day TEXT, book TEXT, sec INT DEFAULT 0, listen INT DEFAULT 0,
  PRIMARY KEY(day, book)) WITHOUT ROWID;
-- 进度流水：阅读页每次存进度一行（at = 写进 books.last_read 的时间，tab = 哪个页面，ok = 0 是被拒的旧页面）。
-- 判断「服务端那条是不是我自己写的」靠它；进度出了岔子也能从这里找回读到过的位置。保留 60 天
CREATE TABLE IF NOT EXISTS progress_log(book TEXT, at REAL, para INT, sent INT, tab TEXT, ok INT);
CREATE INDEX IF NOT EXISTS progress_log_book ON progress_log(book, at);
"""
# 旧库（版本 0）的 books 表没有这些列
NEW_COLS = [("parser_ver", "INT DEFAULT 0"), ("n_paras", "INT DEFAULT 0"), ("progress_para", "INT DEFAULT 0"),
            ("progress_sent", "INT DEFAULT 0"), ("quality", "TEXT"), ("stored", "INT DEFAULT 1")]
# 版本 2 去掉的：划线笔记、生词本、按页存的正文和正文全文索引
DROPPED = ["pages_fts", "pages", "marks", "vocab"]
# 版本 4：摘要搬到 digest.db；books_fts 没有读取方；ai_cache 里 t:、t2: 前缀是旧格式的译文，早已不再读
OLD_KEYS = ("t:%", "t2:%")


class DB:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._local = threading.local()
        self.lock = threading.RLock()
        self.backup: Path | None = None
        with self.lock:
            self._migrate()

    def _migrate(self) -> None:
        digest.ensure_db(self.path.with_name("digest.db"))
        c = self.conn
        v = c.execute("PRAGMA user_version").fetchone()[0]
        if v >= VERSION:
            c.executescript(SCHEMA)
            return
        destructive = v < 2                    # 0 → 2 删表、重新解析；2 → 3 只加列、加表；3 → 4 摘要搬家、删死表；4 → 5 speakers 加 age
        tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}

        def has_rows(t: str) -> bool:
            try:
                return c.execute(f"SELECT 1 FROM {t} LIMIT 1").fetchone() is not None
            except sqlite3.OperationalError:   # 另一个进程正好升级完、删掉了这张表
                return False
        if destructive and any(has_rows(t) for t in ("books", "pages", "marks", "vocab") if t in tables):
            self.backup = self._backup()
        c.executescript(SCHEMA)
        c.execute("BEGIN IMMEDIATE")
        try:
            if c.execute("PRAGMA user_version").fetchone()[0] >= VERSION:   # 另一个进程（扫描命令行）刚升级完
                c.execute("COMMIT")
                return
            cols = {r[1] for r in c.execute("PRAGMA table_info(books)")}
            for name, decl in NEW_COLS:
                if name not in cols:
                    c.execute(f"ALTER TABLE books ADD COLUMN {name} {decl}")
            if "age" not in {r[1] for r in c.execute("PRAGMA table_info(speakers)")}:
                c.execute("ALTER TABLE speakers ADD COLUMN age TEXT")
            if destructive:
                for t in DROPPED:
                    try:
                        c.execute(f"DROP TABLE IF EXISTS {t}")
                    except sqlite3.OperationalError as e:   # 虚表模块不可用之类：留着不影响使用
                        log.warning("删除旧表 %s 失败：%s", t, e)
                c.execute("UPDATE books SET parser_ver=0")   # 全部按新规则重新解析（扫描或打开时）
            moved = self._move_digest(c)
            c.execute(f"PRAGMA user_version={VERSION}")
            c.execute("COMMIT")
        except Exception:
            c.execute("ROLLBACK")
            raise
        if moved:
            try:
                c.execute("VACUUM")             # 摘要搬走了：把空出来的页还给文件系统（674 MB → 约 100 MB）
            except sqlite3.OperationalError as e:
                log.warning("VACUUM 没做成（%s）；可以之后跑 python -m library vacuum", e)

    def _move_digest(self, c) -> bool:
        """3 → 4：main.digest 的行搬到 dg.digest，删掉 main.digest、books_fts 和旧前缀的 ai_cache。返回搬了没有。"""
        tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        moved = False
        if "digest" in tables:
            c.execute("INSERT OR REPLACE INTO dg.digest(book, head, sketch, gate) SELECT book, head, sketch, gate FROM main.digest")
            c.execute("DROP TABLE main.digest")
            moved = True
        if "books_fts" in tables:
            try:
                c.execute("DROP TABLE books_fts")
            except sqlite3.OperationalError as e:
                log.warning("删除 books_fts 失败：%s", e)
        for k in OLD_KEYS:
            c.execute("DELETE FROM ai_cache WHERE key LIKE ?", (k,))
        return moved

    def _backup(self) -> Path:
        """整库备份（先把 WAL 合并进主文件，再用 SQLite 在线备份拷一份一致的快照）。"""
        c = self.conn
        try:
            c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.OperationalError:
            pass
        dest = self.path.with_name(f"{self.path.name}.bak-{time.strftime('%Y%m%d-%H%M%S')}")
        k = 1
        while dest.exists():
            dest = self.path.with_name(f"{self.path.name}.bak-{time.strftime('%Y%m%d-%H%M%S')}-{k}")
            k += 1
        b = sqlite3.connect(dest)
        try:
            c.backup(b)
        finally:
            b.close()
        log.info("数据库升级前已备份到 %s", dest)
        return dest

    @property
    def conn(self) -> sqlite3.Connection:
        c = getattr(self._local, "c", None)
        if c is None:
            c = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA foreign_keys=ON")
            c.execute("PRAGMA synchronous=NORMAL")
            digest.attach(c)                     # 摘要库：SQL 里的 digest 指 dg.digest
            threadconn.keep(self._local, c)
        return c

    def q(self, sql: str, args=()) -> list[sqlite3.Row]:
        return self.conn.execute(sql, args).fetchall()

    def one(self, sql: str, args=()):
        return self.conn.execute(sql, args).fetchone()

    def run(self, sql: str, args=()):
        with self.lock:
            cur = self.conn.execute(sql, args)
            self.conn.commit()
            return cur

    # ---- 书
    def book(self, bid: str):
        return self.one("SELECT * FROM books WHERE id=?", (bid,))

    def remove_book(self, bid: str) -> None:
        with self.lock:
            c = self.conn
            c.execute("DELETE FROM paras WHERE book=?", (bid,))
            c.execute("DELETE FROM digest WHERE book=?", (bid,))
            c.execute("DELETE FROM guides WHERE book=?", (bid,))
            c.execute("DELETE FROM cast WHERE book=?", (bid,))
            c.execute("DELETE FROM maps WHERE book=?", (bid,))
            c.execute("DELETE FROM book_kind WHERE book=?", (bid,))
            for t in ("speech", "speech_done", "speakers", "voice_lock"):
                c.execute(f"DELETE FROM {t} WHERE book=?", (bid,))
            c.execute("DELETE FROM books WHERE id=?", (bid,))
            c.commit()

    def save_book(self, bid: str, path: str, size: int, mtime: float, parsed, light: bool = False) -> None:
        """一本书一个事务：中途中断不会留下半本。重新解析时保留加入时间、阅读进度（按页换算到新的段落）。
        light：只存书目和摘要，不存全文（stored=0，第一次打开时再存）。"""
        rows = [(bid, n, p["page"], p["kind"], p["text"],
                 "" if light else json.dumps(sentences(p["text"]), separators=(",", ":")))
                for n, p in enumerate(parsed.paras)]
        dg = digest.compute(parsed.paras)
        parser_ver = getattr(parsed, "parser_ver", 0)
        with self.lock:
            c = self.conn
            if c.in_transaction:
                c.commit()
            c.execute("BEGIN IMMEDIATE")      # 读旧行到写新行之间不让别的进程写：网站刚记的进度不会被命令行重扫盖掉
            try:
                self._save_book(c, bid, path, size, mtime, parsed, light, rows, dg, parser_ver)
                c.commit()
            except BaseException:
                c.rollback()
                raise

    def _save_book(self, c, bid, path, size, mtime, parsed, light, rows, dg, parser_ver) -> None:
        old = c.execute("SELECT * FROM books WHERE id=?", (bid,)).fetchone()
        c.execute("DELETE FROM paras WHERE book=?", (bid,))
        if not light:
            c.executemany("INSERT INTO paras(book,n,page,kind,text,sents) VALUES(?,?,?,?,?,?)", rows)
        c.execute("INSERT OR REPLACE INTO digest(book,head,sketch,gate) VALUES(?,?,?,?)",
                  (bid, dg["head"], dg["sketch"], dg["gate"]))
        para, sent = 0, 0
        if old and old["parser_ver"] == parser_ver and old["n_paras"] == len(rows):
            para, sent = old["progress_para"] or 0, old["progress_sent"] or 0      # 同样的分段：原样保留
        elif old and rows:
            pg = old["progress_page"] or 0
            para = next((n for n, p in enumerate(parsed.paras) if p["page"] >= pg), len(rows) - 1)
        page = parsed.paras[para]["page"] if rows else 0
        c.execute(
            "INSERT OR REPLACE INTO books(id,path,size,mtime,title,author,pages,has_text,lang,typo_cover,toc,added,"
            "status,error,last_read,progress_page,progress_frac,mode,parser_ver,n_paras,progress_para,progress_sent,"
            "quality,stored) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,'ok',NULL,?,?,?,?,?,?,?,?,?,?)",
            (bid, path, size, mtime, parsed.title, parsed.author, parsed.pages, int(parsed.has_text), parsed.lang,
             int(parsed.typo_cover), json.dumps(parsed.toc, ensure_ascii=False),
             old["added"] if old else time.time(), old["last_read"] if old else None,
             page if old else 0, (para / len(rows) if rows else 0) if old else 0, old["mode"] if old else None,
             parser_ver, len(rows), para, sent, json.dumps(parsed.quality, separators=(",", ":")), int(not light)))

    def mark_error(self, bid: str, path: str, size: int, mtime: float, err: str, parser_ver: int = 0) -> None:
        """解析失败：记下错误和试过的解析器版本（打开时不再反复重试）；已有的书保留加入时间和阅读进度。"""
        self.run("INSERT INTO books(id,path,size,mtime,title,author,pages,added,status,error,parser_ver) "
                 "VALUES(?,?,?,?,?,?,0,?,'error',?,?) ON CONFLICT(id) DO UPDATE SET "
                 "path=excluded.path, size=excluded.size, mtime=excluded.mtime, status='error', error=excluded.error, "
                 "parser_ver=excluded.parser_ver",
                 (bid, path, size, mtime, Path(path).stem, "", time.time(), err[:500], parser_ver))

    def paras_range(self, bid: str, start: int, end: int) -> list[dict]:
        """第 [start, end) 段：{"n","page","kind","text","sents"}，sents 是码点偏移。
        以中文为主的段落当场按现在的断句规则重算（中文断句是后加的，库里存的旧偏移整段一句），
        朗读页和预生成都从这里取，同一句在两边一定是同一个字符串。"""
        rows = self.q("SELECT n, page, kind, text, sents FROM paras WHERE book=? AND n>=? AND n<? ORDER BY n",
                      (bid, start, end))
        return [{"n": r["n"], "page": r["page"], "kind": r["kind"], "text": r["text"],
                 "sents": [list(x) for x in sentences(r["text"])] if is_zh(r["text"])
                 else json.loads(r["sents"] or "[]")} for r in rows]

    # ---- 阅读时长
    def add_time(self, bid: str, sec: int, listen: int, day: str | None = None) -> None:
        day = day or time.strftime("%Y-%m-%d")
        self.run("INSERT INTO reading_log(day, book, sec, listen) VALUES(?,?,?,?) ON CONFLICT(day, book) DO UPDATE SET "
                 "sec=sec+excluded.sec, listen=listen+excluded.listen", (day, bid, sec, listen))

    def time_since(self, day: str, bid: str | None = None) -> int:
        """day（含）以来的阅读秒数；bid 给了就只算这本。"""
        sql, args = "SELECT COALESCE(SUM(sec), 0) n FROM reading_log WHERE day>=?", [day]
        if bid:
            sql, args = sql + " AND book=?", args + [bid]
        return self.one(sql, args)["n"]

    def para_page(self, bid: str, n: int) -> int:
        r = self.one("SELECT page FROM paras WHERE book=? AND n=?", (bid, n))
        return r["page"] if r else 0
