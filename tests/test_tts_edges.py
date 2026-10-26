"""朗读音频的头尾处理（tts/server.py 的 trim / fade / join）：用合成信号（正弦“讲话” + 底噪）测，不加载 MLX。"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parent.parent
SR = 24000


@pytest.fixture(scope="module")
def m():
    spec = importlib.util.spec_from_file_location("tts_server_edges", ROOT / "tts" / "server.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)   # 模块顶层只用标准库，numpy 在函数里导入
    return mod


def at(a, t) -> int:
    """trim 返回的是原数组的切片：算它在原数组里的起点。"""
    return (t.__array_interface__["data"][0] - a.__array_interface__["data"][0]) // a.itemsize


def db(x) -> float:
    return float(20 * np.log10(np.sqrt(np.mean(np.asarray(x, dtype=np.float64) ** 2)) + 1e-12))


def clip(lead=0.5, speech=(0.8, 0.4, 0.8), pause=0.3, tail=0.6, noise_db=-45.0, seed=0):
    """lead 秒底噪 → 几段 220 Hz“讲话”（-14 dBFS 左右）中间夹停顿 → tail 秒底噪；整段叠一层底噪（大教室那种）。
    返回音频和第一段讲话的起点、最后一段讲话的终点（采样点）。"""
    rng = np.random.default_rng(seed)
    parts, t = [np.zeros(int(SR * lead))], int(SR * lead)
    on = t
    for k, d in enumerate(speech):
        n = int(SR * d)
        parts.append(0.3 * np.sin(2 * np.pi * 220 * np.arange(n) / SR) * np.hanning(n) ** 0.1)
        t += n
        if k < len(speech) - 1:
            parts.append(np.zeros(int(SR * pause)))
            t += int(SR * pause)
    off = t
    parts.append(np.zeros(int(SR * tail)))
    a = np.concatenate(parts)
    a += rng.normal(0, 10 ** (noise_db / 20), len(a))
    return a.astype(np.float32), on, off


def test_fade_starts_and_ends_at_silence(m):
    a, _, _ = clip(lead=0.0, tail=0.0, noise_db=-40)      # 第 0 个采样点就是满幅：以前的“台阶”
    f = m.fade(a, SR)
    assert len(f) == len(a)
    assert f[0] == 0 and f[-1] == 0
    assert db(f[:int(SR * 0.002)]) < db(a[:int(SR * 0.002)]) - 20      # 头 2 ms 几乎是静音
    assert db(f[-int(SR * 0.01):]) < db(a[-int(SR * 0.01):]) - 20      # 尾 10 ms 几乎是静音
    mid = slice(int(SR * 0.2), len(a) - int(SR * 0.2))
    assert np.array_equal(f[mid], a[mid])                               # 中间不动


def test_trim_follows_own_noise_floor(m):
    """底噪 -45 dBFS（帧均方根约 0.0056）：固定门限 0.006 几乎贴着底噪，按底噪 ×4 才能把头尾的底噪切掉，讲话不动。"""
    a, on, off = clip(noise_db=-45)
    t = m.trim(a, SR)
    k = at(a, t)
    assert on - int(SR * 0.08) <= k <= on                              # 只留讲话前 ~60 ms
    assert off <= k + len(t) <= off + int(SR * 0.2)                    # 讲话后留 ~180 ms
    assert len(t) < len(a) - int(SR * 0.7)


def test_trim_clean_voice_unchanged_behaviour(m):
    """几乎没底噪（-80 dBFS）的音色：门限就是固定的下限，头尾的静音照样切掉，讲话一点不少。"""
    a, on, off = clip(noise_db=-80)
    t = m.trim(a, SR)
    k = at(a, t)
    assert on - int(SR * 0.08) <= k <= on and off <= k + len(t) <= off + int(SR * 0.2)


def test_trim_keeps_breath_after_clean_silence(m):
    """干净音色（Attenborough）：100 ms 静音 → 200 ms 换气（-40 dBFS）→ 讲话，停顿里是轻微混响（-48 dBFS）。
    按全句的底噪（10 分位）算门限会抬到换气之上把它切掉；开头 50 ms 是静音，所以这一头门限不抬，换气留着。"""
    a, on, off = clip(lead=0.3, noise_db=-48, seed=4)
    rng = np.random.default_rng(5)
    s0, b0 = on - int(SR * 0.3), on - int(SR * 0.2)
    a[s0:b0] = rng.normal(0, 10 ** (-85 / 20), b0 - s0)
    a[b0:on] = rng.normal(0, 10 ** (-40 / 20), on - b0)
    t = m.trim(a, SR)
    assert b0 - int(SR * 0.08) <= at(a, t) <= b0                      # 换气没被切，前面的静音切掉
    assert off <= at(a, t) + len(t) <= off + int(SR * 0.2)


def test_short_clip_not_destroyed(m):
    """很短、一开头就是讲话的一个词：先一个轻音节再一个重音节。开头 50 ms 的“底噪”其实是轻音节（×4 比它还响），
    门限封顶在响亮讲话的 15%，比它响的轻音节（-14 dB）不能被切掉；淡入淡出也不吃掉它。"""
    rng = np.random.default_rng(3)
    n1, n2 = int(SR * 0.15), int(SR * 0.2)
    w = np.arange(n1 + n2) / SR
    amp = np.r_[np.full(n1, 0.06), np.full(n2, 0.3)]
    a = (amp * np.sin(2 * np.pi * 220 * w) + rng.normal(0, 10 ** (-45 / 20), n1 + n2)).astype(np.float32)
    t = m.trim(a, SR)
    assert at(a, t) <= int(SR * 0.01) and len(t) >= len(a) - int(SR * 0.01)
    f = m.fade(t, SR)
    assert db(f[int(SR * 0.03):n1]) > db(a[int(SR * 0.03):n1]) - 1      # 轻音节（淡入以后）完整
    tiny = np.full(40, 0.2, dtype=np.float32)                          # 极短：淡入淡出各不超过 1/4
    g = m.fade(tiny, SR)
    assert g[0] == 0 and g[-1] == 0 and np.all(g[10:30] == np.float32(0.2))
    assert len(m.trim(np.zeros(100, dtype=np.float32), SR)) == 100      # 全静音原样返回
    assert len(m.fade(np.zeros(1, dtype=np.float32), SR)) == 1


def test_join_fades_seams_keeps_length(m):
    a, _, _ = clip(lead=0.0, tail=0.0, noise_db=-40, seed=1)
    b, _, _ = clip(lead=0.0, tail=0.0, noise_db=-40, seed=2)
    j = m.join([a, b], SR)
    gap = int(SR * 0.12)
    assert len(j) == len(a) + gap + len(b)                             # 停顿长度和以前一样
    assert np.array_equal(j[:100], a[:100])                            # 整句开头留给最后的 fade
    assert abs(j[len(a) - 1]) < 1e-3 and abs(j[len(a) + gap]) < 1e-3   # 接缝两边落到 0
    assert np.all(j[len(a):len(a) + gap] == 0)
    assert np.array_equal(m.join([a], SR), a)                          # 只有一段：不动


def codec_tail(seed=6):
    """像模型真实输出：底噪 -45 → 讲话 → 讲话后只有 40 ms 衰减就收尾，再跟 70 ms 解码器补的近乎零（-99 dBFS）。"""
    rng = np.random.default_rng(seed)
    a, on, off = clip(lead=0.4, speech=(0.8, 0.6), tail=0.0, noise_db=-45, seed=seed)
    dec = (rng.normal(0, 10 ** (-50 / 20), int(SR * 0.04)) * np.linspace(1, 0.3, int(SR * 0.04))).astype(np.float32)
    zeros = rng.normal(0, 10 ** (-99 / 20), int(SR * 0.07)).astype(np.float32)
    return np.concatenate([a, dec, zeros]), on, off


def test_edges_short_tail_ends_at_silence_keeps_last_word(m):
    """讲话后面不到 100 ms：淡出落在有声部分的最后一个采样点上（后面的近零不留台阶），淡出缩短，最后一个字不被压低。"""
    a, on, off = codec_tail()
    out = m.edges(a, SR)
    assert out[0] == 0 and out[-1] == 0
    nz = np.flatnonzero(out)
    assert len(out) - 1 - nz[-1] < SR * 0.001                          # 尾巴没有一长串数字静音
    assert db(out[nz[-1] - int(SR * 0.005):nz[-1]]) < -60               # 最后 5 ms 已经淡到很低：没有台阶
    k = at(a, m.trim(a, SR))
    w = slice(off - k - int(SR * 0.05), off - k)                        # 最后一个字的最后 50 ms
    assert db(out[w]) > db(a[off - int(SR * 0.05):off]) - 0.5
    assert db(out[:int(SR * 0.005)]) < -60                              # 开头的底噪切掉并淡入


def test_join_seam_with_codec_zeros(m):
    """分段的末尾带着解码器补的近零：接缝的淡出作用在有声部分上，落到 0 的位置就是它的末尾。"""
    a, _, _ = codec_tail(seed=7)
    b, _, _ = codec_tail(seed=8)
    j = m.join([a, b], SR)
    assert len(j) == len(a) + int(SR * 0.12) + len(b)
    end = int(np.flatnonzero(np.abs(a) > 1e-4)[-1])
    assert j[end] == 0 and np.all(j[end:len(a)] == 0)
    assert db(j[end - int(SR * 0.005):end]) < -60


def test_speak_pipeline_edges(m):
    """speak() 里的顺序：join → edges（trim + 淡入淡出）；结果从 0 起、落回 0，头尾都远低于底噪。"""
    a, _, _ = clip(noise_db=-45)
    out = m.edges(m.join([a[:len(a) // 2], a[len(a) // 2:]], SR), SR)
    assert out[0] == 0 and out[-1] == 0
    assert db(out[:int(SR * 0.005)]) < -60 and db(out[-int(SR * 0.02):]) < -55


# ---- 流式（Streamer / onset / 固定增益）：0.4 s 一块喂进去，出来的音频要和批量路径 edges(join(...)) 一样
def streamed(m, pieces, gain=1.0):
    st, out, n = m.Streamer(SR, gain), b"", int(SR * 0.4)
    for p, a in enumerate(pieces):
        for i in range(0, len(a), n):
            out += st.feed(p, a[i:i + n])
    out += st.finish()
    return np.frombuffer(out, dtype="<i2").astype(np.float32) / 32767


@pytest.mark.parametrize("lead,tail", [(0.5, 0.6), (0.05, 0.1), (1.3, 0.3), (0.0, 0.0)])
def test_streamer_matches_batch_path(m, lead, tail):
    """开头底噪长短不一、结尾底噪比扣住的 HOLD 短：流式出来的和 edges() 逐样本一样（16 位量化误差内）。"""
    a, _, _ = clip(lead=lead, tail=tail)
    ref, got = m.edges(a, SR), streamed(m, [a])
    assert len(got) == len(ref), f"长度差 {(len(got) - len(ref)) / SR * 1000:+.0f} ms"
    assert np.abs(got - ref).max() < 2 / 32767


def test_streamer_joins_pieces_like_join(m):
    """长句分两段生成：段间淡出 / 120 ms 静音 / 淡入和 join 一样，整句头尾和 edges 一样。"""
    a1, _, _ = clip()
    a2, _, _ = clip(seed=1)
    ref, got = m.edges(m.join([a1, a2], SR), SR), streamed(m, [a1, a2])
    assert len(got) == len(ref) and np.abs(got - ref).max() < 2 / 32767


def test_onset_waits_for_speech(m):
    """开头 1.3 s 只有底噪：前几块找不到第一声（不能拿底噪尖峰当讲话），讲话到了以后切头点和 bounds 一致。"""
    a, on, _ = clip(lead=1.3)
    assert m.onset(a[:int(SR * 0.4)], SR) is None
    assert m.onset(a[:int(SR * 0.8)], SR) is None
    s, o = m.onset(a[:int(SR * 1.6)], SR)
    assert s == m.bounds(a, SR)[0] and o == on


def test_streamer_applies_fixed_gain(m):
    """增益是每句固定乘的数：流式出来的 = edges() × gain（限幅前）；ref_gain 由参考音频的讲话均方根决定。"""
    a, _, _ = clip(noise_db=-80)
    ref, got = m.edges(a, SR), streamed(m, [a], gain=0.5)
    assert np.abs(got - ref * 0.5).max() < 2 / 32767
    lv = m.level(a, SR)
    assert 0.15 < lv < 0.25 and abs(m.ref_gain(a, SR) - m.TARGET * m.GAIN_K / lv) < 1e-9
