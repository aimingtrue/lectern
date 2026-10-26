"""装一个朗读音色：截一段 15–30 秒干净的单人原声 → 统一格式和响度 → 自动转写 → 写进 data/voice/<id>/ → 生成试听。

  .venv-tts/bin/python tools/add_voice.py --list                                   目录里的音色和安装状态
  .venv-tts/bin/python tools/add_voice.py <id> <音频/视频文件或链接> --from 0:37.4 --to 1:03.6
        [--text "逐字转写"]   不给就用 Qwen3-ASR 自动转写（转写错字会影响克隆效果，装完看一眼 ref.txt）
        [--source "出处"]     写进 SOURCE.md，默认就是文件名 / 链接
        [--denoise]           老录音有底噪时加一道降噪
        [--separate]          原声垫了背景音乐（朗诵、纪录片解说）：先用 demucs 分离出人声（独立环境 .venv-sep）
        [--auto]              不给时间段：在来源里自动挑一段 15–25 秒连续、干净的讲话（按停顿起止，底噪越低越好）

<id> 必须在 tts/voices.json 里（要加新人先改那个文件）。链接需要 yt-dlp（.venv-tts/bin/pip install yt-dlp），
下载的原始音频留在 data/voice/_src/，换时间段重截不用再下。试听要朗读服务在运行（:8792）。
声音克隆的音色只供本机个人朗读，不外传。
"""
import argparse
import json
import re
import shutil
import subprocess
import sys
import wave
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
VOICE = ROOT / "data" / "voice"
CATALOG = json.loads((ROOT / "tts" / "voices.json").read_text())
TTS = "http://127.0.0.1:8792"
SAMPLE = {"en": "It was the best of times, it was the worst of times, it was the age of wisdom, it was the age of foolishness, it was the epoch of belief, it was the epoch of incredulity.",
          "zh": "人间烟火，是最抚凡人心的味道。"}


def secs(t: str) -> float:
    """37.4 / 0:37.4 / 1:02:03 → 秒"""
    v = 0.0
    for p in t.split(":"):
        v = v * 60 + float(p)
    return v


def listing() -> None:
    cats = {c["id"]: c["name"] for c in CATALOG["cats"]}
    for c in CATALOG["cats"]:
        for lang in ("en", "zh"):
            vs = [v for v in CATALOG["voices"] if v["cat"] == c["id"] and v["lang"] == lang]
            done = [v for v in vs if (VOICE / v["id"] / "ref.wav").exists() and (VOICE / v["id"] / "ref.txt").exists()]
            print(f"{cats[c['id']]} · {'英文' if lang == 'en' else '中文'}（{len(done)}/{len(vs)}）")
            for v in vs:
                print(f"  {'✓' if v in done else '·'} {v['id']:<16} {v['name']}{'  ' + v['note'] if v.get('note') else ''}")


def fetch(src: str, vid: str, section: tuple[float, float] | None = None) -> Path:
    """本地文件直接用；链接用 yt-dlp 下载整条音频到 data/voice/_src/（同一来源不重复下）。
    section = 只要 [起, 止] 秒这一段：在本地截出来另存 .part.wav（让 ffmpeg 分段拉远程流时 TLS 常断）。"""
    if not re.match(r"https?://", src):
        p = Path(src).expanduser()
        if not p.exists():
            sys.exit(f"找不到文件 {p}")
        return p
    cache = VOICE / "_src"
    cache.mkdir(parents=True, exist_ok=True)
    have = sorted(cache.glob(f"{vid}.*"))
    if not (have and (cache / f"{vid}.url").exists() and (cache / f"{vid}.url").read_text().strip() == src):
        download(src, vid, cache, have)
    raw = next(p for p in sorted(cache.glob(f"{vid}.*")) if p.suffix not in (".url", ".wav"))
    if not section:
        return raw
    part = cache / f"{vid}.part.wav"
    subprocess.run([shutil.which("ffmpeg"), "-v", "error", "-y", "-ss", str(section[0]), "-to", str(section[1]), "-i", str(raw),
                    "-ac", "1", "-ar", "24000", str(part)], check=True)
    return part


def download(src: str, vid: str, cache: Path, have: list) -> None:
    ytdlp = shutil.which("yt-dlp") or str(Path(sys.executable).parent / "yt-dlp")
    if not Path(ytdlp).exists():
        sys.exit("下载链接需要 yt-dlp：.venv-tts/bin/pip install yt-dlp")
    for p in have:
        p.unlink()
    print(f"下载音频 {src}")
    import time
    for attempt in range(3):                              # 网络时断时续（SSL EOF）：整轮重来，间隔递增
        for fmt in ("bestaudio", "140", "ba*/b"):        # 有的视频默认音轨会被 403，换 m4a / 带画面的格式再试
            r = subprocess.run([ytdlp, "-q", "--no-warnings", "--retries", "10", "--socket-timeout", "30",
                                "-f", fmt, "-o", str(cache / f"{vid}.%(ext)s"), src])
            if r.returncode == 0:
                (cache / f"{vid}.url").write_text(src)
                return
            for p in cache.glob(f"{vid}.*"):
                p.unlink()
        time.sleep(30 * (attempt + 1))
    raise RuntimeError("下载失败（换格式、重试都不行）")


def separate(src: Path) -> Path:
    """demucs 把人声和背景音乐分开，返回人声轨。torch 很大，单独装在 .venv-sep，不进 .venv-tts。"""
    py = ROOT / ".venv-sep" / "bin" / "python"
    if not py.exists():
        sys.exit("分离人声需要 demucs：python3.12 -m venv .venv-sep && .venv-sep/bin/pip install demucs soundfile")
    out = src.parent / "sep"
    print("分离人声（demucs）…")
    subprocess.run([str(py), "-m", "demucs", "--two-stems=vocals", "-n", "htdemucs", "-d", "mps", "-o", str(out), str(src)],
                   check=True, capture_output=True)
    return out / "htdemucs" / src.stem / "vocals.wav"


def cut(src: Path, out: Path, a: float, b: float, denoise: bool) -> float:
    """单声道 24 kHz 16-bit，滤掉低频嗡声，响度统一到约 -20 LUFS（和朗读输出一致）。"""
    ff = shutil.which("ffmpeg")
    if not ff:
        sys.exit("需要 ffmpeg（brew install ffmpeg）")
    af = "highpass=f=60," + ("afftdn=nf=-25," if denoise else "") + "loudnorm=I=-20:TP=-2"
    out.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run([ff, "-v", "error", "-y", "-ss", str(a), "-to", str(b), "-i", str(src),
                    "-af", af, "-ac", "1", "-ar", "24000", "-c:a", "pcm_s16le", str(out)], check=True)
    with wave.open(str(out)) as w:
        return w.getnframes() / w.getframerate()


ASR = "Qwen/Qwen3-ASR-1.7B"
ALIGNER = "Qwen/Qwen3-ForcedAligner-0.6B"


def asr(path: Path, lang: str, **kw):
    """先用本机已下好的模型（离线）；没下好再联网下载，huggingface.co 连不上就走 hf-mirror.com。"""
    import os
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    from mlx_qwen3_asr import transcribe as run
    language = "English" if lang == "en" else "Chinese"
    try:
        return run(str(path), model=ASR, language=language, **kw)
    except Exception:  # noqa: BLE001
        if os.environ.get("HF_HUB_OFFLINE") != "1":
            raise
        os.environ["HF_HUB_OFFLINE"] = "0"
        import huggingface_hub.constants as hc
        hc.HF_HUB_OFFLINE = False
        try:
            httpx.head("https://huggingface.co", timeout=8)
        except httpx.HTTPError:
            os.environ["HF_ENDPOINT"] = hc.ENDPOINT = "https://hf-mirror.com"
        print("下载转写 / 对齐模型…")
        return run(str(path), model=ASR, language=language, **kw)


def transcribe(path: Path, lang: str) -> str:
    print(f"自动转写（{ASR}）…")
    return re.sub(r"\s+", " ", asr(path, lang).text).strip()


def pick(path: Path, lang: str, lo: float = 15.0, hi: float = 25.0) -> list[tuple[float, float, float]]:
    """在一段长录音里挑参考片段：在停顿（≥0.3 s）处起止、中间没有超过 1.2 s 的停顿、语速正常，
    并且落在一段至少 45 秒不断的讲话里（节目里夹的旁白、串场多是短段，这样不会挑到别人；找不到再放宽），
    按信噪比（50 ms 帧响度的 90 分位 / 10 分位，背景音乐和底噪都会拉低它）打分，时长越接近 20 秒越好。"""
    import numpy as np
    print("找一段连续、干净的讲话（逐词时间戳）…")
    r = asr(path, lang, return_timestamps=True, forced_aligner=ALIGNER)
    ws = [(float(x["start"]), float(x["end"])) for x in (r.segments or [])]
    pcm = subprocess.run([shutil.which("ffmpeg"), "-v", "error", "-i", str(path), "-ac", "1", "-ar", "16000", "-f", "f32le", "-"],
                         capture_output=True, check=True).stdout
    a16 = np.frombuffer(pcm, dtype=np.float32)
    n = len(a16) // 800
    rms = np.sqrt((a16[:n * 800].reshape(n, 800) ** 2).mean(axis=1)) + 1e-6
    run, k0 = [0.0] * len(ws), 0                 # 每个词所在那段连续讲话（停顿 ≤ 2 s）的长度
    for k in range(1, len(ws) + 1):
        if k == len(ws) or ws[k][0] - ws[k - 1][1] > 2.0:
            run[k0:k] = [ws[k - 1][1] - ws[k0][0]] * (k - k0)
            k0 = k
    for min_run, max_gap, low in ((45.0, 1.2, lo), (0.0, 1.2, lo), (0.0, 2.0, 12.0)):   # 朗诵停顿长，最后一档放宽
        cands = window(ws, run, rms, lang, low, hi, min_run, max_gap)
        if cands:
            break
    if not cands:
        raise RuntimeError("没找到 15–25 秒连续的讲话（换个来源，或手动给 --from/--to）")
    out = []                                         # 按得分取几段互不重叠的候选
    for c in sorted(cands, reverse=True):
        if all(c[2] <= o[0] or c[1] >= o[1] for o in out):
            out.append((c[1], c[2], c[3]))
        if len(out) == 6:
            break
    return out


def window(ws: list, run: list, rms, lang: str, lo: float, hi: float, min_run: float, max_gap: float):
    import numpy as np
    cands = []
    for i in range(1, len(ws)):
        if ws[i][0] - ws[i - 1][1] < 0.3 or run[i] < min_run:
            continue
        a = max(0.0, ws[i][0] - 0.15)
        for j in range(i, len(ws)):
            if j > i and ws[j][0] - ws[j - 1][1] > max_gap:
                break
            dur = ws[j][1] - a
            if dur > hi:
                break
            gap = ws[j + 1][0] - ws[j][1] if j + 1 < len(ws) else 9.0
            if dur < lo or gap < 0.3 or (lang == "en" and (j - i + 1) / dur < 1.4):
                continue
            b = ws[j][1] + 0.2
            f = rms[int(a * 20):int(b * 20)]
            snr = float(20 * np.log10(np.percentile(f, 90) / np.percentile(f, 10)))
            score = snr - abs(dur - 20) * 0.3
            cands.append((score, a, b, snr))
    return cands


def other_voice(text: str) -> bool:
    """转写像是混进了别人：有问句（访谈里主持人的提问），或用「您」称呼对方。"""
    return bool(re.search(r"[?？]|您", text))


def accuracy(ref: str, got: str) -> float:
    """试听的可懂度：转写回来的词（中文按字）和原文按顺序对得上的比例（最长公共子序列）。"""
    import difflib
    tok = (lambda t: re.findall(r"[a-z0-9']+", t.lower())) if re.search(r"[A-Za-z]", ref) else (lambda t: re.findall(r"[一-鿿]", t))
    a, b = tok(ref), tok(got)
    m = difflib.SequenceMatcher(None, a, b, autojunk=False)
    return sum(x.size for x in m.get_matching_blocks()) / max(1, len(a))


def install(v: dict, src: str, a: float | None, b: float | None, *, text=None, source=None, denoise=False,
            section=None, play=True, sep=False) -> dict:
    """装一个音色；a/b 为 None 时在来源里自动挑片段。返回 {秒数, 信噪比, 转写}。"""
    raw = fetch(src, v["id"], section)
    if sep:
        raw = separate(raw)
    snr = None
    d = VOICE / v["id"]
    if a is None:                                    # 自动挑：转写里像混进别人的候选跳过
        cands = pick(raw, v["lang"])
        for a, b, snr in cands:
            dur = cut(raw, d / "ref.wav", a, b, denoise)
            text = transcribe(d / "ref.wav", v["lang"])
            if not other_voice(text):
                break
            print(f"跳过（像有别人在说话）：{text[:60]}")
        else:
            a, b, snr = cands[0]
            dur = cut(raw, d / "ref.wav", a, b, denoise)
            text = transcribe(d / "ref.wav", v["lang"])
    else:
        dur = cut(raw, d / "ref.wav", a, b, denoise)
        text = text or transcribe(d / "ref.wav", v["lang"])
    (d / "ref.txt").write_text(text + "\n")
    off = section[0] if section else 0
    (d / "SOURCE.md").write_text(f"{v['name']}{'，' + v['note'] if v.get('note') else ''}\n"
                                 f"{source or src}  — {a + off:.1f}–{b + off:.1f} 秒\n仅供本机个人朗读使用。\n")
    acc = None
    print(f"已装 {v['name']}：data/voice/{v['id']}/（{dur:.1f} 秒{f'，信噪比 {snr:.0f} dB' if snr else ''}）\n转写：{text}")
    try:
        r = httpx.post(f"{TTS}/speak", json={"text": SAMPLE[v["lang"]], "voice": v["id"]}, timeout=300, trust_env=False)
        r.raise_for_status()
        (d / "sample.wav").write_bytes(r.content)
        acc = accuracy(SAMPLE[v["lang"]], transcribe(d / "sample.wav", v["lang"]))
        print(f"试听：data/voice/{v['id']}/sample.wav（转写回来和原文一致 {acc:.0%}）")
        if play and shutil.which("afplay"):
            subprocess.run(["afplay", str(d / "sample.wav")])
    except httpx.HTTPError as ex:
        print(f"没生成试听（朗读服务没运行？）：{ex}")
    return {"dur": dur, "snr": snr, "text": text, "at": (a + off, b + off), "acc": acc}


def main() -> None:
    ap = argparse.ArgumentParser(description="装一个朗读音色")
    ap.add_argument("id", nargs="?")
    ap.add_argument("src", nargs="?")
    ap.add_argument("--from", dest="a", default="0")
    ap.add_argument("--to", dest="b")
    ap.add_argument("--text")
    ap.add_argument("--source")
    ap.add_argument("--denoise", action="store_true")
    ap.add_argument("--separate", action="store_true")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--auto", action="store_true")
    o, more = ap.parse_known_args()
    if more:
        ap.error(f"多余的参数 {more}")
    if o.list or not o.id:
        return listing()
    v = next((x for x in CATALOG["voices"] if x["id"] == o.id), None)
    if v is None:
        sys.exit(f"{o.id} 不在 tts/voices.json 里（--list 看目录）")
    if not o.src or not (o.b or o.auto):
        sys.exit("需要音频来源，以及 --to 结束时间（或 --auto 自动挑）")
    a, b = (None, None) if o.auto else (secs(o.a), secs(o.b))
    if a is not None and not 5 <= b - a <= 60:
        sys.exit("片段要在 5–60 秒之间（建议 15–30 秒）")
    install(v, o.src, a, b, text=o.text, source=o.source, denoise=o.denoise, sep=o.separate)


if __name__ == "__main__":
    main()
