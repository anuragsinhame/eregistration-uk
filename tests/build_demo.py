"""Build tests/demo_harness.html: the GUI running against a mock site with a fake Python bridge.

    python tests/build_demo.py            # then open tests/demo_harness.html in any browser
    python tests/build_demo.py --artifact # body-only variant (for publishing as a Claude artifact)

Left pane: tests/mock_buyerwise.html (an ASP.NET-style Buyer Wise page with a captcha, dropdowns,
a results grid and a menu). Right pane: gui.html with hindi_input.js inlined and window.pywebview
replaced by a fake that answers like uk_ereg_gui.Api would (login, menu, report, save...).
Useful for working on gui.html / hindi_input.js without Playwright or the real site.
"""
import base64
import json
import pathlib
import re
import sys

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
import hindi_input  # noqa: E402

mock = (HERE / "mock_buyerwise.html").read_text(encoding="utf-8")
py = (ROOT / "uk_ereg_gui.py").read_text(encoding="utf-8")
scan = re.search(r'SCAN_JS = r"""(.*?)"""', py, re.S).group(1)
mock_js = mock.replace("</body>", "<script>window.__scan = " + scan + ";</script></body>")

gui = (ROOT / "gui.html").read_text(encoding="utf-8")
gui = gui.replace('<script src="hindi_input.js"></script>',
                  "<script>\n" + (ROOT / "hindi_input.js").read_text(encoding="utf-8") + "\n</script>")
assert "HindiInput = { attach" in gui

demo_words = ["ashutosh", "johri", "ramesh", "kumar", "sita", "devi", "mohan", "lal", "singh", "sharma", "gupta", "anand",
              "deepak", "priya", "rajesh", "suresh", "sunita", "geeta", "rawat", "negi", "bisht", "joshi", "pandey",
              "chandra", "prasad", "ram", "shyam", "putra", "patni"]
demo = {w: hindi_input.phonetic(w) for w in demo_words}
solvers = [{"id": "manual", "label": "Type it myself", "available": True, "reason": "", "model": ""},
           {"id": "claude", "label": "Claude (Anthropic)", "available": False, "reason": "set ANTHROPIC_API_KEY", "model": ""},
           {"id": "openai", "label": "ChatGPT (OpenAI)", "available": True, "reason": "", "model": "gpt-4o-mini"},
           {"id": "gemini", "label": "Gemini (Google)", "available": False, "reason": "set GEMINI_API_KEY", "model": ""},
           {"id": "ollama", "label": "Ollama (local model)", "available": False, "reason": "start Ollama", "model": "llava"}]

shim = r"""<script>
window.__DEMO_HI = %(demo)s;
(function () {
  const feed = ev => parent.harness.feed(ev);
  const later = (ms, fn) => setTimeout(fn, ms);
  let reportTimer = null, mode = null;
  const pageEvent = () => { const pg = parent.harness.scan(); pg.type = 'page'; pg.mode = mode; return pg; };
  const handlers = {
    defaults: () => ({ username: 'testuser', password: 'demo-password', solvers: %(solvers)s, default_solver: 'manual',
                       download_dir: '/Users/test/data_checker/downloads', login_url: 'https://example.invalid/esearchLogin.aspx',
                       settings: { report: { district: 'BAGESHWAR', sro: 'BAGESHWAR', from_year: 2023, to_year: 2023, name: 'आशुतोष जौहरी', roles: ['buyer'] }, preview: true } }),
    save_prefs: () => true,
    download_rows: (sel, dir) => {
      const n = Object.values(sel || {}).reduce((a, v) => a + (v ? v.length : 0), 0);
      const files = []; let i = 0;
      feed({ type: 'busy', busy: true, what: 'Downloading documents...' });
      const tick = () => {
        if (i >= n) { feed({ type: 'downloads', files, dir, done: true, total: n }); feed({ type: 'busy', busy: false }); return; }
        i++; files.push(dir + '/B2023-' + String(i).padStart(4, '0') + '.pdf');
        feed({ type: 'report_progress', role: 'buyer', year: '2023', message: 'downloaded ' + i + '/' + n, total: 0 });
        feed({ type: 'downloads', files, dir, done: false, total: n });
        reportTimer = later(500, tick);
      };
      reportTimer = later(300, tick);
      return { ok: true, count: n };
    },
    login: (u, p, headed, solver) => {
      later(400, () => feed({ type: 'status', connected: true, text: 'Connected' }));
      if (solver && solver !== 'manual') later(600, () => { feed({ type: 'log', message: '(demo) ' + solver + ' read the captcha as K7X2P' }); feed({ type: 'menu' }); feed({ type: 'busy', busy: false }); });
      else later(500, () => feed({ type: 'captcha', image: parent.harness.captchaImage(), prompt: 'Login captcha (demo - type anything)', allow_skip: false }));
      return { ok: true }; },
    submit_captcha: (t) => { later(300, () => { feed({ type: 'log', message: '(demo) captcha answer: ' + t }); feed({ type: 'menu' }); feed({ type: 'busy', busy: false }); }); return true; },
    skip_captcha: () => true, cancel_captcha: () => { later(100, () => feed({ type: 'busy', busy: false })); return true; },
    open_mode: (m) => { mode = m; feed({ type: 'busy', busy: true, what: 'Opening page...' }); later(400, () => { feed(pageEvent()); feed({ type: 'busy', busy: false }); }); return true; },
    menu: () => { mode = null; later(150, () => feed({ type: 'menu' })); return true; },
    rescan: () => { later(200, () => feed(pageEvent())); return true; },
    set_value: () => { later(300, () => feed(pageEvent())); return true; },
    click: () => { later(300, () => feed(pageEvent())); return true; },
    navigate: () => { later(300, () => feed(pageEvent())); return true; },
    preview: () => true, set_options: () => true, logout: () => { later(100, () => feed({ type: 'reset' })); return true; },
    choose_folder: () => '/Users/test/Desktop/docs', open_folder: () => true, open_path: () => true, download_all: () => true,
    transliterate: (w) => ({ ok: true, candidates: window.__DEMO_HI[String(w || '').toLowerCase()] || [], source: 'demo' }),
    run_report: (p) => {
      const pg = parent.harness.scan(); const tbl = pg.tables[0];
      const years = []; for (let y = +p.from_year; y <= +p.to_year; y++) years.push(String(y));
      const sheets = {};
      for (const r of p.roles) {
        const pi = tbl.header.findIndex(h => new RegExp(r === 'buyer' ? 'buyer|second' : 'seller|first', 'i').test(h));
        sheets[r] = { header: ['ID', 'Year', ...tbl.header, 'Relation', 'Relative'], rows: [], party_index: pi >= 0 ? pi + 2 : -1, party_column: pi >= 0 ? tbl.header[pi] : '' };
      }
      const steps = []; for (const r of p.roles) for (const y of years) steps.push([r, y]);
      let i = 0, total = 0;
      feed({ type: 'busy', busy: true, what: 'Running report...' });
      const tick = () => {
        if (i >= steps.length) { feed({ type: 'report_done', params: p, sheets, partial: false }); feed({ type: 'busy', busy: false }); return; }
        const [r, y] = steps[i++];
        feed({ type: 'report_progress', role: r, year: y, message: 'searching', total });
        const rows = tbl.rows.filter(row => row.length === tbl.header.length).map((row, k) =>
          [r[0].toUpperCase() + y + '-' + String(k + 1).padStart(4, '0'), y, ...row.map((c, j) => j === 3 && r === 'seller' ? c + ' (as seller)' : c), k === 0 ? 'S/O' : '', k === 0 ? 'रमेश सिंह' : '']);
        sheets[r].rows.push(...rows); total += rows.length;
        feed({ type: 'report_progress', role: r, year: y, message: 'done: ' + rows.length + ' row(s)', total });
        reportTimer = later(700, tick);
      };
      reportTimer = later(300, tick);
      return { ok: true };
    },
    cancel_report: () => { clearTimeout(reportTimer); later(200, () => { feed({ type: 'log', message: '(demo) report stopped', level: 'warn' }); feed({ type: 'busy', busy: false }); }); return true; },
    save_report: (name, sel) => ({ ok: true, path: '/Users/test/data_checker/downloads/' + name, format: 'xlsx',
                                   counts: Object.fromEntries(Object.entries(sel || {}).map(([k, v]) => [k[0].toUpperCase() + k.slice(1), v ? v.length : 'all'])) }),
  };
  window.pywebview = { api: new Proxy({}, { get: (_, name) => (...args) => {
    parent.harness.calls.push({ name, args: JSON.parse(JSON.stringify(args)) });
    const h = handlers[name];
    return Promise.resolve(h ? h(...args) : { ok: true });
  } }) };
})();
</script>""" % {"demo": json.dumps(demo, ensure_ascii=False), "solvers": json.dumps(solvers)}
gui = gui.replace("<head>", "<head>" + shim, 1)

b64 = lambda s: base64.b64encode(s.encode("utf-8")).decode()   # noqa: E731
body = f"""<title>GUI Demo Harness</title>
<style>
:root{{--bg:#f6f7f9;--fg:#1c1e21;--line:#d9dde3;--muted:#6b7280}}
@media (prefers-color-scheme: dark){{:root:not([data-theme="light"]){{--bg:#15171a;--fg:#e6e8eb;--line:#2d3239;--muted:#9aa3ad;color-scheme:dark}}}}
:root[data-theme="dark"]{{--bg:#15171a;--fg:#e6e8eb;--line:#2d3239;--muted:#9aa3ad;color-scheme:dark}}
html,body{{height:100%}}
body{{margin:0;background:var(--bg);color:var(--fg);display:flex;flex-direction:column;font:13px/1.4 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}}
.bar{{padding:8px 16px;border-bottom:1px solid var(--line);display:flex;gap:14px;align-items:center;flex:none;flex-wrap:wrap}}
.bar span{{color:var(--muted)}}
.frames{{flex:1;display:flex;min-height:0}}
.frames iframe{{border:0;height:100%;background:#fff}}
#site{{width:30%;border-right:1px solid var(--line)}}
#gui{{flex:1}}
@media (max-width:700px){{.frames{{flex-direction:column}} #site{{width:100%;height:40%;border-right:0;border-bottom:1px solid var(--line)}}}}
</style>
<div class="bar"><strong>GUI demo harness</strong><span>left: mock ASP.NET page · right: gui.html on a fake bridge. Press Log in, type any captcha, then try the menu, the report and the Hindi typing (demo words: {", ".join(demo_words[:10])}…).</span><span id="st"></span></div>
<div class="frames"><iframe id="site" title="mock site"></iframe><iframe id="gui" title="gui under test"></iframe></div>
<script>
const MOCK_B64 = "{b64(mock_js)}";
const GUI_B64 = "{b64(gui)}";
const dec = b => new TextDecoder().decode(Uint8Array.from(atob(b), c => c.charCodeAt(0)));
const site = document.getElementById('site'), gui = document.getElementById('gui');
window.harness = {{
  calls: [],
  scan() {{ return site.contentWindow.__scan(); }},
  feed(ev) {{ gui.contentWindow.app.onEvent(ev); return true; }},
  captchaImage() {{ const img = site.contentDocument.getElementById('MainContent_imgCaptcha'); return img ? img.src : null; }},
}};
site.srcdoc = dec(MOCK_B64);
gui.srcdoc = dec(GUI_B64);
let n = 0; const ready = () => {{ if (++n === 2) document.getElementById('st').textContent = 'ready'; }};
site.addEventListener('load', ready); gui.addEventListener('load', ready);
</script>
"""
if "--artifact" in sys.argv:
    out = HERE / "demo_harness_artifact.html"
    out.write_text(body, encoding="utf-8")
else:
    out = HERE / "demo_harness.html"
    out.write_text('<!DOCTYPE html>\n<html lang="en"><head><meta charset="utf-8">\n' + body.replace("<title>", "", 1).replace("GUI Demo Harness</title>", "<title>GUI Demo Harness</title>", 1)
                   .replace("</style>", "</style></head><body>", 1) + "</body></html>\n", encoding="utf-8")
print("wrote", out, len(body))
