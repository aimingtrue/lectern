"""书城里既不能读也不能翻阅的书：doc/docx/chm 转 epub（与原文对照验证）；打不开的坏文件移到废纸篓。"""
import re, shutil, sqlite3, subprocess, sys, tempfile, time
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from library.config import load  # noqa: E402
LIB = load().library_root()


def norm(s):
    return re.sub(r"[\W_]+", "", s.lower())


def text_of(p: Path, tmp: Path) -> str:
    out = tmp / (p.stem[:40] + ".txt")
    if p.suffix.lower() in (".doc", ".docx"):
        subprocess.run(["textutil", "-convert", "txt", "-output", str(out), str(p)], capture_output=True, timeout=300, check=True)
    else:
        subprocess.run(["ebook-convert", str(p), str(out)], capture_output=True, timeout=900, check=True)
    return out.read_text(errors="ignore")


def main():
    c = sqlite3.connect(ROOT / "data/catalog.db")
    rows = c.execute("SELECT w.title, f.path, f.fmt FROM works w JOIN files f ON f.work=w.id WHERE w.kind='book' "
                     "AND w.has_text=0 AND w.layout_src IS NULL").fetchall()
    stamp = time.strftime("%Y%m%d-%H%M%S")
    trash = Path.home() / ".Trash" / f"英文书库-无法阅读-{stamp}"
    log = open(ROOT / "logs" / f"office-{stamp}.txt", "w")
    ok = bad = 0
    for title, path, fmt in rows:
        src = Path(path)
        why = ""
        if fmt in ("doc", "docx", "chm") and src.exists():
            with tempfile.TemporaryDirectory() as td:
                tmp = Path(td)
                try:
                    a = norm(text_of(src, tmp))
                    mid = src
                    if fmt == "doc":                              # calibre 不读 .doc：先用 textutil 转 docx
                        mid = tmp / "x.docx"
                        subprocess.run(["textutil", "-convert", "docx", "-output", str(mid), str(src)], capture_output=True, timeout=300, check=True)
                    epub = tmp / "x.epub"
                    subprocess.run(["ebook-convert", str(mid), str(epub)], capture_output=True, timeout=900, check=True)
                    b = norm(text_of(epub, tmp))
                    if len(a) < 500:
                        why = f"原文只有 {len(a)} 字，不是一本书"
                    elif not 0.95 <= len(b) / len(a) <= 1.10:
                        why = f"转换后字数是原文的 {len(b) / len(a):.0%}"
                    else:
                        dest = src.with_suffix(".epub")
                        if dest.exists():
                            dest = src.with_name(src.stem + ".conv.epub")
                        shutil.copy(epub, dest)
                        log.write(f"OK\t{src}\t→ {dest}\n")
                        ok += 1
                except Exception as ex:  # noqa: BLE001
                    why = f"转换失败（{type(ex).__name__}）"
        else:
            why = "文件打不开或已损坏"
        if why or fmt in ("doc", "docx", "chm"):                  # 转成功的原文件、失败的文件都移到废纸篓
            if src.exists():
                dest = trash / src.relative_to(Path.home())
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(src), str(dest))
                log.write(f"{'移走' if not why else '删除'}\t{src}\t{dest}\t{why or '已转成 epub'}\n")
            if why:
                bad += 1
    log.close()
    print(f"转成 epub {ok} 本，无法阅读移到废纸篓 {bad} 本：{trash}")


if __name__ == "__main__":
    main()
