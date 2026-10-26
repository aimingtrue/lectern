"""质量关卡：已入库的书里找出错误数据（会误导人），按情况处理。

  .venv/bin/python tools/quality_gate.py           只出报告
  .venv/bin/python tools/quality_gate.py --apply   按报告处理
  .venv/bin/python tools/quality_gate.py --apply --only=重转,重新入库   只处理指定的几类
  .venv/bin/python tools/quality_gate.py --since 1760000000   只查这个时间（秒）以后入库的书（新书入库用；全库体检隔段时间跑一次）

分类（只删真正错误的数据）：
  删除  · 碎片化：重新分段后仍有一半以上段落只有 1–3 个字
        · 乱码：「�」超过 20%；西文书像单词的词元不到 30%（例如 `&/ 3 M M&`）；
                中文书生僻字超过 40%（只抓极端的识别错文字；繁体书、古籍可达 20–30%，不能据此删书）
        · 几乎没有内容：全书不到 500 字（导图、剪报、广告页）
        · 残缺：试读页、简介页（「下载后」「点击此处查看」「试读结束」…）或 epub 缺章（ERROR: cannot find chapter）
        · 空的电子书：epub/mobi 读不出任何文字
        · 文件损坏：mobi 中段的正文记录解出来是乱码（calibre 也读不出），书不完整
  翻阅  · 扫描版或图像书（没有文字，或 20 页以上每页不到 20 字）的 PDF：保留文件，删书库记录，编目记 gate=flip（重新编目保留），书城显示「翻阅」
  转翻阅 · epub/mobi 读不出字、calibre 也取不出正文（漫画、绘本、图片内页），10 页以上：转成 PDF 翻阅
  重转  · 少量「�」（引号等被读错编码）的 mobi/azw/epub：用 calibre 重新转 epub，验证后替换
        · mobi 入库的正文不到原文件的 90%（解析器读到一半停下）：calibre 转 epub，验证完整后替换
  修复  · 电子书读不出字或一字一段，但 calibre 能取出正文：calibre 重新打包 epub，验证后替换（是文字书，不转翻阅）
  重新入库 · epub 入库的正文不到原文件的 90%（排版时整段不能换行被裁掉）：按新规则重新解析
          · 中文字之间有空格（「阿 不 思」）：去掉空格重新入库
          · GBK 编码的中文 txt 被当成西文读了（「ºï×Ó±¾ÔÚ」）：按 GB18030 重新入库
  保留  · 本来就短的完整内容（绘本、短文）、表格/填空线多的书
删除和替换下来的文件都移到废纸篓「英文书库-无法阅读-时间」，清单在 logs/。删掉的（删除、转翻阅失败）文件指纹记进排除名单
（exclude.db 的 blocked，规则「质量：原因」）：同一个文件以后再导入，编目时直接拦下。新书入库（tools/ops/new-books.sh）带 --apply 跑。
"""
from __future__ import annotations

import csv
import json
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))
from library.config import load  # noqa: E402
LIB = load().library_root()             # 放书的主文件夹（data/config.json 的 sources）


def metrics(c: sqlite3.Connection, bid: str, common: set | None) -> dict:
    """体检指标（规则见 library/digest.gate）：读入库时存下的摘要；没有摘要的书按全文现算。"""
    from library import digest
    d = digest.read(c, bid)
    m = json.loads(d["gate"]) if d is not None and d["gate"] else digest.gate(
        c.execute("SELECT kind, text FROM paras WHERE book=? ORDER BY n", (bid,)).fetchall(), common)
    if m["sense"] is None:                     # 入库时还没有常用汉字表：不判中文乱码
        m["sense"] = 1.0
    return m


HAN, LATIN = re.compile(r"[\u3400-\u9fff]"), re.compile(r"[A-Za-z]")
COMPLETE = 0.9                                     # 入库正文 / 原文件正文 低于这个算不完整


def weight(s: str) -> float:
    """有效字数：汉字按 2.5 个字母计（和编目判断语言的口径一致）。"""
    return len(HAN.findall(s)) * 2.5 + len(LATIN.findall(s))


def source(path: str) -> tuple[float, int] | None:
    """原文件正文的有效字数和坏记录数：epub 按书脊读全书，mobi 解全部正文记录；其他格式不查（None）。"""
    from library.catalog import epub_meta, mobi_text
    ext = Path(path).suffix.lower()
    try:
        if ext == ".epub":
            return weight(epub_meta(Path(path))["sample"]), 0
        if ext in (".mobi", ".azw"):
            text, bad, n = mobi_text(Path(path))
            return (weight(re.sub(r"<[^>]+>", " ", text)), bad) if n else None
    except Exception:  # noqa: BLE001  打不开的文件由其他规则处理
        return None
    return None


def completeness(args) -> tuple:
    """(书库正文有效字数, 原文件有效字数, 坏记录数)；在子进程里跑。"""
    db_path, bid, path = args
    src = source(path) if Path(path).exists() else None
    if src is None:
        return bid, None
    c = sqlite3.connect(db_path)
    texts = [r[0] for r in c.execute("SELECT text FROM paras WHERE book=?", (bid,))]
    if texts:
        have = weight("".join(texts))
    else:                                          # 没存全文（只留摘要）的书：用入库时记下的有效字数；旧摘要没有就不判
        from library import digest
        d = digest.read(c, bid)
        have = json.loads(d["gate"]).get("w") if d is not None and d["gate"] else None
    c.close()
    return bid, None if have is None else (have, src[0], src[1])


def replacement_chars(epub: Path) -> int:
    """epub 正文里的「�」个数（要解压后在 HTML 里数：压缩数据里碰巧出现这 3 个字节不算）。"""
    import zipfile
    with zipfile.ZipFile(epub) as z:
        return sum(z.read(n).decode("utf-8", "ignore").count("\ufffd")
                   for n in z.namelist() if n.lower().endswith((".xhtml", ".html", ".htm")))


def calibre_chars(path: str) -> int:
    """calibre 取出的正文字数（PyMuPDF 排版某些 epub 时取不出字，calibre 能）。"""
    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "x.txt"
        try:
            subprocess.run(["ebook-convert", path, str(out)], capture_output=True, timeout=900)
        except subprocess.TimeoutExpired:
            return 0
        return len(re.sub(r"\s", "", out.read_text(errors="ignore"))) if out.exists() else 0


def image_count(path: str) -> int | None:
    """epub 里的图片数；不是 zip（mobi 等）时返回 None。"""
    import zipfile
    try:
        with zipfile.ZipFile(path) as z:
            return sum(n.lower().endswith((".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg")) for n in z.namelist())
    except (zipfile.BadZipFile, OSError):
        return None


def is_text_book(path: str) -> bool:
    """calibre 取得出正文、而且文字是主体：2 万字以上，或 3000 字以上且平均每张图配 300 字以上。
    漫画、绘本、插画书只有零散的配字（平均每张图十几个字），转成纯文字会丢掉图，仍按图像书翻阅。"""
    n = calibre_chars(path)
    if n >= 20000:
        return True
    imgs = image_count(path)
    return n >= 3000 and imgs is not None and n / max(1, imgs) >= 300


def verdict(m: dict, n: int, fmt: str, pages: int, exists: bool, path: str = "",
            comp: tuple | None = None) -> tuple[str, str]:
    """(处理, 原因)；处理是 删除 / 翻阅 / 转翻阅 / 修复 / 重转 / 重新入库 / 空串（保留）。
    comp：(书库正文有效字数, 原文件有效字数, 坏记录数)，epub/mobi 才有。"""
    if not exists:
        return "删除", "文件已不在（失效记录）"
    if comp and comp[2]:
        return "删除", f"文件损坏（{comp[2]} 段正文是乱码），书不完整"
    per_page = m["total"] / max(1, pages)
    ebook = fmt != "pdf"
    little = m["total"] == 0 or (pages >= 20 and per_page < 20) or m["total"] < 500
    fragmented = n >= 50 and m["tiny"] > 0.5 and m["total"] / n < 15      # 真碎的书平均每段一两个字；诗集偶有短行不算
    if little or fragmented:
        if ebook:
            # 电子书读不出字或一字一段：先看 calibre 能不能取出正文——能，就是文字书，修复；不能，才是图像书
            if is_text_book(path):
                return "修复", "正文没取出来，用 calibre 重新打包"
            if pages >= 10:
                return "转翻阅", "图像书，转成可翻阅的版本"
            return "删除", "电子书读不出正文"
        if m["total"] == 0 or (pages >= 20 and per_page < 20):
            return "翻阅", "扫描版或图像书，文字在图片里"
        if m["total"] < 500:
            return "删除", "几乎没有内容，不是一本书"
        return "删除", "碎片化（一字一段）"
    if comp and comp[1] >= 2000 and comp[0] < COMPLETE * comp[1]:
        pct = f"入库正文只有原文件的 {comp[0] / comp[1]:.0%}"
        return ("重转", f"{pct}，用 calibre 转 epub") if fmt in ("mobi", "azw") else ("重新入库", f"{pct}，重新解析")
    # 中文：生僻字比例只在极端时算乱码（繁体书、古籍会到 20–30%，不能用来删书）
    if m["repl"] > 0.2 or m["sense"] < (0.6 if m["zh"] else 0.3):
        return "删除", "乱码"
    if m["teaser"]:
        return "删除", "残缺（试读页或缺章）"
    if m["latin1"] > 0.3 and fmt == "txt":
        return "重新入库", "GBK 中文被当成西文读了"
    if m["spaced"] > 0.2:
        return "重新入库", "中文字之间有空格"
    if m["repl"] > 0.001 and fmt in ("mobi", "azw", "azw3", "epub") and not m["zh"]:
        return "重转", "少量编码错误"
    return "", ""


def main(argv: list[str]) -> int:
    from collections import Counter

    from library.catalog import Catalog
    from library.config import load
    from library.db import DB
    cfg = load()
    c = sqlite3.connect(cfg.db_path)
    since = float(argv[argv.index("--since") + 1]) if "--since" in argv else 0
    rows = c.execute("SELECT id, path, title, n_paras, pages FROM books WHERE status='ok' AND COALESCE(added, 0) >= ?",
                     (since,)).fetchall()
    from library import digest
    common = digest.common_han()
    from multiprocessing import Pool
    with Pool(6) as pool:                          # 完整度：读原文件全书，放到子进程并行
        comps = dict(pool.imap_unordered(completeness, [(str(cfg.db_path), r[0], r[1]) for r in rows], chunksize=20))
    found = []
    for i, (bid, path, title, n, pages) in enumerate(rows, 1):
        m = metrics(c, bid, common)
        action, why = verdict(m, n or 0, Path(path).suffix.lower().lstrip("."), pages or 0, Path(path).exists(), path,
                              comps.get(bid))
        if action:
            found.append({"action": action, "reason": why, "id": bid, "path": path, "title": title, **m})
        if i % 3000 == 0:
            print(f"{i:,}/{len(rows):,}", flush=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    report = ROOT / "logs" / f"quality-gate-{stamp}.csv"
    with open(report, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["action", "reason", "title", "total", "tiny", "repl", "zh", "sense", "spaced", "latin1", "teaser", "path", "id"],
                           extrasaction="ignore")
        w.writeheader()
        w.writerows(found)
    print(f"检查 {len(rows):,} 本：" + "、".join(f"{k} {v}" for k, v in Counter(x["action"] for x in found).items()))
    for k, v in Counter((x["action"], x["reason"]) for x in found).most_common():
        print(f"  {k[0]} · {k[1]}：{v}")
    print(f"报告：{report}")
    only = next((a.split("=", 1)[1].split(",") for a in argv if a.startswith("--only=")), None)
    if only:                                       # 只处理指定的几类（其余留着复核）
        found = [x for x in found if x["action"] in only]
        print(f"只处理：{'、'.join(only)}，共 {len(found)} 本")
    if "--apply" not in argv or not found:
        return 0
    db = DB(cfg.db_path)
    cat = Catalog(cfg.data / "catalog.db")
    trash = Path.home() / ".Trash" / f"英文书库-无法阅读-{stamp}"
    log = open(ROOT / "logs" / f"unreadable-{stamp}.txt", "w")

    bad: list[tuple] = []                      # 删掉的文件：(指纹, 书名, 原因)，最后记进排除名单

    def to_trash(p: Path, why: str) -> None:
        if p.exists() and str(p).startswith(str(LIB)):
            dest = trash / p.relative_to(Path.home())
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(p), str(dest))
            log.write(f"{p}\t{dest}\t{why}\n")

    reconvert, reparse, to_pdf = [], [], []
    for x in found:
        p = Path(x["path"])
        if x["action"] == "删除":
            to_trash(p, x["reason"])
            db.remove_book(x["id"])
            if Path(x["path"]).exists() or "文件已不在" not in x["reason"]:    # 文件不在了（改名、移走）不等于文件坏，不拦
                bad.append((x["id"], x["title"], x["reason"]))
        elif x["action"] == "翻阅":                # 扫描版/图像书：留文件，删书库记录；编目里标成没有文字，书城显示「翻阅」
            db.remove_book(x["id"])
            cat.set_gate(str(p), "flip")
        elif x["action"] in ("重转", "修复"):
            reconvert.append(x)
        elif x["action"] == "重新入库":
            reparse.append(x)
        elif x["action"] == "转翻阅":
            to_pdf.append(x)
    ok = 0
    for x in reconvert:                            # calibre 重新转 epub，与原文对照验证，通过才替换
        src = Path(x["path"])
        out = src.with_name(src.stem + ".fixed.epub")
        try:
            subprocess.run(["ebook-convert", str(src), str(out)], capture_output=True, timeout=900, check=True)
            from verify_conversions import check  # noqa: E402
            from library.isolate import Isolated
            iso = Isolated(180)
            r = check(out, src, Path(tempfile.mkdtemp()), iso)
            iso.close()
            if r["ok"] and not replacement_chars(out):
                final = src.with_suffix(".epub") if not src.with_suffix(".epub").exists() else out
                if final != out:
                    out.rename(final)
                db.remove_book(x["id"])                # 先删书库记录再移文件：中途出错时原书还在原处
                to_trash(src, "已换成重新转换、验证完整的 epub")
                ok += 1
            else:                                  # 原文件里本来就是「�」：重新入库，解析时按上下文补回标点
                out.unlink(missing_ok=True)
                reparse.append(x)
                log.write(f"# 重转未通过，原书重新入库补标点：{src}\t{r.get('reason') or '仍有乱码'}\n")
        except Exception as ex:  # noqa: BLE001
            out.unlink(missing_ok=True)
            reparse.append(x)
            log.write(f"# 重转失败，原书重新入库补标点：{src}\t{ex}\n")
    flip_ok = 0
    for x in to_pdf:                               # 图像书：calibre 转 PDF，能打开、页数够、首页能渲染才替换
        src = Path(x["path"])
        out = src.with_suffix(".pdf")
        if out.exists():
            out = src.with_name(src.stem + ".flip.pdf")
        try:
            subprocess.run(["ebook-convert", str(src), str(out)], capture_output=True, timeout=1800, check=True)
            import pymupdf
            with pymupdf.open(out) as d:
                good = d.page_count >= 10 and d[0].get_pixmap(matrix=pymupdf.Matrix(0.2, 0.2)).width > 0
            if not good:
                raise RuntimeError("转出的 PDF 页数太少或打不开")
            to_trash(src, "图像书，已转成可翻阅的 PDF")
            db.remove_book(x["id"])
            flip_ok += 1
        except Exception as ex:  # noqa: BLE001  转不了：按读不出正文删除
            out.unlink(missing_ok=True)
            to_trash(src, f"图像书转 PDF 失败（{ex}），无法阅读")
            db.remove_book(x["id"])
            bad.append((x["id"], x["title"], "图像书转 PDF 失败，无法阅读"))
    if reparse:                                    # 去掉中文字间空格后重新入库（sanitize 已处理）
        from library.dictionary import Dictionary
        from library.scan import Scanner
        sc = Scanner(db, cfg, Dictionary(cfg.dict_path).words())
        for x in reparse:
            if Path(x["path"]).exists():
                sc.add_file(Path(x["path"]), force=True)
    log.close()
    if bad:                                        # 删掉的文件记进排除名单：只记指纹（同一本书别的好版本照常进来）
        from library import exclude
        ec = exclude.open_db(cfg.data)
        ec.executemany("INSERT OR IGNORE INTO blocked(kind, key, why, rule, title, author, run, at) VALUES(?,?,?,?,?,?,?,?)",
                       [("fid", fid, why, f"{exclude.QUALITY}：{why}", title, "", f"quality-gate-{stamp}", time.time())
                        for fid, title, why in bad])
        ec.commit()
        ec.close()
    print(f"图像书转翻阅 {flip_ok}/{len(to_pdf)}；重新入库 {len(reparse)} 本；删除和替换下来的文件在废纸篓：{trash}；重转成功 {ok}/{len(reconvert)}。重新编目…", flush=True)
    cat.scan(cfg.sources)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
