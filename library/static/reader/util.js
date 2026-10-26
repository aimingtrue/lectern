// 朗读页共用的小工具：选择器、转义、本地设置、取接口、提示条、中文判断、分词。纯函数，不碰页面状态。
export const $ = (s, r = document) => r.querySelector(s);
export const $$ = (s, r = document) => [...r.querySelectorAll(s)];
export const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
export const LS = {
  get(k, d) { try { const v = localStorage.getItem('lib.' + k); return v == null ? d : JSON.parse(v); } catch (e) { return d; } },
  set(k, v) { try { localStorage.setItem('lib.' + k, JSON.stringify(v)); } catch (e) {} },
};
export const api = async (url, opt = {}) => {
  if (opt.json !== undefined) { opt.body = JSON.stringify(opt.json); opt.headers = {'content-type': 'application/json'}; delete opt.json; }
  const r = await fetch(url, opt);
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.detail || ('HTTP ' + r.status));
  return j;
};
export const toast = msg => { const t = $('.toast'); t.textContent = msg; t.classList.add('show'); clearTimeout(t._h); t._h = setTimeout(() => t.classList.remove('show'), 1800); };

// 中文句：汉字 ×3 ≥ 拉丁字母（和服务端 text.is_zh 一致）
export const HAN = /[\u3400-\u9fff\uf900-\ufaff]/g;
export const isZh = t => { const h = (t.match(HAN) || []).length; return h > 0 && h * 3 >= (t.match(/[A-Za-z]/g) || []).length; };

export const reEsc = t => t.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
// 和 library/prosody.py 的 TOK 一致：英文词（含 's、n't）或单个汉字
export const tokensOf = text => [...text.matchAll(/[A-Za-z0-9]+(?:['’][A-Za-z]+)*|[\u3400-\u9fff\uf900-\ufaff]/g)].map(m => ({a: m.index, b: m.index + m[0].length, w: m[0]}));
