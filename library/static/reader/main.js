/* 英文书库 · 朗读页
   只做两件事：流畅地读下去，降低理解门槛。书的原版排版不管，只按纯文本段落排，每段左侧标一个小小的 p.N。
   左栏 = 书页段落：当前句高亮、跟着声音逐词点亮、难词的中文浮在词上方；右栏 = 译文与难点，读过的句子留着，可往回翻；E 打开讲解栏（生词 · 美式音标 · 释义 · 点词听，同样连续）。
   读法（B.mode，library/bookkind.py）：外文书如上；文言书只给文言句配白话译文、注文言字词；白话书是母语，不译不注，右栏整栏是导图。
   打开不自动Play，停在上次读到的那一句；Enter Play / Pause。每句读完留一段思考时间（↑/↓ 调），期间画面纹丝不动。 */
import {$, $$, esc, LS, api, toast, HAN, isZh, reEsc, tokensOf} from './util.js';
import {stretcher, wavHeader} from './stretch.js';

const B = window.BOOK;

const qs = new URL(location).searchParams;
// 设置只在 Aa 面板里：字号倍数 · 语速（滚轮）· 句间间隔（秒，+/− 键；0 = 不停顿）
const prefs = Object.assign({k: 1, rate: 0.95, think: 10, voice: 'smith', voiceZh: '', bed: true, tone: 'curve', gloss: 'more', cast: true}, LS.get('rd', {}));
// Narrator默认音色见 tts/voices.json；用户选过的音色记在本地存储里
// 中文句：汉字 ×3 ≥ 拉丁字母（和服务端 text.is_zh 一致）。每句按自己的语言选音色、逐字点亮
const ZH = B.lang === 'zh';
// 读法：en 外文书 · wy 文言书（文言句配白话译文）· zh 白话书（母语，不译不注）
const MODE = B.mode === 'zh' || B.mode === 'wy' ? B.mode : 'en';
// 语速、句间间隔按读法各记一份：外文书要停下来看译文；白话书是母语，默认快一档、停顿短
const PACE = {en: {rate: 0.95, think: 10}, wy: {rate: 1, think: 8}, zh: {rate: 1.15, think: 2}};
const RATE_MIN = 0.7, RATE_MAX = 1.5;
prefs.pace = Object.assign({}, prefs.pace);
if (!prefs.pace.en) prefs.pace.en = {rate: prefs.rate, think: prefs.think};   // 早先只有一份，是按英文书调的
Object.assign(prefs, PACE[MODE], prefs.pace[MODE]);
prefs.rate = Math.max(RATE_MIN, Math.min(RATE_MAX, Math.round((+prefs.rate || PACE[MODE].rate) * 100) / 100));
prefs.think = Math.max(0, Math.min(120, Math.round(+prefs.think) || 0));
const savePrefs = () => { prefs.pace[MODE] = {rate: prefs.rate, think: prefs.think}; LS.set('rd', prefs); };
const toc = (B.toc || []).filter(t => t[3] != null);   // [级别, 标题, 页, 段号]
let total = B.n_paras || 0;

// ------------------------------------------------------------------ 骨架
document.body.insertAdjacentHTML('beforeend', `
<div class="rd present">
  <section class="slide" id="slide">
    <div class="lx-left"><a class="lx-top" href="/book/${B.id}" title="Back to book (Esc)"><span class="lx-t">${esc(B.title)}</span><span id="lx-chap"></span></a></div>
    <div class="lx-tools" id="ttsbar">
      <span class="grp" id="tts-grp"><button class="ib" id="tts-play" title="Play / pause (Enter)">Play</button><select id="tts-voice" title="English voice" hidden></select><select id="tts-voice-zh" title="Chinese voice" hidden></select></span>
      <span class="grp">${B.pair ? `<a class="ib" id="b-pair" href="${esc(B.pair.href)}">${esc(B.pair.label)}</a>` : ''}<button class="ib" id="b-toc" title="Contents (T)">Contents</button><button class="ib" id="b-exp" title="Notes (E)">Notes</button><button class="ib" id="b-aa" title="Text size · speed · pause">Aa</button><button class="ib" id="b-all" title="Generate the whole book's audio up front and keep it on this machine, so replaying never regenerates">Whole book</button></span>
      <span class="grp">${B.mini ? '<button class="ib" id="b-mini" title="Continue from this sentence in Chrome on the Mac mini (M)">Send to mini</button>' : ''}<button class="ib" id="b-fs" title="Full screen (F)">Full screen</button><button class="ib" id="tts-stop" title="Exit (Esc)">退出</button></span>
    </div>
    <div class="sub" id="sub"><div id="sub-list" lang="${ZH ? 'zh-CN' : 'en'}"></div></div>
    <aside class="lx-side" id="lx-side"><div id="side-list"></div></aside>
    <div class="cast" id="cast" hidden><svg></svg><div class="cap"></div></div>
    <div class="msg" id="msg" hidden></div>
    <div class="status" id="status"><div class="st-main"><span id="st-who"></span><span id="st-pace"></span><a id="st-today" href="/shelf/stats" title="Reading time"></a><span id="tts-state"></span></div><div id="st-len"></div></div>
  </section>
  <aside class="explain" id="explain" hidden>
    <div class="ex-head"><b>Notes</b><span id="ex-tip">${ZH ? 'follows the reading · scroll up for earlier text' : 'follows the reading · scroll up for earlier text · tap a word to hear it'}</span><button class="ib" id="ex-close" title="Hide (E)">收起</button></div>
    <div id="ex-list"><p class="ex-empty">${MODE === 'wy' ? 'The current classical-Chinese line appears here in modern Chinese, with background and word notes. Modern lines are not annotated.' : 'The current sentence appears here translated, with notes on what is hard and the new words in US IPA.'}</p></div>
  </aside>
  <aside class="drawer" id="toc"><h4>Contents</h4><div id="toc-list"></div></aside>
  <div class="menu" id="aa" hidden></div>
  <div class="menu" id="all" hidden></div>
</div>
<div class="toast"></div><div class="ctip" id="ctip"></div>`);
const sub = $('#sub'), list = $('#sub-list'), side = $('#lx-side'), sideList = $('#side-list'), exList = $('#ex-list');
$('.rd').classList.add('mode-' + MODE);
if (MODE === 'zh') $('#b-exp').hidden = true;   // 白话书没有译文和讲解：右栏只放导图

// ------------------------------------------------------------------ 段落数据：/text 分窗载入，P = 段号 → {n, page, kind, text, sents, speech, speech_ok}
const P = new Map();
let stale = false;                        // 页面脚本比服务端的旧：读完当前句（或没在读时马上）从这一句重新打开，在读的接着读
let NARR = null;                          // Narrator（第一人称讲故事的那个Person）的称呼，/text 给；第三人称的书是 null
const NOF = new Map();                    // 句子 → 段号（请求音频时带上，服务端按这一段的对白归属配音）
let lo = 0, hi = 0;                       // 已载入 [lo, hi)，始终连续
let lq = Promise.resolve();               // 载入串行执行，lo/hi 不会被并发改乱
const queue = f => { const r = lq.then(f); lq = r.catch(() => {}); return r; };
async function fetchParas(from, count) {
  const j = await api(`/api/books/${B.id}/text?from=${from}&count=${count}`);
  if (j.total != null) total = j.total;
  if (j.narrator !== undefined && j.narrator !== NARR) { NARR = j.narrator; if (cast && cast.kind === 'people') castPaint(); }
  if (j.ver && B.ver && j.ver !== B.ver) stale = true;   // 服务端更新了页面脚本（长期开着的标签页，如 mini 上的）：到句子间隔就重载
  return j.paras || [];
}
const around = n => queue(async () => {   // 跳得远：清空重来，载入 n 附近 40 段
  const from = Math.max(0, n - 10), ps = await fetchParas(from, 40);
  P.clear(); ps.forEach(p => P.set(p.n, p)); lo = from; hi = ps.length ? ps[ps.length - 1].n + 1 : from;
});
const pend = {};
function more(dir) {                      // 往后 / 往前再载 40 段（只是数据，不碰画面）
  if (dir > 0 ? hi >= total : lo <= 0) return Promise.resolve();
  return pend[dir] || (pend[dir] = queue(async () => {
    if (dir > 0) { const ps = await fetchParas(hi, 40); ps.forEach(p => P.set(p.n, p)); if (ps.length) hi = ps[ps.length - 1].n + 1; else total = hi; }
    else { const f = Math.max(0, lo - 40), ps = await fetchParas(f, lo - f); ps.forEach(p => P.set(p.n, p)); lo = f; }
  }).finally(() => { pend[dir] = null; }));
}
function feed(n) {                        // 读到哪儿就往哪儿多载一些；离得太远的数据丢掉
  if (hi - n < 30) more(1).catch(() => {});
  if (n - lo < 10) more(-1).catch(() => {});
  queue(() => { while (hi - lo > 600 && n - lo > 300) P.delete(lo++); while (hi - lo > 600 && hi - n > 300) P.delete(--hi); });
}
async function loadPara(n) {
  if (P.has(n) || n < 0 || n >= total) return P.get(n);
  if (n === hi) await more(1); else if (n === lo - 1) await more(-1); else await around(n);
  return P.get(n);
}
// 句子 = text.slice(a, b) 原样，作为 TTS / 逐词时间 / 译文的键，不再另做规整
const sentText = (p, i) => p.text.slice(p.sents[i][0], p.sents[i][1]);
const speakable = (p, i) => /[A-Za-z0-9\u3400-\u9fff]/.test(sentText(p, i));
const okPos = pos => { const p = P.get(pos.n); return !!p && pos.i >= 0 && pos.i < p.sents.length && speakable(p, pos.i); };
async function move(pos, d) {             // 往后 / 往前找下一句能读的；到头返回 null
  let n = pos.n, i = pos.i + d;
  for (let k = 0; k < 500; k++) {
    const p = await loadPara(n); if (!p) return null;
    if (i === null) i = d > 0 ? 0 : p.sents.length - 1;
    for (; i >= 0 && i < p.sents.length; i += d) if (speakable(p, i)) return {n, i};
    n += d; i = null;
  }
  return null;
}
const fix = async pos => okPos(pos) ? pos : move({n: pos.n, i: -1}, 1);
function ahead(k) {                       // 当前句之后的 k 句（只看已载入的数据）
  const out = []; let n = cur.n, i = cur.i + 1;
  while (out.length < k) {
    const p = P.get(n); if (!p) break;
    if (i < p.sents.length) { if (speakable(p, i)) out.push(sentText(p, i)); i++; } else { n++; i = 0; }
  }
  return out;
}

// ------------------------------------------------------------------ 左栏：书页段落（句子连成整段；当前段清楚，前后段变淡）
let dlo = 0, dhi = 0;                     // 左栏里的段落 [dlo, dhi)
const paraEl = n => list.querySelector(`.sp[data-n="${n}"]`);
const curLine = () => { const p = paraEl(cur.n); return p && p.querySelector(`.ln[data-i="${cur.i}"]`); };
function paraNode(p) {
  const el = document.createElement('div');
  const zh = isZh(p.text);                 // 中文段：句子之间不加空格，按中文排版
  el.className = 'sp' + (p.kind === 'h' ? ' head' : '') + (zh ? ' zh' : ''); el.dataset.n = p.n;
  if (zh !== ZH) el.lang = zh ? 'zh-CN' : 'en';
  if (zh ? !ZH : ZH && (p.text.match(/[A-Za-z]/g) || []).length >= 20) mixed();   // 真有一段另一种语言（不是页码、数字）
  el.innerHTML = `<span class="pg">p.${p.page + 1}</span>` + (p.sents.length
    ? p.sents.map((s, i) => `<span class="ln" data-i="${i}"><span class="en">${esc(sentText(p, i))}</span></span>`).join(zh ? '' : ' ') : esc(p.text));
  $$('.ln', el).forEach(ln => { ln._t = sentText(p, +ln.dataset.i); NOF.set(ln._t, p.n); });
  if (NOF.size > 4000) NOF.clear();
  decorate(el, p);
  return el;
}
// 上方增删段落时，按一个不动的段落量出位移差，同一帧里补回去：屏幕上的字一动不动
function keepPlace(fn) {
  const a = paraEl(cur.n) || list.lastChild, t = a ? a.getBoundingClientRect().top : 0;
  fn();
  if (a && a.isConnected) { const d = a.getBoundingClientRect().top - t; if (d) { y += d; paint(); } }
  wake();
}
// 左栏只留当前段前 15 后 30 段（超出前 40 后 60 才删）；只在句子开始时调用，思考停顿里从不改 DOM
function syncDOM() {
  const n = cur.n;
  const reset = () => { list.textContent = ''; dlo = dhi = n; snap = true; logClear(); };
  if (n < dlo - 20 || n >= dhi + 20) reset();
  keepPlace(() => {
    while (dlo > Math.max(0, n - 15) && P.has(dlo - 1)) list.prepend(paraNode(P.get(--dlo)));
    while (dlo < n - 40) { list.firstChild.remove(); dlo++; }
  });
  while (dhi < n + 30 && P.has(dhi)) list.append(paraNode(P.get(dhi++)));
  while (dhi > n + 60) { list.lastChild.remove(); dhi--; }
  if (!paraEl(n) && P.has(n)) { reset(); syncDOM(); }
}

// ------------------------------------------------------------------ 平滑跟随
// 整列用 transform 平移，不用 scrollTop：scrollTop 在部分浏览器（Safari 等）会取整到整像素，
// 每帧一点点挪会一顿一顿；transform 是合成层平移，支持小数像素，又不触发 scroll 事件和重排。
// 目标 = 正在读的那个词所在行放在 38% 高度（没有逐词时间时用当前句第一行）；
// 每帧按指数平滑逼近（时间常数 0.45 s，与帧率无关），字一行一行地跟着声音滑上去；停稳后取整到设备像素，字不发虚。
let y = 0, shown = NaN, follow = true, snap = true, last = 0;
// 跟随比声音提前 LEAD 秒：朝「快要读到的那一行」滑，一句读完时滚动早已停稳，思考停顿里画面不再动
const LEAD = 1.8;
const paint = () => { if (y !== shown) { list.style.transform = `translate3d(0,${-y}px,0)`; shown = y; } };
function target() {
  const ln = curLine(); if (!ln) return null;
  const w = kara && kara.line === ln && kara.ws && kara.lead >= 0 ? kara.ws[kara.lead] : null;
  const r = w && w.isConnected ? w.getBoundingClientRect() : ln.getClientRects()[0]; if (!r) return null;
  const t = r.top - list.getBoundingClientRect().top - sub.clientHeight * 0.38, d = devicePixelRatio || 1;
  return Math.round(t * d) / d;
}
// 右栏译文同样平移：当前句译文的顶边放在 38% 高度（窄屏放在右栏顶部附近），和左栏正在读的那一行对齐
let sy = 0, sShown = NaN, sFollow = true, sSnap = true;
const wide = matchMedia('(min-width:901px)');
const paintSide = () => { if (sy !== sShown) { sideList.style.transform = `translate3d(0,${-sy}px,0)`; sShown = sy; } };
function sideTarget() {
  const e = sideList.querySelector('.si.cur'); if (!e) return null;
  const t = e.offsetTop - side.clientHeight * (wide.matches ? 0.38 : 0.06), d = devicePixelRatio || 1;
  return Math.round(t * d) / d;
}
const ease = (v, tg, dt) => { v += (tg - v) * (1 - Math.exp(-dt / 0.45)); return Math.abs(tg - v) < 0.3 ? tg : v; };
// 只在需要时跑：在读、或两栏还没停稳；停稳且没在读就歇着（思考停顿、Pause时零开销）。
// 会让目标位置变的事（换句、译文上屏、版面尺寸变、开始Play）都调 wake() 叫醒
let rafOn = false;
function wake() { if (!rafOn) { rafOn = true; last = 0; requestAnimationFrame(frame); } }
function frame(t) {
  const dt = last ? Math.min(0.1, (t - last) / 1000) : 0; last = t;
  const talking = phase === 'speak' && !audio.paused;
  if (phase === 'speak') { paintKara(); toneNow(); }
  let still = true;
  if (follow) {
    const tg = target();
    if (tg != null) { if (snap) { y = tg; snap = false; } else if (y !== tg) y = ease(y, tg, dt); if (y !== tg) still = false; }
  }
  if (sFollow) {
    const tg = sideTarget();
    if (tg != null) { if (sSnap) { sy = tg; sSnap = false; } else if (sy !== tg) sy = ease(sy, tg, dt); if (sy !== tg) still = false; }
  }
  paint(); paintSide();
  if (talking || !still) requestAnimationFrame(frame); else rafOn = false;
}
wake();
new ResizeObserver(wake).observe(list);
const ro = new ResizeObserver(wake);   // 右栏、版面尺寸变（字号、讲解栏开合、窗口大小）
[sideList, sub, side].forEach(e => ro.observe(e));

// 手动滚（触摸 / PageUp·PageDown；滚轮只滚右栏）：Pause跟随，下一句开始时恢复
function manual(d) {
  follow = false;
  y = Math.max(-sub.clientHeight * 0.4, Math.min(list.offsetHeight - sub.clientHeight * 0.4, y + d)); paint();
}
function sideManual(d) {
  sFollow = false;
  const h = side.clientHeight;
  sy = Math.max(-h * 0.4, Math.min(sideList.offsetHeight - h * 0.4, sy + d)); paintSide();
}
const slide = $('#slide');
// 滚轮：右栏上滚译文；其余地方调语速，往上快 0.05×、往下慢 0.05×。攒够约一格（100px）才算一步，触控板一划不会连跳
let wheelAcc = 0;
slide.addEventListener('wheel', ev => {
  if (sub.hidden) return; ev.preventDefault();
  const d = ev.deltaY * (ev.deltaMode === 1 ? 40 : ev.deltaMode === 2 ? sub.clientHeight : 1);
  if (ev.target.closest('#lx-side')) return sideManual(d);
  if (Math.sign(d) !== Math.sign(wheelAcc)) wheelAcc = 0;
  wheelAcc += d;
  const n = Math.trunc(wheelAcc / 100);
  if (n) { wheelAcc -= n * 100; setRate(-n * 0.05); }
}, {passive: false});
let ty = null, tside = false;
slide.addEventListener('touchstart', ev => { ty = ev.touches[0].clientY; tside = !!ev.target.closest('#lx-side'); }, {passive: true});
slide.addEventListener('touchmove', ev => { if (ty == null || sub.hidden) return; ev.preventDefault(); const t = ev.touches[0].clientY; (tside ? sideManual : manual)(ty - t); ty = t; }, {passive: false});
slide.addEventListener('touchend', () => { ty = null; });

// ------------------------------------------------------------------ 注释：整句译文里与词（或短语）对应的那一段，英文和译文里各标一次
function spans(text, gl, pat) {
  const hits = [];
  gl.forEach(g => {
    const m = pat(g).exec(text); if (!m) return;
    const a = m.index, b = a + m[0].length;
    if (!hits.some(h => a < h.b && b > h.a)) hits.push({a, b, g});
  });
  return hits.sort((x, y) => x.a - y.a);
}
function weave(text, hits, wrap, seg = (a, b) => esc(text.slice(a, b))) {
  let out = '', p = 0;
  hits.forEach(h => { out += seg(p, h.a) + wrap(seg(h.a, h.b), h.g); p = h.b; });
  return out + seg(p, text.length);
}
// 逐词高亮：句子拆成词，每个词一个 span（data-k = 第几个词），和对齐结果一一对应
// 词：英文按单词，汉字一个字一个（中文对齐给的就是逐字时间）
function wordify(text, toks) {
  return (a, b) => {
    let out = '', p = a;
    toks.forEach((t, k) => { if (t.a >= a && t.b <= b) { out += esc(text.slice(p, t.a)) + `<span class="w" data-k="${k}">${esc(text.slice(t.a, t.b))}</span>`; p = t.b; } });
    return out + esc(text.slice(p, b));
  };
}
// 中文注释绝对定位浮在词上方，不占行内空间，不会让正文重新折行
const enHTML = (text, gl) => weave(text, spans(text, gl, g => new RegExp(`(?<![A-Za-z])${reEsc(g.en)}(?![A-Za-z])`, 'i')),
  (t, g) => `<span class="gl">${t}<i class="gt">${esc(g.zh)}</i></span>`, wordify(text, tokensOf(text)));
const zhHTML = (zh, gl) => weave(zh, spans(zh, gl, g => new RegExp(reEsc(g.zh))), t => `<span class="zg">${t}</span>`);
function unannotate(ln) { delete ln.dataset.ann; ln.classList.remove('open', 'cur'); ln.querySelector('.en').textContent = ln._t; }
function annotate(ln, d) {
  if (!ln.classList.contains('cur') || ln.dataset.ann) return;
  ln.dataset.ann = '1';
  const gl = d.gloss || [];
  ln.querySelector('.en').innerHTML = enHTML(ln._t, gl);
  if (kara && kara.line === ln) { kara.ws = null; paintKara(); }   // 同一任务里补上点亮状态，新 span 不会闪一下
  const e = logCur(); if (e) fillSide(e, d);
  void ln.offsetHeight; ln.classList.add('open'); wake();
}

// ------------------------------------------------------------------ 右栏 · 讲解栏：读过的句子按书中顺序留着，可以往回翻
// LOG = [{n, i, text, si = 右栏译文, card = 讲解卡片}]，按 (n, i) 排序；两栏都由它画。只在一句开始时增删，思考停顿里不动
let LOG = [];
const logCur = () => LOG.find(e => e.n === cur.n && e.i === cur.i);
function logClear() { LOG = []; sideList.textContent = ''; exList.textContent = ''; sSnap = true; }
function fillSide(e, d) {
  if (e.sf || !d) return;
  if (d.error) { e.si.innerHTML = '<div class="zh k">—</div>'; return; }
  e.sf = true;
  if (d.skip) { e.si.innerHTML = ''; return; }   // 白话句（母语）：不译，右栏留空
  e.si.innerHTML = `<div class="zh">${zhHTML(d.zh || '', d.gloss || [])}</div>`;   // 右栏只留整句译文；难点讲解在 E 讲解栏
}
function fillCard(e, d) {
  if (e.cf || !d) return;
  const zh = e.card.querySelector('.zh');
  if (d.error) { zh.innerHTML = `<span class="k">${esc(d.error)} (needs Ollama running locally)</span>`; return; }
  e.cf = true;
  if (d.skip) { zh.outerHTML = '<div class="zh k">Modern Chinese — no notes needed.</div>'; return; }
  if (d.lang === 'wy') {                  // 文言句：白话译文 · 背景讲解（和译文一起出）· 字词释义
    const gl = (d.gloss || []).map(g => `<li><span class="wd zw">${esc(g.en)}</span><span class="m">${esc(g.zh)}</span></li>`).join('');
    zh.outerHTML = `<div class="zh">${esc(d.zh || '')}</div>` + ((d.notes || []).length ? `<ul class="notes">${d.notes.map(n => `<li>${esc(n)}</li>`).join('')}</ul>` : '');
    if (gl) e.card.insertAdjacentHTML('beforeend', `<ul class="words">${gl}</ul>`);
    return;
  }
  const words = (d.words || []).map(w => `<li><button class="wd" data-w="${esc(w.word)}" title="Hear it">${esc(w.word)}</button>${w.ipa ? `<span class="ipa">US ${esc(w.ipa)}</span>` : ''}<span class="m">${esc(w.zh)}</span></li>`).join('');
  zh.outerHTML = `<div class="zh">${esc(d.zh || '')}</div><ul class="notes" hidden></ul>`;
  if (words) e.card.insertAdjacentHTML('beforeend', `<ul class="words">${words}</ul>`);
  if (!$('#explain').hidden) fillNotes(e);
}
// 难点讲解单独取（/api/notes）：讲解栏开着才要，右栏和正文不等它
const NOTES = new Map();
function fillNotes(e) {
  const ul = e.card.querySelector('ul.notes[hidden]'); if (!ul || e.nf) return;
  e.nf = true;
  if (!NOTES.has(e.text)) {
    NOTES.set(e.text, api('/api/notes', {method: 'POST', json: {text: e.text}}).then(j => j.notes || []).catch(() => { NOTES.delete(e.text); return []; }));
    while (NOTES.size > 80) NOTES.delete(NOTES.keys().next().value);
  }
  NOTES.get(e.text).then(ns => { if (!ns.length) { e.nf = false; return; } ul.innerHTML = ns.map(n => `<li>${esc(n)}</li>`).join(''); ul.hidden = false; });
}
// 当前项上方增删内容时，按一个不动的项量出位移差补回去：两栏里看得见的字都不跳
function keepLog(fn) {
  const a = LOG.find(e => e.si.classList.contains('cur')) || LOG[0];
  const s = a ? a.si.offsetTop : 0, c = a ? a.card.offsetTop : 0;
  fn();
  if (a && a.si.isConnected) { sy += a.si.offsetTop - s; exList.scrollTop += a.card.offsetTop - c; }
  wake();
}
const scrollCard = (e, smooth) => { if (!$('#explain').hidden) exList.scrollTo({top: Math.max(0, e.card.offsetTop - 24), behavior: smooth ? 'smooth' : 'auto'}); };
function logEnter(text) {                 // 一句开始：确保有这句的记录并标成当前；之前没到的结果补上；离得太远的删掉
  let e = logCur();
  keepLog(() => {
    if (!e) {
      e = {n: cur.n, i: cur.i, text, si: document.createElement('div'), card: document.createElement('div')};
      e.si.className = 'si'; e.si.innerHTML = '<div class="zh k">…</div>';
      e.card.className = 'excard'; e.card.innerHTML = `<div class="en" lang="${isZh(text) ? 'zh-CN' : 'en'}">${esc(text)}</div><div class="zh"><span class="k">The local model is working…</span></div>`;
      const k = LOG.findIndex(x => x.n > e.n || (x.n === e.n && x.i > e.i));
      if (k < 0) { LOG.push(e); sideList.append(e.si); exList.append(e.card); }
      else { LOG[k].si.before(e.si); LOG[k].card.before(e.card); LOG.splice(k, 0, e); }
      const empty = $('.ex-empty', exList); if (empty) empty.remove();
    }
    LOG.forEach(x => { if (x !== e) { const d = TR.get(x.text); fillSide(x, d); fillCard(x, d); } });
    while (LOG.length > 80) { const k = LOG.indexOf(e), x = k > LOG.length - 1 - k ? LOG.shift() : LOG.pop(); x.si.remove(); x.card.remove(); }
  });
  LOG.forEach(x => { x.si.classList.toggle('cur', x === e); x.card.classList.toggle('cur', x === e); });
  scrollCard(e, true);
  return e;
}
// 换了生词档位（Aa）：当前句按新档重标，讲解卡片的生词表也换掉；读过的句子不动，下次经过时再按新档取
function reglossCur() {
  EXP.clear(); TR.clear();
  const ln = curLine(), e = logCur(); if (!ln || !e) return;
  unannotate(ln); ln.classList.add('cur'); ln.querySelector('.en').innerHTML = enHTML(ln._t, []);
  if (kara && kara.line === ln) { kara.ws = null; paintKara(); }
  e.card.querySelectorAll('ul.words,ul.notes').forEach(u => u.remove()); e.cf = e.nf = false;
  explainOf(ln._t).then(d => { if (curLine() === ln) { annotate(ln, d); fillCard(e, d); } });   // 用户主动换的，停顿里也直接上屏
}
// 当前句的译文 / 讲解：已到手就直接上；没到的等它，思考停顿里到的先不上屏（停顿被打断或下一句开始时再补）
function fillCur(ln, e) {
  const show = d => { annotate(ln, d); fillCard(e, d); }, d = TR.get(ln._t);
  if (d) return show(d);
  explainOf(ln._t).then(d => curLine() === ln && phase !== 'think' && show(d));
}

// ------------------------------------------------------------------ 朗读：多个音色（本机声音克隆，tts/voices.json），默认音色见 tts/voices.json
const player = new Audio(); player.preservesPitch = true; player.preload = 'auto';
const once = new Audio(); once.preservesPitch = true;
let audio = player;             // 正在读这一句的Play器：player（文件），或流式的那一个（见 openStream）
// 背景音：有配乐的音色（tts/voices.json 里 bed=true，tools/add_bed.py 做的）朗读时在底下循环；
// 读句子时压低，思考停顿里抬起一点，Pause就淡出。Aa 面板里可以关
const bed = new Audio(); bed.loop = true; bed.volume = 0;
const BED_SPEAK = 0.45, BED_THINK = 0.8;
let bedFade = 0;
function bedTo(vol) {
  clearInterval(bedFade);
  if (vol > 0 && bed.paused) bed.play().catch(() => {});
  bedFade = setInterval(() => {
    const d = vol - bed.volume, s = Math.sign(d) * Math.min(Math.abs(d), 0.04);
    bed.volume = Math.max(0, Math.min(1, bed.volume + s));
    if (Math.abs(vol - bed.volume) < 0.005) { bed.volume = vol; clearInterval(bedFade); if (vol === 0) bed.pause(); }
  }, 40);
}
function bedSync() {                      // 跟着Play状态和当前句的音色走；每 250 ms 看一次
  const ln = curLine(), l = ln && isZh(ln._t) ? 'zh' : 'en';
  const v = (VOICES[l] || []).find(x => x.id === prefs[vkey(l)]);
  const want = playing && prefs.bed && v && v.bed ? (phase === 'think' || phase === 'after' ? BED_THINK : BED_SPEAK) : 0;
  const src = v && v.bed ? `/api/voice/${v.id}/bed.m4a` : '';
  if (src && !bed.src.endsWith(src) && want > 0) { bed.src = src; bed.volume = 0; }
  if (Math.abs(want - bed.volume) > 0.01 || (want > 0 && bed.paused)) bedTo(want);
}
setInterval(bedSync, 250);
let ttsOK = false;
const TTSV = {en: '', zh: ''};  // 当前英文 / 中文音色的指纹：换了音色 / 参考音频 / 模型，缓存自动作废
const VOICES = {en: [], zh: []};  // 装好、可选的音色，按语言分
const LANGS = new Set([B.lang === 'zh' ? 'zh' : 'en']);   // 这本书里出现过的语言（对照书两种都有）：朗读栏显示对应的音色选择
const vkey = l => l === 'zh' ? 'voiceZh' : 'voice';
// s = 这一段对白归属（谁说哪几句、什么音色）的指纹：归属做好或重排角色后地址跟着变，浏览器里归属前缓存的纯Narrator音频不会再被拿来用
const speechSig = n => { const p = P.get(n); if (!p || !p.speech || !p.speech.length) return '0'; let h = 5381; for (const c of JSON.stringify(p.speech)) h = (h * 33 + c.charCodeAt(0)) >>> 0; return h.toString(36); };
const voiceOf = text => { const l = isZh(text) ? 'zh' : 'en', n = NOF.has(text) ? NOF.get(text) : -1; return '&voice=' + prefs[vkey(l)] + '&v=' + TTSV[l] + '&b=' + B.id + '&n=' + n + '&s=' + speechSig(n); };
const AUD = new Map();          // 句子 → Promise<blob URL>
function remember(text, pr) {
  AUD.set(text, pr);
  while (AUD.size > 60) { const [k, v] = AUD.entries().next().value; AUD.delete(k); v.then(u => URL.revokeObjectURL(u)).catch(() => {}); }
}
function fetchAudio(text) {
  if (!AUD.has(text)) {
    const pr = fetch('/api/tts?t=' + encodeURIComponent(text) + voiceOf(text)).then(async r => {
      if (!r.ok) { const j = await r.json().catch(() => ({})); throw new Error(j.detail || ('HTTP ' + r.status)); }
      return URL.createObjectURL(await r.blob());
    });
    pr.catch(() => AUD.delete(text));
    remember(text, pr);
  }
  return AUD.get(text);
}
// ---- 流式：没预取过的句子（首次打开新章、跳远）边生成边播。/api/tts/stream 有缓存回文件（X-Tts: file），没有就 wav 头 + 逐块 PCM
// （X-Tts: stream）：到一块 WebAudio 排一块。变速不变调靠 WSOLA（WebAudio 没有 preservesPitch）；本句的语速开始时定下，中途改语速下一句生效。
// 读完把收到的音频存成 blob（AUD），之后重听、逐词对齐、Tone都走文件。
let actx = null, noStream = '';
const canStream = () => !!(window.AudioContext || window.webkitAudioContext);
// 返回 {url}（服务端有缓存）或一个Play器对象：paused / currentTime（原音频时间轴，秒）/ duration（结束前按字数估）/ play / pause / stop /
// onended / done（整句收完）/ onfail（一块都没收到就断了：调用方改走文件路径）
function openStream(text) {
  const ctx = actx || (actx = new (window.AudioContext || window.webkitAudioContext)()), ctrl = new AbortController(), rate = prefs.rate;
  return fetch('/api/tts/stream?t=' + encodeURIComponent(text) + voiceOf(text), {signal: ctrl.signal}).then(async r => {
    if (!r.ok) { const j = await r.json().catch(() => ({})); throw new Error(j.detail || ('HTTP ' + r.status)); }
    if (r.headers.get('x-tts') !== 'stream') { const url = URL.createObjectURL(await r.blob()); remember(text, Promise.resolve(url)); return {url}; }
    const s = {paused: true, ended: false, onended: null, onfail: null, estDur: 0, sr: 24000, got: 0, start: -1, at: 0, gap: 0, over: 0, fin: false, srcs: [], pcm: [],
      get currentTime() { return s.start < 0 ? 0 : Math.max(0, Math.min(s.got / s.sr, (ctx.currentTime - s.start - s.gap) * rate)); },
      get duration() { return s.fin ? s.got / s.sr : s.estDur; },
      play() { s.paused = false; wake(); return ctx.state === 'running' ? Promise.resolve() : ctx.resume(); },
      pause() { s.paused = true; ctx.suspend(); },
      stop() { ctrl.abort(); s.onended = s.onfail = null; s.paused = true; s.srcs.forEach(n => { try { n.stop(); } catch (e) {} }); if (ctx.state === 'suspended') ctx.resume(); }};
    const finish = () => { if (!s.ended) { s.ended = true; if (s.onended) s.onended(); } };
    const sched = x => {                                  // 一块音频接在上一块后面；来晚了就从现在起（gap 记下来，currentTime 扣掉）
      if (!x.length) return;
      const b = ctx.createBuffer(1, x.length, s.sr); b.copyToChannel(x, 0);
      const n = ctx.createBufferSource(); n.buffer = b; n.connect(ctx.destination);
      const at = Math.max(s.at, ctx.currentTime + 0.02);
      if (s.start < 0) s.start = at; else if (at > s.at) s.gap += at - s.at;
      n.start(at); s.at = at + b.duration; s.srcs.push(n);
      n.onended = () => { s.over++; if (s.fin && s.over === s.srcs.length) finish(); };
    };
    s.done = (async () => {
      const rd = r.body.getReader(); let carry = new Uint8Array(0), st = null;
      for (;;) {
        const {value, done} = await rd.read();
        if (done) break;
        let buf = new Uint8Array(carry.length + value.length); buf.set(carry); buf.set(value, carry.length);
        if (!st) {
          if (buf.length < 44) { carry = buf; continue; }
          s.sr = new DataView(buf.buffer).getUint32(24, true); st = stretcher(s.sr, rate); buf = buf.subarray(44);
        }
        const n = buf.length & ~1, i16 = new Int16Array(buf.buffer.slice(buf.byteOffset, buf.byteOffset + n)); carry = buf.slice(n);
        s.pcm.push(i16); s.got += i16.length;
        const f = new Float32Array(i16.length); for (let i = 0; i < i16.length; i++) f[i] = i16[i] / 32768;
        sched(st.push(f));
      }
      if (st) sched(st.end());
      s.fin = true;
      if (s.over === s.srcs.length) finish();
      remember(text, Promise.resolve(URL.createObjectURL(new Blob([wavHeader(s.sr, s.got * 2), ...s.pcm], {type: 'audio/wav'}))));
    })().catch(e => {
      if (ctrl.signal.aborted) return;
      s.fin = true;
      if (!s.srcs.length) { if (s.onfail) s.onfail(e); }       // 一块都没播：改走文件路径（那边会给出正常的错误提示）
      else if (s.over === s.srcs.length) finish();             // 播到一半断了：这句到此为止，照常进入停顿
      throw e;
    });
    s.done.catch(() => {});
    return s;
  });
}
function dropStream() { if (audio !== player) { audio.stop(); audio = player; } }
const WRD = new Map();          // 句子 → Promise<[[词, 起, 止], …]>
function fetchWords(text) {
  if (!WRD.has(text)) {
    WRD.set(text, api('/api/tts/words?t=' + encodeURIComponent(text) + voiceOf(text)).then(j => j.words || []).catch(() => []));
    while (WRD.size > 120) WRD.delete(WRD.keys().next().value);
  }
  return WRD.get(text);
}
// 打开、跳句、换音色后、按Play之前：先把当前句和后两句的音频（预生成过的直接回文件）与逐词时间取到手，按回车立刻出声
function warmAudio() {
  const g = gen;
  (ttsOK ? Promise.resolve(true) : ttsReady(true)).then(ok => {
    if (!ok || g !== gen || playing) return;
    [curLine()._t, ...ahead(2)].forEach(t => fetchAudio(t).then(() => fetchWords(t)).catch(() => {}));
  });
}
const EXP = new Map(), TR = new Map();   // 句子 → Promise<{zh, notes, gloss, words}>；TR = 已到手的，可同步取
const SKIP = {zh: '', gloss: [], notes: [], words: [], lang: 'zh', skip: true};   // 白话句：不译不注（和服务端 narrate.SKIP 一样）
function explainOf(text) {
  if (MODE === 'zh' && isZh(text)) {      // 白话书的中文句：不用问服务端
    TR.set(text, SKIP); while (TR.size > 80) TR.delete(TR.keys().next().value);
    return Promise.resolve(SKIP);
  }
  if (!EXP.has(text)) {
    const pr = api('/api/translate', {method: 'POST', json: {text, book: B.id, level: prefs.gloss}}).catch(e => ({error: e.message}));
    pr.then(d => { if (d.error) EXP.delete(text); else { TR.set(text, d); while (TR.size > 80) TR.delete(TR.keys().next().value); } });
    EXP.set(text, pr);
    while (EXP.size > 80) EXP.delete(EXP.keys().next().value);
  }
  return EXP.get(text);
}
async function ttsReady(quiet) {
  let h; try { h = await api('/api/tts/status'); } catch (e) { h = {down: true}; }
  if (h.ok) {
    let changed = false;
    for (const l of ['en', 'zh']) {
      VOICES[l] = (h.voices || []).filter(v => v.lang === l);
      const k = vkey(l), v = VOICES[l].find(x => x.id === prefs[k]) || VOICES[l].find(x => x.id === h.default) || VOICES[l][0];
      if (v) { prefs[k] = v.id; if (v.vid !== TTSV[l]) { TTSV[l] = v.vid; changed = true; } }
    }
    if (changed) dropAudio();
    CATS = h.cats || []; voiceMenu();
    if (list.firstChild) showWho();       // 音色名到手了：左下角补上「· 谁的声音」
    return (ttsOK = true);
  }
  if (!quiet) toast(h.down ? 'The speech service is not running. Start it with: bash tools/setup_tts.sh' : (h.error ? 'Could not load the voice: ' + h.error : 'The speech model is still loading — try again in a few seconds'));
  return (ttsOK = false);
}
function dropAudio() { AUD.forEach(v => v.then(u => URL.revokeObjectURL(u)).catch(() => {})); AUD.clear(); WRD.clear(); }
// 音色选择：英文、中文各一个，按行业分组（tts/voices.json 的分类），只列装好的；只显示这本书用得到的语言
let CATS = [];
function voiceMenu() {
  const names = Object.fromEntries(CATS.map(c => [c.id, c.name]));
  for (const l of ['en', 'zh']) {
    const sel = $(l === 'zh' ? '#tts-voice-zh' : '#tts-voice'), vs = VOICES[l], k = vkey(l);
    const groups = [...new Set(vs.map(v => v.cat))].sort((a, b) => CATS.findIndex(c => c.id === a) - CATS.findIndex(c => c.id === b));
    sel.innerHTML = groups.map(g => `<optgroup label="${esc(names[g] || g)}">${vs.filter(v => v.cat === g).map(v =>
      `<option value="${esc(v.id)}"${v.id === prefs[k] ? ' selected' : ''}>${esc(v.name)}</option>`).join('')}</optgroup>`).join('');
    sel.hidden = !LANGS.has(l) || vs.length < 2;
  }
}
function mixed() {                        // 段落里出现了另一种语言：两个音色选择都显示
  if (LANGS.size < 2) { LANGS.add('en'); LANGS.add('zh'); voiceMenu(); }
}
function setVoice(id, l) {                // 换音色：当前句用新音色从头读（在读就接着读，没在读就备好音频）
  const v = VOICES[l].find(x => x.id === id); if (!v || v.vid === TTSV[l]) return;
  prefs[vkey(l)] = id; savePrefs(); TTSV[l] = v.vid; dropAudio(); prepKey = '';
  toast((l === 'zh' ? 'Chinese voice: ' : 'Voice: ') + v.name);
  if (!list.firstChild) return;
  showWho();
  begin(cur, playing);
}
$('#tts-voice').onchange = ev => { setVoice(ev.target.value, 'en'); ev.target.blur(); };
$('#tts-voice-zh').onchange = ev => { setVoice(ev.target.value, 'zh'); ev.target.blur(); };
// 对齐结果按顺序对到句子里的词上；对不上的词用前后词的时间插值
const normW = w => w.toLowerCase().replace(/[^a-z0-9\u3400-\u9fff\uf900-\ufaff]/g, '');
function wordTimes(text, words) {
  const toks = tokensOf(text), out = toks.map(() => null);
  if (!words || !words.length) return null;
  let j = 0;
  toks.forEach((t, k) => {
    const n = normW(t.w);
    for (let d = 0; d < 4 && j + d < words.length; d++) {
      if (normW(words[j + d][0]) === n) { out[k] = [words[j + d][1], words[j + d][2]]; j += d + 1; return; }
    }
  });
  if (out.filter(Boolean).length < toks.length * 0.6) return null;
  for (let k = 0; k < out.length; k++) {
    if (out[k]) continue;
    const prev = out.slice(0, k).reverse().find(Boolean), next = out.slice(k + 1).find(Boolean);
    const s = prev ? prev[1] : 0, e = next ? next[0] : s + 0.3;
    out[k] = [s, Math.max(s, e)];
  }
  return out;
}
// 逐词点亮：读过的词亮，正在读的词最亮；没有对齐结果时按字符位置估算。读完（思考停顿里）整句全亮
let kara = null;                // {line, text, toks, times, ws, at}；at = 最后一个点亮的词，滚动跟着它所在的行
function paintKara() {
  const k = kara; if (!k || !k.line.isConnected) return;
  const ws = k.ws || (k.ws = $$('.w', k.line)), fin = phase === 'think' || phase === 'after';
  const t = audio.currentTime, dur = audio.duration, tl = t + LEAD;
  // 点亮状态的指纹（亮了几个词 · 跟随到第几个 · 正在读哪个）：没变就不碰 DOM，每帧省掉逐词改 class
  let sig = 'fin';
  if (!fin && k.times) { const T = k.times, n = T.filter(([s]) => t >= s).length, j = n - 1; sig = n + ':' + T.filter(([s]) => tl >= s).length + ':' + (j >= 0 && (t < T[j][1] || (T[j + 1] ? t < T[j + 1][0] : t < T[j][1] + 0.2)) ? j : -1); }
  else if (!fin) sig = dur ? Math.round(t / dur * 400) + ':' + Math.round(tl / dur * 400) : 'x';
  if (ws === k.paintedWs && sig === k.sig) return;
  k.paintedWs = ws; k.sig = sig;
  let at = -1, lead = -1;
  ws.forEach((el, x) => {
    const j = +el.dataset.k; let lit, now = false, soon;
    if (fin) lit = soon = true;
    else if (k.times) { const [s, e] = k.times[j]; lit = t >= s; soon = tl >= s; now = lit && (t < e || (k.times[j + 1] ? t < k.times[j + 1][0] : t < e + 0.2)); }
    else { const p = k.toks[j].a / k.text.length; lit = dur ? p <= t / dur + 0.03 : false; soon = dur ? p <= tl / dur + 0.03 : false; }
    if (lit) at = x;
    if (soon) lead = x;
    if (el.classList.contains('lit') !== lit) el.classList.toggle('lit', lit);
    if (el.classList.contains('now') !== now) el.classList.toggle('now', now);
  });
  k.at = at; k.lead = lead;
  if (!fin && at >= 0 && k.hits && k.hits.length) {   // 读到人名：关系图里这个人的圆点脉冲一下（每处名字一次）
    const tok = k.toks[+ws[at].dataset.k], h = tok && k.hits.find(h => tok.a >= h.a && tok.a < h.b);
    if (h && h !== k.lastHit) { k.lastHit = h; castPulse(h.name); }
  }
}

// ------------------------------------------------------------------ Tone：当前句的Pitch contour（默认）或重音圆点 + Tone符号（Aa 里切换）
// 数据是这一句音频实测的（/api/tts/prosody，library/prosody.py）：按词的位置画在段落里的一层 SVG 上，垫在字后面
const PROS = new Map();
function fetchPros(text) {
  if (!PROS.has(text)) {
    PROS.set(text, api('/api/tts/prosody?t=' + encodeURIComponent(text) + voiceOf(text)).then(j => j.prosody || null).catch(() => { PROS.delete(text); return null; }));
    while (PROS.size > 60) PROS.delete(PROS.keys().next().value);
  }
  return PROS.get(text);
}
let tone = null;                // {line, pr, svg, pts, dot}
const TUNE_MARK = {fall: '↘', rise: '↗', lowrise: '↗', fallrise: '↘↗', risefall: '↗↘', level: '→'};
const TUNE_NAME = {fall: 'Falling: finished', rise: 'High rising: yes/no question', lowrise: 'Low rising: continuing', fallrise: 'Fall-rise: reservation, contrast', risefall: 'Rise-fall: emphasis, surprise', level: 'Level'};
const TONE_NAME = {'H*': 'H*: high target', '!H*': '!H*: downstepped high, a step below the last accent', 'L+H*': 'L+H*: sharp rise from low', 'L*': 'L*: low target'};
const LINK_NAME = {cv: 'Linking: consonant to vowel', vv: 'Linking: vowel to vowel via j / w', r: 'Linking r (British)', flap: 'Flap [ɾ]: final t/d before a vowel', cc: 'Identical consonants said once'};
const TOPIC = {'sentence-stress': 'Sentence stress', 'continuation-rise': 'Continuation rise', 'falling-tone': 'Intonation', intonation: 'Tone', 'reduced-vowel': 'Weak form', elision: 'Linking', stress: 'Word stress', rhythm: 'Rhythm'};
// 讲解卡片里的「Tone」块：调核、Intonation、Linking、Weak form、闪音、喉塞、Rhythm（library/phonrules.py 按教材规则 + 这句的实测）
function toneCard(text, pr) {
  const e = LOG.find(x => x.text === text); if (!e || e.tf || !pr || !(pr.notes || []).length) return;
  e.tf = true;
  e.card.insertAdjacentHTML('beforeend', `<ul class="tnotes">${pr.notes.map(n => `<li><i class="tk">${esc(TOPIC[n.k] || n.k)}</i>${esc(n.zh)}${n.ref ? `<span class="ref">${esc(n.ref)}</span>` : ''}</li>`).join('')}</ul>`);
}
const svgEl = (n, a) => { const e = document.createElementNS('http://www.w3.org/2000/svg', n); for (const k in a) e.setAttribute(k, a[k]); return e; };
function clearTone() { if (tone && tone.svg) tone.svg.remove(); tone = null; }
function showTone(ln, text) {             // 当前句开始读时调用；数据到了再画
  clearTone();
  if (prefs.tone === 'off' || isZh(text)) return;
  const my = tone = {line: ln, pr: null};
  fetchPros(text).then(pr => { if (tone === my && pr) { my.pr = pr; drawTone(); } });
}
function drawTone() {
  const T = tone; if (!T || !T.pr || !T.line.isConnected) return;
  if (T.svg) T.svg.remove();
  const sp = T.line.closest('.sp'), P = sp.getBoundingClientRect(), fs = parseFloat(getComputedStyle(sp).fontSize);
  const box = {};                         // 第 k 个词在段落里的位置（和 .w 的 data-k 一一对应）
  $$('.w', T.line).forEach(el => { const r = el.getBoundingClientRect(); box[+el.dataset.k] = {x0: r.left - P.left, x1: r.right - P.left, top: r.top - P.top, bot: r.bottom - P.top}; });
  const W = T.pr.words || [];
  const at = t => {                       // 时间 → 画面上的 x 和所在行（词与词之间的空隙按前后两个词插值；跨行就断开）
    for (let k = 0; k < W.length; k++) {
      const w = W[k], b = box[k]; if (!b) continue;
      if (t >= w.s && t <= w.e) return {x: b.x0 + (w.e > w.s ? (t - w.s) / (w.e - w.s) : 0.5) * (b.x1 - b.x0), b};
      const n = W[k + 1], nb = box[k + 1];
      if (n && nb && t > w.e && t < n.s) return Math.abs(nb.top - b.top) < 2 ? {x: b.x1 + (t - w.e) / (n.s - w.e) * (nb.x0 - b.x1), b} : null;
    }
    return null;
  };
  const svg = T.svg = svgEl('svg', {class: 'tone', width: P.width, height: P.height});
  if (prefs.tone === 'curve') {
    // Tone轮廓：每个音节取实测音高峰值一个点，按短语连成平滑的波浪线；短语末按实测升降多画一段收尾。
    // 不画逐帧音高（清辅音处断成碎段、抖动，反而看不清起伏）；短语边界和换行处断开
    const yOf = (st, b) => { const h = b.bot - b.top, v = Math.max(-6, Math.min(6, st)); return b.top + h * 0.1 + (1 - (v + 6) / 12) * h * 0.8; };
    const ends = new Set((T.pr.phrases || []).map(ph => ph.b));
    const segs = []; let seg = [], line = null, last = null; T.pts = [];
    const push = (t, st, k) => {
      const m = at(t); if (!m || st == null) return;
      if (line != null && Math.abs(m.b.top - line) > 2) { if (seg.length > 1) segs.push(seg); seg = []; }
      const pt = [m.x, yOf(st, m.b)]; seg.push(pt); line = m.b.top; last = st; T.pts.push({t, x: pt[0], y: pt[1]});
    };
    W.forEach((w, k) => {
      (w.sy || []).forEach(([s0, s1, , pk]) => push((s0 + s1) / 2, pk, k));
      if (w.nuc && w.tune_st != null && last != null) push(w.e - 0.02, last + w.tune_st * 0.7, k);   // Intonation收尾：降 / 升
      if (ends.has(k)) { if (seg.length > 1) segs.push(seg); seg = []; line = null; }
    });
    if (seg.length > 1) segs.push(seg);
    segs.forEach(sg => {
      let d = `M${sg[0][0].toFixed(1)},${sg[0][1].toFixed(1)}`;
      for (let i = 1; i < sg.length; i++) { const [a, b] = sg[i - 1], [c, e] = sg[i], m = ((a + c) / 2).toFixed(1); d += ` C${m},${b.toFixed(1)} ${m},${e.toFixed(1)} ${c.toFixed(1)},${e.toFixed(1)}`; }
      svg.append(svgEl('path', {d, class: 'tc-soft', 'stroke-width': (fs * 0.32).toFixed(1)}), svgEl('path', {d, class: 'tc'}));
    });
    W.forEach((w, k) => {                  // 主重音：重读音节的音高点上一个金点
      const b = box[k]; if (!w.nuc || !b) return;
      const sy = (w.sy || []).find(x => x[2] === 1) || (w.sy || [])[0]; if (!sy || sy[3] == null) return;
      const m = at((sy[0] + sy[1]) / 2); if (m) svg.append(svgEl('circle', {cx: m.x, cy: yOf(sy[3], b), r: fs * 0.11, class: 'tn'}));
    });
    T.dot = svgEl('circle', {r: fs * 0.08, class: 'td', visibility: 'hidden'}); svg.append(T.dot);
  } else {
    const txt = (x, y, s, cls, title) => { const e = svgEl('text', {x, y, class: cls, 'font-size': (fs * 0.62).toFixed(1)}); e.textContent = s; if (title) e.append(Object.assign(svgEl('title', {}), {textContent: title})); svg.append(e); };
    W.forEach((w, k) => {
      const b = box[k]; if (!b) return;
      (w.sy || []).forEach(([s0, s1, str]) => {   // 圆点：● 主重音 · • 重读音节 · ∙ Weak form，排在音节下方
        const m = at((s0 + s1) / 2), x = m ? m.x : (b.x0 + b.x1) / 2, big = w.nuc && str === 1, mid = w.acc && str === 1;
        svg.append(svgEl('circle', {cx: x, cy: b.bot + fs * 0.12, r: fs * (big ? 0.12 : mid ? 0.085 : 0.045), class: big ? 'tn' : mid ? 'tdot' : 'tdot weak'}));
        if (mid || big) { const a = at(s0); txt((a ? a.x : b.x0) - fs * 0.12, b.top + fs * 0.42, 'ˈ', 'tm'); }
      });
      if (w.nuc) {                         // Intonation：主重音后 ↘ 降 · ↗ 高升 · ↗ 低升 · ↘↗ 降升 · ↗↘ 升降 · → 平；和教科书规则不一致的灰色显示
        txt(b.x1 + fs * 0.04, b.top + fs * 0.5, TUNE_MARK[w.tune] || '→', w.odd ? 'tm odd' : 'tm ar',
            (TUNE_NAME[w.tune] || '') + (w.odd ? ': this reader departs from the usual rule here' : ''));
      }
      if (prefs.tone === 'tobi' && w.tone) {   // Prosody layer：重读音节上方的 ToBI 目标调
        const sy = (w.sy || []).find(x => x[2] === 1) || (w.sy || [])[0], m = sy && at((sy[0] + sy[1]) / 2);
        txt((m ? m.x : (b.x0 + b.x1) / 2) - fs * 0.3, b.top - fs * 0.05, w.tone, w.nuc ? 'tm tb nuc' : 'tm tb', TONE_NAME[w.tone] || '');
      }
      if (w.glot) txt(b.x1 - fs * 0.5, b.bot + fs * 0.42, 'ʔ', 'tm lk', 'Glottal: unreleased t');
    });
    (T.pr.links || []).forEach(([k, kind]) => {   // Linking点：‿ Linking · ɾ 闪音，画在两个词的接缝下方（换行处不画）
      const a = box[k], c = box[k + 1]; if (!a || !c || Math.abs(a.top - c.top) > 2) return;
      txt((a.x1 + c.x0) / 2 - fs * 0.2, a.bot + fs * 0.42, kind === 'flap' ? 'ɾ' : '‿', 'tm lk', LINK_NAME[kind] || '');
    });
    if (prefs.tone === 'tobi') (T.pr.phrases || []).forEach(ph => {   // 边界调 L-L% / H-H% / L-H% / H-L%
      const b = box[ph.b]; if (!b || !ph.bt) return;
      txt(b.x1 + fs * 0.04, b.bot + fs * 0.42, ph.bt, 'tm tb', 'Boundary tone (ToBI)');
    });
    (T.pr.beats || []).forEach(tb => {       // 节拍：重读音节中点一个小刻度，看重音是不是等时
      const m = at(tb); if (!m) return;
      svg.append(svgEl('rect', {x: m.x - 0.6, y: m.b.bot + fs * 0.22, width: 1.2, height: fs * 0.16, class: 'tbeat'}));
    });
    (T.pr.phrases || []).forEach(ph => {  // 停顿：| 短停 · ‖ 句末
      const b = box[ph.b]; if (!b || !ph.brk) return;
      txt(b.x1 + fs * (W[ph.b] && W[ph.b].nuc ? 0.62 : 0.14), b.top + fs * 0.8, ph.brk, 'tm br');
    });
  }
  sp.prepend(svg);
}
const fs0 = T => parseFloat(T.dot.getAttribute('r')) / 0.08;   // 字号（小白点半径按字号定）
function toneNow() {                      // Tone线上跟着声音走的小白点（每帧，只改一个圆的坐标）
  const T = tone; if (!T || !T.dot || !T.pts || !T.pts.length) return;
  const t = audio.currentTime, ps = T.pts;
  let lo = 0, hi = ps.length - 1;
  while (lo < hi) { const m = (lo + hi + 1) >> 1; if (ps[m].t <= t) lo = m; else hi = m - 1; }
  const p = ps[lo], q = ps[lo + 1], on = phase === 'speak' && t >= ps[0].t - 0.05 && t <= ps[ps.length - 1].t + 0.1;
  T.dot.setAttribute('visibility', on ? 'visible' : 'hidden');
  if (!on) return;
  const f = q && Math.abs(q.y - p.y) < fs0(T) * 2 && q.t > p.t ? Math.max(0, Math.min(1, (t - p.t) / (q.t - p.t))) : 0;   // 同一行的相邻两点之间插值
  T.dot.setAttribute('cx', (p.x + (q ? (q.x - p.x) * f : 0)).toFixed(1)); T.dot.setAttribute('cy', (p.y + (q ? (q.y - p.y) * f : 0)).toFixed(1));
}
let toneRaf = 0;
new ResizeObserver(() => { cancelAnimationFrame(toneRaf); toneRaf = requestAnimationFrame(drawTone); }).observe(list);   // 字号、窗口变了重画

// ------------------------------------------------------------------ 导图（右栏下方，宽屏；白话书占满右栏）：/api/books/{id}/map（library/concepts.py）按书的类型给
// kind = people：到这一章为止的Person与关系（library/cast.py）。Whole bookPerson固定一张网（力导向，位置不变好记）：正在说话的人金色点亮
// （对白按归属的说话人；Narrator句点亮Narrator），这句里被提到的人白色半亮、和说话人之间的连线与关系标签显出，直接相关的人微亮，
// 其他人退成暗点；读到名字那个词时圆点脉冲一下。悬停看身份，点一个人钉住看他的全部关系（换句自动松开）
// kind = concepts：论述、科普、修行类的书，这一节的关键Concept、Method、Claim和关系；读到哪个Concept点亮哪个，没点亮时下方显示这一节的主旨
const castEl = $('#cast'), ctip = $('#ctip');
let cast = null;                          // {kind, span, people, links, byName, pairs, pos, ND, E, on, pin}
let castAt = 0, castFor = -1;
const CKIND = {concept: 'Concept', method: 'Method', claim: 'Claim', person: 'Person', example: 'Example'};
const castOn = () => prefs.cast && wide.matches;
function castHits(text) {                 // 句子里提到的人：别名按长的先配、整词、区分大小写，所有格也算；和 library/cast.py 的 mentions 一致
  if (!cast || !castOn()) return [];
  const hits = [];
  cast.pairs.forEach(([a, name]) => {
    const re = new RegExp(`(?<![A-Za-z])${reEsc(a)}(?:['’]s)?(?![A-Za-z])`, 'g'); let m;
    while ((m = re.exec(text))) if (!hits.some(h => m.index < h.b && m.index + m[0].length > h.a)) hits.push({a: m.index, b: m.index + m[0].length, name});
  });
  return hits.sort((x, y) => x.a - y.a);
}
const inSpan = (sp, n) => !!sp && n >= sp[0] && n < sp[1];
function castSync() {                     // 每句开始：读出这份图管的范围（换章 / 换节）、或还没抽好（每 20 秒再问一次）时取数据；再按这句点亮
  if (!castOn()) { castShow(false); return; }
  const n = cur.n, old = Date.now() - castAt > 20000;
  if ((!cast || !inSpan(cast.span, n) || (cast.pending && old)) && (castFor !== n || old)) {
    castFor = n; castAt = Date.now();
    api(`/api/books/${B.id}/map?para=${n}`).then(d => {
      if (d.mode && d.mode !== MODE) stale = true;   // 读法打开页面后才定下来（中文书问过模型）：到句子间隔重新打开
      if (inSpan(d.span, cur.n)) { castData(d); castMark(); }
    }).catch(() => {});
  }
  castMark();
}
function castShow(on) { castEl.hidden = !on; $('.rd').classList.toggle('cast-on', on); }
function castData(d) {
  if (!d || !(d.people || []).length) { cast = {kind: d.kind, span: d.span, people: [], links: [], pairs: [], pending: !!d.pending}; castShow(false); return; }
  const same = cast && cast.kind === d.kind;
  cast = {kind: d.kind || 'people', span: d.span, thesis: d.thesis || '', people: d.people, links: d.links || [], pending: !!d.pending, byName: {},
          on: same && cast.on || [], said: [], pin: null, narrator: d.narrator || null};
  d.people.forEach(p => { cast.byName[p.name] = p; });
  cast.pairs = d.people.flatMap(p => p.aliases.map(a => [a, p.name])).sort((x, y) => y[0].length - x[0].length);
  castShow(true); castBuild();
}
const castNb = name => cast.links.filter(l => l.a === name || l.b === name).map(l => ({o: l.a === name ? l.b : l.a, rel: l.rel}));
// 圆点下的短名：书里常用的那个单词叫法（名 Jordan、或姓 Gatsby），带称谓、所有格（Jordan’s undergraduate）的显示全名
function castShort(p) {
  if (cast && cast.kind === 'concepts') return p.name;
  const w = p.name.split(' ');
  if (w.length > 1 && (/^(Mr|Mrs|Ms|Miss|Dr|Sir|Lady|Lord|Madame|Captain|Colonel|Professor|Uncle|Aunt)\.?$/i.test(w[0]) || /['’]s$/.test(w[0]))) return p.name;
  const one = (p.aliases || []).filter(a => !a.includes(' '));
  return [w[0], w[w.length - 1]].find(x => one.includes(x)) || one[0] || w[0];
}
const castR = p => Math.min(19, Math.sqrt(p.n || 1) * 1.5 + 5);   // 圆点 5–19px：主角大、路人小
function castLayout(W, H) {               // 力导向：从一个圆开始迭代（无随机数，同样的人每次位置一样）；边长 ~130px，斥力按面积定，往中心轻轻拉
  const N = cast.people.map((p, i) => ({name: p.name, x: W / 2 + Math.cos(i * 2.4) * W * .3, y: H / 2 + Math.sin(i * 2.4) * H * .3, vx: 0, vy: 0}));
  const ix = Object.fromEntries(N.map((n, i) => [n.name, i]));
  const K = Math.max(5000, W * H / Math.max(1, N.length) * .9);   // 斥力：每个人分到的面积越大推得越开（标签互不压着）
  for (let it = 0; it < 400; it++) {
    N.forEach(a => N.forEach(b => { if (a === b) return; const dx = a.x - b.x, dy = (a.y - b.y) * 1.4, d2 = dx * dx + dy * dy + 40, f = K / d2, d = Math.sqrt(d2); a.vx += dx / d * f; a.vy += dy / d * f * 1.4; }));
    cast.links.forEach(l => { const A = N[ix[l.a]], B = N[ix[l.b]]; if (!A || !B) return; const dx = B.x - A.x, dy = B.y - A.y, d = Math.hypot(dx, dy) || 1, f = (d - 130) * .02; A.vx += dx / d * f; A.vy += dy / d * f; B.vx -= dx / d * f; B.vy -= dy / d * f; });
    N.forEach(n => { n.vx += (W / 2 - n.x) * .003; n.vy += (H / 2 - n.y) * .003; n.x += n.vx *= .82; n.y += n.vy *= .82; n.x = Math.max(44, Math.min(W - 44, n.x)); n.y = Math.max(22, Math.min(H - 30, n.y)); });
  }
  // 再把名字标签挤开：两个人的标签框（宽 ≈ 字数 × 6.8px，高 30px）叠着就沿着挪得少的那个方向分开，直到不叠或 80 轮
  const LW = n => [...castShort(cast.byName[n.name])].reduce((w, c) => w + (/[\u3400-\u9fff]/.test(c) ? 13 : 6.8), 12), LH = 30;   // 汉字约一个字号宽
  for (let it = 0; it < 80; it++) {
    let moved = false;
    for (let i = 0; i < N.length; i++) for (let j = i + 1; j < N.length; j++) {
      const a = N[i], b = N[j], w = (LW(a) + LW(b)) / 2, dx = b.x - a.x, dy = b.y - a.y;
      if (Math.abs(dx) >= w || Math.abs(dy) >= LH) continue;
      const px = (w - Math.abs(dx)) / 2, py = (LH - Math.abs(dy)) / 2;
      const edge = Math.min(a.x, b.x) <= 44 || Math.max(a.x, b.x) >= W - 44;   // 已经顶到左右边了：只能上下错开
      if (px < py && !edge) { const s = Math.sign(dx) || 1; a.x -= px * s; b.x += px * s; } else { const s = Math.sign(dy) || 1; a.y -= py * s; b.y += py * s; }
      moved = true;
    }
    N.forEach(n => { n.x = Math.max(44, Math.min(W - 44, n.x)); n.y = Math.max(22, Math.min(H - 30, n.y)); });
    if (!moved) break;
  }
  return Object.fromEntries(N.map(n => [n.name, n]));
}
function castBuild() {
  if (!cast || castEl.hidden) return;
  const svg = castEl.querySelector('svg'), W = castEl.clientWidth, H = Math.max(80, castEl.clientHeight - 56);
  if (!W) return;
  svg.textContent = ''; const pos = cast.pos = castLayout(W, H);
  cast.E = cast.links.map(l => { const A = pos[l.a], B = pos[l.b];
    const e = svgEl('line', {x1: A.x, y1: A.y, x2: B.x, y2: B.y, class: 'ed'}), t = svgEl('text', {x: (A.x + B.x) / 2, y: (A.y + B.y) / 2 - 3, class: 'el'});
    t.textContent = l.rel; svg.append(e, t); return {a: l.a, b: l.b, e, t}; });
  cast.ND = {};
  cast.people.forEach(p => {
    const r = castR(p), g = svgEl('g', {class: 'nd'}), c = svgEl('circle', {cx: pos[p.name].x, cy: pos[p.name].y, r}), t = svgEl('text', {x: pos[p.name].x, y: pos[p.name].y + r + 14});
    c.style.setProperty('--r', r + 'px'); t.textContent = castShort(p); g.append(c, t); svg.append(g); cast.ND[p.name] = g;
    if (p.name === cast.narrator) g.append(svgEl('circle', {cx: pos[p.name].x, cy: pos[p.name].y, r: r + 5, class: 'nr'}));   // Narrator：外面一圈虚线
    g.onmouseenter = () => { ctip.innerHTML = `<b>${esc(p.name)}${p.name === cast.narrator ? ' · narrator' : CKIND[p.kind] && p.kind !== 'concept' ? ' · ' + CKIND[p.kind] : ''}</b>${esc(p.role || '')}`; ctip.style.opacity = 1; };
    g.onmousemove = ev => { ctip.style.left = ev.clientX + 14 + 'px'; ctip.style.top = ev.clientY - 10 + 'px'; };
    g.onmouseleave = () => { ctip.style.opacity = 0; };
    g.onclick = ev => { ev.stopPropagation(); cast.pin = cast.pin === p.name ? null : p.name; castPaint(); };
  });
  castPaint();
}
// 说话人（/text 的 speech 里的称呼）对到图里的人：称呼相同，或是他的某个叫法（图只到这一章，称呼可能还是早先的短名）
function castWho(name) {
  if (!cast || !name) return null;
  if (cast.byName[name]) return name;
  const k = name.toLowerCase(), pr = cast.pairs.find(([a]) => a.toLowerCase() === k);
  return pr ? pr[1] : null;
}
function castMark() {                     // 每句：说话的人点亮（对白 = 归属的说话人，Narrator = Narrator）；提到的人半亮；都没有就保持上一句、整体变淡
  if (!cast || castEl.hidden) return;
  const ln = curLine(), w = cast.kind === 'people' ? whoOf(P.get(cur.n), cur.i) : null;   // Concept图没有「谁在说」：句子里提到的Concept点亮
  const said = ln ? [...new Set(castHits(ln._t).map(h => h.name))] : [];
  const who = cast.kind !== 'people' ? null : w ? castWho(w.name) : castWho(cast.narrator);
  if (kara && ln && kara.line === ln) { kara.hits = castHits(ln._t); kara.lastHit = null; }
  cast.stale = !who && !said.length;
  if (who) { cast.on = [who]; cast.said = said.filter(n => n !== who); }
  else if (said.length) { cast.on = said; cast.said = []; }
  cast.pin = null; castPaint();
}
function castPaint() {
  if (!cast || !cast.ND) return;
  const act = cast.pin ? [cast.pin] : cast.on, said = cast.pin ? [] : (cast.said || []).filter(n => !act.includes(n));
  const lit = [...act, ...said], near = new Set(act.flatMap(n => castNb(n).map(x => x.o)));
  cast.people.forEach(p => { cast.ND[p.name].setAttribute('class', 'nd' + (act.includes(p.name) ? ' on' : said.includes(p.name) ? ' said' : near.has(p.name) ? ' near' : '') + (cast.pin === p.name ? ' pin' : '')); });
  cast.E.forEach(e => { const k = lit.includes(e.a) && lit.includes(e.b) && (act.includes(e.a) || act.includes(e.b)) ? ' on' : act.includes(e.a) || act.includes(e.b) ? ' near' : ''; e.e.setAttribute('class', 'ed' + k); e.t.setAttribute('class', 'el' + k); });
  const cap = castEl.querySelector('.cap'), n0 = act[0], p0 = n0 && cast.byName[n0];
  const rels = p0 ? said.map(n => { const o = castNb(n0).find(x => x.o === n); return `${esc(castShort(cast.byName[n]))}${o ? `（${esc(o.rel)}）` : ''}`; }) : [];
  const cc = cast.kind === 'concepts';
  if (cc && !act.length) cap.innerHTML = cast.thesis ? `<div><i>This section</i>${esc(cast.thesis)}</div>` : '';
  else cap.innerHTML = (p0 ? `<div><b>${esc(p0.name)}</b>${p0.name === cast.narrator ? '<i>narrator</i> ' : cc && CKIND[p0.kind] ? `<i>${CKIND[p0.kind]}</i>` : ''}${esc(p0.role || '')}</div>` : '') +
    (rels.length ? `<div class="to">${cc ? 'related' : 'mentions'} ${rels.join(', ')}</div>` : '');
  cap.insertAdjacentHTML('beforeend', `<span class="n">${cast.people.length} ${cc ? 'concepts' : 'people'}</span>`);
  castEl.classList.toggle('stale', !!cast.stale && !cast.pin);
}
function castPulse(name) {
  const g = cast && cast.ND && cast.ND[name]; if (!g || !castOn()) return;
  g.classList.remove('pulse'); void g.getBBox(); g.classList.add('pulse');
  const r = svgEl('circle', {cx: cast.pos[name].x, cy: cast.pos[name].y, class: 'ring go'}); g.parentNode.insertBefore(r, g); setTimeout(() => r.remove(), 900);
}
let castRaf = 0;
new ResizeObserver(() => { cancelAnimationFrame(castRaf); castRaf = requestAnimationFrame(castBuild); }).observe(castEl);
wide.addEventListener('change', castSync);

// ------------------------------------------------------------------ Play
// phase：idle 这句还没开始 · load 取音频中 · speak 音频已就位（在读，或停在句中）· think 读完、思考停顿中 · after 读完、停顿被打断 / 等下一句
let cur = {n: 0, i: 0}, gen = 0, nav = 0, playing = false, phase = 'idle';
let thinkT = 0, thinkAt = 0, thinkWords = 0;
const setState = t => { $('#tts-state').textContent = t; };
// ------------------------------------------------------------------ 多角色：这一句是谁说的（/text 的 speech，library/speech.py），左下角显示名字 + 性别图标 + 音色
const GICON = {f: '<svg viewBox="0 0 12 14"><circle cx="6" cy="5" r="3.6"/><path d="M6 8.6v5M3.6 11.6h4.8"/></svg>',
               m: '<svg viewBox="0 0 14 14"><circle cx="5.5" cy="8.5" r="3.6"/><path d="M8.2 5.8l4.3-4.3M8.8 1.5h3.7v3.7"/></svg>'};
const hueOf = s => { let h = 0; for (const c of s) h = (h * 31 + c.charCodeAt(0)) % 360; return h; };
function whoOf(p, i) {                    // 这一句落在哪条对白里（重叠最多的那条）
  if (!p || !p.speech || !p.speech.length || !p.sents[i]) return null;
  const [a, b] = p.sents[i]; let best = null, ov = 0;
  p.speech.forEach(s => { const o = Math.min(b, s[1]) - Math.max(a, s[0]); if (o > ov) { ov = o; best = s; } });
  return best ? {name: best[2], g: best[3], voice: best[4], narr: !!best[5]} : null;
}
function decorate(el, p) {                // 对白句：前面一小条按Person固定颜色的色条，悬停看人名
  $$('.ln', el).forEach(ln => {
    const w = whoOf(p, +ln.dataset.i);
    ln.classList.toggle('q', !!w);
    if (w) { ln.style.setProperty('--qc', `hsl(${hueOf(w.name)} 55% 68%)`); ln.title = w.name; } else { ln.style.removeProperty('--qc'); ln.removeAttribute('title'); }
  });
}
function showWho() {                      // 左下角一行淡字：名字过长就省略，不把后面的状态挤走
  const p = P.get(cur.n), w = whoOf(p, cur.i), el = $('#st-who');
  const ln = curLine(), l = ln && isZh(ln._t) ? 'zh' : 'en', v = (VOICES[l] || []).find(x => x.id === prefs[vkey(l)]);
  const icon = w ? (GICON[w.g] || '') : '';
  let name, role;
  if (w && !w.narr) { name = w.name; role = w.voice || (v ? v.name : ''); }
  else {
    const who = w ? w.name : NARR;
    name = who || 'Narrator';
    role = [who ? (w ? 'Narrator' : 'Narrator') : '', v ? v.name : ''].filter(Boolean).join(' · ');
  }
  el.innerHTML = `${icon ? `<i class="g">${icon}</i>` : ''}<b>${esc(name)}</b>${role ? `<span class="role">${esc(role)}</span>` : ''}`;
}
let speechAt = 0;
function speechSync() {                   // 这一段有引号却还没有对白归属（预生成 / Whole book准备会补）：最多每分钟重取一次，做好了就换上并作废旧音频
  const p = P.get(cur.n);
  if (!p || (p.speech_ok && p.speech && p.speech.length) || !/[“"「‘]/.test(p.text) || Date.now() - speechAt < 60000) return;
  speechAt = Date.now();
  fetchParas(cur.n, 40).then(ps => ps.forEach(q => {
    const old = P.get(q.n); if (!old || !q.speech_ok || (old.speech_ok && JSON.stringify(old.speech) === JSON.stringify(q.speech))) return;
    old.speech = q.speech; old.speech_ok = true;
    old.sents.forEach((_, i) => { const t = sentText(old, i); const u = AUD.get(t); if (u) { AUD.delete(t); u.then(x => URL.revokeObjectURL(x)).catch(() => {}); } WRD.delete(t); PROS.delete(t); });
    const el = paraEl(q.n); if (el) decorate(el, old);
    if (q.n === cur.n) showWho();
  })).catch(() => {});
}
const setBtn = () => { $('#tts-play').textContent = playing ? 'Pause' : 'Play'; };
// 思考时间 = 基础秒数 + 每词 0.1 s（最多加 4 s）；基础为 0 就不停顿
const pauseMs = words => prefs.think > 0 ? (prefs.think + Math.min(4, 0.1 * words)) * 1000 : 0;
// 进入一句：换高亮、换译文、补段落、存进度 —— 画面只在这里（以及用户操作时）变
function begin(pos, play) {
  if (stale && !(play !== undefined ? play : playing)) { cur = pos; relaunch(pos, false); return; }
  gen++; clearTimeout(thinkT); audio.pause(); dropStream(); kara = null; clearTone(); setState('');
  cur = pos; phase = 'idle'; follow = sFollow = true;
  if (play !== undefined) { playing = play; setBtn(); }
  syncDOM(); markCur(); chapterUI(); castSync(); showWho(); speechSync(); feed(cur.n); saveSoon(); wake();
  ahead(3).forEach(explainOf);
  if (playing) run(gen); else warmAudio();
}
function markCur() {
  const here = paraEl(cur.n), ps = [...list.children], h = ps.indexOf(here);
  ps.forEach((p, k) => { const c = 'sp' + (p.classList.contains('head') ? ' head' : '') + (p.classList.contains('zh') ? ' zh' : '') + (k === h ? ' here' : k === h - 1 ? ' prev' : Math.abs(k - h) > 2 ? ' far' : ''); if (p.className !== c) p.className = c; });
  $$('.ln.cur', list).forEach(unannotate);
  const ln = curLine(), text = ln._t;
  ln.classList.add('cur'); ln.querySelector('.en').innerHTML = enHTML(text, []);
  fillCur(ln, logEnter(text));
}
async function run(g) {                   // 开始读当前句（需要时先确认音色服务）
  if (!ttsOK && !await ttsReady()) { if (g === gen) { playing = false; setBtn(); } return; }
  if (g !== gen || !playing) return;
  speak(g); prep();                       // 先把这一句的请求发出去，再发This chapter预生成（同一句别让预生成抢先走非流式）
}
async function speak(g) {
  phase = 'load';
  const ln = curLine(), text = ln._t;
  const slow = setTimeout(() => g === gen && setState('Preparing…'), 400);
  let url, live = null;
  try {
    if (AUD.has(text) || !canStream() || noStream === text) url = await fetchAudio(text);
    else { const r = await openStream(text); if (r.url) url = r.url; else live = r; }   // 没预取过：边生成边播；服务端已有缓存就回文件
  }
  catch (e) { clearTimeout(slow); if (g === gen) { setState(''); phase = 'idle'; playing = false; setBtn(); toast(e.message); } return; }
  clearTimeout(slow);
  if (g !== gen) { if (live) live.stop(); return; }
  setState('');
  // 后面 6 句：先下一句（起步快），其余几句随后；生成完顺手取逐词时间。只取数据，到了那一句才用
  const up = ahead(6), pre = t => fetchAudio(t).then(() => fetchWords(t)).catch(() => {});
  if (up.length) pre(up[0]).finally(() => up.slice(1).forEach(pre));
  if (live) { audio = live; live.estDur = isZh(text) ? text.length / 4.5 : text.length / 14; }   // 结束前按字数估时长（逐词点亮的兜底）
  else { audio = player; player.src = url; player.defaultPlaybackRate = player.playbackRate = prefs.rate; }
  kara = {line: ln, text, toks: tokensOf(text), times: null, ws: null, at: -1, hits: castHits(text)};   // 逐词时间到之前按字符位置估算
  const k = kara;
  const words = () => fetchWords(text).then(ws => { if (kara === k) { k.times = wordTimes(text, ws); k.sig = null; } });
  const tones = () => { if (!tone || tone.line !== ln) showTone(ln, text); if (!isZh(text)) fetchPros(text).then(pr => toneCard(text, pr)); };   // 讲解栏的Tone说明（数据和Tone层同一份）
  if (live) { live.done.then(() => { if (g === gen) { words(); tones(); } }); live.onfail = () => { if (g === gen) { noStream = text; audio = player; speak(g); } }; }   // 流式：文件落盘后才能对齐、算Tone
  else { words(); tones(); }
  phase = 'speak';
  audio.onended = () => g === gen && ended(g);
  if (playing) audio.play().catch(e => { if (g === gen && e.name !== 'AbortError') { toast('Playback failed: ' + e.message); playing = false; setBtn(); } });
  wake();
}
player.addEventListener('play', wake);
function ended(g) {                       // 读完：整句全亮，进入思考停顿 —— 此后到下一句开始，画面不再变
  phase = 'think'; paintKara();
  if (kara) measure(kara.text, audio.duration);   // 原速时长（playbackRate 不影响 duration）：校准左下角的时长估算
  thinkWords = kara ? (isZh(kara.text) ? Math.round(kara.toks.length / 2) : kara.toks.length) : 0; thinkAt = performance.now();   // 中文约两字一词
  schedule(g);
}
function schedule(g) {                    // 按当前思考时间（重新）排定停顿结束；已经超时就立刻接下一句
  clearTimeout(thinkT);
  const left = pauseMs(thinkWords) - (performance.now() - thinkAt);
  if (left <= 0) next(g); else thinkT = setTimeout(() => next(g), left);
}
async function next(g) {
  if (g !== gen || !playing) return;
  clearTimeout(thinkT); phase = 'after';
  const my = ++gen;
  let p; try { p = await move(cur, 1); } catch (e) { if (my === gen) { playing = false; setBtn(); toast('Could not load: ' + e.message); } return; }
  if (my !== gen || !playing) return;
  if (!p) { playing = false; setBtn(); toast('Finished'); return; }
  if (stale) { relaunch(p, true); return; }
  begin(p);
}
// Enter：Play ↔ Pause。句中Pause → 原地接着读；停顿里Pause → 取消停顿，再按直接读下一句
// 停顿里到达的译文 / 讲解没有上屏（停顿中画面不变）；停顿被打断时补上
function catchUp() {
  const ln = curLine(), e = logCur();
  if (ln && e) fillCur(ln, e);
}
async function toggle() {
  if (!list.firstChild) return;
  if (playing) {
    playing = false; setBtn(); setState('');
    if (phase === 'think' || phase === 'after') { gen++; clearTimeout(thinkT); phase = 'after'; catchUp(); }
    else audio.pause();
    return;
  }
  playing = true; setBtn();
  if (phase === 'speak') { prep(); audio.play().catch(() => {}); }
  else if (phase === 'after') next(gen);
  else if (phase === 'idle') run(gen);
  // load：音频到手后 speak() 自己接着播
}
async function go(get, play) {            // 用户跳句：←/→、点句子、目录
  const t = ++nav; let p;
  try { p = await get(); } catch (e) { toast('Could not load: ' + e.message); return; }
  if (t === nav && p) begin(p, play);
}
const step = d => go(() => move(cur, d), true);
const jump = (n, play) => go(async () => { await loadPara(n); return fix({n, i: 0}); }, play);
function setThink(d) {                    // +/− 键 ±1 秒，↑/↓ 或 Aa 面板 ±10 秒；停顿进行中立即按新值重排
  prefs.think = Math.max(0, Math.min(120, prefs.think + d)); savePrefs(); showPace();
  toast(prefs.think ? `${prefs.think}s pause between sentences` : 'No pause between sentences');
  if (!$('#aa').hidden) renderAa();
  if (phase === 'think' && playing) schedule(gen);
}
// This chapter后台预生成：开始Play时静默调用一次（每章一次），出错、已在跑都不管
let prepKey = '';
function chapterRange() {
  const n = cur.n, lv1 = toc.filter(t => t[0] === 1);
  let k = -1; lv1.forEach((t, i) => { if (t[3] <= n) k = i; });
  if (k < 0) return {start: n, end: Math.min(total, n + 200)};
  const st = lv1[k][3], en = k + 1 < lv1.length ? lv1[k + 1][3] : total;
  return {start: st, end: Math.max(en, st + 1)};
}
let prepWait = 0, prepTold = '';
function prep() {
  const r = {...chapterRange(), para: cur.n, voice: prefs.voice, voice_zh: prefs.voiceZh}, key = [r.start, r.end, r.voice, r.voice_zh].join('-');
  if (key === prepKey || Date.now() < prepWait) return; prepKey = key;
  // 忙（409）或音色没就绪：不算发过，一分钟后再试（不是每句都发）；忙的原因提示一次
  const fail = async x => {
    if (prepKey === key) prepKey = '';
    prepWait = Date.now() + 60000;
    if (x && x.status === 409 && prepTold !== key) { prepTold = key; const j = await x.json().catch(() => ({})); toast('This chapter can't pre-generate right now (' + (j.detail || 'another job is running') + ') — generating as you read'); }
  };
  fetch(`/api/books/${B.id}/prep`, {method: 'POST', headers: {'content-type': 'application/json'}, body: JSON.stringify(r)}).then(x => x.ok || fail(x), () => fail());
}
// 讲解栏里点词听：一次性的 Audio；正在读就先停一下，听完接着读
async function hear(text) {
  if (!ttsOK && !await ttsReady()) return;
  const g = gen, resume = playing && phase === 'speak' && !audio.paused;
  if (resume) audio.pause();
  const back = () => { if (resume && g === gen && playing && phase === 'speak' && audio.paused) audio.play().catch(() => {}); };
  try { once.src = await fetchAudio(text); once.playbackRate = 0.9; once.onended = back; await once.play(); }
  catch (e) { toast(e.message); back(); }
}
// 正文区点一下：别的句子 → 跳过去读；当前句或空白处 → Play / Pause（同 Enter）。选字、双击、点掉菜单不算
let menuOpen = false;
sub.addEventListener('mousedown', () => { menuOpen = !$('#aa').hidden || !$('#all').hidden || $('#toc').classList.contains('open'); });
sub.addEventListener('click', ev => {
  if (ev.detail > 1 || menuOpen || !getSelection().isCollapsed) return;
  const ln = ev.target.closest('.ln');
  if (ln) {
    const n = +ln.parentElement.dataset.n, i = +ln.dataset.i;
    if (n !== cur.n || i !== cur.i) { go(async () => (await loadPara(n)) && okPos({n, i}) ? {n, i} : null, true); return; }
  }
  toggle();
});
// 语速：滚轮 ±0.05×（0.7–1.5），变速不变调（preservesPitch），不重新生成音频；正在读的这句立即生效
const rateText = () => prefs.rate.toFixed(2).replace(/0$/, '') + '×';
function setRate(d) {
  prefs.rate = Math.max(RATE_MIN, Math.min(RATE_MAX, Math.round((prefs.rate + d) * 100) / 100)); savePrefs();
  player.defaultPlaybackRate = player.playbackRate = prefs.rate;
  showPace(); if (!$('#aa').hidden) renderAa();
  toast(`Speed ${rateText()}`);
}
// 左下角状态区：语速 · 句间间隔 · 今日已读
function showPace() { $('#st-pace').innerHTML = `<span class="rate">${rateText()}</span><span>${prefs.think}s pause</span>`; }
$('#tts-play').onclick = toggle;
$('#tts-stop').onclick = () => exit();
function toggleFs() {
  if (document.fullscreenElement) document.exitFullscreen().catch(() => {});
  else document.documentElement.requestFullscreen().catch(e => toast('Could not go full screen: ' + e.message));
}
$('#b-fs').onclick = toggleFs;
document.addEventListener('fullscreenchange', () => {
  const on = !!document.fullscreenElement, b = $('#b-fs');
  b.textContent = on ? 'Leave full screen' : 'Full screen'; b.title = on ? 'Leave full screen (F / Esc)' : 'Full screen (F)';
});
// 指针 2 秒没动：<html> 加 still —— Full screen时藏起光标，句子也不因指针恰好停在上面而变亮（正文从指针下面滑过时，那一句会像第二处高亮）。
// 只认坐标真的变了：正文在指针下平移时浏览器补发的 mousemove 坐标不变，不算动
let ptX = NaN, ptY = NaN, stillT = 0;
function stir() {
  document.documentElement.classList.remove('still'); clearTimeout(stillT);
  stillT = setTimeout(() => document.documentElement.classList.add('still'), 2000);
}
document.documentElement.classList.add('still');
addEventListener('mousemove', e => { if (e.screenX !== ptX || e.screenY !== ptY) { ptX = e.screenX; ptY = e.screenY; stir(); } }, {passive: true});
addEventListener('mousedown', stir, {passive: true});

// ------------------------------------------------------------------ 讲解栏（译文 · 难点 · 生词）
function openExplain(on) {
  $('#explain').hidden = !on; $('#b-exp').classList.toggle('on', on); LS.set('explain', on);
  $('.rd').classList.toggle('ex-on', on); sSnap = true;   // 窄屏下右栏随之收起 / 显示，回来时直接对齐
  const e = logCur(); if (on && e) scrollCard(e, false);
  if (on) { const k = LOG.indexOf(e); LOG.slice(Math.max(0, k - 3), k + 2).forEach(fillNotes); }   // 当前句和前后几句
}
$('#b-exp').onclick = () => openExplain($('#explain').hidden);
$('#ex-close').onclick = () => openExplain(false);
exList.addEventListener('click', ev => { const w = ev.target.closest('.wd'); if (w) hear(w.dataset.w); });

// ------------------------------------------------------------------ 目录 · Aa
$('#toc-list').innerHTML = toc.length ? toc.map(([lv, t, p], i) => `<div class="it" data-i="${i}" style="padding-left:${18 + (lv - 1) * 16}px"><span>${esc(t)}</span><span class="pg2">${p + 1}</span></div>`).join('')
  : '<p class="none">This book has no table of contents.</p>';
$('#toc-list').onclick = ev => { const it = ev.target.closest('.it'); if (it) { closeAll(); jump(toc[+it.dataset.i][3]); } };
const chapterOf = n => { let k = -1; toc.forEach((t, i) => { if (t[3] <= n) k = i; }); return k; };
function chapterUI() {
  const k = chapterOf(cur.n), p = P.get(cur.n);
  $('#lx-chap').textContent = (k >= 0 ? toc[k][1] + ' · ' : '') + (p ? `p. ${p.page + 1}` : '');
  $$('#toc-list .it').forEach(it => it.classList.toggle('on', +it.dataset.i === k));
}
function closeAll() { $('#aa').hidden = true; $('#all').hidden = true; $('#toc').classList.remove('open'); }
function toggleToc() {
  const d = $('#toc'), open = !d.classList.contains('open'); closeAll();
  if (open) { d.classList.add('open'); const on = $('#toc-list .it.on'); on && on.scrollIntoView({block: 'center'}); }
}
$('#b-toc').onclick = toggleToc;
function applyPrefs() {
  list.style.setProperty('--k', prefs.k);
  player.defaultPlaybackRate = player.playbackRate = prefs.rate;
  if (follow) snap = true;               // 字号变了直接对齐，不滑
}
const TSTEP = MODE === 'zh' ? 1 : 10;    // Aa 面板里句间间隔一步几秒：白话书停顿本来就短
function renderAa() {
  const seg = (key, opts, val) => `<span class="seg">${opts.map(([v, l]) => `<button data-k="${key}" data-v="${v}" class="${String(val) === String(v) ? 'on' : ''}">${l}</button>`).join('')}</span>`;
  $('#aa').innerHTML = `
    <div class="r"><span>Text size</span><span class="step"><button class="ib" data-d="k:-0.1">A−</button><b>${Math.round(prefs.k * 100)}%</b><button class="ib" data-d="k:0.1">A+</button></span></div>
    <div class="r"><span>Speed</span><span class="step"><button class="ib" data-d="rate:-0.05">−</button><b>${rateText()}</b><button class="ib" data-d="rate:0.05">+</button></span></div>
    <p class="k">0.7×–1.5×，变速不变调；正文上滚轮也能调。外文书、文言书、白话书各记一份：白话书是母语，默认 1.15×、间隔 2 秒。</p>
    <div class="r"><span>背景音</span>${seg('bed', [[1, 'on'], [0, 'off']], prefs.bed ? 1 : 0)}</div>
    <div class="r"><span>生词标注</span>${seg('gloss', [['std', 'standard'], ['more', 'more'], ['most', 'most']], prefs.gloss)}</div>
    <p class="k">正文上浮着中文的词有多少：「标准」不标常用词和中考词；「多」连中等常用词也标；「更多」中考词也标。切换后当前句立即重标。</p>
    <div class="r"><span>导图</span>${seg('cast', [[1, 'on'], [0, 'off']], prefs.cast ? 1 : 0)}</div>
    <p class="k">右栏${MODE === 'zh' ? '' : 'below the translation'}，按书的类型画：小说、史书、传记画到这一章为止的Person和关系，谁在说话就点亮谁（Narrator句点亮Narrator），句子里提到的人半亮；论述、科普、修行类的书画这一节的关键Concept和关系，读到哪个Concept点亮哪个，没点亮时显示这一节的主旨。悬停看解释，点一个钉住看它的关系。在后台预生成时抽取。</p>
    <div class="r"><span>Tone</span>${seg('tone', [['curve', 'Pitch contour'], ['dots', 'Stress · intonation'], ['tobi', 'Prosody layer'], ['off', 'off']], prefs.tone)}</div>
    <p class="k">按这个音色朗读这句的声音实测：曲线越高声音越高，金点是调核；「Stress · intonation」里 ● 调核、• 重读、∙ Weak form，↘ 降 ↗ 升 ↘↗ 降升 ↗↘ 升降 → 平，| 短停 ‖ 句末，‿ Linking、ɾ 闪音、ʔ 喉塞，下方小刻度是节拍；「Prosody layer」再标 ToBI 目标调（H* !H* L+H* L*）和边界调。灰色表示和教科书规则不同。讲解栏里有这句的Tone说明。</p>
    <div class="r"><span>Pause between sentences</span><span class="step"><button class="ib" data-d="think:-${TSTEP}">−</button><b>${prefs.think}s</b><button class="ib" data-d="think:${TSTEP}">+</button></span></div>
    <p class="k">每句读完停一会儿，想一想再听下一句；长句多停一点。0 秒 = 不停顿。</p>
    <p class="k">Enter / 空格 / 点屏幕 Play · Pause · ←/→ 换句 · 滚轮 语速 ±0.05× · +/− 句间间隔 ±1 秒 · ↑/↓ ±10 秒 · T 目录 · E 讲解 · F Full screen · Esc Back to book</p>`;
}
$('#aa').addEventListener('click', ev => {
  const b = ev.target.closest('button'); if (!b) return;
  if (b.dataset.d) {
    const [k, d] = b.dataset.d.split(':');
    if (k === 'think') return setThink(+d);
    if (k === 'rate') return setRate(+d);
    prefs.k = Math.max(0.8, Math.min(1.5, Math.round((prefs.k + +d) * 10) / 10));
  }
  else if (b.dataset.k === 'bed') prefs.bed = b.dataset.v === '1';
  else if (b.dataset.k === 'gloss') { if (prefs.gloss !== b.dataset.v) { prefs.gloss = b.dataset.v; reglossCur(); } }
  else if (b.dataset.k === 'cast') { prefs.cast = b.dataset.v === '1'; cast = null; castFor = -1; castSync(); }
  else if (b.dataset.k === 'tone') {
    prefs.tone = b.dataset.v;
    const ln = curLine();
    if (prefs.tone === 'off') clearTone(); else if (tone && tone.pr) drawTone(); else if (ln && phase !== 'idle') showTone(ln, ln._t);
  }
  applyPrefs(); savePrefs(); renderAa();
});
$('#b-aa').onclick = () => { const m = $('#aa'), open = m.hidden; closeAll(); if (open) { renderAa(); m.hidden = false; } };
document.addEventListener('mousedown', ev => { if (!ev.target.closest('#aa,#b-aa,#toc,#b-toc,#all,#b-all')) closeAll(); });

// ------------------------------------------------------------------ 进度 · 退出
// 进度存服务端，rev（= 服务端的 last_read）是这个页面上次和服务端对上的版本。别处（mini、另一个标签页）写过之后，
// 停在旧位置的页面存不进去（409），回到前台时也先比一比：别处读到了别的地方、这里又没在朗读，就重新打开接上那一句。
// 正在朗读的页面以自己为准（force）。TAB = 这个页面的编号：隐藏时用 sendBeacon 存，看不到回应、rev 跟不上，
// 服务端认得出那条是这个页面自己写的，就不会把自己当成「别处」、拉回旧位置
const TAB = (crypto.randomUUID ? crypto.randomUUID() : Math.random().toString(36).slice(2) + Date.now().toString(36)).slice(0, 36);
let saveT = 0, started = false, rev = +B.rev || 0, saving = 0, leaving = false;
const saveSoon = () => { clearTimeout(saveT); saveT = setTimeout(save, 1200); };
const here = j => j.tab === TAB || (j.para === cur.n && j.sent === cur.i);   // 服务端那条是这个页面自己的
function save() {
  clearTimeout(saveT); saveT = 0; if (!started) return Promise.resolve(true);
  const body = JSON.stringify({para: cur.n, sent: cur.i, rev, tab: TAB, force: playing}), url = `/api/books/${B.id}/progress`;
  if (document.visibilityState === 'hidden' && navigator.sendBeacon) { navigator.sendBeacon(url, new Blob([body], {type: 'application/json'})); return Promise.resolve(true); }
  saving++;
  return fetch(url, {method: 'POST', headers: {'content-type': 'application/json'}, body, keepalive: true})
    .then(async r => {
      const j = await r.json().catch(() => ({}));
      if (r.ok) { if (j.rev) rev = j.rev; return true; }
      if (r.status !== 409) return true;
      if (here(j)) { rev = j.rev; return true; }        // 服务端那条是自己写的，或正好就是这一句：跟上 rev
      if (!leaving) rejoin();
      return false;
    }).catch(() => true).finally(() => { saving--; });
}
// 别处读到了别的地方：从服务端的进度重新打开本页
function rejoin() {
  started = false; gen++; clearTimeout(thinkT); audio.pause(); dropStream();
  location.replace(`/read/${B.id}?moved=1`);
}
async function sync() {
  if (!started || playing || leaving || saving || saveT) return;
  const j = await api(`/api/books/${B.id}/progress`).catch(() => null);
  if (!j || !(j.rev > rev) || !started || playing || saving || saveT) return;
  if (here(j)) rev = j.rev; else rejoin();
}
addEventListener('pagehide', save);
document.addEventListener('visibilitychange', () => document.visibilityState === 'hidden' ? save() : sync());
addEventListener('focus', sync);
addEventListener('pageshow', ev => ev.persisted && sync());   // 前进 / 后退从缓存里恢复的页面
function exit() { leaving = true; audio.pause(); dropStream(); save().finally(() => { location.href = `/book/${B.id}`; }); }
// Send to mini：本机先停、把这一句存好，mini 的 Chrome 打开同一本书（服务端进度，停在同一句）
async function toMini() {
  const b = $('#b-mini'); if (!b || b.disabled) return;
  if (playing) await toggle();
  b.disabled = true; b.textContent = 'Sending…';
  try {
    await save();
    const j = await api('/api/mini/open', {method: 'POST', json: {book: B.id}});
    toast(`Opened on the mini${j.tunnel === 'new' ? ' (tunnel up)' : ''}`);
  } catch (e) { toast('Could not send: ' + e.message); }
  finally { b.disabled = false; b.textContent = 'Send to mini'; }
}
if (B.mini) $('#b-mini').onclick = toMini;

// ------------------------------------------------------------------ 阅读时长：页面可见且 3 分钟内有操作，或正在朗读（后台也算），才计时
const IDLE = 180e3;
let lastAct = Date.now(), tick = performance.now(), tSec = 0, tListen = 0, todaySec = +B.today || 0;
['keydown', 'mousedown', 'mousemove', 'wheel', 'touchstart'].forEach(t => addEventListener(t, () => { lastAct = Date.now(); }, {capture: true, passive: true}));
const showToday = () => showLen();
setInterval(() => {
  const now = performance.now(), d = Math.min((now - tick) / 1000, 65);   // 后台计时器会被放慢；电脑睡眠醒来那一下不算
  tick = now;
  if (!started) return;
  showToday();
  const looking = document.visibilityState === 'visible' && Date.now() - lastAct < IDLE;
  if (!playing && !looking) return;
  tSec += d; if (playing) tListen += d;
  if (tSec >= 30) sendTime();
}, 1000);
function sendTime() {
  const sec = Math.floor(tSec), listen = Math.min(sec, Math.floor(tListen));
  if (!sec) return;
  tSec -= sec; tListen = Math.max(0, tListen - listen); bookSent += sec;
  const body = JSON.stringify({sec, listen}), url = `/api/books/${B.id}/time`;
  if (document.visibilityState === 'hidden' && navigator.sendBeacon) { navigator.sendBeacon(url, new Blob([body], {type: 'application/json'})); todaySec += sec; return; }
  fetch(url, {method: 'POST', headers: {'content-type': 'application/json'}, body, keepalive: true})
    .then(r => r.ok ? r.json() : null).then(j => { if (j) { todaySec = j.today; showToday(); } }).catch(() => {});
}
addEventListener('pagehide', sendTime);
document.addEventListener('visibilitychange', () => document.visibilityState === 'hidden' && sendTime());

// ------------------------------------------------------------------ 朗读时长：按现在的语速、句间间隔，This chapter / Whole book一共要读多久、还剩多久；本书累计读了多久
// /length 给每段的原速音频秒数（按字数估）、句数、各句词数，这里做成前缀和，O(1) 算任意一段范围。
// 按字数估的音频时长用这个音色实际Play过的句子校准（实测 / 估算，存在本机，越读越准）
let LEN = null, bookSent = 0;
const PK = 'pace.' + prefs.voice, pace = LS.get(PK, {a: 0, e: 0});
const estSec = t => isZh(t) ? t.length / 4.5 : t.length / 14;
function measure(text, dur) {             // 一句读完：记下实测时长（原速）和估算，样本只留最近约一小时
  if (!text || !(dur > 0.3) || !isFinite(dur)) return;
  pace.a += dur; pace.e += estSec(text);
  if (pace.e > 3600) { pace.a *= 3600 / pace.e; pace.e = 3600; }
  LS.set(PK, pace);
}
api(`/api/books/${B.id}/length`).then(j => {
  const pre = xs => { const r = new Float64Array(xs.length + 1); xs.forEach((x, i) => { r[i + 1] = r[i] + x; }); return r; };
  LEN = {A: pre(j.a.map(x => x / 10)), S: pre(j.s), W: pre(j.w), n: j.a.length};
  showLen();
}).catch(() => {});
function spanSec(from, to) {              // 第 [from, to) 段按现在的设置读完要几秒（音频按语速缩放，加每句的思考停顿）
  from = Math.max(0, Math.min(from, LEN.n)); to = Math.max(from, Math.min(to, LEN.n));
  const k = pace.e > 20 ? pace.a / pace.e : 1;
  const a = (LEN.A[to] - LEN.A[from]) * k / prefs.rate, sn = LEN.S[to] - LEN.S[from], w = LEN.W[to] - LEN.W[from];
  return a + (prefs.think > 0 ? sn * prefs.think + 0.1 * w : 0);
}
function leftSec(end) {                   // 从当前这一句（含）到第 end 段之前
  const n = cur.n, sn = LEN.S[n + 1] - LEN.S[n];
  return spanSec(n + 1, end) + spanSec(n, n + 1) * (sn ? Math.max(0, sn - cur.i) / sn : 1);
}
const hm = s => { const m = Math.max(1, Math.round(s / 60)); return m < 60 ? `${m} min` : `${Math.floor(m / 60)} h${m % 60 ? ` ${m % 60} min` : ''}`; };
// 剩余时间各占一格固定宽度，名字长短变了这一格也不挪。一共多久写在悬停里
const seg = (label, left, all) => `<span class="meter" title="${label}: ${hm(left)} left of ${hm(all)}">${label}: ${hm(left)} left</span>`;
let lenKey = '', readKey = '';
function showLen() {
  const spent = (+B.spent || 0) + bookSent + tSec, today = todaySec + tSec;
  const r = spent >= 60 || today >= 60 ? `This book ${hm(spent)}<span class="of">today ${hm(today)}</span>` : '';
  if (r !== readKey) { readKey = r; $('#st-today').innerHTML = r; }
  if (!LEN || !started) return;
  const k = chapterOf(cur.n), a = k >= 0 ? toc[k][3] : 0, b = k + 1 < toc.length ? toc[k + 1][3] : LEN.n;
  const parts = [];
  if (toc.length && b > a) parts.push(seg('This chapter', leftSec(b), spanSec(a, b)));
  parts.push(seg('Whole book', leftSec(LEN.n), spanSec(0, LEN.n)));
  const t = parts.join('');
  if (t === lenKey) return;
  lenKey = t;
  const el = $('#st-len'); el.innerHTML = t;
  el.title = `Estimated at the current speed ${rateText()} with a ${prefs.think}s pause${pace.e > 20 ? ` (calibrated on ${Math.round(pace.e / 60)} min of this voice)` : ' (estimated from word count; calibrated once you have read a few sentences)'}`;
}

// ------------------------------------------------------------------ Whole book预生成：从读到的这一句起把Whole book书的朗读音频（外文书连同译文）做好，存在本机（data/tts），
// 以后反复听不用再生成；换音色另存一份。别的长任务在跑就排队；随时可停，再点接着做，做过的不重做
const allEl = $('#all');
let allT = 0, allLast = null;
const num = n => n.toLocaleString('zh-CN');
async function allPoll() {
  clearTimeout(allT);
  let j; try { j = await api(`/api/books/${B.id}/prep-all?voice=${prefs.voice}&voice_zh=${prefs.voiceZh}`); } catch (e) { j = {error: e.message}; }
  allLast = j; allRender();
  if (j.running || !allEl.hidden) allT = setTimeout(allPoll, j.running ? 5000 : 15000);
}
function allRender() {
  const j = allLast || {}, btn = $('#b-all'), pct = j.total ? Math.floor(j.audio / j.total * 100) : 0;
  btn.textContent = j.running ? `Whole book ${pct}%` : 'Whole book'; btn.classList.toggle('on', !!j.running);
  if (allEl.hidden) return;
  const v = (j.voices || []).filter((x, i) => i === (ZH ? 1 : 0) || LANGS.size > 1).join(' / ');
  let state;
  if (j.error) state = `<span class="warn">${esc(j.error)}</span>`;
  else if (j.waiting) state = `Queued behind: ${esc(j.waiting)} — starts when that finishes`;
  else if (j.running) {
    const rate = j.since ? (j.audio - j.audio0) / ((Date.now() / 1000 - j.since) / 60) : 0;   // 句 / 分钟
    state = rate > 0.2 ? `Generating about ${rate.toFixed(1)} sentences a minute — roughly ${hm((j.total - j.audio) / rate * 60)} to go` : 'Generating…';
  } else if (j.total && j.audio >= j.total) state = 'The whole book is generated and stored locally — replay offline any time';
  else if (j.busy) state = esc(j.busy) + ' — waiting for it to finish';
  else state = j.total ? `${num(j.total - j.audio)} sentences not generated yet` : '';
  allEl.innerHTML = `<div class="r"><span>Whole book预生成</span><b>${esc(v)}</b></div>
    <div class="pbar"><b style="width:${pct}%"></b></div>
    <div class="r"><span>朗读音频</span><span>${num(j.audio || 0)} / ${num(j.total || 0)} 句 · ${pct}%</span></div>
    ${MODE !== 'zh' && j.total ? `<div class="r"><span>${MODE === 'wy' ? 'Classical → modern' : 'Translation'}</span><span>${num(j.tr || 0)} / ${num(j.total)}</span></div>` : ''}
    <p class="k">${state}</p>
    <div class="r">${j.running ? '<button class="btn2" data-a="stop">Stop</button>' : j.total && j.audio >= j.total ? '' : `<button class="btn2 pri" data-a="go">${j.audio ? 'Resume' : 'Generate whole book'}</button>`}</div>
    <p class="k">从读到的这一句往后生成，到书尾再回头补前面；生成好的音频一直存在本机，读完可以反复听，不用再生成。换了音色要另外生成一份。停了再点会接着做，做过的句子不重做。</p>`;
}
allEl.addEventListener('click', async ev => {
  const b = ev.target.closest('button[data-a]'); if (!b) return;
  b.disabled = true;
  try {
    if (b.dataset.a === 'go') { await api(`/api/books/${B.id}/prep-all`, {method: 'POST', json: {para: cur.n, voice: prefs.voice, voice_zh: prefs.voiceZh}}); toast('Whole-book generation started'); }
    else { await api('/api/prep/stop', {method: 'POST', json: {}}); toast('Stopped — press again to resume'); }
  } catch (e) { toast(e.message); }
  allPoll();
});
$('#b-all').onclick = () => { const open = allEl.hidden; closeAll(); if (open) { allEl.hidden = false; allLast ? allRender() : allEl.innerHTML = '<p class="k">Counting the sentences in this book…</p>'; allPoll(); } };
allPoll();

// ------------------------------------------------------------------ 键盘
document.addEventListener('keydown', ev => {
  if (ev.target.closest && ev.target.closest('input,textarea,select,[contenteditable]')) return;
  if (ev.metaKey || ev.ctrlKey || ev.altKey) return;
  const k = ev.key;
  // 按键归我们：不让焦点所在的按钮再被 Enter / 空格点一次，也不让页面滚
  const own = () => { ev.preventDefault(); const a = document.activeElement; if (a && a !== document.body && a.blur) a.blur(); };
  if (k === 'Enter' || k === ' ') { own(); if (!ev.repeat) toggle(); }
  else if (k === 'ArrowRight' || k === 'ArrowLeft') { own(); step(k === 'ArrowRight' ? 1 : -1); }
  else if (k === 'ArrowUp' || k === 'ArrowDown') { own(); setThink(k === 'ArrowUp' ? 10 : -10); }
  else if (k === '+' || k === '=') { own(); setThink(1); }
  else if (k === '-' || k === '_') { own(); setThink(-1); }
  else if (k === 'PageDown' || k === 'PageUp') { own(); manual((k === 'PageDown' ? 1 : -1) * sub.clientHeight * 0.8); }
  else if (k === 'Escape') { if (!$('#aa').hidden || !$('#all').hidden || $('#toc').classList.contains('open')) closeAll(); else exit(); }
  else if (k === 't' || k === 'T') toggleToc();
  else if ((k === 'e' || k === 'E') && MODE !== 'zh') openExplain($('#explain').hidden);
  else if (k === 'f' || k === 'F') toggleFs();
  else if ((k === 'm' || k === 'M') && B.mini) toMini();
});

// ------------------------------------------------------------------ 启动：停在上次那一句，亮着、译文在右边，不出声
function msg(t, retry) {
  const m = $('#msg'); m.hidden = false; sub.hidden = true; $('#tts-grp').hidden = true; $('#status').hidden = true;
  m.innerHTML = `<div class="pp-t">${esc(t)}</div><div class="pp-acts">${retry ? '<button onclick="location.reload()">Retry</button>' : ''}<a href="/book/${B.id}">Back to book</a></div>`;
}
(async () => {
  applyPrefs(); showPace(); showToday();
  if (LS.get('explain', false) && MODE !== 'zh') openExplain(true);
  if (!B.has_text) return msg('This is a scanned PDF with no text layer, so it cannot be read aloud.');
  if (!total) return msg('This book has no readable text.');
  // 重新打开时带的 rev 比服务端旧、位置又不同：那之后别处读到了别的地方，接着别处的读
  const behind = qs.has('rev') && (+B.rev || 0) > (+qs.get('rev') || 0) && B.tab !== qs.get('tab') && (+qs.get('para') !== +B.para || +qs.get('sent') !== +B.sent);
  const st = qs.has('para') && !behind ? {n: +qs.get('para') || 0, i: +qs.get('sent') || 0} : {n: +B.para || 0, i: +B.sent || 0};
  st.n = Math.max(0, Math.min(total - 1, st.n));
  let p;
  try { await around(st.n); p = await fix(st); } catch (e) { return msg('Could not load the text: ' + e.message, true); }
  if (!p) return msg('Nothing here can be read aloud.');
  started = true;
  begin(p, qs.get('play') === '1' && !behind);   // play=1：重载后接着读（版本更新的自动重载带着它）
  if (qs.has('moved') || behind) { const k = chapterOf(cur.n); toast(`Picked up where you left off elsewhere${k >= 0 ? '：' + toc[k][1] : ''}`); }
  if (qs.get('play') === '1' || qs.has('moved') || qs.has('rev')) history.replaceState(null, '', `/read/${B.id}`);
})();
function relaunch(pos, play) {            // 用当前这一句的位置重新打开本页（脚本版本旧了）
  gen++; clearTimeout(thinkT); audio.pause(); dropStream(); cur = pos;   // 存的就是重新打开的那一句
  const known = rev;                       // 隐藏时存进度用 sendBeacon，看不到结果：带上 rev，重新打开时发现别处写过就以服务端为准
  save().then(ok => ok && location.replace(`/read/${B.id}?para=${pos.n}&sent=${pos.i}&rev=${known}&tab=${TAB}${play ? '&play=1' : ''}`));
}
