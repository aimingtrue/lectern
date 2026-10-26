"""给音色配一段背景音：从纪录片里分离出「去掉人声」的配乐和环境声，挑一段平稳不断档的，做成可循环的背景音轨。

  .venv-tts/bin/python tools/add_bed.py <音色 id> <视频链接或音频文件> [--skip 120] [--span 600] [--len 150]

结果是 data/voice/<id>/bed.m4a（响度约 -30 LUFS，首尾淡入淡出，循环播放时接缝不突兀）和 BED.md（出处）。
朗读页播放这个音色时，背景音在底下循环：读句子时压低，思考停顿里稍微抬起，Aa 面板里可以关。
配乐有版权，只在本机播放，不进 git（.gitignore）。人声分离用 tools/add_voice.py 同一套（demucs，.venv-sep）。
"""
import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from add_voice import VOICE, fetch, separate  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
CATALOG = json.loads((ROOT / "tts" / "voices.json").read_text())


def per_second_db(path: Path):
    import numpy as np
    pcm = subprocess.run([shutil.which("ffmpeg"), "-v", "error", "-i", str(path), "-ac", "1", "-ar", "8000", "-f", "f32le", "-"],
                         capture_output=True, check=True).stdout
    a = np.frombuffer(pcm, dtype=np.float32)
    n = len(a) // 8000
    return 20 * np.log10(np.sqrt((a[:n * 8000].reshape(n, 8000) ** 2).mean(axis=1)) + 1e-6)


def steadiest(music: Path, vocals: Path, length: float) -> float:
    """每秒响度里挑一段 length 秒：配乐不断档、响度平稳，并且人声轨几乎没声（没有旁白，分离的残留也就听不见）。
    返回起点秒数。"""
    import numpy as np
    m, v = per_second_db(music), per_second_db(vocals)
    n, L = min(len(m), len(v)), int(length)
    if n <= L:
        return 0.0
    best, at = None, 0
    for s in range(0, n - L, 5):
        w, vw = m[s:s + L], v[s:s + L]
        speech = float(np.mean(vw > m[s:s + L] - 12))       # 人声轨离配乐不到 12 dB 的秒数比例：有人在说话
        score = np.percentile(w, 5) - 0.5 * np.std(w) - 60 * speech
        if best is None or score > best:
            best, at = score, s
    return float(at)


def main() -> None:
    ap = argparse.ArgumentParser(description="给音色配背景音")
    ap.add_argument("id")
    ap.add_argument("src")
    ap.add_argument("--skip", type=float, default=120)
    ap.add_argument("--span", type=float, default=600)
    ap.add_argument("--len", dest="length", type=float, default=150)
    o = ap.parse_args()
    v = next((x for x in CATALOG["voices"] if x["id"] == o.id), None)
    if v is None:
        sys.exit(f"{o.id} 不在 tts/voices.json 里")
    raw = fetch(o.src, f"{o.id}-bed", (o.skip, o.skip + o.span))
    vocals = separate(raw)
    music = vocals.with_name("no_vocals.wav")
    at = steadiest(music, vocals, o.length)
    out = VOICE / o.id / "bed.m4a"
    fade = 4
    subprocess.run([shutil.which("ffmpeg"), "-v", "error", "-y", "-ss", str(at), "-t", str(o.length), "-i", str(music),
                    "-af", f"loudnorm=I=-30:TP=-6,afade=t=in:d={fade},afade=t=out:st={o.length - fade}:d={fade}",
                    "-ac", "2", "-ar", "44100", "-c:a", "aac", "-b:a", "128k", str(out)], check=True)
    (VOICE / o.id / "BED.md").write_text(f"{v['name']} 的背景音：{o.src}  — 去掉人声后 {o.skip + at:.0f}–{o.skip + at + o.length:.0f} 秒\n"
                                       "配乐有版权，仅供本机朗读时播放，不外传。\n")
    for f in (VOICE / "_src").glob(f"{o.id}-bed.*"):
        f.unlink()
    shutil.rmtree(VOICE / "_src" / "sep" / "htdemucs" / f"{o.id}-bed.part", ignore_errors=True)
    print(f"背景音：data/voice/{o.id}/bed.m4a（{o.length:.0f} 秒，取自分离后的 {o.skip + at:.0f} 秒起）")


if __name__ == "__main__":
    main()
