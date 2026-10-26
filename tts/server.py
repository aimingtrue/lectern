"""朗读音色服务（本机，Apple Silicon / MLX）。

模型 Qwen3-TTS 1.7B Base（bf16，不量化）只加载一份；每个音色就是一段原声做零样本克隆：
data/voice/<id>/ref.wav + ref.txt（逐字转写）+ SOURCE.md（出处）。音色目录（行业、人名、语言）在 tts/voices.json，
装新音色用 tools/add_voice.py。默认音色 id 见 tts/voices.json，可用 LECTERN_VOICE 覆盖。

  .venv-tts/bin/python tts/server.py                    启动服务 http://127.0.0.1:8792
  .venv-tts/bin/python tts/server.py --prefetch [音色]  下载模型并生成一段试听 data/voice/<音色>/sample.wav
"""

import hashlib
import io
import itertools
import json
import logging
import os
import re
import sys
import threading
import time
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VOICE = ROOT / "data" / "voice"
CATALOG = ROOT / "tts" / "voices.json"
DEFAULT = os.environ.get("LECTERN_VOICE", "mine")    # 默认旁白音色 id；装好的音色见 data/voice/
READY = VOICE / ".model_ready"
MODEL = os.environ.get("LECTERN_MODEL", "mlx-community/Qwen3-TTS-12Hz-1.7B-Base-bf16")
PORT = int(os.environ.get("LECTERN_PORT", "8792"))
# 模型下载好以后离线加载，不再联网检查
if READY.exists() and READY.read_text().strip() == MODEL:
    os.environ.setdefault("HF_HUB_OFFLINE", "1")

log = logging.getLogger("voice")


def ref_files(v: str) -> tuple[Path, Path]:
    return VOICE / v / "ref.wav", VOICE / v / "ref.txt"


_fps: dict = {}


def voice_id(v: str) -> str | None:
    """模型 + 参考音频 + 参考文字 的指纹；任何一个变了，书库那边这个音色的缓存自动作废。没装好返回 None。"""
    if not re.fullmatch(r"[a-z0-9_-]{1,40}", v or ""):
        return None
    wav, txt = ref_files(v)
    if not wav.exists() or not txt.exists():
        return None
    k = (v, wav.stat().st_mtime_ns, wav.stat().st_size, txt.stat().st_mtime_ns)
    if k not in _fps:
        h = hashlib.sha1(MODEL.encode())
        h.update(wav.read_bytes())
        h.update(txt.read_text().strip().encode())
        _fps[k] = h.hexdigest()[:12]
    return _fps[k]


def catalog() -> dict:
    return json.loads(CATALOG.read_text())


def voices() -> list[dict]:
    """装好的音色（目录里有、参考文件齐全），每个带指纹 vid；pick = 在角色配音偏好表 cast_order 里的位置（没排的没有）。"""
    cat = catalog()
    order = {v: i for i, v in enumerate(cat.get("cast_order") or [])}
    return [{**x, "vid": vid, "bed": (VOICE / x["id"] / "bed.m4a").exists(),   # bed：有配好的背景音（tools/add_bed.py）
             **({"pick": order[x["id"]]} if x["id"] in order else {})}
            for x in cat["voices"] if (vid := voice_id(x["id"]))]


HAN = re.compile(r"[\u3400-\u9fff\uf900-\ufaff]")


def is_zh(text: str) -> bool:
    """中文句：汉字数 ×3 ≥ 拉丁字母数（一个汉字大约顶一个英文词）。夹几个英文词的中文句仍算中文。"""
    han = len(HAN.findall(text))
    return han > 0 and han * 3 >= len(re.findall(r"[A-Za-z]", text))


def clean(text: str) -> str:
    if is_zh(text):                     # 中文标点模型认得，只把破折号换成停顿
        t = re.sub(r"[—–]+", "，", text)
        return re.sub(r"\s+", " ", t).strip()
    t = text.replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    t = t.replace("—", ", ").replace("–", ", ").replace("…", "...")
    t = re.sub(r"\s+", " ", t).strip()
    return t


def chunks(text: str, limit: int | None = None) -> list[str]:
    """很长的句子按标点切小段分别生成（单段太长时模型容易跑偏）。中文每段最多 90 字（约 20 秒），英文 260 字符。"""
    zh = is_zh(text)
    limit = limit or (90 if zh else 260)
    if len(text) <= limit:
        return [text]
    parts = re.split(r"(?<=[，；：、。！？…])" if zh else r"(?<=[,;:])\s+", text)
    sep = "" if zh else " "
    out, cur = [], ""
    for p in parts:
        if not p:
            continue
        if cur and len(cur) + len(sep) + len(p) > limit:
            out.append(cur)
            cur = p
        else:
            cur = f"{cur}{sep}{p}".strip()
    if cur:
        out.append(cur)
    final = []
    for c in out:  # 仍然超长的硬切到词边界；中文没有空格，直接按字数切
        while len(c) > limit * 1.6:
            k = c.rfind(" ", 0, limit)
            if k <= 0:
                k = limit
            final.append(c[:k])
            c = c[k:].lstrip()
        final.append(c)
    return final


class Voice:
    """模型常驻；同一音色同时到达的几句攒成一批一起生成（批量吞吐约为单句的 3–4 倍），句与句之间音色一致。"""
    BATCH = 4

    def __init__(self):
        import queue
        import mlx.core as mx  # noqa: F401
        from mlx_audio.tts.utils import load_model
        t0 = time.time()
        log.info("加载模型 %s …", MODEL)
        self.model = load_model(MODEL)
        self.sr = int(getattr(self.model, "sample_rate", 24000))
        self.refs: dict = {}                                         # 音色 → (指纹, 参考音频数组, 参考文字)
        self.ref_lock = threading.Lock()
        self.temp = float(os.environ.get("LECTERN_TEMP", "0.7"))      # 稳一点：句与句之间音色更一致
        self.can_batch = hasattr(self.model, "batch_generate")
        self.q: "queue.Queue" = queue.PriorityQueue()               # (优先级, 序号, 任务)：流式（有人等着听）排在预生成的前面
        self.seq = itertools.count()
        threading.Thread(target=self._worker, daemon=True).start()
        log.info("模型就绪（%.1fs），采样率 %d，批量=%s", time.time() - t0, self.sr, self.can_batch)

    def ref(self, v: str):
        """音色的参考音频和文字；同一个数组反复用 → 模型内部缓存参考音频的编码。参考文件换了自动重载。"""
        import numpy as np
        from mlx_audio.utils import load_audio
        vid = voice_id(v)
        if vid is None:
            raise KeyError(v)
        with self.ref_lock:
            r = self.refs.get(v)
            if r is None or r[0] != vid:
                wav, txt = ref_files(v)
                audio = load_audio(str(wav), sample_rate=self.sr)
                r = self.refs[v] = (vid, audio, txt.read_text().strip(), ref_gain(np.array(audio, dtype=np.float32).reshape(-1), self.sr))
            return r[1], r[2]

    def gain(self, v: str) -> float:
        """这个音色的固定增益（见 ref_gain）：每句都乘同一个数，流式时不用先看整句。"""
        self.ref(v)
        return self.refs[v][3]

    # ---- 生成
    def _one(self, text: str, ref):
        import numpy as np
        out = []
        for r in self.model.generate(text=text, ref_audio=ref[0], ref_text=ref[1], temperature=self.temp):
            out.append(np.array(r.audio, dtype=np.float32).reshape(-1))
        return np.concatenate(out) if out else np.zeros(1, dtype=np.float32)

    def _many(self, texts: list[str], ref) -> list:
        import numpy as np
        if len(texts) == 1 or not self.can_batch:
            return [self._one(t, ref) for t in texts]
        res: dict = {}
        for r in self.model.batch_generate(texts=texts, ref_audio=ref[0], ref_text=ref[1],
                                           temperature=self.temp, stream=False):
            res.setdefault(r.sequence_idx, []).append(np.array(r.audio, dtype=np.float32).reshape(-1))
        return [np.concatenate(res[i]) if i in res else self._one(t, ref) for i, t in enumerate(texts)]

    def _worker(self):
        import queue
        while True:
            jobs = [self.q.get()[2]]               # 任务 = (文字, 音色, box, 流式输出队列或 None)
            if jobs[0][3] is None:                 # 普通任务：稍等一下，把同时到的几句凑成一批（流式任务不攒批，到了就单独做）
                deadline = time.time() + 0.08
                while len(jobs) < self.BATCH:
                    try:
                        jobs.append(self.q.get(timeout=max(0.0, deadline - time.time()))[2])
                    except queue.Empty:
                        break
            for text, v, _, out in [j for j in jobs if j[3] is not None]:
                self._stream(text, v, out)
            jobs = [j for j in jobs if j[3] is None]
            groups: dict = {}                      # 一批只放同一个音色
            for j in jobs:
                groups.setdefault(j[1], []).append(j)
            for v, js in groups.items():
                texts = [t for t, _, _, _ in js]
                t0 = time.time()
                try:
                    audios = self._many(texts, self.ref(v))
                    for (_, _, box, _), a in zip(js, audios):
                        box["audio"] = a
                except Exception as ex:  # noqa: BLE001
                    log.exception("生成失败")
                    for _, _, box, _ in js:
                        box["error"] = ex
                finally:
                    for _, _, box, _ in js:
                        box["done"].set()
                log.info("%.2fs ×%d [%s]  %s", time.time() - t0, len(js), v, " | ".join(x[:40] for x in texts))

    def _stream(self, text: str, v: str, out) -> None:
        """流式任务（在生成线程里跑）：长句的几段依次生成，每出一块（约 STREAM_SEC 秒音频）就放进 out；做完放 None，出错放异常。"""
        import numpy as np
        t0, first = time.time(), 0.0
        try:
            ref = self.ref(v)
            for i, piece in enumerate(chunks(text)):
                for r in self.model.generate(text=piece, ref_audio=ref[0], ref_text=ref[1], temperature=self.temp,
                                             stream=True, streaming_interval=STREAM_SEC):
                    first = first or time.time() - t0
                    out.put((i, np.array(r.audio, dtype=np.float32).reshape(-1)))
            out.put(None)
        except Exception as ex:  # noqa: BLE001
            log.exception("流式生成失败")
            out.put(ex)
        log.info("%.2fs 流式（首块 %.2fs）[%s]  %s", time.time() - t0, first, v, text[:40])

    def _gen(self, text: str, v: str):
        box = {"done": threading.Event()}
        self.q.put((1, next(self.seq), (text, v, box, None)))
        box["done"].wait()
        if "error" in box:
            raise box["error"]
        return box["audio"]

    def speak(self, text: str, v: str):
        """返回的就是最终音频（切头尾、淡入淡出、固定增益都在这里做完）；逐词对齐在书库存好这个文件之后才跑，时间天然一致。"""
        a = join([self._gen(c, v) for c in chunks(clean(text))], self.sr)
        return edges(a, self.sr) * self.gain(v)                  # 响度：按音色的固定增益（流式用同一个数），wav() 转 16 位时限幅

    def stream(self, text: str, v: str):
        """边生成边给：产出 16 位 PCM 块（切头、淡入淡出、段间停顿、增益都和 speak 一样，见 Streamer），最后一块之后结束。
        生成出错时抛出异常：HTTP 响应随之中断，书库那边当作失败。"""
        import queue
        out: "queue.Queue" = queue.Queue()
        self.q.put((0, next(self.seq), (clean(text), v, {}, out)))
        st = Streamer(self.sr, self.gain(v))
        while True:
            item = out.get()
            if item is None:
                break
            if isinstance(item, Exception):
                raise item
            data = st.feed(*item)
            if data:
                yield data
        yield st.finish()

    def wav(self, text: str, v: str = DEFAULT) -> bytes:
        import numpy as np
        a = self.speak(text, v)
        pcm = (np.clip(a, -1, 1) * 32767).astype("<i2").tobytes()
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(self.sr)
            w.writeframes(pcm)
        return buf.getvalue()


# ---- 句子头尾：参考音频带底噪（大教室混响）的音色，生成的每句开头就是满幅底噪、结尾被硬切到数字静音，
# 句与句之间听着一顿一顿的。切头尾的门限跟着这句自己的底噪走，再淡入淡出，让每句都从静音起、落回静音。
FADE_IN, FADE_OUT = 0.02, 0.10
QUIET = 1e-4                                             # -80 dBFS 以下当作静音（模型解码器收尾、拼接补的零）


def frames(a, sr: int, ms: float = 10):
    """每 ms 毫秒一帧的均方根，和帧长（采样点数）。"""
    import numpy as np
    n = max(1, int(sr * ms / 1000))
    m = len(a) // n
    return np.sqrt((np.asarray(a[:m * n], dtype=np.float64).reshape(m, n) ** 2).mean(axis=1)), n


def bounds(a, sr: int, thr: float = 0.006) -> tuple[int, int, int, int]:
    """切头尾的位置 (起, 止, 第一帧讲话起, 最后一帧讲话止)，采样点。前留 60 ms、后留 180 ms，不超出有声的范围。
    按 10 ms 帧的均方根判断（单个采样点会被底噪的尖峰触发）。门限头尾各算各的 = max(thr, 这一头的底噪 ×4)，
    底噪取这一头 50 ms 的中位数：开头就是满幅底噪的切到讲话前；开头是静音的干净音色门限就是 thr，
    讲话前的换气照旧留着。门限最多到响亮讲话（95 分位）的 15%（约 -16 dB）：一开头就是讲话、或者很短的句子，
    不能因此把字切掉（比这还轻的头一个音节靠前留的 60 ms 保住开头）。"""
    import numpy as np
    loud = np.flatnonzero(np.abs(a) > QUIET)
    if not len(loud):
        return 0, len(a), 0, len(a)
    r, n = frames(a, sr)
    live = r[r > 1e-5]                                   # 数字静音不算底噪
    th = tt = thr
    if len(live) >= 10:
        cap = float(np.percentile(live, 95)) * 0.15
        th = max(thr, min(float(np.median(live[:5])) * 4, cap))
        tt = max(thr, min(float(np.median(live[-5:])) * 4, cap))
    head, tail = np.flatnonzero(r > th), np.flatnonzero(r > tt)
    on, off = (int(head[0]) * n, (int(tail[-1]) + 1) * n) if len(head) and len(tail) else (0, len(a))
    pad = int(sr * 0.06)
    s = max(int(loud[0]), on - pad)
    e = max(s + 1, min(int(loud[-1]) + 1, off + pad * 3))
    return s, e, min(max(on, s), e), min(max(off, s), e)


def trim(a, sr: int, thr: float = 0.006):
    """去掉头尾的静音和底噪（位置见 bounds），返回原数组的切片。"""
    s, e, _, _ = bounds(a, sr, thr)
    return a[s:e]


def fade(a, sr: int, fin: float = FADE_IN, fout: float = FADE_OUT):
    """升余弦淡入淡出，作用在有声的那一段上（两头 -80 dBFS 以下的静音置 0）：有声部分的第一个和最后一个采样点都是 0。
    音频太短时淡入淡出各不超过四分之一。"""
    import numpy as np
    a = np.array(a, dtype=np.float32)
    loud = np.flatnonzero(np.abs(a) > QUIET)
    if not len(loud):
        return a
    s, e = int(loud[0]), int(loud[-1]) + 1
    a[:s], a[e:] = 0, 0
    k, j = min(int(sr * fin), (e - s) // 4), min(int(sr * fout), (e - s) // 4)
    if k > 1:
        a[s:s + k] *= 0.5 - 0.5 * np.cos(np.linspace(0, np.pi, k, dtype=np.float32))
    if j > 1:
        a[e - j:e] *= 0.5 + 0.5 * np.cos(np.linspace(0, np.pi, j, dtype=np.float32))
    return a


def edges(a, sr: int):
    """speak 的最后一步：切头尾再淡入淡出。淡入 20 ms、淡出 100 ms 都落在讲话外面留的底噪上；
    模型收尾太快、讲话后面不够 100 ms 时淡出跟着缩短（最少 30 ms），不吃掉最后一个字的尾音。"""
    s, e, on, off = bounds(a, sr)
    return fade(a[s:e], sr, min(FADE_IN, max(0.005, (on - s) / sr)), min(FADE_OUT, max(0.03, (e - off) / sr)))


def join(pieces: list, sr: int, gap: float = 0.12):
    """长句分段生成的几段拼起来：接缝两边各淡出 30 ms / 淡入 10 ms，中间 120 ms 静音（以前底噪直接硬接数字静音）。
    淡入淡出不改长度，停顿和以前一样长；整句的头尾留给 edges。"""
    import numpy as np
    out = []
    for i, p in enumerate(pieces):
        p = fade(p, sr, 0.01 if i else 0, 0.03 if i < len(pieces) - 1 else 0)
        out += [np.zeros(int(sr * gap), dtype=np.float32), p] if i else [p]
    return np.concatenate(out) if out else np.zeros(1, dtype=np.float32)


# ---- 响度：每个音色一个固定增益，不再按每句自己的均方根归一（流式时整句还没生成完，看不到整句）。
# 增益 = 目标 × K ÷ 参考音频的讲话响度。K 是实测校准值：模型生成的句子平均比参考音频轻 2.7 dB（12 个音色 × 3 句，标准差 2 dB），
# 乘上它以后整句均方根平均仍在 -20 dBFS，句与句之间会有 ±2 dB 的自然起伏（以前每句都正好 -20 dBFS）。
TARGET = 0.1                                             # -20 dBFS
GAIN_K = 1.36                                            # +2.7 dB
STREAM_SEC = 0.4                                         # 流式每块约多长（12.5 个码元 / 秒 → 5 个码元一块）


def level(a, sr: int, thr: float = 0.006) -> float:
    """讲话的均方根：10 ms 帧里高于门限的那些帧（门限同 bounds 的上限：响亮讲话的 15%），不算停顿和底噪。"""
    import numpy as np
    r, _ = frames(a, sr)
    live = r[r > 1e-5]
    if len(live) < 10:
        return float(np.sqrt(np.mean(np.asarray(a, dtype=np.float64) ** 2)) + 1e-9)
    sp = live[live > max(thr, float(np.percentile(live, 95)) * 0.15)]
    return float(np.sqrt(np.mean((sp if len(sp) else live) ** 2)) + 1e-9)


def ref_gain(ref, sr: int) -> float:
    return TARGET * GAIN_K / level(ref, sr)


def onset(a, sr: int, thr: float = 0.006):
    """流式切头：只看已到的开头找第一声，门限和 bounds 的头门限一样算。还没出现返回 None（开头的底噪 / 静音先攒着，
    批量路径也会把它切掉）：讲话还没出现时 95 分位只是底噪、上限落到门限以下，不能拿底噪的尖峰当第一声。返回 (切头位置, 讲话起点)。"""
    import numpy as np
    loud = np.flatnonzero(np.abs(a) > QUIET)
    if not len(loud):
        return None
    r, n = frames(a, sr)
    live = r[r > 1e-5]
    if len(live) < 10:
        return None
    cap = float(np.percentile(live, 95)) * 0.15
    if cap < thr:
        return None
    head = np.flatnonzero(r > max(thr, min(float(np.median(live[:5])) * 4, cap)))
    if not len(head):
        return None
    on = int(head[0]) * n
    return max(int(loud[0]), on - int(sr * 0.06)), on


class Streamer:
    """流式输出的头尾处理：切头、淡入淡出、段间停顿、增益都按 join + edges 的规则，但只看已经到手的音频。
    开头攒到第一声（onset）再发；每段结尾扣住 HOLD 秒不发（实测模型收尾的静音最长 0.5 s），段生成完了按整段算出尾切点再补发；
    段与段之间同 join：淡出 30 ms · 120 ms 静音 · 淡入 10 ms。feed / finish 返回的是可直接发出的 16 位 PCM。"""
    HOLD, GAP = 0.6, 0.12

    def __init__(self, sr: int, gain: float):
        self.sr, self.gain = sr, gain
        self.piece, self.cur, self.sent, self.started, self.out = -1, None, 0, False, 0

    def pcm(self, a) -> bytes:
        import numpy as np
        self.out += len(a)
        return (np.clip(a * self.gain, -1, 1) * 32767).astype("<i2").tobytes()

    def feed(self, piece: int, a) -> bytes:
        import numpy as np
        sr, out = self.sr, b""
        if piece != self.piece:
            if self.piece >= 0:
                out += self._close(last=False)
            self.piece, self.cur, self.sent, self.started = piece, np.zeros(0, dtype=np.float32), 0, piece > 0
            if piece > 0:
                a = fade(a, sr, 0.01, 0)
        self.cur = np.concatenate([self.cur, np.asarray(a, dtype=np.float32).reshape(-1)])
        if not self.started:
            got = onset(self.cur, sr)
            if got is None:
                return out
            s, on = got
            self.cur = fade(self.cur[s:], sr, min(FADE_IN, max(0.005, (on - s) / sr)), 0)
            self.started = True
        avail = len(self.cur) - int(sr * self.HOLD)
        if avail > self.sent:
            out += self.pcm(self.cur[self.sent:avail])
            self.sent = avail
        return out

    def _close(self, last: bool) -> bytes:
        import numpy as np
        sr, x = self.sr, self.cur
        if x is None:
            return b""
        if not self.started:                                 # 整段都没找到第一声（极短或全是底噪）：按批量路径整段切头尾
            s, e, on, off = bounds(x, sr)
            x = fade(x[s:e], sr, min(FADE_IN, max(0.005, (on - s) / sr)), min(FADE_OUT, max(0.03, (e - off) / sr)))
            self.cur, self.started, e, j = x, True, len(x), 0
        elif last:
            _, e, _, off = bounds(x, sr)
            j = min(FADE_OUT, max(0.03, (e - off) / sr))
        else:                                                # 段与段之间：整段都留着（同 join），只在有声的末尾淡出
            e, j = len(x), 0.03
        x = fade(x[:e], sr, 0, j)
        out = self.pcm(x[self.sent:]) if e > self.sent else b""
        self.sent = max(self.sent, e)
        return out if last else out + self.pcm(np.zeros(int(sr * self.GAP), dtype=np.float32))

    def finish(self) -> bytes:
        import numpy as np
        out = self._close(last=True)
        self.piece = -1
        return out if self.out else self.pcm(np.zeros(int(self.sr * 0.01), dtype=np.float32))


def wav_header(sr: int, nbytes: int = 0xFFFFFFFF) -> bytes:
    """单声道 16 位 wav 头（44 字节）；长度未知（流式）时两处长度都填 0xFFFFFFFF。"""
    import struct
    riff = 0xFFFFFFFF if nbytes == 0xFFFFFFFF else nbytes + 36
    return b"RIFF" + struct.pack("<I", riff) + b"WAVEfmt " + struct.pack("<IHHIIHH", 16, 1, 1, sr, sr * 2, 2, 16) + b"data" + struct.pack("<I", nbytes)


# ---- 逐词对齐：Qwen3-ForcedAligner（和 NTU caption 同款），已知原文 + 生成好的音频 → 每个词的起止时间
ALIGNER = os.environ.get("LECTERN_ALIGNER", "Qwen/Qwen3-ForcedAligner-0.6B")
_align_pool = None
_aligner = None


def _align(path: str, text: str) -> list:
    global _aligner
    from mlx_qwen3_asr.audio import load_audio_np
    from mlx_qwen3_asr.forced_aligner import ForcedAligner
    if _aligner is None:
        _aligner = ForcedAligner(ALIGNER)
    ws = _aligner.align(load_audio_np(path), text, "Chinese" if is_zh(text) else "English")   # 中文按字给时间
    return [[w.text, round(float(w.start_time), 3), round(float(w.end_time), 3)] for w in ws]


_align_at = 0.0
WARM_EVERY = 600                                          # 对齐模型空闲这么久就跑一次保温（空闲后首句曾慢到 0.8–6 s）


def align(path: str, text: str) -> list:
    """对齐模型只在一个专用线程里加载和运行，不和朗读生成线程争用 MLX。"""
    global _align_pool, _align_at
    if _align_pool is None:
        from concurrent.futures import ThreadPoolExecutor
        _align_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="align")
    _align_at = time.time()
    return _align_pool.submit(_align, path, text).result()


def keep_aligner_warm(sample: Path, text: str) -> None:
    """后台：对齐模型空闲超过 WARM_EVERY 秒就对一段短音频对齐一次，让它常驻在 GPU 内存里。"""
    while True:
        time.sleep(30)
        if _align_pool is not None and time.time() - _align_at > WARM_EVERY and sample.exists():
            try:
                align(str(sample), text)
            except Exception as ex:  # noqa: BLE001
                log.info("对齐保温失败：%s", ex)


def serve() -> None:
    import uvicorn
    from fastapi import FastAPI, HTTPException, Request
    from fastapi.responses import Response, StreamingResponse

    app = FastAPI(title="朗读音色")
    state: dict = {"voice": None, "error": None}

    def boot():
        try:
            state["voice"] = Voice()
            warm = VOICE / ".warm.wav"
            warm.write_bytes(state["voice"].wav("Ready."))  # 预热；这段也留给对齐保温用
            READY.write_text(MODEL)
            threading.Thread(target=keep_aligner_warm, args=(warm, "Ready."), daemon=True).start()
        except Exception as ex:  # noqa: BLE001
            log.exception("模型加载失败")
            state["error"] = str(ex)
    threading.Thread(target=boot, daemon=True).start()

    @app.get("/health")
    def health():
        """voices = 装好的音色；default 是默认音色的 id，voice_id 是它的指纹。"""
        vs = voices()
        d = next((x for x in vs if x["id"] == DEFAULT), vs[0] if vs else {})
        return {"ok": state["voice"] is not None and bool(vs), "loading": state["voice"] is None and not state["error"],
                "error": state["error"] or (None if vs else "一个音色也没装（data/voice/<id>/ref.wav + ref.txt）"),
                "model": MODEL, "default": d.get("id"), "voice": d.get("name"), "voice_id": d.get("vid"),
                "cats": catalog()["cats"], "voices": vs}

    def want(d: dict) -> tuple:
        """/speak 与 /speak/stream 共同的参数检查：(文字, 音色, 模型)。"""
        text = clean(d.get("text", ""))[:2000]
        if not text:
            raise HTTPException(400, "no text")
        name = str(d.get("voice") or DEFAULT)
        if voice_id(name) is None:
            raise HTTPException(404, f"音色 {name} 没装")
        v = state["voice"]
        if v is None:
            raise HTTPException(503, state["error"] or "朗读模型还在加载")
        return text, name, v

    @app.post("/speak")
    async def speak(req: Request):
        text, name, v = want(await req.json())
        import anyio
        t0 = time.time()
        data = await anyio.to_thread.run_sync(v.wav, text, name)
        log.info("%.2fs [%s]  %s", time.time() - t0, name, text[:70])
        return Response(data, media_type="audio/wav")

    @app.post("/speak/stream")
    async def speak_stream(req: Request):
        """同 /speak，但边生成边回：wav 头（长度未知）+ 逐块 PCM，chunked 传输；书库一边转发给浏览器一边落盘。"""
        text, name, v = want(await req.json())

        def body():
            yield wav_header(v.sr)
            yield from v.stream(text, name)
        return StreamingResponse(body(), media_type="audio/wav", headers={"X-Sample-Rate": str(v.sr), "Cache-Control": "no-store"})

    @app.post("/align")
    async def align_api(req: Request):
        d = await req.json()
        path = Path(d.get("path", "")).resolve()
        text = clean(d.get("text", ""))[:2000]
        if not path.is_relative_to(ROOT / "data") or path.suffix != ".wav" or not path.exists() or not text:
            raise HTTPException(400, "bad parameters")
        import anyio
        t0 = time.time()
        try:
            words = await anyio.to_thread.run_sync(align, str(path), text)
        except Exception as ex:  # noqa: BLE001
            log.exception("对齐失败")
            raise HTTPException(503, f"对齐失败：{ex}")
        log.info("对齐 %.2fs  %d 词", time.time() - t0, len(words))
        return {"words": words}

    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning")


def prefetch(name: str = DEFAULT) -> None:
    if voice_id(name) is None:
        sys.exit(f"音色 {name} 没装：缺 data/voice/{name}/ref.wav 或 ref.txt")
    v = Voice()
    t0 = time.time()
    sample = ("Here's our first question. What's the right thing to do? "
              "It was the best of times, it was the worst of times, it was the age of wisdom, it was the age of foolishness.")
    data = v.wav(sample, name)
    (VOICE / name / "sample.wav").write_bytes(data)
    READY.write_text(MODEL)
    secs = (len(data) - 44) / 2 / v.sr
    print(f"试听已生成：data/voice/{name}/sample.wav（{secs:.1f} 秒音频，用时 {time.time() - t0:.1f} 秒）")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname).1s %(message)s", datefmt="%H:%M:%S")
    if "--prefetch" in sys.argv:
        rest = sys.argv[sys.argv.index("--prefetch") + 1:]
        prefetch(rest[0] if rest else DEFAULT)
    else:
        serve()
