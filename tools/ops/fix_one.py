"""修一本打不开的 epub：calibre 重新打包，与原文对照验证，阅读器能打开才替换。"""
import shutil, sys, tempfile, time
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "tools"))


def main(src: Path):
    from verify_conversions import check
    from library.isolate import Isolated
    out = src.with_name(src.stem + ".fixed.epub")
    import subprocess
    subprocess.run(["ebook-convert", str(src), str(out)], capture_output=True, timeout=900, check=True)
    iso = Isolated(300)
    r = check(out, src, Path(tempfile.mkdtemp()), iso)
    iso.close()
    print(r)
    if r["ok"]:
        trash = Path.home() / ".Trash" / f"英文书库-修复-{time.strftime('%Y%m%d-%H%M%S')}" / src.relative_to(Path.home())
        trash.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(trash))
        print("已替换，原文件在", trash)
    else:
        out.unlink(missing_ok=True)


if __name__ == "__main__":
    main(Path(sys.argv[1]))
