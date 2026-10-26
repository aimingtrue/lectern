// 流式朗读的音频处理：WSOLA 变速不变调（WebAudio 没有 preservesPitch）和 wav 头。纯函数。
export function stretcher(sr, rate) {   // WSOLA：输入按 rate 倍速前进，输出帧 50% 重叠汉宁窗叠加；每帧在 ±8 ms 里找和上一帧自然延续最相似的接点
  const N = Math.round(sr * 0.025) & ~1, H = N / 2, D = Math.round(sr * 0.008);
  const win = new Float32Array(N); for (let i = 0; i < N; i++) win[i] = 0.5 - 0.5 * Math.cos(2 * Math.PI * i / N);
  let inp = new Float32Array(0), used = 0, k = 0, prev = -1, done = false;
  const ola = new Float32Array(N);
  function run() {
    const outs = [];
    for (;;) {
      const nom = Math.round(k * H * rate) - used;
      const need = Math.max(nom + N + D, prev >= 0 ? prev + H + N : 0);
      if (!done && need > inp.length) break;
      if (nom + N > inp.length) break;
      let best = nom;
      if (prev >= 0) {
        const t0 = prev + H, L = Math.min(N, inp.length - t0), lo = Math.max(0, nom - D), hi = Math.min(inp.length - N, nom + D);
        let bs = -Infinity;
        for (let p = lo; p <= hi; p += 2) {
          let s = 0, e = 1e-9;
          for (let i = 0; i < L; i += 2) { const c = inp[p + i]; s += inp[t0 + i] * c; e += c * c; }
          s /= Math.sqrt(e);
          if (s > bs) { bs = s; best = p; }
        }
      }
      for (let i = 0; i < N; i++) ola[i] += inp[best + i] * (prev < 0 && i < H ? 1 : win[i]);
      outs.push(ola.slice(0, H)); ola.copyWithin(0, H); ola.fill(0, H);
      prev = best; k++;
      const drop = Math.max(0, Math.min(nom, prev) - D - N);
      if (drop > 0) { inp = inp.subarray(drop); used += drop; prev -= drop; }
    }
    if (done) { outs.push(ola.slice(0, H)); ola.fill(0); }
    const out = new Float32Array(outs.reduce((n, x) => n + x.length, 0)); let o = 0;
    for (const x of outs) { out.set(x, o); o += x.length; }
    return out;
  }
  return {
    push(x) { if (rate === 1) return x; const b = new Float32Array(inp.length + x.length); b.set(inp); b.set(x, inp.length); inp = b; return run(); },
    end() { if (rate === 1) return new Float32Array(0); done = true; return run(); },
  };
}
export function wavHeader(sr, nbytes) {
  const b = new ArrayBuffer(44), d = new DataView(b), w = (o, t) => [...t].forEach((c, i) => d.setUint8(o + i, c.charCodeAt(0)));
  w(0, 'RIFF'); d.setUint32(4, 36 + nbytes, true); w(8, 'WAVEfmt '); d.setUint32(16, 16, true); d.setUint16(20, 1, true); d.setUint16(22, 1, true);
  d.setUint32(24, sr, true); d.setUint32(28, sr * 2, true); d.setUint16(32, 2, true); d.setUint16(34, 16, true); w(36, 'data'); d.setUint32(40, nbytes, true);
  return b;
}
