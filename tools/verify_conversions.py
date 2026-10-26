"""验证格式转换（azw3/mobi → epub）有没有丢内容；不完整的算转换失败，epub 和原文件一起移到废纸篓。

  .venv/bin/python tools/verify_conversions.py           只验证，写报告
  .venv/bin/python tools/verify_conversions.py --apply   验证后把失败的移到废纸篓「英文书库-转换失败-时间」

对照方法：用 calibre 把原文件和转出来的 epub 各自转成纯文本（同一个提取器，比较才公平），去掉空白和标点后比较：
  · 字数：epub ≥ 原文件的 95%（也不能多出 10% 以上）
  · 抽查：在原文里均匀取 20 段 40 字的片段，至少 19 段要在 epub 里原样出现；开头、结尾两段必须出现
  · 原文件本身不到 5000 字（约 1000 个英文词）：书本身不完整，同样算失败
  · 阅读器（PyMuPDF）要能打开 epub 并读出正文
原文件：azw3 在去重时已移到废纸篓，从 logs/dedupe-*.txt 清单里找；mobi 还在原处。
"""
from __future__ import annotations

import csv
import random
import re
import shutil
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from library.config import load  # noqa: E402
LIB = load().library_root()             # 放书的主文件夹（data/config.json 的 sources）
LOGS = ROOT / "logs"


CONVERTED_AFTER = time.mktime(time.strptime("2026-10-08 22:50", "%Y-%m-%d %H:%M"))   # 这次转换开始的时间


def pairs() -> list[tuple[Path, Path]]:
    """[(转出来的 epub, 原文件当前位置)]。转出来的 epub = 转换开始后才生成、并且有同名 azw3/mobi 原文件的 epub
    （拷贝时保留了原始修改时间，盘上原有的 epub 都早于这个时间，不会被误认）。"""
    moved = {}
    for m in sorted(LOGS.glob("dedupe-*.txt")):
        for line in m.read_text(errors="replace").splitlines():
            if line and not line.startswith("#") and "\t" in line:
                a, b = line.split("\t", 1)
                moved[a] = b
    out = []
    for e in LIB.rglob("*.epub"):
        if any(x.startswith(".") for x in e.relative_to(LIB).parts) or e.stat().st_mtime < CONVERTED_AFTER:
            continue
        for ext in (".azw3", ".mobi", ".AZW3", ".MOBI"):
            src = e.with_suffix(ext)
            cur = src if src.exists() else (Path(moved[str(src)]) if str(src) in moved else None)
            if cur is not None and cur.exists():
                out.append((e, cur))
                break
        else:
            out.append((e, None))                        # 找不到原文件：无法对照，按失败处理
    return sorted(out)


def calibre_text(path: Path, tmp: Path) -> str:
    out = tmp / f"{abs(hash(str(path)))}.txt"
    r = subprocess.run(["ebook-convert", str(path), str(out)], capture_output=True, timeout=900)
    if r.returncode != 0 or not out.exists():
        raise RuntimeError("calibre 读不出文字")
    return out.read_text(errors="ignore")


def norm(s: str) -> str:
    return re.sub(r"[\W_]+", "", s.lower())


def _reader_ok(path: str) -> bool:
    import pymupdf
    with pymupdf.open(path) as d:
        n = d.page_count
        return n > 0 and any(len(d[i].get_text().strip()) > 50 for i in sorted({0, n // 3, n // 2, 2 * n // 3} & set(range(n))))


def check(epub: Path, src: Path, tmp: Path, iso) -> dict:
    r = {"epub": str(epub), "source": str(src), "ok": False, "reason": ""}
    if src is None or not src.exists():
        r["reason"] = "找不到原文件，无法对照"
        return r
    try:
        a, b = norm(calibre_text(src, tmp)), norm(calibre_text(epub, tmp))
    except Exception as ex:  # noqa: BLE001
        r["reason"] = f"提取文字失败：{ex}"
        return r
    r["src_chars"], r["epub_chars"] = len(a), len(b)
    if len(a) < 5000:
        r["reason"] = f"原书内容过少（{len(a)} 字），书本身不完整"
        return r
    ratio = len(b) / len(a)
    r["ratio"] = round(ratio, 3)
    rnd = random.Random(str(src))
    k = 40
    pos = [int(len(a) * f) for f in (0.01, 0.99)] + [rnd.randrange(0, len(a) - k) for _ in range(20)]
    found = [a[p:p + k] in b for p in pos]
    r["edges"], r["samples"] = sum(found[:2]), sum(found[2:])
    try:
        reader = iso.call(_reader_ok, str(epub))
    except Exception:  # noqa: BLE001
        reader = False
    r["reader"] = reader
    if not 0.95 <= ratio <= 1.10:
        r["reason"] = f"字数不符：epub 是原文的 {ratio:.0%}"
    elif r["edges"] < 2:
        r["reason"] = "开头或结尾缺失"
    elif r["samples"] < 19:
        r["reason"] = f"抽查只对上 {r['samples']}/20 段"
    elif not reader:
        r["reason"] = "阅读器打不开或读不出正文"
    else:
        r["ok"] = True
    return r


def main(argv: list[str]) -> int:
    from library.isolate import Isolated
    ps = pairs()
    print(f"待验证的转换：{len(ps)} 本", flush=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    results = []
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        isos = [Isolated(180) for _ in range(4)]

        def run(i_p):
            i, (e, s) = i_p
            return check(e, s, tmp, isos[i % 4])
        with ThreadPoolExecutor(4) as ex:
            for n, r in enumerate(ex.map(run, enumerate(ps)), 1):
                results.append(r)
                if not r["ok"]:
                    print(f"✗ {Path(r['epub']).stem[:60]}：{r['reason']}", flush=True)
                if n % 50 == 0:
                    print(f"{n}/{len(ps)}", flush=True)
        for iso in isos:
            iso.close()
    report = LOGS / f"verify-convert-{stamp}.csv"
    keys = ["ok", "reason", "ratio", "edges", "samples", "reader", "src_chars", "epub_chars", "epub", "source"]
    with open(report, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=keys, extrasaction="ignore")
        w.writeheader()
        w.writerows(results)
    bad = [r for r in results if not r["ok"]]
    print(f"通过 {len(results) - len(bad)}，失败 {len(bad)}。报告：{report}", flush=True)
    if "--apply" in argv and bad:
        trash = Path.home() / ".Trash" / f"英文书库-转换失败-{stamp}"
        with open(LOGS / f"convert-failed-{stamp}.txt", "w") as log:
            for r in bad:
                for p in [Path(r["epub"])] + ([Path(r["source"])] if r["source"] not in ("", "None") else []):
                    if p.exists() and str(p).startswith(str(LIB)):        # 只动书库里的；废纸篓里的原文件留在原处
                        dest = trash / p.relative_to(Path.home())
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        shutil.move(str(p), str(dest))
                        log.write(f"{p}\t{dest}\t{r['reason']}\n")
        print(f"失败的已移到废纸篓：{trash}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
