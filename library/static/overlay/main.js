// 视频图层（library/overlay.py）：OV.load(spec) 装一段朗读，OV.seek(t) 画第 t 秒。
// 所有动画都由 t 算出、不用计时器和 CSS 过渡：同一个 t 永远是同一帧，无头浏览器逐帧截图才稳定。
// spec：{layout, side, title, chapter, start, end, cast:{people, links, narrator},
//        sents:[{para, text, T, toks:[[a, b, t0, t1]], gloss:[{term, a, b, def}], hits:[{a, b, name}], who, zh, zgloss}]}
// layout = panel（默认）：右半边一块面板（正文 + 英英注释 + 人物网），左半边留给讲者；
//          reader：和朗读页一样的两栏铺满全屏，盖在漫画背景上。lang = en（面向全球）：左栏英文，难词下划线，
//          右栏上面是大的人物网、下面一张英英词义卡（gloss）；lang = zh：难词上方浮中文（zgloss [{a, b, zh, za, zb}]），
//          右栏读过的句子的译文（zh）连成一列、下方人物网
// toks 的 a、b 是词在 text 里的位置，t0、t1 是这个词的起止秒数（讲堂流水线的强制对齐）。
const $ = (s, el = document) => el.querySelector(s);
const NS = 'http://www.w3.org/2000/svg';
const svgEl = (tag, attrs = {}) => { const n = document.createElementNS(NS, tag); for (const k in attrs) n.setAttribute(k, attrs[k]); return n; };
const clamp = (x, a = 0, b = 1) => Math.max(a, Math.min(b, x));
const ramp = (t, a, b) => clamp((t - a) / (b - a));
const ease = x => x < .5 ? 2 * x * x : 1 - (-2 * x + 2) ** 2 / 2;
const lerp = (a, b, k) => a + (b - a) * k;
const q2 = x => Math.round(x * 100) / 100;
const GOLD = '#e8c88a', DIMDOT = '#8d8574';
const LEAD = .15, SCROLL = .7, FADE = .4;   // 换句：比第一个词早 0.15 秒开始；滚动 0.7 秒；前后句明暗 0.4 秒

let S = null;

function pane(cls, g) {
  const d = document.createElement('div');
  d.className = 'pane ' + cls;
  Object.assign(d.style, {left: g.x + 'px', top: g.y + 'px', width: g.w + 'px', height: g.h + 'px'});
  return d;
}

// 一句里位置 [a, b) 的内容第一次被读到的时刻：和它重叠的第一个词的 t0
const timeAt = (s, a, b) => (s.toks.find(([x, y]) => x < b && y > a) || [0, 0, s.T])[2];

// ---------------------------------------------------------------- 人物网：和朗读页同一套力导向（static/reader/main.js 的 castLayout），无随机数
function shortName(p) {
  const w = p.name.split(' ');
  if (w.length > 1 && (/^(Mr|Mrs|Ms|Miss|Dr|Sir|Lady|Lord|Madame|Captain|Colonel|Professor|Uncle|Aunt)\.?$/i.test(w[0]) || /['’]s$/.test(w[0]))) return p.name;
  const one = (p.aliases || []).filter(a => !a.includes(' '));
  return [w[0], w[w.length - 1]].find(x => one.includes(x)) || one[0] || w[0];
}
const dotR = p => Math.min(24, Math.sqrt(p.n || 1) * 2 + 8);

function layout(people, links, W, H) {
  const N = people.map((p, i) => ({name: p.name, label: shortName(p), x: W / 2 + Math.cos(i * 2.4) * W * .3, y: H / 2 + Math.sin(i * 2.4) * H * .3, vx: 0, vy: 0}));
  const ix = Object.fromEntries(N.map((n, i) => [n.name, i]));
  const K = Math.max(5000, W * H / Math.max(1, N.length) * .9);
  for (let it = 0; it < 400; it++) {
    N.forEach(a => N.forEach(b => { if (a === b) return; const dx = a.x - b.x, dy = (a.y - b.y) * 1.4, d2 = dx * dx + dy * dy + 40, f = K / d2, d = Math.sqrt(d2); a.vx += dx / d * f; a.vy += dy / d * f * 1.4; }));
    links.forEach(l => { const A = N[ix[l.a]], B = N[ix[l.b]]; if (!A || !B) return; const dx = B.x - A.x, dy = B.y - A.y, d = Math.hypot(dx, dy) || 1, f = (d - 170) * .02; A.vx += dx / d * f; A.vy += dy / d * f; B.vx -= dx / d * f; B.vy -= dy / d * f; });
    N.forEach(n => { n.vx += (W / 2 - n.x) * .003; n.vy += (H / 2 - n.y) * .003; n.x += n.vx *= .82; n.y += n.vy *= .82; n.x = clamp(n.x, 60, W - 60); n.y = clamp(n.y, 26, H - 34); });
  }
  // 撑满画框：人少的时候力导向只占中间一小块。画框又宽又矮，横竖分开放大（各自封顶）再居中
  const xs = N.map(n => n.x), ys = N.map(n => n.y), bw = Math.max(...xs) - Math.min(...xs) || 1, bh = Math.max(...ys) - Math.min(...ys) || 1;
  const kx = Math.min(3, (W - 160) / bw), ky = Math.min(2.2, (H - 76) / bh), cx = (Math.max(...xs) + Math.min(...xs)) / 2, cy = (Math.max(...ys) + Math.min(...ys)) / 2;
  N.forEach(n => { n.x = W / 2 + (n.x - cx) * kx; n.y = H / 2 - 10 + (n.y - cy) * ky; });   // 名字在圆点下面：整体往上提一点
  const LW = n => n.label.length * 10.5 + 16, LH = 40;   // 名字标签框：18px 字约 10.5px 一个字母
  for (let it = 0; it < 80; it++) {
    let moved = false;
    for (let i = 0; i < N.length; i++) for (let j = i + 1; j < N.length; j++) {
      const a = N[i], b = N[j], w = (LW(a) + LW(b)) / 2, dx = b.x - a.x, dy = b.y - a.y;
      if (Math.abs(dx) >= w || Math.abs(dy) >= LH) continue;
      const px = (w - Math.abs(dx)) / 2, py = (LH - Math.abs(dy)) / 2;
      const edge = Math.min(a.x, b.x) <= 60 || Math.max(a.x, b.x) >= W - 60;
      if (px < py && !edge) { const s = Math.sign(dx) || 1; a.x -= px * s; b.x += px * s; } else { const s = Math.sign(dy) || 1; a.y -= py * s; b.y += py * s; }
      moved = true;
    }
    N.forEach(n => { n.x = clamp(n.x, 60, W - 60); n.y = clamp(n.y, 26, H - 34); });
    if (!moved) break;
  }
  return Object.fromEntries(N.map(n => [n.name, n]));
}

function buildCast(c, g, bare = false) {
  const el = pane(bare ? 'cast bare' : 'cast', g);
  el.innerHTML = '<div class="eyebrow">Who’s who</div><svg></svg><div class="who"></div>';
  const svg = $('svg', el), top = 56, W = g.w, H = g.h - top - 56;   // 下面留一行身份说明
  const pos = layout(c.people, c.links, W, H);
  const gr = svgEl('g', {transform: `translate(0 ${top})`});
  svg.append(gr);
  const edges = c.links.filter(l => pos[l.a] && pos[l.b]).map(l => {
    const A = pos[l.a], B = pos[l.b];
    const line = svgEl('line', {x1: A.x, y1: A.y, x2: B.x, y2: B.y, class: 'lk'});
    const rel = svgEl('text', {x: (A.x + B.x) / 2, y: (A.y + B.y) / 2 - 7, class: 'rel'});
    rel.textContent = l.rel || '';
    gr.append(line, rel);
    return {...l, line, rel};
  });
  const nodes = {};
  c.people.forEach(p => {
    const n = pos[p.name], r = dotR(p), g2 = svgEl('g');
    const ring = svgEl('circle', {cx: n.x, cy: n.y, r, fill: 'none', stroke: GOLD, 'stroke-width': 2});
    const dot = svgEl('circle', {cx: n.x, cy: n.y, r});
    const name = svgEl('text', {x: n.x, y: n.y + r + 20, class: 'nm'});
    name.textContent = n.label;
    g2.append(ring, dot, name);
    if (p.name === c.narrator) g2.append(svgEl('circle', {cx: n.x, cy: n.y, r: r + 6, fill: 'none', stroke: 'rgba(255,255,255,.45)', 'stroke-width': 1.2, 'stroke-dasharray': '3 4'}));
    gr.append(g2);
    nodes[p.name] = {p, r, ring, dot, name};
  });
  return {el, edges, nodes, who: $('.who', el), byName: Object.fromEntries(c.people.map(p => [p.name, p]))};
}

// ---------------------------------------------------------------- 装一段
async function load(spec) {
  return spec.layout === 'reader' ? loadReader(spec) : loadPanel(spec);
}

async function loadPanel(spec) {
  const root = $('#ov');
  root.textContent = '';
  const x = spec.side === 'left' ? 80 : 1000;
  const read = pane('read', {x, y: 80, w: 840, h: 600});
  read.innerHTML = '<div class="eyebrow"></div><div class="view"><div class="flow"></div></div><div class="note"><div class="term"></div><div class="def"></div></div>';
  $('.eyebrow', read).textContent = [spec.title, spec.chapter].filter(Boolean).join('  ·  ');
  root.append(read);
  const flow = $('.flow', read);
  let p = null, para = null;
  const sents = spec.sents.map(s => {
    if (s.para !== para || !p) { p = document.createElement('p'); flow.append(p); para = s.para; } else p.append(' ');
    const words = s.toks.map(([a, b, t0, t1], k) => {
      const span = document.createElement('span');
      span.className = 'w';
      span.textContent = s.text.slice(a, b);
      if (k) p.append(' ');
      p.append(span);
      const gl = (s.gloss || []).find(g => a < g.b && b > g.a);
      if (gl) span.classList.add('g');
      return {el: span, t0, t1, tg: gl ? timeAt(s, gl.a, gl.b) : null};
    });
    const notes = (s.gloss || []).map(g => ({...g, tg: timeAt(s, g.a, g.b)}));
    const hits = (s.hits || []).map(h => ({...h, th: timeAt(s, h.a, h.b)}));
    return {...s, words, notes, hits, E: s.toks.length ? s.toks[s.toks.length - 1][3] : s.T};
  });
  const cast = spec.cast && (spec.cast.people || []).length ? buildCast(spec.cast, {x, y: 704, w: 840, h: 296}) : null;
  if (cast) root.append(cast.el);
  await document.fonts.ready;
  const view = $('.view', read), anchor = view.clientHeight * .3;
  sents.forEach(s => { s.y = s.words.length ? Math.max(0, s.words[0].el.offsetTop - anchor) : 0; });
  const notes = sents.flatMap(s => s.notes.map(n => ({...n, E: s.E}))).sort((a, b) => a.tg - b.tg);
  notes.forEach((n, k) => { n.until = Math.min(notes[k + 1] ? notes[k + 1].tg : Infinity, Math.max(n.E + 3, n.tg + 10)); });   // 留够读的时间，下一条来了就换
  S = {mode: 'panel', spec, root, flow, sents, notes, cast, note: $('.note', read), term: $('.note .term', read), def: $('.note .def', read), lastNote: null};
  seek(spec.start ?? 0);
  return {sents: sents.length, notes: notes.length, people: cast ? Object.keys(cast.nodes).length : 0};
}

// ---------------------------------------------------------------- 画第 t 秒；返回这一帧的状态串（相同 = 画面相同，导出时可以复用上一帧）
function seek(t) {
  if (!S) return '';
  return S.mode === 'reader' ? seekReader(t) : seekPanel(t);
}

function seekPanel(t) {
  const {sents, spec} = S, K = [];
  let i = 0;
  sents.forEach((s, k) => { if (s.T - LEAD <= t) i = k; });
  const s = sents[i], a = s.T - LEAD;
  const vis = q2(ramp(t, spec.start - .5, spec.start) * (1 - ramp(t, spec.end, spec.end + .6)));
  S.root.style.opacity = vis;
  K.push(vis);
  if (!vis) return K.join();

  const y0 = i ? sents[i - 1].y : s.y, y = Math.round(lerp(y0, s.y, ease(ramp(t, a, a + SCROLL))));
  S.flow.style.transform = `translateY(${-y}px)`;
  K.push(y);
  const sw = ramp(t, a, a + FADE);
  sents.forEach((x, k) => {
    const base = k < i - 1 ? .32 : k === i - 1 ? lerp(1, .32, sw) : k === i ? lerp(.18, .5, sw) : .18;   // 读过的 · 正在读（未读到的词）· 后面的
    x.words.forEach(w => {
      const op = q2(k === i ? lerp(base, 1, ramp(t, w.t0 - .04, w.t0 + .08)) : base);   // 当前句：读到的词点亮
      w.el.style.opacity = op;
      if (w.tg !== null) w.el.classList.toggle('on', k < i || (k === i && t >= w.tg));
      K.push(op);
    });
  });

  let n = null;
  for (const x of S.notes) if (x.tg <= t && t < x.until + .3) n = x;
  const nop = n ? q2(ramp(t, n.tg, n.tg + .25) * (1 - ramp(t, n.until - .3, n.until))) : 0;
  if (n !== S.lastNote) { S.term.textContent = n ? n.term : ''; S.def.textContent = n ? n.def : ''; S.lastNote = n; }
  S.note.style.opacity = nop;
  K.push(n ? n.tg : '-', nop);

  if (S.cast) castPaint(t, s, a, K);
  return K.join();
}

function castPaint(t, s, a, K) {
  const C = S.cast, sw = ramp(t, a, a + FADE);
  const said = s.hits.filter(h => h.th <= t);
  const lit = new Set(said.map(h => h.name));
  const who = C.nodes[s.who] ? s.who : null;
  Object.values(C.nodes).forEach(nd => {
    const name = nd.p.name, speaking = name === who, men = lit.has(name);
    const on = speaking || men ? 1 : .55;
    nd.dot.setAttribute('fill', speaking ? GOLD : men ? '#fff' : DIMDOT);
    nd.name.setAttribute('fill', speaking ? GOLD : '#fff');
    const op = q2(lerp(.55, on, speaking ? sw : 1));
    nd.dot.style.opacity = op;
    nd.name.style.opacity = q2(speaking || men ? 1 : .6);
    const h = said.filter(x => x.name === name).pop();          // 名字刚被读到：金色圆环往外扩 0.7 秒
    const k = h ? ramp(t, h.th, h.th + .7) : 1;
    nd.ring.setAttribute('r', q2(nd.r + 3 + 16 * k));
    nd.ring.style.opacity = q2(1 - k);
    K.push(op, nd.ring.style.opacity);
  });
  const focus = said.length ? said[said.length - 1].name : who;   // 最近读到的人（没人被提到就是说话的人）：下面一行写他的身份，关系标签只标他的
  C.edges.forEach(e => {
    const ends = [e.a, e.b], near = ends.filter(x => x === who || lit.has(x)).length;
    const op = near === 2 ? .75 : near === 1 ? .32 : .14;
    e.line.style.opacity = op;
    e.rel.style.opacity = near === 2 && ends.includes(focus) && e.rel.textContent ? .9 : 0;
    K.push(op, e.rel.style.opacity);
  });
  const p = focus && C.byName[focus];
  const html = p ? `<b>${esc(p.name)}</b>${esc(p.role || '')}` : '';
  if (C.who.innerHTML !== html) C.who.innerHTML = html;
  K.push(focus || '-');
}

// ---------------------------------------------------------------- reader 布局：朗读页的样子（static/reader.css 的 .sp / .ln / .gl / .si）
const RL = {x: 110, w: 1010, top: 96, bottom: 1030}, RR = {x: 1210, w: 620};   // 左栏正文、右栏译文和人物网

async function loadReader(spec) {
  const root = $('#ov');
  root.textContent = '';
  root.className = 'rdr';
  const zh = spec.lang === 'zh';
  root.innerHTML = '<div class="scrim"></div><div class="top"></div>' +
    `<div class="lv" style="left:${RL.x}px;width:${RL.w}px;top:${RL.top}px;bottom:${1080 - RL.bottom}px"><div class="flow"></div></div>` +
    (zh ? `<div class="rv" style="left:${RR.x}px;width:${RR.w}px"><div class="list"></div></div>`
        : `<div class="card" style="left:${RR.x}px;width:${RR.w}px"><div class="term"></div><div class="def"></div></div>`);
  $('.top', root).innerHTML = `<b>${esc(spec.title || '')}</b><span>${esc(spec.chapter || '')}</span>`;
  const flow = $('.lv .flow', root), list = zh ? $('.rv .list', root) : null;
  let p = null, para = null;
  const sents = spec.sents.map(s => {
    if (s.para !== para || !p) { p = document.createElement('p'); flow.append(p); para = s.para; } else p.append(' ');
    const zg = zh ? s.zgloss || [] : [];
    const words = s.toks.map(([a, b, t0, t1], k) => {
      if (k) p.append(' ');
      const span = document.createElement('span');
      span.className = 'w';
      span.textContent = s.text.slice(a, b);
      p.append(span);
      return {el: span, a, b, t0, t1};
    });
    const marks = zg.map(g => {                       // 难词：从它的第一个词起，中文浮在上方（和朗读页 .gl .gt 一样）
      const w = words.find(w => w.a < g.b && w.b > g.a);
      if (!w) return null;
      words.filter(x => x.a < g.b && x.b > g.a).forEach(x => x.el.classList.add('gl'));
      let at = w.el;
      if (g.a > w.a && g.a < w.b && !w.el.querySelector('.wt')) {   // 注释从词中间开始（fool—that’s）：中文对准后半截
        const k = g.a - w.a, word = s.text.slice(w.a, w.b), held = [...w.el.querySelectorAll('.gt')];   // 前半截已有的注释留在前半截
        const head = document.createElement('span'), tail = document.createElement('span');
        head.className = tail.className = 'wt';
        head.textContent = word.slice(0, k);
        tail.textContent = word.slice(k);
        head.append(...held);
        w.el.replaceChildren(head, tail);
        at = tail;
      }
      const gt = document.createElement('span');
      gt.className = 'gt';
      gt.textContent = g.zh;
      at.append(gt);
      return gt;
    }).filter(Boolean);
    let item = null;
    if (zh) {                                         // 右栏这一句的译文；难词对应的中文片段下划线
      item = document.createElement('div');
      item.className = 'si';
      const tr = s.zh || '';
      let html = '', at = 0;
      zg.filter(g => g.zb > g.za).sort((x, y) => x.za - y.za).forEach(g => {
        if (g.za < at) return;
        html += esc(tr.slice(at, g.za)) + `<span class="zg">${esc(tr.slice(g.za, g.zb))}</span>`;
        at = g.zb;
      });
      item.innerHTML = html + esc(tr.slice(at));
      list.append(item);
    } else {                                          // 英文版：难词下划线，释义在右栏的词义卡里
      (s.gloss || []).forEach(g => words.filter(x => x.a < g.b && x.b > g.a).forEach(x => x.el.classList.add('gl')));
    }
    const notes = zh ? [] : (s.gloss || []).map(g => ({...g, tg: timeAt(s, g.a, g.b)}));
    const tone = s.pros ? svgEl('svg', {class: 'tone', style: 'position:absolute;left:0;top:0'}) : null;   // 当前句的音高曲线（朗读页的 drawTone，library/prosody.py 的实测）；先绝对定位，不占正文的高
    if (tone) flow.append(tone);
    const hits = (s.hits || []).map(h => ({...h, th: timeAt(s, h.a, h.b)}));
    return {...s, words, marks, item, notes, hits, para, tone, E: s.toks.length ? s.toks[s.toks.length - 1][3] : s.T};
  });
  const cg = zh ? {x: RR.x - 20, y: 610, w: RR.w + 40, h: 400} : {x: RR.x - 20, y: 90, w: RR.w + 40, h: 660};   // 英文版：人物网占右栏上部
  const cast = spec.cast && (spec.cast.people || []).length ? buildCast(spec.cast, cg, true) : null;
  if (cast) root.append(cast.el);
  await document.fonts.ready;
  const lh = RL.bottom - RL.top, rh = 470;
  sents.forEach(s => {
    s.y = s.words.length ? Math.max(0, s.words[0].el.offsetTop - lh * .38) : 0;   // 当前句停在左栏 38% 高处（朗读页同样）
    s.ry = s.item ? Math.max(0, s.item.offsetTop - rh * .3) : 0;
  });
  const notes = sents.flatMap(s => s.notes.map(n => ({...n, E: s.E}))).sort((a, b) => a.tg - b.tg);
  notes.forEach((n, k) => { n.until = Math.min(notes[k + 1] ? notes[k + 1].tg : Infinity, Math.max(n.E + 3, n.tg + 10)); });
  sents.forEach(drawTone);
  const card = $('.card', root);
  S = {mode: 'reader', spec, root, flow, list, sents, cast, notes, card, term: card && $('.term', card), def: card && $('.def', card), lastNote: null};
  seek(spec.start ?? 0);
  return {sents: sents.length, notes: zh ? sents.reduce((n, s) => n + s.marks.length, 0) : notes.length, people: cast ? Object.keys(cast.nodes).length : 0};
}

function seekReader(t) {
  const {sents, spec} = S, K = [];
  let i = 0;
  sents.forEach((s, k) => { if (s.T - LEAD <= t) i = k; });
  const s = sents[i], a = s.T - LEAD, sw = ramp(t, a, a + FADE);
  const vis = q2(ramp(t, spec.start - .5, spec.start) * (1 - ramp(t, spec.end, spec.end + .6)));
  S.root.style.opacity = vis;
  K.push(vis);
  if (!vis) return K.join();
  const mv = ease(ramp(t, a, a + SCROLL));
  const y = Math.round(lerp(i ? sents[i - 1].y : s.y, s.y, mv)), ry = Math.round(lerp(i ? sents[i - 1].ry : s.ry, s.ry, mv));
  S.flow.style.transform = `translateY(${-y}px)`;
  if (S.list) S.list.style.transform = `translateY(${-ry}px)`;
  K.push(y, ry);
  sents.forEach((x, k) => {
    // 朗读页：当前句未读的词 .66、读过纯白、正在读的词带柔光；同段别的句子 .36，别的段落 .22
    const base = k === i ? .66 : x.para === s.para ? .36 : .22;
    const prevBase = k === i - 1 ? lerp(1, base, sw) : base;
    x.words.forEach(w => {
      const op = q2(k === i ? lerp(.66, 1, ramp(t, w.t0 - .04, w.t0 + .08)) : prevBase);
      const now = k === i && t >= w.t0 - .04 && t < w.t1;
      w.el.style.opacity = op;
      w.el.classList.toggle('now', now);
      K.push(op, now ? 1 : 0);
    });
    const gop = q2(k === i ? sw : k === i - 1 ? 1 - sw : 0);       // 难词中文只浮在当前句上
    x.marks.forEach(m => { m.style.opacity = gop; });
    x.words.forEach(w => w.el.classList.toggle('lit', k === i || k === i - 1 && sw < 1));
    if (x.item) {
      const iop = q2(k < i ? .3 : k === i ? lerp(i ? 0 : 1, 1, sw) : 0);   // 右栏：读过的译文变淡，没读到的还不出现
      x.item.style.opacity = k === i - 1 ? q2(lerp(1, .3, sw)) : iop;
      x.item.classList.toggle('cur', k === i);
      K.push(gop, x.item.style.opacity);
    } else {
      x.words.forEach(w => { if (w.el.classList.contains('gl')) w.el.classList.toggle('on', k < i || (k === i && t >= w.t0)); });
    }
  });
  sents.forEach((x, k) => {                         // 音调线只在当前句上：换句时淡入淡出；小白点跟着声音走
    if (!x.tone) return;
    const op = q2(k === i ? sw : k === i - 1 ? 1 - sw : 0);
    x.tone.style.opacity = op;
    if (op && x.tdot) {
      const ps = x.tpts, on = k === i && ps.length && t >= ps[0].t - .05 && t <= ps[ps.length - 1].t + .1;
      x.tdot.setAttribute('visibility', on ? 'visible' : 'hidden');
      if (on) {
        let j = 0;
        while (j + 1 < ps.length && ps[j + 1].t <= t) j++;
        const p = ps[j], q = ps[j + 1], f = q && q.line === p.line && q.t > p.t ? clamp((t - p.t) / (q.t - p.t)) : 0;
        x.tdot.setAttribute('cx', q2(p.x + (q ? (q.x - p.x) * f : 0))); x.tdot.setAttribute('cy', q2(p.y + (q ? (q.y - p.y) * f : 0)));
        K.push(x.tdot.getAttribute('cx'), x.tdot.getAttribute('cy'));
      }
    }
    K.push(op);
  });
  if (S.card) {                                     // 英英词义卡：词被读到时出现，留够读的时间，下一条来了就换
    let n = null;
    for (const x of S.notes) if (x.tg <= t && t < x.until + .3) n = x;
    const op = n ? q2(ramp(t, n.tg, n.tg + .25) * (1 - ramp(t, n.until - .3, n.until))) : 0;
    if (n !== S.lastNote) { S.term.textContent = n ? n.term : ''; S.def.textContent = n ? n.def : ''; S.lastNote = n; }
    S.card.style.opacity = op;
    K.push(n ? n.tg : '-', op);
  }
  if (S.cast) castPaint(t, s, a, K);
  return K.join();
}

// 音高曲线：每个音节取实测音高峰值一个点，按短语连成平滑的波浪线，换行和短语边界处断开；调核（主重音）一个金点。
// 和朗读页（static/reader/main.js drawTone）同一画法；位置由时间换算：落在哪个词里，就按它在这个词里的时间比例取 x
function drawTone(s) {
  if (!s.tone || !s.pros) return;
  const fr = s.tone.parentElement.getBoundingClientRect(), fs = 40;
  const box = s.words.map(w => { const r = w.el.getBoundingClientRect(); return {x0: r.left - fr.left, x1: r.right - fr.left, top: r.top - fr.top, bot: r.bottom - fr.top}; });
  Object.assign(s.tone.style, {position: 'absolute', left: 0, top: 0, overflow: 'visible', pointerEvents: 'none', opacity: 0});
  s.tone.setAttribute('width', fr.width); s.tone.setAttribute('height', fr.height);
  const at = t => {
    for (let k = 0; k < s.words.length; k++) {
      const w = s.words[k], b = box[k];
      if (t >= w.t0 && t <= w.t1) return {x: b.x0 + (w.t1 > w.t0 ? (t - w.t0) / (w.t1 - w.t0) : .5) * (b.x1 - b.x0), b};
      const n = s.words[k + 1], nb = box[k + 1];
      if (n && t > w.t1 && t < n.t0) return Math.abs(nb.top - b.top) < 2 ? {x: b.x1 + (t - w.t1) / (n.t0 - w.t1) * (nb.x0 - b.x1), b} : null;
    }
    return null;
  };
  // 和朗读页一样画在字身里、垫在字下面一层（.tone z-index:-1），线很淡，只是一层若有若无的起伏
  const yOf = (st, b) => { const h = b.bot - b.top, v = clamp(st, -6, 6); return b.top + h * .1 + (1 - (v + 6) / 12) * h * .8; };
  const W = s.pros.words || [], ends = new Set((s.pros.phrases || []).map(ph => ph.b));
  const segs = []; let seg = [], line = null, last = null; s.tpts = [];
  const push = (t, st) => {
    const m = at(t); if (!m || st == null) return;
    if (line != null && Math.abs(m.b.top - line) > 2) { if (seg.length > 1) segs.push(seg); seg = []; }
    const pt = [m.x, yOf(st, m.b)]; seg.push(pt); line = m.b.top; last = st; s.tpts.push({t, x: pt[0], y: pt[1], line});
  };
  W.forEach((w, k) => {
    (w.sy || []).forEach(([s0, s1, , pk]) => push((s0 + s1) / 2, pk));
    if (w.nuc && w.tune_st != null && last != null) push(w.e - .02, last + w.tune_st * .7);
    if (ends.has(k)) { if (seg.length > 1) segs.push(seg); seg = []; line = null; }
  });
  if (seg.length > 1) segs.push(seg);
  segs.forEach(sg => {
    let d = `M${sg[0][0].toFixed(1)},${sg[0][1].toFixed(1)}`;
    for (let i = 1; i < sg.length; i++) { const [a, b] = sg[i - 1], [c, e] = sg[i], m = ((a + c) / 2).toFixed(1); d += ` C${m},${b.toFixed(1)} ${m},${e.toFixed(1)} ${c.toFixed(1)},${e.toFixed(1)}`; }
    s.tone.append(svgEl('path', {d, class: 'tc-soft', 'stroke-width': (fs * .32).toFixed(1)}), svgEl('path', {d, class: 'tc'}));
  });
  W.forEach(w => {
    if (!w.nuc) return;
    const sy = (w.sy || []).find(x => x[2] === 1) || (w.sy || [])[0]; if (!sy || sy[3] == null) return;
    const m = at((sy[0] + sy[1]) / 2); if (m) s.tone.append(svgEl('circle', {cx: m.x, cy: yOf(sy[3], m.b), r: fs * .11, class: 'tn'}));
  });
  s.tdot = svgEl('circle', {r: fs * .08, class: 'td', visibility: 'hidden'}); s.tone.append(s.tdot);
}

const esc = s => String(s).replace(/[&<>"]/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;'}[c]));

window.OV = {load, seek};
