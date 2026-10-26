"""扫描书库：可断点续扫（已入库、大小/修改时间没变、且是当前版本解析器解析的书直接跳过）。
几本书同时解析（默认 4 个解析子进程，各自独立；文件少于 8 个时串行），写库仍是一本书一个事务。"""
from __future__ import annotations

import logging
import os
import re
import sqlite3
import threading
import time
import traceback
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from .catalog import clean_author
from .db import DB
from .extract import book_id, parse, parser_ver, toc_paras
from .reflow import CJK_SPACE, fragmented, reflow

log = logging.getLogger(__name__)
EXTS = {".pdf"}
PARSE_TIMEOUT = 300          # 单本解析超过 5 分钟就放弃（个别坏文件会让 PyMuPDF 卡死）
BIG_FILE = 50 << 20           # 超过 50 MB 的文件（百册合集之类）：常规解析和兜底各只给一半时间，坏文件最多耗 5 分钟而不是 15
WORKERS = max(1, min(4, (os.cpu_count() or 4) // 4))
PARALLEL_MIN = 8              # 文件少于这么多时串行（测试、单本入库不必起一排子进程）
_WORDS: set | None = None


def _init_words(words) -> None:
    global _WORDS
    _WORDS = words


NBSP_JOIN = re.compile(r"(?<=[A-Za-z0-9,.;:!?'\"’”)\]])(?:&#160;|&#xa0;|&nbsp;|\xa0)(?=[A-Za-z0-9\"'“‘(\[])", re.I)


INLINE_NOWRAP = re.compile(r"white-space\s*:\s*nowrap", re.I)


ESCAPED_NBSP = "&amp;nbsp;"            # 转义了两次的不换行空格：阅读时显示成字面的「&nbsp;」，也连成一整行不能换行


def _fix_wrap(html: str) -> str:
    return INLINE_NOWRAP.sub("white-space: normal", NBSP_JOIN.sub(" ", html.replace(ESCAPED_NBSP, " ")))


def _is_page(z, info) -> bool:
    """正文页：按扩展名，没有扩展名的（有的 epub 章节文件就叫 xxx_17）看开头是不是 HTML。"""
    name = info.filename.lower()
    if name.endswith((".xhtml", ".html", ".htm")):
        return True
    if "." in name.rsplit("/", 1)[-1] or info.file_size == 0:
        return False
    with z.open(info) as f:
        head = f.read(512).lower()
    return b"<html" in head or b"<!doctype html" in head


def _wrap_copy(path: Path, least: int = 50):
    """epub 里英文词之间用不换行空格连接（&#160;），或段落上直接写了 style="white-space: nowrap"：
    整段不能换行，MuPDF 排版时会把超出页宽的部分裁掉（样式表里的 nowrap 由 extract 的用户样式覆盖，行内样式盖不住）。
    这样的书改好存一份临时副本再解析（原文件不动）；没有这种情况返回 None。"""
    import zipfile
    try:
        z = zipfile.ZipFile(path)
    except (zipfile.BadZipFile, OSError):         # 坏文件交给解析器报错
        return None
    with z:
        pages = [i for i in z.infolist() if _is_page(z, i)]
        html = [z.read(i).decode("utf-8", "ignore") for i in pages]
        if (sum(len(NBSP_JOIN.findall(h)) + h.count(ESCAPED_NBSP) for h in html) < least
                and not any(INLINE_NOWRAP.search(h) for h in html)):
            return None
        import tempfile
        tmp = Path(tempfile.mkdtemp()) / (path.stem[:60] + ".epub")    # 没有元数据书名时按文件名
        with zipfile.ZipFile(tmp, "w") as out:
            for i in z.infolist():
                data = z.read(i)
                if i in pages:
                    data = _fix_wrap(data.decode("utf-8", "ignore")).encode("utf-8")
                out.writestr(i, data, compress_type=zipfile.ZIP_STORED if i.filename == "mimetype" else zipfile.ZIP_DEFLATED)
        return tmp


def _parse_child(path: str):
    src = Path(path)
    ext = src.suffix.lower()
    if ext == ".txt":                           # txt 按原文件的行和空行分段（library/txt.py），不经 MuPDF 排版
        from .txt import parse_txt
        return sanitize(parse_txt(src, _WORDS))
    if ext == ".md":                            # 资料的 Markdown（library/md.py）
        from .md import parse_md
        return sanitize(parse_md(src, _WORDS))
    tmp = _wrap_copy(src) if ext == ".epub" else None
    try:
        parsed = parse(tmp or src, _WORDS)
    finally:
        if tmp:
            import shutil
            shutil.rmtree(tmp.parent, ignore_errors=True)
    if fragmented(parsed.paras):                # 一字一段的中文书：按页面纯文字重新分段
        paras = reflow(Path(path))
        if paras and not fragmented(paras):
            parsed.paras = paras
            parsed.toc = toc_paras([(lv, t, pg) for lv, t, pg, *_ in parsed.toc], paras)
    return sanitize(parsed)


def _reflow_child(path: str):
    """常规解析超时的大部头：按页面纯文字简单分段（快得多），目录用书里自带的书签。"""
    import pymupdf
    from .extract import Parsed
    paras = reflow(Path(path))
    with pymupdf.open(path) as d:
        md, n = d.metadata or {}, d.page_count
        toc = [(lv, t.strip(), max(0, pg - 1)) for lv, t, pg, *_ in d.get_toc(simple=False)] if d.get_toc() else []
    if not paras:
        raise RuntimeError("no extractable text")
    p = Parsed(title=(md.get("title") or Path(path).stem).strip(), author=(md.get("author") or "").strip(), pages=n,
               toc=toc_paras(toc, paras), paras=paras, has_text=True,
               lang="other" if sum(1 for x in paras[:200] if re.search(r"[\u3400-\u9fff]", x["text"])) > 20 else "en")
    return sanitize(p)


SURROGATES = re.compile(r"[\ud800-\udfff\x00]")
CJK = re.compile(r"[\u3400-\u9fff]")


def fix_text(s):
    """去掉写不进数据库的孤立代理字符；GBK 中文被当成 Latin-1 读出来的乱码（ÊÀ½ç…）还原成中文。"""
    if not isinstance(s, str):
        return s
    s = SURROGATES.sub("", s)
    if s and not CJK.search(s) and all(ord(c) < 256 for c in s) and re.search(r"[\x80-\xff]{2}", s):
        try:
            t = s.encode("latin-1").decode("gbk")
            if CJK.search(t):
                return t
        except UnicodeError:
            pass
    return s


def fix_replacement(text: str) -> str:
    """西文里制作电子书时丢掉的引号、撇号、破折号（显示成「�」）按上下文补回来。"""
    if "\ufffd" not in text or CJK.search(text):
        return text
    t = re.sub(r"(?i)copyright\s*\ufffd", "Copyright ©", text)
    t = re.sub(r"(?<=[A-Za-z])\ufffd(?=(?:t|s|d|ll|re|ve|m)\b)", "’", t)      # couldn�t / nurse�s
    t = re.sub(r"(?<=[A-Za-z])\ufffd(?=[A-Za-z])", "—", t)                      # suspense�his
    t = re.sub(r"(?:(?<=^)|(?<=[\s(\[—-]))\ufffd(?=\S)", "“", t)               # �Flight
    t = re.sub(r"(?<=\S)\ufffd", "”", t)                                         # desk.�
    return t.replace("\ufffd", "")


def sanitize(parsed):
    parsed.title, parsed.author = fix_text(parsed.title), clean_author(fix_text(parsed.author))   # Administrator、xb21cn 这类占位不算作者
    parsed.toc = [(e[0], fix_text(e[1]), *e[2:]) for e in parsed.toc]
    for p in parsed.paras:
        p["text"] = fix_replacement(SURROGATES.sub("", p["text"]))
        if CJK.search(p["text"]):                  # 「阿 不 思 露 出」：中文字之间的空格去掉
            p["text"] = CJK_SPACE.sub("", p["text"])
    return parsed


@dataclass
class ScanState:
    running: bool = False
    total: int = 0
    done: int = 0
    added: int = 0
    skipped: int = 0
    failed: int = 0
    current: str = ""
    started: float = 0
    finished: float = 0
    stop: bool = False
    errors: list = field(default_factory=list)

    def as_dict(self) -> dict:
        d = {k: v for k, v in self.__dict__.items() if k != "errors"}
        d["errors"] = self.errors[-20:]
        if self.running and self.done:
            rate = (time.time() - self.started) / self.done
            d["eta_s"] = int(rate * (self.total - self.done))
        return d


def find_pdfs(dirs: list[str]) -> list[Path]:
    out = []
    for d in dirs:
        root = Path(os.path.expanduser(d))
        if root.is_file() and root.suffix.lower() in EXTS:
            out.append(root)
            continue
        for dp, dn, fn in os.walk(root):
            dn[:] = [x for x in dn if not x.startswith(".")]
            for f in fn:
                if not f.startswith(".") and Path(f).suffix.lower() in EXTS:
                    out.append(Path(dp) / f)
    return sorted(set(out))


def _under(path, root: Path) -> bool:
    """path 是否在 root 里（按路径分隔符判断：/Books 不包含 /Books2 下的书）。"""
    p = str(path)
    return p == str(root) or p.startswith(str(root).rstrip(os.sep) + os.sep)


class Scanner:
    def __init__(self, db: DB, cfg, words: set | None):
        self.db, self.cfg, self.words = db, cfg, words
        self._cat = None
        self._local = threading.local()   # 每个线程自己的解析子进程（并行扫描时各用各的）
        self._isos: list = []
        self.state = ScanState()
        self._t: threading.Thread | None = None
        self.lock = threading.RLock()     # 扫描状态计数
        self._paths: dict[str, threading.Lock] = {}   # 同一个文件同时只解析一次（扫描和「打开时按需重新解析」撞上时）
        self._paths_guard = threading.Lock()

    @property
    def catalog(self):
        """书城编目（data/catalog.db）：有的话按作品入库，每个作品只入库文字来源。"""
        data = getattr(self.cfg, "data", None)
        if self._cat is None and data and (data / "catalog.db").exists():
            from .catalog import Catalog
            self._cat = Catalog(data / "catalog.db")
        return self._cat

    def text_sources(self, roots: list[Path]) -> list[Path] | None:
        """编目里每个作品的文字来源（epub > 有文字层的 pdf > mobi）；杂志、重复文件、其他格式都不入库。
        英文书在前。没有编目时返回 None（退回只扫 PDF）。"""
        cat = self.catalog
        if cat is None:
            return None
        rows = cat.q("SELECT text_src FROM works WHERE kind='book' AND text_src IS NOT NULL "
                     "ORDER BY lang='zh', sort_title")
        if not rows:
            return None
        return [Path(r[0]) for r in rows if any(_under(r[0], root) for root in roots)]

    def _drop_replaced_failures(self) -> None:
        """入库失败的文件，如果同一作品已换别的格式入库成功，就删掉失败记录（管理页不再报错）。"""
        for b in self.db.q("SELECT id, path FROM books WHERE status='error'"):
            w = self._cat_meta(Path(b["path"]))
            if w is None or not w["text_src"] or w["text_src"] == b["path"]:
                continue
            ok = self.db.one("SELECT 1 FROM books WHERE path=? AND status='ok'", (w["text_src"],))
            if ok:
                self.db.remove_book(b["id"])

    def sync_titles(self) -> int:
        """没重新解析的书（文件没变就跳过）也跟上编目里的书名作者：编目规则改了（比如拆开 Anna's Archive 文件名）之后，
        阅读库、书架上的书名和书城一致。返回改了几本。"""
        cat = {r["text_src"]: (r["title"], r["author"] or "") for r in
               self.catalog.q("SELECT text_src, title, author FROM works WHERE kind='book' AND text_src IS NOT NULL AND title!=''")}
        upd = []
        for r in self.db.q("SELECT id, path, title, author FROM books"):
            if r["path"] not in cat:
                continue
            title, author = cat[r["path"]]
            author = author or clean_author(r["author"] or "")      # 编目没有作者时留着解析出的（占位作者不留）
            if (title, author) != (r["title"], r["author"] or ""):
                upd.append((title, author, r["id"]))
        if upd:
            with self.db.lock:
                self.db.conn.executemany("UPDATE books SET title=?, author=? WHERE id=?", upd)
                self.db.conn.commit()
        return len(upd)

    def _cat_meta(self, path: Path):
        cat = self.catalog
        if cat is None:
            return None
        return cat.one("SELECT w.* FROM works w JOIN files f ON f.work=w.id WHERE f.path=?", (str(path),))

    def _isolated(self):
        """这个线程的解析子进程（词表换了重开）。"""
        from .isolate import Isolated
        iso, words = getattr(self._local, "iso", None), getattr(self._local, "iso_words", None)
        if iso is None or words is not self.words:
            if iso is not None:
                iso.close()
            iso = Isolated(PARSE_TIMEOUT, _init_words, (self.words,))
            self._local.iso, self._local.iso_words = iso, self.words
            with self.lock:
                self._isos.append(iso)
        return iso

    def close_isolated(self) -> None:
        """扫描结束：关掉所有解析子进程（每个带着一份词表，几个加起来几百 MB）。"""
        with self.lock:
            isos, self._isos = self._isos, []
        for iso in isos:
            iso.close()
        self._local.iso = None

    def _parse(self, path: Path):
        """在常驻子进程里解析：坏文件让 PyMuPDF 崩溃或卡死时，只这一本失败，扫描和网站照常。
        大文件（> BIG_FILE）常规解析和兜底各只给一半时间。"""
        from .isolate import Isolated, IsolatedTimeout
        big = path.stat().st_size > BIG_FILE
        limit = PARSE_TIMEOUT // 2 if big else PARSE_TIMEOUT
        iso = self._isolated()
        try:
            return iso.call(_parse_child, str(path), timeout=limit)
        except IsolatedTimeout:                       # 超时：下面改用简单分段兜底
            pass                                      # 崩溃（IsolatedError）不接：文件坏了，不再试
        log.info("normal parse timed out, falling back to simple paragraphing: %s", path)
        self._local.iso = None                        # 刚被杀掉的子进程不再用；下一本重开
        fallback = Isolated(limit)
        try:
            return fallback.call(_reflow_child, str(path))
        finally:
            fallback.close()

    @contextmanager
    def _only_one(self, path: Path):
        key = str(path)
        with self._paths_guard:
            lk = self._paths.setdefault(key, threading.Lock())
        with lk:
            yield
        with self._paths_guard:
            if not lk.locked():
                self._paths.pop(key, None)

    def draw_cover(self, bid: str, path: Path, w=None) -> Path:
        """画一本书的阅读页封面：epub/mobi 用自带封面图、判过不能用的按书目库的结论、PDF 渲染第一页（8–34 ms）。"""
        from .covers import reader_cover
        if w is None:
            w = self._cat_meta(path)
        files = self.catalog.q("SELECT fid, path, fmt FROM files WHERE work=?", (w["id"],)) if w is not None else []
        dest = self.cfg.covers / f"{bid}.jpg"
        reader_cover(getattr(self.cfg, "data", None), files, path, dest)
        return dest

    def add_file(self, path: Path, force: bool = False, full: bool | None = None) -> str:
        """入库一本，返回 'added' / 'skipped' / 'failed'。
        full：存全文（打开阅读、单本入库）；None 时只有存过全文或读过的书才存，其余只存书目和摘要。"""
        with self._only_one(path):
            return self._add_file(path, force, full)

    def refresh(self, bid: str) -> str:
        """打开阅读页、取正文时按需解析一本：解析器升级过，或者还没存全文（扫描只存了摘要）。
        返回 'fresh'（不需要）/ 'added' / 'failed' / 'missing'（文件不在了）。"""
        b = self.db.book(bid)
        if not b:
            return "fresh"
        with self._only_one(Path(b["path"])):
            b = self.db.book(bid)
            if not b or ((b["parser_ver"] or 0) >= parser_ver(b["path"]) and b["stored"]):
                return "fresh"
            p = Path(b["path"])
            if not p.exists():
                return "missing"
            if b["status"] == "error" and (b["parser_ver"] or 0) >= parser_ver(p) and not b["stored"]:
                st = p.stat()                         # 这一版解析器已经读不出这个文件：没换文件就不再反复解析
                if b["size"] == st.st_size and abs((b["mtime"] or 0) - st.st_mtime) < 1:
                    return "failed"
            return self._add_file(p, force=True, full=True)

    def _add_file(self, path: Path, force: bool, full: bool | None = None) -> str:
        st = path.stat()
        row = self.db.one("SELECT id, size, mtime, status, parser_ver, stored, last_read FROM books WHERE path=?",
                          (str(path),))
        if full is None:                              # 扫描：读过的书照旧存全文，其余只存摘要
            full = bool(row and (row["stored"] or row["last_read"]))
        if row and not force and row["size"] == st.st_size and abs((row["mtime"] or 0) - st.st_mtime) < 1 \
                and row["status"] == "ok" and (row["parser_ver"] or 0) >= parser_ver(path) and (row["stored"] or not full):
            return "skipped"
        bid = book_id(path)
        dup = self.db.one("SELECT path, status FROM books WHERE id=?", (bid,))
        if dup and dup["path"] != str(path) and Path(dup["path"]).exists() and dup["status"] == "ok" and not force:
            return "skipped"          # 同一本书的另一个副本
        try:
            parsed = self._parse(path)
            w = self._cat_meta(path)
            if w is not None and w["title"]:          # 编目里的书名作者（读自 epub/mobi 元数据、文件名）更准
                parsed.title, parsed.author = w["title"], w["author"] or parsed.author
            self.db.save_book(bid, str(path), st.st_size, st.st_mtime, parsed, light=not full)
            cover = self.cfg.covers / f"{bid}.jpg"
            same = row and row["id"] == bid and row["size"] == st.st_size and abs((row["mtime"] or 0) - st.st_mtime) < 1
            if full and not (same and cover.exists()):   # 只给存全文（打开读过）的书画封面；其余的第一次要显示时现画（reader.cover）
                try:
                    self.draw_cover(bid, path, w)
                except Exception as e:  # 封面失败不影响入库
                    log.warning("cover failed %s: %s", path, e)
            if row and row["id"] != bid:
                self.db.remove_book(row["id"])
            return "added"
        except Exception as e:
            if isinstance(e, sqlite3.OperationalError) and "locked" in str(e):    # 别的进程正在写库：不是书的问题，不记失败，下次再入库
                log.warning("database busy, will retry %s: %s", path, e)
                self.state.errors.append({"path": str(path), "error": str(e)[:200]})
                return "failed"
            log.error("parse failed %s: %s", path, e)
            self.db.mark_error(bid, str(path), st.st_size, st.st_mtime, f"{type(e).__name__}: {e}", parser_ver(path))
            self.state.errors.append({"path": str(path), "error": str(e)[:200]})
            if self.catalog is not None:            # 编目里标记这个文件读不了：重新归并时换别的格式当文字来源
                try:                                # 原因记在书库（mark_error），编目只记结论；重新编目时保留
                    self.catalog.set_gate(str(path), "unreadable")
                except Exception as ex:  # noqa: BLE001
                    log.warning("could not flag catalogue failure %s: %s", path, ex)
            return "failed"

    def run(self, dirs: list[str], progress=None, workers: int | None = None) -> ScanState:
        s = self.state = ScanState(running=True, started=time.time())
        workers = workers or WORKERS
        try:
            # 书库目录不存在（外接盘没插、文件夹改名）时 os.walk 静默返回空，后面的清理会把整个目录的书删光：
            # 先挑出存在的目录，不存在的跳过并报错，这些目录下的书原样保留
            roots = [Path(os.path.expanduser(d)) for d in dirs]
            live = [r for r in roots if r.exists()]
            for r in roots:
                if r not in live:
                    s.errors.append({"path": str(r), "error": "folder missing (external drive not mounted?) — skipped; books under it are left as they are"})
            files = None
            if live and self.catalog is not None:
                s.current = "Updating the catalogue…"            # 新加的书先编目，再按作品取文字来源
                self.catalog.scan([str(r) for r in live])
                files = self.text_sources(live)
            if files is None:
                files = find_pdfs([str(r) for r in live])
            s.total = len(files)
            tried = set()

            def one(f: Path) -> None:
                if s.stop:
                    return
                s.current = str(f)
                try:
                    r = self.add_file(f)
                except OSError as e:                # 扫描途中文件被移走或改名：记下这一本，接着扫，不让整轮中断
                    with self.lock:
                        s.errors.append({"path": str(f), "error": f"{type(e).__name__}: {e}"[:200]})
                    r = "failed"
                with self.lock:
                    setattr(s, r, getattr(s, r) + 1)
                    s.done += 1
                    if progress:
                        progress(s)

            def batch(fs: list[Path]) -> None:
                tried.update(str(f) for f in fs)
                if len(fs) < PARALLEL_MIN or workers <= 1:
                    for f in fs:
                        if s.stop:
                            break
                        one(f)
                    return
                from concurrent.futures import ThreadPoolExecutor
                with ThreadPoolExecutor(workers, thread_name_prefix="scan") as ex:
                    list(ex.map(one, fs))
            while True:
                batch(files)
                # 有文字来源解析失败：编目重新挑来源（同一本书的其他格式），只补跑换了来源的作品
                if s.stop or not s.failed or self.catalog is None:
                    break
                self.catalog.rebuild()
                files = [f for f in (self.text_sources(live) or []) if str(f) not in tried]
                if not files:
                    break
                s.total += len(files)
            if self.catalog is not None and s.failed:
                self._drop_replaced_failures()
            if self.catalog is not None and not s.stop:
                self.sync_titles()
            # 文件已不存在的书：从书库移除（阅读进度随书一起删掉）
            if not s.stop:
                alive = tried
                rows = self.db.q("SELECT id, path FROM books")
                for root in live:
                    under = [r for r in rows if _under(r["path"], root)]
                    if under and not any(_under(f, root) for f in alive):
                        # 目录在、一本 PDF 都没找到，书库里却有这个目录的书：多半是挂载点还在但盘没接好，不清理
                        s.errors.append({"path": str(root),
                                         "error": f"no PDFs found in the folder but {len(under)} are catalogued under it — nothing removed, to be safe"})
                        continue
                    for r in under:
                        if r["path"] not in alive and not Path(r["path"]).exists():
                            self.db.remove_book(r["id"])
        except Exception:
            s.errors.append({"path": "", "error": traceback.format_exc()[-300:]})
        finally:
            self.close_isolated()
            s.running = False
            s.current = ""
            s.finished = time.time()
        return s

    def start(self, dirs: list[str]) -> bool:
        if self.state.running:
            return False
        self._t = threading.Thread(target=self.run, args=(dirs,), daemon=True)
        self._t.start()
        return True
