"""把书库里所有 azw3 转成同目录同名的 epub（已有就跳过）。临时文件放在隐藏目录，编目不会扫到半成品。"""
import os, shutil, subprocess, sys, time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from library.config import load  # noqa: E402
ROOT = load().library_root()
TMP = ROOT / ".convert-tmp"
TMP.mkdir(exist_ok=True)
files = sorted(p for p in ROOT.rglob("*") if p.suffix.lower() == ".azw3" and not any(x.startswith(".") for x in p.relative_to(ROOT).parts))

def one(i_p):
    i, p = i_p
    out = p.with_suffix(".epub")
    if out.exists():
        return "跳过", p
    tmp = TMP / f"{i}.epub"
    try:
        r = subprocess.run(["ebook-convert", str(p), str(tmp)], capture_output=True, timeout=600)
        if r.returncode == 0 and tmp.exists() and tmp.stat().st_size > 0:
            shutil.move(str(tmp), str(out))
            return "OK", p
        return "失败", p
    except subprocess.TimeoutExpired:
        return "超时", p
    finally:
        tmp.unlink(missing_ok=True)

print(f"共 {len(files)} 个 azw3", flush=True)
with ThreadPoolExecutor(4) as ex:
    for k, (st, p) in enumerate(ex.map(one, enumerate(files)), 1):
        print(f"{k}/{len(files)} {st}：{p.relative_to(ROOT)}", flush=True)
shutil.rmtree(TMP, ignore_errors=True)
print("=== 完成", time.strftime("%F %T"), flush=True)
