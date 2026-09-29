/*  hindi_input.js - Hindi (Devanagari) typing helper for any <input> or <textarea>.

    Type in English letters; a popup offers Devanagari spellings for the word at the caret.
    Space / Enter / Tab / 1-5 / click picks one, arrows move, Esc keeps the English word.
    Words finished before the suggestions arrive are swapped in place when they do.
    An optional on-screen Devanagari keyboard fixes single letters/matras.

    Usage
      HindiInput.configure({ provider: word => Promise<string[]> });   // where candidates come from
      HindiInput.attach(inputEl, { enabled: true, keyboard: true, key: 'unique-id' });
      await HindiInput.flush();          // before reading values programmatically (pending swaps)
      HindiInput.closeAll();             // before re-rendering a form

    Providers
      Any function returning candidates for one word/phrase. `HindiInput.providers.google` calls
      Google Input Tools directly from the browser (works where CORS allows); a page with a
      backend can proxy instead (the e-registration GUI uses its Python side: hindi_input.py).

    No dependencies. Styles are injected once; every class is prefixed "hi-". Colours use the
    host page's CSS variables (--card, --border, --accent, --muted, --hover, --bg, --text)
    with neutral fallbacks, so it fits any theme.
*/
(function (global) {
  'use strict';

  const CSS = `
.hi-wrap{display:block;min-width:0}
.hi-field{position:relative}
.hi-tools{display:flex;gap:6px;align-items:center;margin-top:4px;flex-wrap:wrap}
.hi-btn{font:inherit;font-size:12px;line-height:1.2;padding:2px 8px;border-radius:6px;border:1px solid var(--border,#d0d4da);background:var(--card,#fff);color:var(--muted,#555);cursor:pointer}
.hi-btn:hover{background:var(--hover,#eef3ff)}
.hi-btn.hi-on{border-color:var(--accent,#2563eb);color:var(--accent,#2563eb);font-weight:600}
.hi-hint{font-size:11.5px;color:var(--muted,#777)}
.hi-suggest{position:absolute;left:0;top:100%;z-index:50;min-width:220px;max-width:100%;margin-top:2px;padding:4px;background:var(--card,#fff);border:1px solid var(--border,#d0d4da);border-radius:8px;box-shadow:0 8px 24px rgba(0,0,0,.18)}
.hi-opt{display:flex;gap:10px;align-items:baseline;padding:5px 8px;border-radius:6px;cursor:pointer;font-size:17px;line-height:1.35;color:var(--text,#222)}
.hi-opt.hi-sel,.hi-opt:hover{background:var(--hover,#eef3ff)}
.hi-num{font-size:11px;color:var(--muted,#777);min-width:10px}
.hi-opt.hi-roman{font-size:13px;color:var(--muted,#777)}
.hi-kbd{margin-top:6px;padding:6px;border:1px solid var(--border,#d0d4da);border-radius:8px;background:var(--bg,#f6f7f9);display:flex;flex-direction:column;gap:3px}
.hi-row{display:flex;gap:3px;flex-wrap:wrap}
.hi-key{font:inherit;font-size:16px;line-height:1;min-width:30px;padding:5px 4px;border-radius:6px;border:1px solid var(--border,#d0d4da);background:var(--card,#fff);color:var(--text,#222);cursor:pointer}
.hi-key:hover{background:var(--hover,#eef3ff)}
.hi-key.hi-wide{min-width:64px;font-size:12px;color:var(--muted,#555)}
.hi-kbd[hidden],.hi-hint[hidden],.hi-suggest[hidden],.hi-tools[hidden]{display:none!important}
`;

  // Rows are kept to <= 11 keys so they fit a ~420px column without wrapping mid-row.
  const KEY_ROWS = [
    ['अ', 'आ', 'इ', 'ई', 'उ', 'ऊ', 'ए', 'ऐ', 'ओ', 'औ', 'ऋ'],          // vowels
    ['ा', 'ि', 'ी', 'ु', 'ू', 'े', 'ै', 'ो', 'ौ', 'ृ'],               // matras
    ['ं', 'ँ', 'ः', '्', 'ऽ', '।', '॰'],                            // signs, halant, danda
    ['क', 'ख', 'ग', 'घ', 'ङ', 'च', 'छ', 'ज', 'झ', 'ञ'],
    ['ट', 'ठ', 'ड', 'ढ', 'ण', 'त', 'थ', 'द', 'ध', 'न'],
    ['प', 'फ', 'ब', 'भ', 'म', 'य', 'र', 'ल', 'व'],
    ['श', 'ष', 'स', 'ह', 'क्ष', 'त्र', 'ज्ञ', 'श्र'],
    ['ड़', 'ढ़', 'ज़', 'फ़', 'क़', 'ख़', 'ग़'],                            // nukta forms
  ];

  const cfg = { provider: null, maxSuggestions: 5, debounce: 140, minLength: 2, onError: null };   // 1-letter words (S/o, W/o) stay as typed
  const prefs = new Map();      // key -> { enabled, keyboard }
  const cache = new Map();      // lower-cased word -> candidates
  const pending = new Set();    // in-flight "swap when ready" promises
  let cur = null;               // open popup: { input, popup, start, end, word, cands, idx }
  let timer = null, seq = 0, styled = false, errorShown = false;

  const h = (tag, cls, text) => { const n = document.createElement(tag); if (cls) n.className = cls; if (text !== undefined) n.textContent = text; return n; };
  const isRoman = ch => /[A-Za-z]/.test(ch || '');

  function injectStyle() {
    if (styled || document.getElementById('hi-style')) { styled = true; return; }
    const s = h('style'); s.id = 'hi-style'; s.textContent = CSS; document.head.appendChild(s); styled = true;
  }

  function fire(input) {
    const iv = new Event('input', { bubbles: true }); iv.hiSynthetic = true; input.dispatchEvent(iv);
    input.dispatchEvent(new Event('change', { bubbles: true }));
  }

  function wordAtCaret(input) {
    const v = input.value, end = input.selectionStart;
    if (end === null || end === undefined || input.selectionEnd !== end) return null;
    let start = end;
    while (start > 0 && isRoman(v[start - 1])) start--;
    if (end - start < cfg.minLength) return null;
    return { start, end, word: v.slice(start, end) };
  }

  async function candidates(word) {
    const key = word.toLowerCase();
    if (cache.has(key)) return cache.get(key);
    let list = [];
    try {
      if (!cfg.provider) throw new Error('HindiInput: no provider configured');
      list = (await cfg.provider(word)) || [];
    } catch (e) {
      if (!errorShown) { errorShown = true; (cfg.onError || console.warn)('Hindi suggestions unavailable: ' + (e && e.message || e)); }
      list = [];
    }
    list = list.filter((c, i) => c && list.indexOf(c) === i).slice(0, cfg.maxSuggestions);
    cache.set(key, list);
    return list;
  }

  // ---- suggestion popup -----------------------------------------------------------------
  function close() { if (cur) { cur.popup.remove(); cur = null; } }

  function open(input, w, list) {
    close();
    if (!list.length) return;
    const cands = list.slice(); if (!cands.includes(w.word)) cands.push(w.word);
    const popup = h('div', 'hi-suggest');
    cands.forEach((c, i) => {
      const opt = h('div', 'hi-opt' + (i === 0 ? ' hi-sel' : '') + (c === w.word ? ' hi-roman' : ''));
      opt.append(h('span', 'hi-num', String(i + 1)), document.createTextNode(c));
      opt.addEventListener('mousedown', e => { e.preventDefault(); commit(i, ' '); });   // keep focus in the field
      popup.append(opt);
    });
    input.parentNode.append(popup);
    cur = { input, popup, start: w.start, end: w.end, word: w.word, cands, idx: 0 };
  }

  function highlight(i) {
    if (!cur) return;
    cur.idx = (i + cur.cands.length) % cur.cands.length;
    cur.popup.querySelectorAll('.hi-opt').forEach((o, k) => o.classList.toggle('hi-sel', k === cur.idx));
  }

  function commit(i, suffix) {
    if (!cur) return;
    const { input, start, end, cands } = cur;
    const text = cands[i === undefined ? cur.idx : i] + (suffix || '');
    close();
    input.setRangeText(text, start, end, 'end');
    fire(input);
  }

  function schedule(input) {
    clearTimeout(timer);
    const w = wordAtCaret(input);
    if (!w) { close(); return; }
    timer = setTimeout(async () => {
      const my = ++seq;
      const list = await candidates(w.word);
      if (my !== seq) return;                                   // a newer keystroke superseded this
      const now = wordAtCaret(input);
      if (!now || now.word !== w.word || document.activeElement !== input) return;
      open(input, now, list);
    }, cfg.debounce);
  }

  // A word was finished (space, punctuation, blur) before suggestions arrived: swap it in place later.
  function replaceLater(input, w) {
    if (!w) return;
    const p = (async () => {
      const list = await candidates(w.word);
      const best = list[0];
      if (!best || best === w.word) return;
      if (input.value.slice(w.start, w.end) !== w.word) return;  // text moved on meanwhile
      input.setRangeText(best, w.start, w.end, 'preserve');
      fire(input);
    })();
    pending.add(p);
    p.finally(() => pending.delete(p));
  }

  function onKeydown(e, p) {
    const input = e.target;
    if (!p.enabled) return;
    if (cur && cur.input === input) {
      switch (e.key) {
        case 'ArrowDown': e.preventDefault(); highlight(cur.idx + 1); return;
        case 'ArrowUp': e.preventDefault(); highlight(cur.idx - 1); return;
        case 'Escape': e.preventDefault(); close(); return;
        case 'Enter': case 'Tab': e.preventDefault(); commit(cur.idx, ''); return;
        case ' ': e.preventDefault(); commit(cur.idx, ' '); return;
        default:
      }
      if (/^[1-9]$/.test(e.key) && Number(e.key) <= cur.cands.length) { e.preventDefault(); commit(Number(e.key) - 1, ' '); return; }
      if (/^[,.;:!?()\-\/]$/.test(e.key)) { e.preventDefault(); commit(cur.idx, e.key); return; }
      return;
    }
    if (e.key === ' ' || e.key === 'Enter' || e.key === 'Tab' || /^[,.;:!?()\-\/]$/.test(e.key)) {
      replaceLater(input, wordAtCaret(input));                  // boundary typed before the popup opened
    }
  }

  // ---- on-screen keyboard ---------------------------------------------------------------
  function insert(input, text) {
    input.focus();
    const s = input.selectionStart ?? input.value.length, e = input.selectionEnd ?? s;
    input.setRangeText(text, s, e, 'end');
    fire(input);
  }
  function backspace(input) {
    input.focus();
    const s = input.selectionStart ?? input.value.length, e = input.selectionEnd ?? s;
    if (s !== e) input.setRangeText('', s, e, 'end');
    else if (s > 0) input.setRangeText('', s - 1, s, 'end');
    fire(input);
  }
  function buildKeyboard(input, onClose) {
    const kbd = h('div', 'hi-kbd'); kbd.hidden = true;
    const key = (label, act, wide) => {
      const b = h('button', 'hi-key' + (wide ? ' hi-wide' : ''), label); b.type = 'button';
      b.addEventListener('mousedown', e => { e.preventDefault(); act(); });
      return b;
    };
    for (const row of KEY_ROWS) { const r = h('div', 'hi-row'); for (const ch of row) r.append(key(ch, () => insert(input, ch))); kbd.append(r); }
    const last = h('div', 'hi-row');
    last.append(key('Space', () => insert(input, ' '), true), key('Backspace', () => backspace(input), true), key('Close', onClose, true));
    kbd.append(last);
    return kbd;
  }

  // ---- public API -----------------------------------------------------------------------
  function attach(input, opts = {}) {
    if (!input || input.hindiInput) return input && input.hindiInput;
    injectStyle();
    const key = opts.key || input.id || input.name || ('hi_' + Math.random().toString(36).slice(2));
    const p = prefs.get(key) || { enabled: opts.enabled !== false, keyboard: !!opts.keyboardOpen };
    prefs.set(key, p);
    const showKeyboardButton = opts.keyboard !== false;

    const wrap = h('div', 'hi-wrap'), field = h('div', 'hi-field');
    input.parentNode.insertBefore(wrap, input);
    wrap.append(field); field.append(input);

    const tools = h('div', 'hi-tools');
    const bHindi = h('button', 'hi-btn', 'अ Hindi typing'); bHindi.type = 'button';
    bHindi.title = 'Type in English letters and pick the Hindi spelling from the suggestions';
    const bKbd = h('button', 'hi-btn', '⌨ Keyboard'); bKbd.type = 'button'; bKbd.title = 'On-screen Devanagari keyboard';
    const hint = h('span', 'hi-hint', 'Space / Enter / 1–5 pick a suggestion · Esc keeps English');
    tools.append(bHindi); if (showKeyboardButton) tools.append(bKbd); tools.append(hint);
    wrap.append(tools);
    const kbd = buildKeyboard(input, () => { p.keyboard = false; sync(); input.focus(); });
    wrap.append(kbd);

    const sync = () => {
      bHindi.classList.toggle('hi-on', p.enabled); hint.hidden = !p.enabled;
      bKbd.classList.toggle('hi-on', p.keyboard); kbd.hidden = !p.keyboard;
    };
    bHindi.addEventListener('click', () => { p.enabled = !p.enabled; if (!p.enabled) close(); sync(); input.focus(); });
    bKbd.addEventListener('click', () => { p.keyboard = !p.keyboard; sync(); input.focus(); });

    input.addEventListener('input', e => { if (p.enabled && !e.hiSynthetic) schedule(input); });
    input.addEventListener('keydown', e => onKeydown(e, p));
    input.addEventListener('blur', () => {
      if (cur && cur.input === input) commit(cur.idx, '');
      else if (p.enabled) replaceLater(input, wordAtCaret(input));
    });
    sync();

    const ctl = {
      get enabled() { return p.enabled; },
      setEnabled(v) { p.enabled = !!v; if (!v) close(); sync(); },
      showKeyboard(v) { p.keyboard = !!v; sync(); },
      detach() { close(); wrap.parentNode.insertBefore(input, wrap); wrap.remove(); delete input.hindiInput; },
    };
    input.hindiInput = ctl;
    return ctl;
  }

  function configure(options) { Object.assign(cfg, options || {}); return HindiInput; }
  function flush() { return Promise.allSettled(Array.from(pending)).then(() => undefined); }
  function closeAll() { clearTimeout(timer); close(); }

  const providers = {
    // Direct call to Google Input Tools (needs a page whose origin the endpoint accepts).
    google: async (word, num = 5) => {
      const url = 'https://inputtools.google.com/request?' + new URLSearchParams({ text: word, itc: 'hi-t-i0-und', num: String(num), cp: '0', cs: '1', ie: 'utf-8', oe: 'utf-8', app: 'hindi_input' });
      const data = await (await fetch(url)).json();
      if (!data || data[0] !== 'SUCCESS') throw new Error('Google Input Tools: ' + (data && data[0]));
      return data[1].flatMap(item => item[1]);
    },
  };

  const HindiInput = { attach, configure, flush, closeAll, providers, cache };
  global.HindiInput = HindiInput;
})(window);
