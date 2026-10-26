"""书的封面：从 epub / mobi 里取自带的封面图，或者渲染 pdf 第一页，统一转成 JPEG 缩略图。
编目、书目层、入库和书城都用（原先在 library/store.py 里）。自带封面不能用的书（扉页、logo…）见下面 cover_reject 一节。

  python -m library.covers audit [--all]                    口碑高分书的封面检查：像扉页的记「可疑」，联系表写到 logs/
  python -m library.covers reject 作品编号… [--why 说明]     人工确认自带的不是封面（错图）：外部封面 → 文字封面
  python -m library.covers unreject 作品编号…                撤销
标记之后跑 `python -m library.biblio covers` 到 Open Library 按可信 ISBN 取封面。
"""
from __future__ import annotations

import logging
import posixpath
import struct
import zipfile
from pathlib import Path
from urllib.parse import unquote

from .catalog import _xml

log = logging.getLogger(__name__)


def _opf(z: zipfile.ZipFile) -> str:
    root = _xml(z.read("META-INF/container.xml"))
    return next(el.get("full-path") for el in root.iter() if el.tag.endswith("rootfile"))


def epub_cover(path: Path) -> bytes | None:
    with zipfile.ZipFile(path) as z:
        opf = _opf(z)
        o = _xml(z.read(opf))
        items, cover_id = {}, None
        for el in o.iter():
            tag = el.tag.rsplit("}", 1)[-1]
            if tag == "item":
                items[el.get("id")] = (el.get("href") or "", el.get("media-type") or "", el.get("properties") or "")
            elif tag == "meta" and el.get("name") == "cover":
                cover_id = el.get("content")
        imgs = {k: v for k, v in items.items() if v[1].startswith("image/")}
        pick = imgs.get(cover_id) or next((v for v in imgs.values() if "cover-image" in v[2]), None) \
            or next((v for k, v in imgs.items() if "cover" in (k + v[0]).lower()), None) \
            or next(iter(imgs.values()), None)
        if not pick:
            return None
        href = posixpath.normpath(posixpath.join(posixpath.dirname(opf), unquote(pick[0])))
        return z.read(href)


def mobi_cover(path: Path) -> bytes | None:
    """PalmDB 记录表 + EXTH 201（封面在图片记录里的偏移）；没有 201 时取第一张图。"""
    with open(path, "rb") as f:
        head = f.read(78)
        n = struct.unpack(">H", head[76:78])[0]
        table = f.read(8 * n)
        offs = [struct.unpack(">I", table[8 * i:8 * i + 4])[0] for i in range(n)]
        f.seek(offs[0])
        r0 = f.read(min(1 << 16, (offs[1] if n > 1 else offs[0] + (1 << 16)) - offs[0]))
        if r0[16:20] != b"MOBI":
            return None
        first_img = struct.unpack(">I", r0[108:112])[0]
        hlen = struct.unpack(">I", r0[20:24])[0]
        cov = 0
        flags = struct.unpack(">I", r0[128:132])[0]
        if flags & 0x40 and r0[16 + hlen:16 + hlen + 4] == b"EXTH":
            p, cnt = 16 + hlen + 12, struct.unpack(">I", r0[16 + hlen + 8:16 + hlen + 12])[0]
            for _ in range(cnt):
                t, ln = struct.unpack(">II", r0[p:p + 8])
                if t == 201 and ln >= 12:
                    cov = struct.unpack(">I", r0[p + 8:p + 12])[0]
                p += max(ln, 8)
        idx = first_img + cov
        if not 0 < idx < n:
            return None
        f.seek(offs[idx])
        data = f.read((offs[idx + 1] if idx + 1 < n else offs[idx] + (4 << 20)) - offs[idx])
        return data if data[:3] == b"\xff\xd8\xff" or data[:8] == b"\x89PNG\r\n\x1a\n" or data[:3] == b"GIF" else None


def to_jpeg(img: bytes, width: int = 360) -> bytes:
    import pymupdf
    pix = pymupdf.Pixmap(img)
    if pix.colorspace is None or pix.colorspace.n not in (1, 3) or pix.alpha:
        pix = pymupdf.Pixmap(pymupdf.csRGB, pix)
        if pix.alpha:
            pix = pymupdf.Pixmap(pix, 0)
    if pix.width > width:
        pix = pymupdf.Pixmap(pix, width, round(pix.height * width / pix.width), None)
    return pix.tobytes("jpeg", jpg_quality=82)


def pdf_cover(path: Path, width: int = 360) -> bytes:
    import pymupdf
    with pymupdf.open(path) as d:
        pg = d[0]
        z = width / max(1, pg.rect.width)
        return pg.get_pixmap(matrix=pymupdf.Matrix(z, z)).tobytes("jpeg", jpg_quality=80)


def make_cover(w, files: list) -> bytes | None:
    """依次试：文字来源/各格式文件里自带的封面 → pdf 第一页。"""
    order = sorted(files, key=lambda f: {"epub": 0, "azw3": 1, "mobi": 1, "azw": 1, "pdf": 2}.get(f["fmt"], 9))
    for f in order:
        p = Path(f["path"])
        if not p.exists():
            continue
        try:
            if f["fmt"] == "epub":
                img = epub_cover(p)
            elif f["fmt"] in ("mobi", "azw", "azw3"):
                img = mobi_cover(p)
            elif f["fmt"] == "pdf":
                return pdf_cover(p)
            else:
                continue
            if img:
                return to_jpeg(img)
        except Exception as ex:          # 坏文件：换下一个
            log.info("封面失败 %s: %s", p, ex)
    return None


# ---------------------------------------------------------------- 自带封面不能用的书
# 有的 epub / mobi 把扉页、出版社 logo、calibre 生成的花纹图、随手的照片标成了封面。这些书记在书目库 cover_reject：
#   bad      人工确认是错图：外部封面 → 文字封面，不再用它
#   suspect  机器初筛像扉页（几乎全是白底、没有颜色）：外部封面 → 它本身（极简设计的真封面也长这样，不能丢）
# 书城（store_media）、入库（scan）、阅读库都按这个结论选封面。

def looks_blank(img: bytes) -> bool:
    """像扉页：九成以上是白底、几乎没有彩色像素（文字扉页、白底 logo 页）。缩小后按像素算。"""
    import pymupdf
    pix = pymupdf.Pixmap(img)
    if pix.colorspace is None or pix.colorspace.n != 3 or pix.alpha:
        pix = pymupdf.Pixmap(pymupdf.csRGB, pix)
        if pix.alpha:
            pix = pymupdf.Pixmap(pix, 0)
    while pix.width > 120:
        pix.shrink(1)
    s, n = pix.samples, pix.n
    total = len(s) // n
    if not total:
        return False
    white = color = 0
    for i in range(0, total * n, n):
        r, g, b = s[i], s[i + 1], s[i + 2]
        lo, hi = min(r, g, b), max(r, g, b)
        white += lo > 235
        color += hi - lo > 40
    return white >= 0.9 * total and color < 0.005 * total


def override_cover(data_dir: Path | None, fids, dest: Path) -> bool:
    """阅读库一本书的封面图 dest：作品的自带封面判过不能用时按结论处理，返回 True（调用方不再取自带封面）。
    有外部封面 → 写成外部封面；bad 又没有外部封面 → 删掉 dest（阅读库显示文字封面）；suspect 没有外部封面 → False。"""
    fids = list(fids)
    if not data_dir or not fids:
        return False
    from .biblio import cover_override
    v, ext = cover_override(data_dir, fids)
    if not v:
        return False
    if ext:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(to_jpeg(ext.read_bytes()))
        return True
    if v == "bad":
        dest.unlink(missing_ok=True)
        return True
    return False


def reader_cover(data_dir: Path, files: list, path: Path, dest: Path) -> None:
    """阅读库一本书（path）的封面图：判过不能用的按结论；否则 epub/mobi 用自带封面图，pdf 和取不到的渲染第一页。"""
    if override_cover(data_dir, [f["fid"] for f in files], dest):
        return
    img = make_cover(None, files) if path.suffix.lower() != ".pdf" and files else None
    if img:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(img)
    else:
        from .extract import render_cover
        render_cover(path, dest)


def refresh(data_dir: Path, works: list[str]) -> dict:
    """标记 / 撤销 / 取到外部封面之后：清掉这些作品的书城封面缓存，按新结论重画阅读库里这些书的封面。"""
    import sqlite3
    st = {"store": 0, "reader": 0}
    if not works:
        return st
    cat = sqlite3.connect(f"file:{data_dir / 'catalog.db'}?mode=ro", uri=True)
    cat.row_factory = sqlite3.Row
    lib = data_dir / "library.db"
    books = sqlite3.connect(f"file:{lib}?mode=ro", uri=True) if lib.exists() else None
    try:
        for wid in dict.fromkeys(works):
            for ext in (".jpg", ".none"):
                p = data_dir / "store_covers" / f"{wid}{ext}"
                if p.exists():
                    p.unlink()
                    st["store"] += 1
            files = [dict(r) for r in cat.execute("SELECT fid, path, fmt FROM files WHERE work=?", (wid,))]
            if not files or books is None:
                continue
            qs = ",".join("?" * len(files))
            for r in books.execute(f"SELECT id, path FROM books WHERE path IN ({qs})", [f["path"] for f in files]):
                try:
                    reader_cover(data_dir, files, Path(r[1]), data_dir / "covers" / f"{r[0]}.jpg")
                    st["reader"] += 1
                except Exception as ex:  # noqa: BLE001  坏文件：留着原来的
                    log.warning("阅读库封面失败 %s: %s", r[1], ex)
    finally:
        cat.close()
        if books is not None:
            books.close()
    return st


# ---------------------------------------------------------------- 口碑高分书的封面检查（命令行）
# 「口碑高分」与书城一致（store.HIGH_SQL，wv 视图里的口碑分口径）：综合口碑分 ≥ ratings.HIGH 且有能显示的来源。
# 要在 catalog.db 的连接上 ATTACH 书目库为 b。
HIGH_WORKS = ("SELECT w.id, w.title, w.author, w.lang FROM works w LEFT JOIN enrich e ON e.work=w.id "
              "LEFT JOIN files f ON f.path=COALESCE(w.text_src, w.layout_src) LEFT JOIN b.score rs ON rs.fid=f.fid "
              "WHERE w.kind='book' "
              "AND (CASE WHEN rs.fid IS NULL THEN (CASE WHEN e.rating_n>=5 THEN e.rating * 2 END) ELSE rs.score END) >= ? "
              "AND (CASE WHEN rs.fid IS NULL THEN e.rating ELSE rs.shown END) IS NOT NULL ORDER BY w.lang, w.sort_title")


def _open_cat(data_dir: Path):
    import sqlite3
    from .biblio import DB_NAME, open_db
    open_db(data_dir / DB_NAME).close()                        # 保证书目库升到最新结构（有 cover_reject）
    cat = sqlite3.connect(f"file:{data_dir / 'catalog.db'}?mode=ro", uri=True)
    cat.row_factory = sqlite3.Row
    cat.execute("ATTACH DATABASE ? AS b", (f"file:{data_dir / DB_NAME}?mode=ro",))
    return cat


def audit(data_dir: Path, show_all: bool = False) -> dict:
    """口碑高分书逐本取自带封面：像扉页的记 suspect（已判过的不动）；写一张缩略图联系表到 logs/ 供人工确认。
    联系表列出判过的书（bad / suspect，含这次新记的）和没有任何封面的书；show_all 时列出全部高分书。"""
    import base64
    import html
    from .biblio import DB_NAME, open_db, set_reject
    from .ratings import HIGH
    cat = _open_cat(data_dir)
    try:
        works = [dict(r) for r in cat.execute(HIGH_WORKS, (HIGH,))]
        files = {}
        for w in works:
            files[w["id"]] = [dict(r) for r in cat.execute("SELECT fid, path, fmt FROM files WHERE work=?", (w["id"],))]
        verdicts = {r[0]: r[1] for r in cat.execute("SELECT fid, verdict FROM b.cover_reject")}
        ext = {r[0] for r in cat.execute("SELECT fid FROM b.covers WHERE status='ok'")}
    finally:
        cat.close()
    st = {"works": len(works), "own": 0, "new_suspect": 0, "suspect": 0, "bad": 0, "ext": 0, "none": 0}
    shown, c = [], open_db(data_dir / DB_NAME)
    try:
        for w in works:
            fl = files[w["id"]]
            vs = {verdicts.get(f["fid"]) for f in fl} - {None}
            v = "bad" if "bad" in vs else "suspect" if vs else None
            has_ext = any(f["fid"] in ext for f in fl)
            img = make_cover(None, fl)
            if img and v is None:
                try:
                    blank = looks_blank(img)
                except Exception:  # noqa: BLE001  图解不开：当作没封面
                    blank, img = False, None
                if blank:
                    set_reject(c, [f["fid"] for f in fl], w["id"], "suspect", "像扉页：几乎全是白底、没有颜色")
                    v = "suspect"
                    st["new_suspect"] += 1
            st["own"] += bool(img)
            st["ext"] += has_ext
            if v:
                st[v] += 1
            if not has_ext and (not img or v == "bad"):
                st["none"] += 1                                    # 书城显示文字封面
            if show_all or v or not img and not has_ext:
                shown.append((w, img, v, has_ext))
        c.commit()
    finally:
        c.close()
    st["sheet"] = str(_sheet(data_dir, shown, base64, html))
    return st


def _sheet(data_dir: Path, shown: list, base64, html) -> Path:
    import time
    cards = []
    for w, img, v, has_ext in shown:
        src = f"data:image/jpeg;base64,{base64.b64encode(img).decode()}" if img else ""
        tag = {"bad": "错图", "suspect": "可疑"}.get(v, "") + (" · 有外部封面" if has_ext else "")
        cards.append(f'<figure>{f"<img src={src!r}>" if src else "<div class=none>没有自带封面</div>"}'
                     f'<figcaption><b>{html.escape(w["title"] or "")}</b><br>{html.escape(w["author"] or "")}'
                     f'<br><code>{w["id"]}</code> {tag}</figcaption></figure>')
    out = data_dir.parent / "logs" / f"cover_audit-{time.strftime('%Y%m%d-%H%M%S')}.html"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("<!doctype html><meta charset=utf-8><title>口碑高分书封面检查</title><style>"
                   "body{font:13px -apple-system,sans-serif;margin:16px}main{display:flex;flex-wrap:wrap;gap:12px}"
                   "figure{width:150px;margin:0}img,.none{width:150px;height:220px;object-fit:contain;background:#eee}"
                   ".none{display:flex;align-items:center;justify-content:center;color:#888}code{font-size:11px}</style>"
                   f"<h1>口碑高分书封面检查（{len(shown)} 本）</h1><p>错图用 <code>python -m library.covers reject 作品编号…</code> "
                   f"标记；误判的用 <code>unreject</code> 撤销。</p><main>{''.join(cards)}</main>", encoding="utf-8")
    return out


def mark(data_dir: Path, ids: list[str], verdict: str | None, why: str = "") -> dict:
    """按作品编号或文件 fid 标记 bad（verdict=None 撤销），再刷新这些作品的封面。"""
    from .biblio import DB_NAME, open_db, set_reject
    cat = _open_cat(data_dir)
    try:
        hit: dict[str, list[str]] = {}
        unknown = []
        for i in ids:
            r = cat.execute("SELECT id FROM works WHERE id=?", (i,)).fetchone() or \
                cat.execute("SELECT work FROM files WHERE fid=?", (i,)).fetchone()
            if r is None:
                unknown.append(i)
                continue
            hit[r[0]] = [f[0] for f in cat.execute("SELECT fid FROM files WHERE work=?", (r[0],))]
    finally:
        cat.close()
    c = open_db(data_dir / DB_NAME)
    try:
        for wid, fids in hit.items():
            set_reject(c, fids, wid, verdict, why)
        c.commit()
    finally:
        c.close()
    return {"works": len(hit), "unknown": unknown, **refresh(data_dir, list(hit))}


def rejected_works(data_dir: Path) -> list[str]:
    """判过自带封面的作品（按现在的编目）。"""
    cat = _open_cat(data_dir)
    try:
        return [r[0] for r in cat.execute("SELECT DISTINCT f.work FROM files f JOIN b.cover_reject r ON r.fid=f.fid")]
    finally:
        cat.close()


def main(argv: list[str]) -> int:
    from .config import load
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname).1s %(message)s", datefmt="%H:%M:%S")
    data = load().data
    cmd, rest = (argv[0] if argv else ""), list(argv[1:])
    why = ""
    if "--why" in rest[:-1]:
        i = rest.index("--why")
        why = rest.pop(i + 1)
        rest.pop(i)
    args = [x for a in rest if not a.startswith("--") for x in a.split()]      # 一个参数里写了几个编号也认
    if cmd == "audit":
        from . import busy
        try:
            with busy.hold("cover check", kind="io"):                     # 要读上千本书的封面：和别的长任务错开
                st = audit(data, "--all" in argv)
        except busy.Busy as ex:
            print(f"⚠️ {ex}")
            return 1
        print(f"口碑高分 {st['works']:,} 本：自带封面 {st['own']:,}，外部封面 {st['ext']:,}；判为错图 {st['bad']}，"
              f"可疑 {st['suspect']}（这次新记 {st['new_suspect']}）；显示文字封面 {st['none']}")
        print(f"联系表：{st['sheet']}")
        return 0
    if cmd in ("reject", "unreject") and args:
        st = mark(data, args, "bad" if cmd == "reject" else None, why or "人工确认：自带的不是封面")
        print(f"{'标记' if cmd == 'reject' else '撤销'} {st['works']} 部作品；清掉书城缓存 {st['store']} 个，"
              f"重画阅读库封面 {st['reader']} 本" + (f"；找不到：{' '.join(st['unknown'])}" if st["unknown"] else ""))
        print("有可信 ISBN 的会在下次 `python -m library.biblio covers` 时去 Open Library 取封面。")
        return 0
    print(__doc__)
    return 1


if __name__ == "__main__":
    import sys
    sys.exit(main(sys.argv[1:]))
