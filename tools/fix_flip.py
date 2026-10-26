"""纠正质量体检的误判：被转成「翻阅」的电子书里，calibre 能取出正文的其实是文字书（PyMuPDF 排版时没取出字）。

  .venv/bin/python tools/fix_flip.py logs/unreadable-*.txt ...           只出报告
  .venv/bin/python tools/fix_flip.py logs/unreadable-*.txt ... --apply   执行

对清单里「图像书，已转成可翻阅的 PDF」的每一本：用 calibre 从废纸篓里的原电子书取正文；
  · 文字是主体（2 万字以上，或 3000 字以上且平均每张图配 300 字以上）：calibre 重新打包成 epub 放回原位置，与原文对照验证（字数、抽查、首尾、阅读器）通过后，
    把误生成的翻阅 PDF 移到废纸篓；之后扫描入库就是一本正常可读的书
  · 没有正文：确实是图像书，保持翻阅
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))
FLIP = "图像书，已转成可翻阅的 PDF"


def main(argv: list[str]) -> int:
    from library.isolate import Isolated
    from quality_gate import calibre_chars, is_text_book
    from verify_conversions import check
    apply = "--apply" in argv
    rows = []
    for m in [a for a in argv if not a.startswith("--")]:
        for line in Path(m).read_text().splitlines():
            parts = line.split("\t")
            if len(parts) >= 3 and parts[2] == FLIP:
                rows.append((Path(parts[0]), Path(parts[1])))
    print(f"转成翻阅的电子书 {len(rows)} 本，逐本用 calibre 复查正文…", flush=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    trash = Path.home() / ".Trash" / f"英文书库-误转翻阅-{stamp}"
    log = open(ROOT / "logs" / f"fix-flip-{stamp}.txt", "w")
    text_books = fixed = 0
    for src, orig in rows:
        if not orig.exists() or src.exists():           # 原文件不在，或已经修复过
            continue
        if not is_text_book(str(orig)):                   # 漫画、绘本、插画书：保持翻阅
            continue
        n = calibre_chars(str(orig))
        text_books += 1
        print(f"  文字书 {n:>7} 字  {src.name[:50]}", flush=True)
        if not apply:
            continue
        out = src.with_suffix(".epub")
        if out.exists():
            out = src.with_name(src.stem + ".fixed.epub")
        try:
            subprocess.run(["ebook-convert", str(orig), str(out)], capture_output=True, timeout=900, check=True)
            iso = Isolated(300)
            r = check(out, orig, Path(tempfile.mkdtemp()), iso)
            iso.close()
            if not r["ok"]:
                raise RuntimeError(r["reason"])
            for pdf in (src.with_suffix(".pdf"), src.with_name(src.stem + ".flip.pdf")):
                if pdf.exists():
                    dest = trash / pdf.relative_to(Path.home())
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(pdf), str(dest))
                    log.write(f"{pdf}\t{dest}\t误转的翻阅 PDF（这是文字书）\n")
            log.write(f"# 修复为文字书：{out}\n")
            fixed += 1
        except Exception as ex:  # noqa: BLE001  修不好：保持翻阅
            out.unlink(missing_ok=True)
            log.write(f"# 修复未通过，保持翻阅：{src}\t{ex}\n")
    log.close()
    print(f"复查完：其中文字书 {text_books} 本" + (f"，修复为文字书 {fixed} 本" if apply else "（加 --apply 执行）"), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
