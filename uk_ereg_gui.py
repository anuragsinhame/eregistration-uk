#!/usr/bin/env python3
"""
uk_ereg_gui.py - desktop GUI around uk_eregistration.py

What it does
  * Asks for username / password in a native window (pywebview -> HTML/JS UI).
  * Drives the site with Playwright exactly like the CLI script (same URLs, selectors
    and navigation helpers, imported from uk_eregistration.py).
  * Captchas are shown IN THE GUI and typed by you, or read automatically by the solver you
    pick at login (captcha_solvers.py: Claude, OpenAI, Gemini, any OpenAI-compatible API,
    local Ollama, Tesseract). A solver that fails or gets rejected twice falls back to you.
  * After login it reads the controls on the current page (dropdowns, text boxes,
    radio buttons, buttons, result tables, menu links) and renders them as a form.
    Changing a dropdown triggers the site's postback and the form is re-read;
    clicking a button fills in your values, asks for a captcha if the page has one
    (same captcha box), clicks, and re-reads the page.
  * "Download all documents" saves every "Download ..." link on the results page.
  * Hindi typing on text fields: type in English, pick the Devanagari spelling from the
    suggestions (hindi_input.js in the page, hindi_input.py for the candidates), or use the
    on-screen Devanagari keyboard. Both files are standalone and reusable elsewhere.
  * After login: Search for Buyer / Search for Seller (the site's pages, all options) or a
    Custom report: District + SRO + a range of years + a name, searched year by year (as buyer,
    seller or both), every result page collected, filtered in the app (free text + S/o, W/o,
    पुत्र... chips) and saved as Excel with Buyer and Seller sheets (report_export.py).

Files: uk_ereg_gui.py (this), gui.html, hindi_input.js, hindi_input.py, captcha_solvers.py,
       report_export.py, uk_eregistration.py (the original CLI script: URLs, selectors,
       login/navigation helpers), tests/.  README.md explains everything in detail.

Setup (same venv as the script)
  pip install -r requirements-gui.txt
  playwright install chromium
  cp captcha_settings.example.json captcha_settings.json   # optional: API keys for the solvers

Run
  python uk_ereg_gui.py
  python uk_ereg_gui.py --debug      # opens the webview dev-tools

Architecture (one file, three parts)
  UIBridge  - pushes JSON events into the page (window.app.onEvent) from any thread.
  Worker    - the ONLY thread that touches Playwright. Receives commands from a
              queue; blocks on `captcha_answers` whenever a captcha must be typed.
  Api       - object exposed to JavaScript (window.pywebview.api.*). Its methods
              just enqueue commands / captcha answers and return immediately.
"""
from __future__ import annotations

import base64
import json
import os
import platform
import queue
import re
import subprocess
import sys
import threading
import time
import traceback
from pathlib import Path

FROZEN = bool(getattr(sys, "frozen", False))                 # running from a PyInstaller .app
# Bundled, read-only resources (gui.html, hindi_input.js...) live next to this file in development
# and in the PyInstaller bundle when frozen.
HERE = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
# Writable per-user data (settings.json, captcha_settings.json, downloads): the project folder in
# development, ~/Library/Application Support/<app> (or the OS equivalent) when frozen.
if FROZEN:
    if sys.platform == "darwin":
        APP_DIR = Path.home() / "Library" / "Application Support" / "UK e-Registration Search"
    elif os.name == "nt":
        APP_DIR = Path(os.environ.get("APPDATA", Path.home())) / "UK e-Registration Search"
    else:
        APP_DIR = Path.home() / ".uk_ereg_search"
    APP_DIR.mkdir(parents=True, exist_ok=True)
else:
    APP_DIR = HERE
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))


def _stderr(msg: str) -> None:
    """Write to the real stderr if there is one (a windowed .app has none)."""
    stream = sys.__stderr__
    if stream is not None:
        try:
            stream.write(msg)
        except Exception:  # noqa: BLE001
            pass

try:
    import uk_eregistration as script  # URLs, selectors, go_to_buyer_wise(), solve_captcha()
except ModuleNotFoundError as exc:  # e.g. `anthropic` missing in this environment
    sys.exit(
        f"Could not import uk_eregistration.py ({exc}).\n"
        "It must sit next to this file, and its imports must be installed "
        "(pip install anthropic playwright)."
    )

try:
    import khasra_search
except ModuleNotFoundError as exc:
    sys.exit(f"Could not import khasra_search.py ({exc}). It must sit next to this file.")

try:
    import hindi_input  # Roman -> Devanagari suggestions (hindi_input.py, reusable on its own)
except ModuleNotFoundError:
    hindi_input = None

try:
    import captcha_solvers  # Claude / OpenAI / Gemini / OpenAI-compatible / Ollama / Tesseract readers
    if FROZEN:
        captcha_solvers.SETTINGS_FILE = APP_DIR / "captcha_settings.json"
except ModuleNotFoundError:
    captcha_solvers = None

try:
    import webview  # pywebview
except ImportError:
    sys.exit("pywebview is not installed.  Run:  pip install pywebview")

if FROZEN and not os.environ.get("PLAYWRIGHT_BROWSERS_PATH"):
    # build_app.py bundles Chromium inside the playwright package; tell the driver to look there.
    if (HERE / "playwright" / "driver" / "package" / ".local-browsers").is_dir():
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = "0"

from playwright.sync_api import Error as PWError, TimeoutError as PWTimeout, sync_playwright  # noqa: E402

# ------------------------------------------------------------------ config
WINDOW_TITLE = "UK e-Registration Search"
HTML_FILE = HERE / "gui.html"
SETTINGS_FILE = APP_DIR / "settings.json"                    # last-used report fields, solver, folders
DEFAULT_DOWNLOAD_DIR = ((Path.home() / "Downloads" / "uk_ereg") if FROZEN
                        else (APP_DIR / script.DOWNLOAD_DIR)).resolve()
MAX_CAPTCHA_ATTEMPTS = 4            # per action (login / search click)
SOLVER_MAX_STRIKES = 2              # after this many rejected answers from an LLM/OCR solver, ask the user instead
ACTION_TIMEOUT_MS = 20_000
VIEWPORT = {"width": 1200, "height": 850}

# buttons that never need a captcha even when the page shows one
NO_CAPTCHA_BUTTON = re.compile(r"reset|clear|cancel|back|logout|log out|sign out|refresh|print|close", re.I)
CAPTCHA_REJECTED = re.compile(
    r"captcha.*(invalid|incorrect|wrong|not match|mismatch|expired|again)|"
    r"(invalid|incorrect|wrong|enter).*captcha", re.I)
CREDENTIAL_ERROR = re.compile(r"user ?name|password|credential|account", re.I)
BROWSER_MISSING = re.compile(r"Executable doesn't exist|playwright install|browser.{0,40}not (found|installed)", re.I)


def playwright_install_command(browser: str = "chromium") -> tuple[list[str], dict]:
    """The subprocess command for `playwright install <browser>` plus its environment.

    Uses the Node driver inside the playwright package (playwright/driver/node + package/cli.js),
    which is what `python -m playwright` runs too - and the only option in a frozen app where
    sys.executable is the app itself.
    """
    env = os.environ.copy()
    try:
        from playwright._impl._driver import compute_driver_executable  # type: ignore
        exe = compute_driver_executable()
        cmd = [*map(str, exe)] if isinstance(exe, (tuple, list)) else [str(exe)]
        try:
            from playwright._impl._driver import get_driver_env  # type: ignore
            env.update(get_driver_env())
        except Exception:  # noqa: BLE001
            pass
    except Exception:  # noqa: BLE001 - private API changed: fall back to the module runner
        cmd = [sys.executable, "-m", "playwright"]
    return [*cmd, "install", browser], env


# custom report
MENU_PARENT = "Search By Party Name"
ROLE_PAGES = {"buyer": "Buyer Wise", "seller": "Seller Wise"}
REPORT_LABELS = {                   # how the report finds the form fields on the Buyer/Seller Wise pages
    "district": r"district",
    "sro": r"sub.?reg|s\.?r\.?o\b|registrar",
    "year": r"year",
    "name": r"name",
    "search": r"^\s*search\s*$|^\s*search\b(?!\.)",
}
MAX_RESULT_PAGES = 400
RESULT_SCAN_ROWS = 5000
PAGER_TEXT = re.compile(r"^[\d\s.…<>|/«»‹›-]*$|^(next|prev(ious)?|first|last)$", re.I)

# Which result column holds "our" party for each search role (site headers: "Buyer/Second Party",
# "Seller/First Party"). The relation chips in the GUI and the Relation/Relative columns use it.
PARTY_COLUMN = {"buyer": r"buyer|second\s*party|purchaser|vendee|claimant",
                "seller": r"seller|first\s*party|vendor|executant"}
# "S/O", "W/O", "s/0"... and the Hindi words; longest Hindi alternatives first
RELATION_RE = re.compile(r"(?<![A-Za-z])([SWDHC])\s*/\s*[O0](?![A-Za-z])|(पुत्री|पुत्र|पत्नी|पत्नि|पति|विधवा|पिता|माता)", re.I)
RELATIVE_STOP = re.compile(r"[,;]|\s{2,}|(?<![A-Za-z])[SWDHC]\s*/\s*[O0](?![A-Za-z])|पुत्री|पुत्र|पत्नी|पत्नि|पति|विधवा|निवासी|द्वारा", re.I)


def parse_relation(text: str) -> tuple[str, str]:
    """'अनुराग गुप्ता S/O ठाकुर दास गुप्ता , ,' -> ('S/O', 'ठाकुर दास गुप्ता'); ('', '') when none."""
    m = RELATION_RE.search(text or "")
    if not m:
        return "", ""
    relation = (m.group(1).upper() + "/O") if m.group(1) else m.group(2)
    rest = (text or "")[m.end():]
    relative = RELATIVE_STOP.split(rest, maxsplit=1)[0].strip(" ,;:-।")
    return relation, relative


def party_column_index(header: list[str], role: str) -> int | None:
    """Index (in the site's header) of the column holding the searched party's name, or None."""
    pattern = re.compile(PARTY_COLUMN.get(role, role), re.I)
    hits = [i for i, h in enumerate(header) if pattern.search(h or "")]
    if not hits:
        return None
    named = [i for i in hits if re.search(r"party|name", header[i], re.I) and not re.search(r"gender|sex|age", header[i], re.I)]
    return (named or [i for i in hits if not re.search(r"gender|sex|age", header[i], re.I)] or hits)[0]


class Aborted(Exception):
    """Raised inside the worker when the user cancels the pending action."""


CANCEL = object()   # sentinel placed on the captcha queue to abort the current action


# ------------------------------------------------------------------ page scanner (runs inside the site's page)
SCAN_JS = r"""
(maxRows) => {
  maxRows = maxRows || 300;
  const clean = s => (s || '').replace(/\u00a0/g, ' ').replace(/\s+/g, ' ').replace(/\s*[:*]+\s*$/, '').trim();
  const cssEsc = s => (window.CSS && CSS.escape) ? CSS.escape(s) : String(s).replace(/([^\w-])/g, '\\$1');
  const isVisible = el => {
    if (!el || !el.isConnected) return false;
    const st = getComputedStyle(el);
    if (st.display === 'none' || st.visibility === 'hidden') return false;
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
  };
  const textOf = node => clean(node.innerText !== undefined ? node.innerText : node.textContent);
  const hasControl = node => !!(node && node.querySelector &&
      node.querySelector('input:not([type=hidden]),select,textarea,button'));
  const selectorFor = el => {
    const tag = el.tagName.toLowerCase();
    if (el.id) return '#' + cssEsc(el.id);
    if (el.name && document.getElementsByName(el.name).length === 1)
      return `${tag}[name="${el.name.replace(/(["\\])/g, '\\$1')}"]`;
    return `${tag} >> nth=${Array.from(document.getElementsByTagName(tag)).indexOf(el)}`;
  };
  const humanize = id => {
    let s = String(id || '').replace(/^.*\$/, '').replace(/^.*_/, '');
    s = s.replace(/^(ddl|txt|btn|chk|rbl|rb|lbl|lst|cb|dd|tb|cbo|img|lnk|hl|gv|dg)(?=[A-Z0-9])/, '');
    s = s.replace(/([a-z])([A-Z])/g, '$1 $2').replace(/_/g, ' ');
    return clean(s) || String(id || '');
  };
  const prevCellText = cell => {
    let p = cell.previousElementSibling;
    while (p && (!textOf(p) || hasControl(p))) p = p.previousElementSibling;
    if (p && textOf(p)) return textOf(p).slice(0, 80);
    const tr = cell.parentElement, prevTr = tr && tr.previousElementSibling;
    if (prevTr && prevTr.cells && prevTr.cells[cell.cellIndex]) {
      const c = prevTr.cells[cell.cellIndex];
      if (!hasControl(c) && textOf(c)) return textOf(c).slice(0, 80);
    }
    return null;
  };
  const labelFor = el => {
    if (el.id) {
      const l = document.querySelector(`label[for="${cssEsc(el.id)}"]`);
      if (l && textOf(l)) return textOf(l);
    }
    if (el.labels && el.labels.length && textOf(el.labels[0])) return textOf(el.labels[0]);
    for (const a of ['aria-label', 'placeholder', 'title']) {
      const v = el.getAttribute(a);
      if (v && clean(v)) return clean(v);
    }
    const cell = el.closest('td,th');
    if (cell) { const t = prevCellText(cell); if (t) return t; }
    let p = el.previousSibling, n = 0;
    while (p && n++ < 6) {
      if (p.nodeType === 3 && clean(p.textContent)) return clean(p.textContent).slice(0, 80);
      if (p.nodeType === 1 && !hasControl(p) && textOf(p)) return textOf(p).slice(0, 80);
      p = p.previousSibling;
    }
    return humanize(el.id || el.name || el.tagName);
  };

  // ---- captcha: image + the text box that goes with it
  const capImg = Array.from(document.images).find(i =>
      /captcha/i.test([i.id, i.src, i.alt, i.className, i.title].join(' ')) && isVisible(i)) || null;
  let capInput = null;
  if (capImg) {
    const texts = Array.from(document.querySelectorAll('input[type=text], input:not([type])')).filter(isVisible);
    capInput = texts.find(i => /captcha|secur|verif|code/i.test(i.id + ' ' + i.name + ' ' + (i.placeholder || ''))) || null;
    if (!capInput) {
      const row = capImg.closest('tr');
      capInput = (row && Array.from(row.querySelectorAll('input[type=text], input:not([type])')).find(isVisible)) ||
                 texts.find(i => capImg.compareDocumentPosition(i) & Node.DOCUMENT_POSITION_FOLLOWING) || null;
    }
  }

  // ---- form controls
  const controls = [];
  const seenRadio = new Set();
  for (const el of document.querySelectorAll('input, select, textarea, button')) {
    const tag = el.tagName.toLowerCase();
    const type = (el.getAttribute('type') || (tag === 'button' ? 'submit' : 'text')).toLowerCase();
    if (type === 'hidden' || type === 'file') continue;
    if (!isVisible(el) || el === capInput) continue;
    const base = { selector: selectorFor(el), id: el.id || null, name: el.name || null, disabled: !!el.disabled };
    if (tag === 'select') {
      controls.push({ ...base, kind: 'select', label: labelFor(el), multiple: !!el.multiple, value: el.value,
        options: Array.from(el.options).map(o => ({ value: o.value, label: clean(o.text), selected: o.selected })) });
    } else if (tag === 'textarea') {
      controls.push({ ...base, kind: 'textarea', label: labelFor(el), value: el.value, placeholder: el.placeholder || '' });
    } else if (tag === 'button' || ['submit', 'button', 'image', 'reset'].includes(type)) {
      const label = clean(el.value) || textOf(el) || clean(el.getAttribute('alt')) || clean(el.title) || humanize(el.id || el.name || 'button');
      controls.push({ ...base, kind: 'button', label, type });
    } else if (type === 'checkbox') {
      controls.push({ ...base, kind: 'checkbox', label: labelFor(el), checked: el.checked });
    } else if (type === 'radio') {
      const key = el.name || el.id;
      if (seenRadio.has(key)) continue;
      seenRadio.add(key);
      const group = el.name
        ? Array.from(document.getElementsByName(el.name)).filter(r => r.type === 'radio' && isVisible(r)) : [el];
      const tbl = el.closest('table');
      const rblTable = (tbl && tbl.id && /rbl|radio|rb/i.test(tbl.id)) ? tbl : null;   // ASP.NET RadioButtonList
      const cell = (rblTable || el).closest('td,th');
      let glabel = cell ? prevCellText(cell) : null;
      if (!glabel && rblTable) glabel = humanize(rblTable.id);
      controls.push({ ...base, kind: 'radio', name: key, label: glabel || labelFor(el),
        options: group.map(r => ({ value: r.value, label: labelFor(r), selector: selectorFor(r), checked: r.checked })) });
    } else {
      controls.push({ ...base, kind: type === 'password' ? 'password' : 'text', label: labelFor(el), value: el.value,
        placeholder: el.placeholder || '', maxlength: el.maxLength > 0 ? el.maxLength : null });
    }
  }

  // ---- data tables (search results)
  const tables = [];
  const isDataCore = t => {
    const rows = Array.from(t.rows);
    if (rows.length < 2) return false;
    const maxCols = Math.max(...rows.map(r => r.cells.length));
    if (maxCols < 2) return false;
    if (t.querySelector('select, textarea, input[type=text], input[type=password], input[type=radio], input[type=checkbox]')) return false;
    const cells = rows.reduce((a, r) => a.concat(Array.from(r.cells)), []);
    const textCells = cells.filter(c => textOf(c)).length;
    const linkCells = cells.filter(c => c.querySelector('a') && !/download/i.test(textOf(c))).length;
    if (textCells && linkCells / textCells > 0.6) return false;           // a menu, not data
    const looks = /grid|gv|result|dg|list|data/i.test(t.id + ' ' + t.className) || !!t.querySelector('th');
    return looks || (rows.length >= 3 && textCells >= 6);
  };
  // a data table must not contain another data table (layout tables wrap everything)
  const isData = t => isDataCore(t) && !Array.from(t.querySelectorAll('table')).some(isDataCore);
  const everyTable = Array.from(document.querySelectorAll('table'));
  for (const t of everyTable.filter(isVisible)) {
    if (!isData(t)) continue;
    const allRows = Array.from(t.rows);
    const header = Array.from(allRows[0].cells).every(c => c.tagName === 'TH') ? Array.from(allRows.shift().cells).map(c => textOf(c).slice(0, 200)) : null;
    const rows = allRows.slice(0, maxRows).map(r => Array.from(r.cells).map(c => textOf(c).slice(0, 200)));
    const downloads = Array.from(t.querySelectorAll('a, input[type=submit], input[type=button], button'))
        .filter(a => /download/i.test(textOf(a) || a.value || '')).length;
    tables.push({ id: t.id || null, index: everyTable.indexOf(t), header, rows, total_rows: allRows.length, downloads });
  }

  // ---- status / error messages
  const messages = [], seenMsg = new Set();
  const pushMsg = (t, level) => { t = clean(t); if (t && t.length <= 300 && !seenMsg.has(t)) { seenMsg.add(t); messages.push({ text: t, level }); } };
  for (const el of document.querySelectorAll('span, div, label, p, td, li')) {
    if (messages.length >= 8) break;
    if (!isVisible(el) || hasControl(el) || el.querySelector('div, table, ul')) continue;
    const idc = el.id + ' ' + el.className;
    if (!/(msg|message|error|\berr\b|status|result|info|warn|alert|success|notice|validation)/i.test(idc)) continue;
    pushMsg(textOf(el), /error|err|invalid|fail|warn|alert|danger/i.test(idc) ? 'error' : 'info');
  }
  for (const line of (document.body.innerText || '').split('\n').map(clean)) {
    if (messages.length >= 8) break;
    if (line.length > 200) continue;
    if (/(no record|not found|invalid|incorrect|wrong|expired|does not match|try again|successfull|unauthori|session)/i.test(line))
      pushMsg(line, /invalid|incorrect|wrong|expired|not match|no record|not found|unauthori/i.test(line) ? 'error' : 'info');
  }

  // ---- navigation links (menu items; hidden sub-menu items are reached by hovering their parent)
  const links = [], seenLink = new Set();
  for (const a of document.querySelectorAll('a')) {
    if (links.length >= 80) break;
    const text = textOf(a).slice(0, 60);
    if (!text || /download/i.test(text)) continue;
    const li = a.closest('li');
    const parentLi = li && li.parentElement ? li.parentElement.closest('li') : null;
    const vis = isVisible(a);
    if (!vis && !parentLi) continue;
    const tbl = a.closest('table');
    if (tbl && isData(tbl)) continue;
    let hover = null;
    if (parentLi) {
      const head = parentLi.querySelector(':scope > a, :scope > span, :scope > div, :scope > td');
      hover = clean(head ? head.innerText : parentLi.innerText.split('\n')[0]).slice(0, 60) || null;
    }
    if (hover === text) hover = null;
    const key = (hover ? hover + ' > ' : '') + text;
    if (seenLink.has(key)) continue;
    seenLink.add(key);
    links.push({ text, hover, href: a.getAttribute('href') || '', visible: vis });
  }

  const headEl = document.querySelector('h1, h2, h3') ||
                 document.querySelector('[id*="Heading" i], [id*="Title" i], [class*="title" i]');
  return {
    url: location.href,
    title: clean(document.title),
    heading: headEl ? textOf(headEl).slice(0, 80) : '',
    controls,
    captcha: capImg ? { img: selectorFor(capImg), input: capInput ? selectorFor(capInput) : null } : null,
    tables, messages, links,
  };
}
"""

# Marks the pager control that leads to the next page of results (GridView pager rows, DataTables
# "Next" buttons, numbered links) with data-ereg-next="1" so Playwright can click it.
NEXT_PAGE_JS = r"""
(tableIndex) => {
  const t = document.querySelectorAll('table')[tableIndex];
  if (!t) return { status: 'no-table' };
  document.querySelectorAll('[data-ereg-next]').forEach(e => e.removeAttribute('data-ereg-next'));
  const clean = s => (s || '').replace(/\s+/g, ' ').trim();
  const label = n => clean(n.tagName === 'INPUT' ? n.value : n.innerText);
  const disabled = n => n.disabled || n.classList.contains('disabled') || n.getAttribute('aria-disabled') === 'true' ||
                        !!n.closest('.disabled, [aria-disabled="true"]');
  const PAGER = /^(\d+|\.\.\.|…|next|prev(ious)?|first|last|>|>>|<|<<|›|»|‹|«)$/i;
  const containers = [t];
  for (let p = t.parentElement, i = 0; p && p !== document.body && i < 3; p = p.parentElement, i++) containers.push(p);
  for (const c of containers) {
    const items = Array.from(c.querySelectorAll('a, button, input[type=submit], input[type=button], span, li'))
      .filter(n => PAGER.test(label(n)) && !n.querySelector('a, button, span'));
    if (items.length < 2) continue;
    const current = items.find(n => /^\d+$/.test(label(n)) &&
      (n.tagName === 'SPAN' || n.tagName === 'LI' || /\b(active|current|selected)\b/i.test(n.className) || n.getAttribute('aria-current')));
    const curNum = current ? parseInt(label(current), 10) : null;
    const clickable = items.filter(n => ['A', 'BUTTON', 'INPUT'].includes(n.tagName) && !disabled(n));
    let target = null;
    if (curNum !== null) target = clickable.find(n => label(n) === String(curNum + 1));
    if (!target) target = clickable.find(n => /^(next|>|›)$/i.test(label(n)));
    if (!target && current) {
      const after = items.slice(items.indexOf(current) + 1);
      target = after.find(n => /^(\.\.\.|…)$/.test(label(n)) && clickable.includes(n));
    }
    if (!target) return { status: 'last', current: curNum };
    target.setAttribute('data-ereg-next', '1');
    return { status: 'ok', current: curNum, next: label(target) };
  }
  return { status: 'no-pager' };
}
"""

# DataTables / GridView "show N entries" selectors: pick the largest page size so fewer pages are needed.
PAGE_SIZE_JS = r"""
() => {
  const clean = s => (s || '').replace(/\s+/g, ' ').trim();
  for (const sel of document.querySelectorAll('select')) {
    if (sel.offsetParent === null) continue;
    const opts = Array.from(sel.options).map(o => clean(o.text));
    const nums = opts.filter(o => /^\d+$/.test(o)).map(Number);
    const hint = /length|size|page|entries|records/i.test(sel.name + ' ' + sel.id + ' ' + (sel.getAttribute('aria-label') || ''));
    if (!(hint || (nums.length >= 3 && opts.length <= 8 && nums.includes(10)))) continue;
    let pick = Array.from(sel.options).find(o => /^(all|-1)$/i.test(clean(o.text)) || o.value === '-1');
    if (!pick && nums.length) { const max = Math.max(...nums); pick = Array.from(sel.options).find(o => clean(o.text) === String(max)); }
    if (!pick || pick.selected) return null;
    sel.value = pick.value;
    sel.dispatchEvent(new Event('change', { bubbles: true }));
    return clean(pick.text);
  }
  return null;
}
"""

# Finds the result row whose cells match `cells` (nth match for duplicates) and marks its
# "Download ..." link/button with data-ereg-dl="1". Returns what it found for logging.
ROW_LINK_JS = r"""
([tableIndex, cells, nth]) => {
  const t = document.querySelectorAll('table')[tableIndex];
  if (!t) return { status: 'no-table' };
  document.querySelectorAll('[data-ereg-dl]').forEach(e => e.removeAttribute('data-ereg-dl'));
  const clean = s => (s || '').replace(/ /g, ' ').replace(/\s+/g, ' ').trim();
  const want = cells.map(clean);
  let seen = 0;
  for (const row of Array.from(t.rows)) {
    const texts = Array.from(row.cells).map(c => clean(c.innerText).slice(0, 200));
    if (texts.length !== want.length) continue;
    if (!texts.every((v, i) => v === want[i] || /download/i.test(want[i]))) continue;  // link text may differ
    if (seen++ < nth) continue;
    const link = Array.from(row.querySelectorAll('a, input[type=submit], input[type=button], button'))
      .find(a => /download/i.test(clean(a.innerText) || clean(a.value) || clean(a.title)));
    if (!link) return { status: 'no-link', row: texts };
    link.setAttribute('data-ereg-dl', '1');
    return { status: 'ok', row: texts };
  }
  return { status: 'no-row', rows: t.rows.length };
}
"""


# ------------------------------------------------------------------ settings (last-used values)
_settings_lock = threading.Lock()


def load_settings() -> dict:
    try:
        return json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except Exception as exc:  # noqa: BLE001
        _stderr(f"[settings] could not read {SETTINGS_FILE}: {exc}\n")
        return {}


def save_settings(patch: dict) -> dict:
    """Merge `patch` into settings.json (one level deep for dict values) and return the result."""
    with _settings_lock:
        data = load_settings()
        for key, value in patch.items():
            if isinstance(value, dict) and isinstance(data.get(key), dict):
                data[key] = {**data[key], **value}
            else:
                data[key] = value
        try:
            SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
            SETTINGS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as exc:  # noqa: BLE001
            _stderr(f"[settings] could not write {SETTINGS_FILE}: {exc}\n")
        return data


# ------------------------------------------------------------------ UI bridge (Python -> JS)
class UIBridge:
    """Thread-safe `emit(event)` that runs `window.app.onEvent(event)` in the webview."""

    def __init__(self) -> None:
        self.window = None
        self.closing = False          # set when the window is closing: emits become no-ops
        self._ready = False
        self._backlog: list[str] = []
        self._lock = threading.Lock()

    def attach(self, window) -> None:
        self.window = window
        window.events.loaded += self._on_loaded

    def _on_loaded(self, *_args) -> None:
        with self._lock:
            self._ready = True
            backlog, self._backlog = self._backlog, []
        for js in backlog:
            self._run(js)

    def _run(self, js: str) -> None:
        try:
            run_js = getattr(self.window, "run_js", None)
            if run_js:
                run_js(js)
            else:
                self.window.evaluate_js(js)
        except Exception as exc:  # window gone / JS error - never let this kill the worker
            _stderr(f"[ui] could not push event: {exc}\n")

    def emit(self, event: dict) -> None:
        if self.closing:
            return
        js = f"window.app && window.app.onEvent({json.dumps(event, ensure_ascii=True)});"
        with self._lock:
            if not self._ready or self.window is None:
                self._backlog.append(js)
                return
        self._run(js)


class StdoutTee:
    """Mirror print() output (e.g. from uk_eregistration.py helpers) into the GUI log."""

    def __init__(self, real, ui: UIBridge) -> None:
        self.real = real
        self.ui = ui
        self._buf = ""
        self._local = threading.local()

    def write(self, s: str) -> int:
        if self.real:
            self.real.write(s)
        if getattr(self._local, "busy", False):
            return len(s)
        self._local.busy = True
        try:
            self._buf += s
            while "\n" in self._buf:
                line, self._buf = self._buf.split("\n", 1)
                if line.strip():
                    self.ui.emit({"type": "log", "message": line.rstrip()})
        finally:
            self._local.busy = False
        return len(s)

    def flush(self) -> None:
        if self.real:
            self.real.flush()

    def __getattr__(self, name):          # isatty, encoding, ...
        if self.real is None:             # e.g. launched without a console
            raise AttributeError(name)
        return getattr(self.real, name)


# ------------------------------------------------------------------ Playwright worker
class Worker(threading.Thread):
    """Owns the Playwright browser. Everything browser-related happens on this thread."""

    def __init__(self, ui: UIBridge) -> None:
        super().__init__(daemon=True, name="playwright-worker")
        self.ui = ui
        self.cmds: queue.Queue = queue.Queue()
        self.captcha_answers: queue.Queue = queue.Queue()
        self.opts: dict = {"preview": True, "solver": "manual", "headed": False}
        self._pw = None
        self.browser = None
        self.ctx = None
        self.page = None
        self._alive = True
        self.mode: str | None = None          # "buyer" | "seller" | "report" | "khasra" | None (menu)
        self.report: dict | None = None       # last custom report: {"params", "sheets": {role: {"header", "rows"}}, "partial"}
        self._cancel = threading.Event()      # set by the GUI's Stop button during a report
        self._nav_count = 0                   # main-frame navigations seen (used by _settle)
        self._solver_strikes = 0              # rejected answers from the automatic captcha solver
        self.last_answer_source = "manual"    # who produced the most recent captcha answer

    # ---- plumbing -------------------------------------------------
    def post(self, cmd: str, **kwargs) -> None:
        self.cmds.put((cmd, kwargs))

    def shutdown(self, timeout: float = 6.0) -> None:
        self._alive = False
        self.captcha_answers.put(CANCEL)      # unblock a pending captcha wait
        self.cmds.put(("quit", {}))
        self.join(timeout)

    def log(self, message: str, level: str = "info") -> None:
        self.ui.emit({"type": "log", "message": message, "level": level})

    def error(self, message: str) -> None:
        self.ui.emit({"type": "error", "message": message})

    def busy(self, flag: bool, what: str = "") -> None:
        self.ui.emit({"type": "busy", "busy": flag, "what": what})

    def status(self, connected: bool, text: str = "") -> None:
        self.ui.emit({"type": "status", "connected": connected, "text": text})

    def run(self) -> None:
        while True:
            cmd, kwargs = self.cmds.get()
            if cmd == "quit":
                self._close_browser()
                return
            fn = getattr(self, f"cmd_{cmd}", None)
            if fn is None:
                self.log(f"Unknown command: {cmd}", "warn")
                continue
            self._cancel.clear()                               # a Stop press never leaks into the next action
            self.busy(True, kwargs.pop("_what", cmd.replace("_", " ").capitalize() + "..."))
            try:
                fn(**kwargs)
            except Aborted:
                self.log("Cancelled.", "warn")
                self._safe_push_page()
            except PWTimeout as exc:
                self.error(f"Timed out: {first_line(exc)}")
                self._safe_push_page()
            except PWError as exc:
                msg = first_line(exc)
                if "Executable doesn't exist" in str(exc) or "playwright install" in str(exc):
                    msg = "Chromium for Playwright is not installed. Run:  playwright install chromium"
                elif "Target page, context or browser has been closed" in str(exc):
                    msg = "The browser was closed. Log in again."
                    self._forget_browser()
                    self.status(False)
                    self.ui.emit({"type": "reset"})
                self.error(msg)
            except Exception as exc:  # noqa: BLE001
                self.error(f"{type(exc).__name__}: {first_line(exc)}")
                _stderr(traceback.format_exc())
            finally:
                self.busy(False)

    # ---- browser lifecycle ------------------------------------------
    def _ensure_browser(self, headed: bool) -> None:
        if self.browser is not None and self.opts.get("headed") != headed:
            self._close_browser()
        if self.browser is None:
            self.log("Starting Chromium" + (" (visible)" if headed else " (headless)") + "...")
            self._pw = sync_playwright().start()
            try:
                self.browser = self._pw.chromium.launch(headless=not headed)
            except PWError as exc:
                if not BROWSER_MISSING.search(str(exc)):
                    raise
                if not self._install_browsers():             # first run on a new machine: fetch Chromium
                    raise
                self.browser = self._pw.chromium.launch(headless=not headed)
            self.ctx = self.browser.new_context(accept_downloads=True, locale="en-IN", viewport=VIEWPORT)
            self.page = self.ctx.new_page()
            self.page.set_default_timeout(ACTION_TIMEOUT_MS)
            self._nav_count = 0
            page = self.page
            page.on("framenavigated", lambda frame: self._on_navigated(frame, page))
            self.opts["headed"] = headed

    def _on_navigated(self, frame, page) -> None:
        if frame == page.main_frame:
            self._nav_count += 1

    def _install_browsers(self) -> bool:
        """Run Playwright's `install chromium` with the driver that ships in the playwright package
        (works from a venv and from a frozen app). Progress is shown in the status bar and the log."""
        cmd, env = playwright_install_command()
        self.log("Chromium for Playwright is not installed on this machine - downloading it now "
                 "(one time, roughly 150-250 MB)...")
        self.status(False, "Downloading Chromium...")
        self.busy(True, "Downloading Chromium...")
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env,
                                    text=True, encoding="utf-8", errors="replace", bufsize=1)
        except OSError as exc:
            self.error(f"Could not start the Playwright installer ({first_line(exc)}). "
                       f"Run:  playwright install chromium")
            return False
        last_pct, buf = None, ""
        assert proc.stdout is not None
        while True:
            chunk = proc.stdout.read(256)
            if not chunk:
                break
            buf += chunk
            while True:                                        # progress bars use \r; messages use \n
                sep = re.search(r"[\r\n]", buf)
                if not sep:
                    break
                line, buf = buf[:sep.start()].strip(), buf[sep.end():]
                if not line:
                    continue
                pct = re.search(r"(\d{1,3})%", line)
                if pct:
                    if pct.group(1) != last_pct:
                        last_pct = pct.group(1)
                        name = re.match(r"(Downloading [^\d]+)", line)
                        self.busy(True, f"{(name.group(1).strip() if name else 'Downloading')} {last_pct}%")
                else:
                    self.log(line)
        rc = proc.wait()
        if rc != 0:
            self.error(f"Playwright installer exited with code {rc}. Run manually:  playwright install chromium")
            return False
        self.log("Chromium downloaded.")
        return True

    def _close_browser(self) -> None:
        try:
            if self.browser is not None:
                self.browser.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            if self._pw is not None:
                self._pw.stop()
        except Exception:  # noqa: BLE001
            pass
        self._forget_browser()

    def _forget_browser(self) -> None:
        self._pw = self.browser = self.ctx = self.page = None

    def _require_page(self):
        if self.page is None:
            raise RuntimeError("Not logged in yet.")
        return self.page

    # ---- helpers ----------------------------------------------------
    def _settle(self, quiet: float = 0.4) -> None:
        """Wait for an ASP.NET postback / navigation to finish - including a follow-up postback the
        page starts on its own right after loading (the Buyer Wise page does this to fill the SRO list)."""
        page = self._require_page()
        page.wait_for_timeout(300)
        deadline = time.time() + 30
        while True:
            for state in ("load", "networkidle"):
                try:
                    page.wait_for_load_state(state, timeout=15_000)
                except PWTimeout:
                    pass
            nav_seen = self._nav_count
            page.wait_for_timeout(int(quiet * 1000))           # pumps Playwright events (framenavigated)
            if self._nav_count == nav_seen or time.time() > deadline:
                break                                          # no navigation started during the quiet period

    def _evaluate(self, js: str, arg=None, retries: int = 4):
        """page.evaluate that survives 'Execution context was destroyed' (a navigation in between)."""
        page = self._require_page()
        for attempt in range(retries):
            try:
                return page.evaluate(js, arg)
            except PWError as exc:
                text = str(exc)
                if attempt < retries - 1 and re.search(r"context was destroyed|navigat|detached|Target closed",
                                                       text, re.I):
                    self._settle(0.5)
                    continue
                raise

    def _goto(self, url: str) -> None:
        page = self._require_page()
        try:
            page.goto(url, wait_until="networkidle", timeout=45_000)
        except PWTimeout:
            page.goto(url, wait_until="load", timeout=45_000)

    def _scan(self, max_rows: int = 300) -> dict:
        return self._evaluate(SCAN_JS, max_rows)

    def _preview_data_uri(self) -> str | None:
        page = self._require_page()
        try:
            height = int(self._evaluate("() => document.documentElement.scrollHeight") or 0)
            jpeg = page.screenshot(type="jpeg", quality=55, full_page=height <= 2500)
            return "data:image/jpeg;base64," + base64.b64encode(jpeg).decode()
        except Exception as exc:  # noqa: BLE001
            self.log(f"Preview failed: {first_line(exc)}", "warn")
            return None

    def _push_page(self) -> dict:
        state = self._scan()
        state["type"] = "page"
        state["mode"] = self.mode
        if self.opts.get("preview"):
            state["preview"] = self._preview_data_uri()
        self.ui.emit(state)
        return state

    def _safe_push_page(self) -> None:
        if self.page is None:
            return
        try:
            self._push_page()
        except Exception:  # noqa: BLE001
            pass

    def _find_captcha(self) -> dict | None:
        return self._scan().get("captcha")

    def _label_of(self, selector: str) -> str:
        try:
            txt = self._require_page().locator(selector).first.evaluate(
                "el => el.value || el.innerText || el.getAttribute('alt') || el.id || ''")
            return re.sub(r"\s+", " ", str(txt)).strip()[:60] or selector
        except Exception:  # noqa: BLE001
            return selector

    def _apply_values(self, values: dict | None) -> None:
        """Fill text boxes / textareas with what the user typed in the GUI."""
        page = self._require_page()
        for selector, value in (values or {}).items():
            try:
                loc = page.locator(selector).first
                if loc.count() == 0:
                    continue
                if loc.input_value() != str(value):
                    loc.fill(str(value))
            except Exception as exc:  # noqa: BLE001
                self.log(f"Could not fill {selector}: {first_line(exc)}", "warn")

    def _error_lines(self) -> str:
        try:
            body = self._require_page().inner_text("body")
        except Exception:  # noqa: BLE001
            return ""
        hits = [l.strip() for l in body.splitlines()
                if re.search(r"captcha|invalid|incorrect|wrong|expired|not match|failed|error", l, re.I)]
        return " | ".join(dict.fromkeys(h for h in hits if len(h) < 200))[:400]

    # ---- captcha ----------------------------------------------------
    def _captcha_png(self, img_selector: str) -> bytes:
        page = self._require_page()
        img = page.locator(img_selector).first
        img.wait_for(state="visible", timeout=10_000)
        for _ in range(25):                                   # wait for the bitmap to load
            try:
                if img.evaluate("i => i.complete && i.naturalWidth > 0"):
                    break
            except Exception:  # noqa: BLE001
                break
            time.sleep(0.2)
        time.sleep(0.2)
        return img.screenshot(type="png")

    def _get_captcha_answer(self, img_selector: str, prompt: str, allow_skip: bool = True) -> str:
        """Return the captcha text: from Claude (if enabled) or typed by the user.

        Returns "" when the user chose Skip; raises Aborted on Cancel.
        """
        png = self._captcha_png(img_selector)

        solver = self.opts.get("solver") or "manual"
        if solver != "manual" and captcha_solvers is not None and self._solver_strikes < SOLVER_MAX_STRIKES:
            label = captcha_solvers.SOLVERS.get(solver, {}).get("label", solver)
            try:
                code = captcha_solvers.solve(solver, png)
                if code:
                    self.log(f"{label} read the captcha as {code}")
                    self.last_answer_source = "solver"
                    return code
                self.log(f"{label} returned nothing usable - asking you instead.", "warn")
            except Exception as exc:  # noqa: BLE001
                self.log(f"{label} failed ({first_line(exc)}) - asking you instead.", "warn")
        elif solver != "manual" and self._solver_strikes >= SOLVER_MAX_STRIKES:
            self.log("The automatic solver was rejected twice - please type this one.", "warn")

        self.last_answer_source = "manual"
        while True:                                            # drop stale answers
            try:
                self.captcha_answers.get_nowait()
            except queue.Empty:
                break
        self.log("Captcha required - please type it in the captcha box.")
        self.ui.emit({"type": "captcha",
                      "image": "data:image/png;base64," + base64.b64encode(png).decode(),
                      "prompt": prompt, "allow_skip": allow_skip})
        while True:                                            # wait for the GUI, but stay cancellable
            try:
                answer = self.captcha_answers.get(timeout=0.25)
                break
            except queue.Empty:
                if self._cancel.is_set() or not self._alive:
                    answer = CANCEL
                    break
        self.ui.emit({"type": "captcha_done"})
        if answer is CANCEL or not self._alive:
            raise Aborted()
        return str(answer)

    # ---- commands (called on this thread, one at a time) -------------
    def cmd_login(self, username: str, password: str, headed: bool = False,
                  solver: str = "manual", preview: bool = True) -> None:
        self.opts.update(solver=solver, preview=preview)
        self._solver_strikes = 0
        self._ensure_browser(headed)
        page = self.page
        self.status(False, "Logging in...")

        for attempt in range(1, script.MAX_LOGIN_ATTEMPTS + 1):
            self.log(f"Opening login page (attempt {attempt}/{script.MAX_LOGIN_ATTEMPTS})...")
            self._goto(script.LOGIN_URL)
            if "esearchlogin" not in page.url.lower():        # session still valid -> already inside
                self.log(f"Already logged in -> {page.url}")
                self.status(True, "Connected")
                self._after_login()
                return
            page.fill(script.SEL_USER, username)
            page.fill(script.SEL_PASS, password)

            img_sel, txt_sel = script.SEL_CAPTCHA_IMG, script.SEL_CAPTCHA_TXT
            if page.locator(img_sel).count() == 0:           # selectors changed? fall back to detection
                cap = self._find_captcha()
                if cap:
                    img_sel, txt_sel = cap["img"], cap["input"] or txt_sel
            code = self._get_captcha_answer(img_sel, "Login captcha", allow_skip=False)
            page.fill(txt_sel, code)
            page.click(script.SEL_LOGIN_BTN)
            self._settle()

            if "esearchlogin" not in page.url.lower():
                self.log(f"Logged in -> {page.url}")
                self.status(True, "Connected")
                self._after_login()
                return

            err = self._error_lines()
            self.log(f"Login failed: {err or 'unknown reason'}", "warn")
            if self.last_answer_source == "solver":
                self._solver_strikes += 1                      # the model probably misread it
            if err and CREDENTIAL_ERROR.search(err) and not re.search("captcha", err, re.I):
                break                                          # wrong password: retrying won't help

        self.status(False, "Login failed")
        self.error("Login failed. Check the username/password and try again.")

    def _after_login(self) -> None:
        self.mode = None
        self.ui.emit({"type": "menu"})                         # Buyer / Seller / Custom report

    # ---- modes -------------------------------------------------------
    def _open_role_page(self, role: str) -> None:
        """Navigate to the Buyer Wise / Seller Wise search page."""
        page = self._require_page()
        if role == "buyer":
            script.go_to_buyer_wise(page)                      # the CLI script's verified navigation
        elif role == "khasra":
            khasra_search.open_page(page, script.SEARCH_URL, self._goto, self._settle, self.log)
        else:
            item = ROLE_PAGES[role]
            self._goto(script.SEARCH_URL)
            try:
                page.get_by_text(MENU_PARENT, exact=False).first.hover(timeout=5_000)
                time.sleep(0.4)
            except Exception:  # noqa: BLE001
                pass
            link = page.locator("a").filter(has_text=re.compile(rf"^\s*{re.escape(item)}\s*$", re.I)).first
            try:
                link.click(timeout=5_000)
            except Exception:  # noqa: BLE001
                try:
                    page.get_by_text(item, exact=True).first.click(timeout=5_000)
                except Exception:  # noqa: BLE001
                    link.evaluate("el => el.click()")
            self._settle()
            self.log(f"[nav] {item} -> {page.url}")
        self._settle(0.2)

    def cmd_open_mode(self, mode: str) -> None:
        if mode not in ("buyer", "seller", "report", "khasra"):
            raise ValueError(f"unknown mode {mode!r}")
        self._require_page()
        self.mode = mode
        self._open_role_page("seller" if mode == "seller" else "khasra" if mode == "khasra" else "buyer")
        if mode == "report":
            self._apply_saved_report_prefs()
        self._push_page()

    def _apply_saved_report_prefs(self) -> None:
        """Re-select the last-used District / SRO / from-year on the live page (settings.json)."""
        prefs = (load_settings().get("report") or {})
        if not prefs.get("district"):
            return
        try:
            ctl = self._report_controls(self._scan())
            self._select_label(ctl["district"]["selector"], prefs["district"])
            if prefs.get("sro"):
                ctl = self._report_controls(self._scan())
                self._select_label(ctl["sro"]["selector"], prefs["sro"])
            if prefs.get("from_year"):
                ctl = self._report_controls(self._scan())
                self._select_label(ctl["year"]["selector"], str(prefs["from_year"]))
            self.log(f"Restored last report settings: {prefs.get('district')} / {prefs.get('sro')} / "
                     f"{prefs.get('from_year')}-{prefs.get('to_year')}")
        except Exception as exc:  # noqa: BLE001
            self.log(f"Could not restore the saved District/SRO ({first_line(exc)}).", "warn")

    def cmd_menu(self) -> None:
        self.mode = None
        self.ui.emit({"type": "menu"})

    def cmd_set_value(self, selector: str, value, kind: str = "text") -> None:
        page = self._require_page()
        loc = page.locator(selector).first
        if kind == "select":
            v = value if isinstance(value, dict) else {"value": value}
            try:
                loc.select_option(value=str(v.get("value", "")))
            except Exception:  # noqa: BLE001
                loc.select_option(label=str(v.get("label", v.get("value", ""))))
            self.log(f"{v.get('field') or selector} -> {v.get('label') or v.get('value')}")
            self._settle()                                     # AutoPostBack repopulates dependents
            self._push_page()
        elif kind == "checkbox":
            loc.set_checked(bool(value))
            self._settle(0.2)
            self._push_page()
        elif kind == "radio":
            loc.check()
            self._settle(0.2)
            self._push_page()
        else:
            loc.fill("" if value is None else str(value))

    def _click_with_captcha(self, selector: str, label: str, values: dict | None = None) -> None:
        """Fill values, answer the page captcha if there is one (same GUI box), click, retry on rejection."""
        page = self._require_page()
        self._apply_values(values)
        needs_captcha = not NO_CAPTCHA_BUTTON.search(label)

        for attempt in range(1, MAX_CAPTCHA_ATTEMPTS + 1):
            cap = self._find_captcha() if needs_captcha else None
            answer = ""
            if cap:
                answer = self._get_captcha_answer(cap["img"], f"Captcha for \u201c{label}\u201d")
                if answer and cap.get("input"):
                    page.fill(cap["input"], answer)
                elif answer:
                    self.log("Found a captcha image but no text box for it - clicking anyway.", "warn")
            self.log(f"Clicking \u201c{label}\u201d...")
            page.locator(selector).first.click()
            self._settle()
            if cap and answer and CAPTCHA_REJECTED.search(self._error_lines() or ""):
                if self.last_answer_source == "solver":
                    self._solver_strikes += 1
                    self.log("The site rejected the solver's captcha answer - trying again.", "warn")
                else:
                    self.log("The site rejected the captcha - please try the new one.", "warn")
                self._apply_values(values)
                continue
            break
        else:
            self.log("Giving up on this captcha after several attempts.", "warn")

    def cmd_click(self, selector: str, values: dict | None = None) -> None:
        self._click_with_captcha(selector, self._label_of(selector), values)
        self._push_page()

    def cmd_navigate(self, text: str, hover: str | None = None) -> None:
        page = self._require_page()
        self.log(f"Opening \u201c{text}\u201d...")
        if hover:
            try:
                page.get_by_text(hover, exact=True).first.hover(timeout=5_000)
                time.sleep(0.4)
            except Exception:  # noqa: BLE001
                pass
        link = page.locator("a").filter(has_text=re.compile(rf"^\s*{re.escape(text)}\s*$")).first
        try:
            link.click(timeout=5_000)
        except Exception:  # noqa: BLE001
            try:
                page.get_by_text(text, exact=True).first.click(timeout=5_000)
            except Exception:  # noqa: BLE001
                link.evaluate("el => el.click()")             # hidden sub-menu item
        self._settle()
        self._push_page()

    def cmd_rescan(self) -> None:
        self._push_page()

    def cmd_preview(self) -> None:
        img = self._preview_data_uri()
        if img:
            self.ui.emit({"type": "preview", "image": img})

    def cmd_download_all(self, out_dir: str) -> None:
        page = self._require_page()
        out = Path(out_dir or DEFAULT_DOWNLOAD_DIR).expanduser()
        out.mkdir(parents=True, exist_ok=True)
        links_sel = ('a:has-text("download"), button:has-text("download"), '
                     'input[type=submit][value*="download" i], input[type=button][value*="download" i]')
        n = page.locator(links_sel).count()
        self.log(f"{n} download link(s) found; saving to {out}")
        saved: list[str] = []
        for i in range(n):
            link = page.locator(links_sel).nth(i)
            try:
                with page.expect_download(timeout=60_000) as dl_info:
                    link.click()
                dl = dl_info.value
                target = unique_path(out / (dl.suggested_filename or f"document_{i + 1}.pdf"))
                dl.save_as(target)
                saved.append(str(target))
                self.log(f"Saved {target.name}")
            except PWTimeout:
                if len(page.context.pages) > 1:                # opened in a new tab instead
                    newp = page.context.pages[-1]
                    newp.wait_for_load_state()
                    target = unique_path(out / f"document_{i + 1}.pdf")
                    target.write_bytes(newp.request.get(newp.url).body())
                    newp.close()
                    saved.append(str(target))
                    self.log(f"Saved {target.name} (from new tab)")
                else:
                    self.log(f"Row {i + 1}: no download was triggered", "warn")
            self.ui.emit({"type": "downloads", "files": saved, "dir": str(out), "done": False, "total": n})
        try:
            page.screenshot(path=str(out / "results.png"), full_page=True)
        except Exception:  # noqa: BLE001
            pass
        self.log(f"Done. {len(saved)} file(s) in {out}")
        self.ui.emit({"type": "downloads", "files": saved, "dir": str(out), "done": True, "total": n})

    def cmd_logout(self) -> None:
        self._close_browser()
        self.status(False, "Not connected")
        self.ui.emit({"type": "reset"})
        self.log("Browser closed.")

    # ---- custom report -------------------------------------------------
    def _check_cancel(self) -> None:
        if self._cancel.is_set():
            raise Aborted()

    def _progress(self, role: str, year: str | None, message: str) -> None:
        total = sum(len(s["rows"]) for s in (self.report or {}).get("sheets", {}).values())
        self.ui.emit({"type": "report_progress", "role": role, "year": year, "message": message, "total": total})

    @staticmethod
    def _find_control(controls: list, kind: str | tuple, label_re: str) -> dict | None:
        kinds = (kind,) if isinstance(kind, str) else kind
        for c in controls:
            if c.get("kind") in kinds and re.search(label_re, c.get("label") or "", re.I):
                return c
        return None

    def _report_controls(self, scan: dict) -> dict:
        """Locate the District / SRO / Year / Name / Search controls on a Buyer- or Seller-Wise page."""
        controls = scan.get("controls", [])
        found = {
            "district": self._find_control(controls, "select", REPORT_LABELS["district"]),
            "sro": self._find_control(controls, "select", REPORT_LABELS["sro"]),
            "year": self._find_control(controls, "select", REPORT_LABELS["year"]),
            "name": (self._find_control(controls, "textarea", ".") or
                     self._find_control(controls, "text", REPORT_LABELS["name"])),
            "search": self._find_control(controls, "button", REPORT_LABELS["search"]),
        }
        missing = [k for k, v in found.items() if v is None]
        if missing:
            labels = ", ".join(f"{c['kind']}:{c.get('label')}" for c in controls)
            raise RuntimeError(f"Could not find the {', '.join(missing)} control(s) on {scan.get('url')}. "
                               f"Controls seen: {labels}")
        return found

    def _select_label(self, selector: str, label: str) -> None:
        """Choose a dropdown option by its visible text (exact, then case-insensitive contains) and wait."""
        page = self._require_page()
        loc = page.locator(selector).first
        try:
            loc.select_option(label=label)
        except Exception:  # noqa: BLE001
            options = loc.evaluate("s => Array.from(s.options).map(o => [o.value, o.text.trim()])")
            want = (label or "").strip().lower()
            match = next((v for v, t in options if t.lower() == want), None) or \
                    next((v for v, t in options if want and want in t.lower()), None)
            if match is None:
                raise RuntimeError(f"Option “{label}” not found; available: "
                                   + ", ".join(t for _, t in options)[:300])
            loc.select_option(value=match)
        self._settle()

    def _best_table(self, scan: dict) -> dict | None:
        tables = [t for t in scan.get("tables", []) if t.get("rows") or t.get("header")]
        if not tables:
            return None
        return max(tables, key=lambda t: (t.get("downloads", 0) > 0, t.get("total_rows", 0)))

    @staticmethod
    def _table_rows(tbl: dict) -> tuple[list[str], list[list[str]]]:
        rows = [list(r) for r in tbl.get("rows", [])]
        header = list(tbl.get("header") or [])
        if not header and rows:
            header, rows = rows[0], rows[1:]
        data = []
        for r in rows:
            text = " ".join(r).strip()
            if not text or PAGER_TEXT.match(text):
                continue                                       # pager row / empty row
            if header and len(r) != len(header):
                if len(r) < len(header) and len(r) <= 2:
                    continue                                   # colspan row (pager, "no records")
                r = (r + [""] * len(header))[:len(header)]
            data.append(r)
        return header, data

    def _wait_for_results(self, timeout: float = 12.0) -> dict:
        """Poll until a data table or a 'no record' style message shows up (AJAX grids)."""
        deadline = time.time() + timeout
        scan = self._scan(RESULT_SCAN_ROWS)
        while time.time() < deadline:
            if self._best_table(scan) or any(
                    re.search(r"no record|not found|no data|no result", m["text"], re.I)
                    for m in scan.get("messages", [])):
                break
            time.sleep(0.6)
            scan = self._scan(RESULT_SCAN_ROWS)
        return scan

    def _prepare_results(self) -> dict:
        """After Search: wait for the grid, switch it to the largest page size, return a fresh scan.
        Used by collection AND by the document re-download so both see the same page numbering."""
        scan = self._wait_for_results()
        picked = self._evaluate(PAGE_SIZE_JS)                  # e.g. DataTables "Show All"
        if picked:
            self.log(f"Page size set to {picked}")
            self._settle()
            scan = self._scan(RESULT_SCAN_ROWS)
        return scan

    def _next_result_page(self, scan: dict) -> dict | None:
        """Click the pager control for the next page; returns the new scan or None when on the last page."""
        tbl = self._best_table(scan)
        if not tbl:
            return None
        nxt = self._evaluate(NEXT_PAGE_JS, tbl.get("index", 0))
        if not nxt or nxt.get("status") != "ok":
            return None
        self._require_page().locator('[data-ereg-next="1"]').first.click()
        self._settle()
        return self._scan(RESULT_SCAN_ROWS)

    def _collect_results(self, role: str, year: str, emit_progress: bool = True) -> tuple[list[str], list[list[str]], list[dict]]:
        """Walk every result page. Returns (header, rows, meta) where meta[i] records where row i
        came from ({"page": n, "cells": [...]}) so its document can be downloaded later."""
        scan = self._prepare_results()
        header: list[str] = []
        rows_all: list[list[str]] = []
        meta: list[dict] = []
        prev_rows: list[list[str]] | None = None
        for page_no in range(1, MAX_RESULT_PAGES + 1):
            self._check_cancel()
            tbl = self._best_table(scan)
            if not tbl:
                break
            hdr, rows = self._table_rows(tbl)
            if rows == prev_rows:
                break                                          # pager click changed nothing
            prev_rows = rows
            if not header:
                header = hdr
            rows_all.extend(rows)
            meta.extend({"page": page_no, "cells": r} for r in rows)
            if emit_progress:
                self._progress(role, year, f"page {page_no}: {len(rows_all)} row(s)")
            scan = self._next_result_page(scan)
            if scan is None:
                break
        return header, rows_all, meta

    def cmd_run_khasra_search(self, params: dict) -> None:
        self._cancel.clear()

        def progress(year: str, number: str, completed: int, total: int, row_count: int) -> None:
            self.ui.emit({"type": "khasra_progress", "year": year, "number": number,
                          "completed": completed, "total": total, "rows": row_count})

        def collect(role: str, year: str) -> tuple[list[str], list[list[str]], list[dict]]:
            return self._collect_results(role, year, emit_progress=False)

        try:
            result = khasra_search.run_search(
                self._require_page(), params,
                scan=self._scan,
                select_label=self._select_label,
                click_search=lambda selector: self._click_with_captcha(selector, "Search"),
                collect_results=collect,
                cancelled=self._cancel.is_set,
                progress=progress,
                log=self.log,
            )
        except Aborted:
            result = {"params": params, "header": [], "rows": [], "partial": True}
        self.ui.emit({"type": "khasra_done", **result})

    def _search_role_year(self, role: str, ctl: dict, year: str, name: str) -> dict:
        """Select the year, fill the name, click Search (captcha if needed). Returns fresh controls."""
        self._select_label(ctl["year"]["selector"], year)
        ctl = self._report_controls(self._scan())              # form may have re-rendered
        self._require_page().locator(ctl["name"]["selector"]).first.fill(name)
        self._click_with_captcha(ctl["search"]["selector"], "Search")
        return ctl

    def _open_role_search(self, role: str, district: str, sro: str) -> dict:
        """Open the role's page and apply District + SRO by label. Returns the controls."""
        self._open_role_page(role)
        ctl = self._report_controls(self._scan())
        self._select_label(ctl["district"]["selector"], district)
        ctl = self._report_controls(self._scan())
        self._select_label(ctl["sro"]["selector"], sro)
        return self._report_controls(self._scan())

    def cmd_run_report(self, params: dict) -> None:
        roles = [r for r in ("buyer", "seller") if r in (params.get("roles") or [])]
        if not roles:
            raise ValueError("Pick Buyer, Seller or both.")
        name = " ".join(str(params.get("name") or "").split())
        y_from, y_to = sorted((int(params["from_year"]), int(params["to_year"])))
        district, sro = params.get("district") or "", params.get("sro") or ""
        if not district or not sro:
            raise ValueError("Pick a District and a Sub-Registrar Office first.")

        self._cancel.clear()
        clean_params = {**params, "from_year": y_from, "to_year": y_to, "name": name, "roles": roles}
        save_settings({"report": {k: clean_params[k] for k in ("district", "sro", "from_year", "to_year", "name", "roles")}})
        self.report = {"params": {**clean_params, "started": time.strftime("%Y-%m-%d %H:%M")},
                       "sheets": {r: {"header": [], "rows": []} for r in roles},
                       "meta": {r: [] for r in roles},           # per row: role/year/page/cells (for downloads)
                       "partial": False}
        self.log(f"Report: {', '.join(roles)} | {district} / {sro} | {y_from}-{y_to} | “{name}”")
        try:
            for role in roles:
                self._check_cancel()
                self._progress(role, None, f"Opening {ROLE_PAGES[role]} page")
                ctl = self._open_role_search(role, district, sro)
                years = sorted({o["label"].strip() for o in ctl["year"]["options"]
                                if o["label"].strip().isdigit() and y_from <= int(o["label"]) <= y_to}, key=int)
                if not years:
                    self.log(f"No year options between {y_from} and {y_to} on the {ROLE_PAGES[role]} page.", "warn")
                sheet, meta = self.report["sheets"][role], self.report["meta"][role]
                for year in years:
                    self._check_cancel()
                    self._progress(role, year, "searching")
                    ctl = self._search_role_year(role, ctl, year, name)
                    header, rows, row_meta = self._collect_results(role, year)
                    if header and not sheet["header"]:
                        party = party_column_index(header, role)
                        sheet["header"] = ["ID", "Year", *header, "Relation", "Relative"]
                        sheet["party_column"] = header[party] if party is not None else ""
                        sheet["party_index"] = party + 2 if party is not None else -1   # after ID, Year
                    party = party_column_index(header, role) if header else None
                    for n, r in enumerate(rows, start=1):
                        rid = f"{role[0].upper()}{year}-{n:04d}"               # B2023-0001, S2024-0017
                        relation, relative = parse_relation(r[party]) if party is not None and party < len(r) else ("", "")
                        sheet["rows"].append([rid, year, *r, relation, relative])
                    meta.extend({"role": role, "year": year, **m} for m in row_meta)
                    self.log(f"{ROLE_PAGES[role]} {year}: {len(rows)} row(s)")
                    self._progress(role, year, f"done: {len(rows)} row(s)")
        except Aborted:
            if not self._alive:
                raise
            self.report["partial"] = True
            self.log("Report stopped - showing what was collected so far.", "warn")
        total = sum(len(s["rows"]) for s in self.report["sheets"].values())
        self.log(f"Report finished: {total} row(s).")
        self.ui.emit({"type": "report_done", **{k: v for k, v in self.report.items() if k != "meta"}})

    # ---- documents for selected report rows ------------------------------------------------
    def cmd_download_rows(self, selection: dict, out_dir: str) -> None:
        """Re-run the searches behind the selected report rows and download each row's document.

        selection = {"buyer": [row indexes into sheets["buyer"]["rows"]], "seller": [...]}.
        Rows are grouped by (role, year, result page) so each page is opened once.
        """
        if not self.report:
            raise RuntimeError("Run a report first.")
        params = self.report["params"]
        out = Path(out_dir or DEFAULT_DOWNLOAD_DIR).expanduser()
        out.mkdir(parents=True, exist_ok=True)
        self._cancel.clear()

        jobs: dict[tuple[str, str, int], list[tuple[int, dict]]] = {}
        for role, idxs in (selection or {}).items():
            metas = (self.report.get("meta") or {}).get(role) or []
            for i in sorted(set(int(x) for x in (idxs or []))):
                if 0 <= i < len(metas):
                    m = metas[i]
                    jobs.setdefault((role, m["year"], int(m["page"])), []).append((i, m))
        total = sum(len(v) for v in jobs.values())
        if not total:
            self.log("No rows selected for download.", "warn")
            return
        self.log(f"Downloading {total} document(s) to {out}")

        saved: list[str] = []
        done = 0
        current: tuple[str, str] | None = None                 # (role, year) whose results are on screen
        ctl: dict | None = None
        scan: dict | None = None
        at_page = 0

        def goto_page(scan_in: dict | None, wanted: int) -> dict:
            nonlocal at_page
            if scan_in is None or at_page > wanted:            # grid was reset: start from page 1 again
                scan_in, at_page = self._prepare_results(), 1
            while at_page < wanted:
                self._check_cancel()
                scan_in = self._next_result_page(scan_in)
                if scan_in is None:
                    raise RuntimeError(f"Result page {wanted} is not reachable any more.")
                at_page += 1
            return scan_in

        try:
            for (role, year, page_no) in sorted(jobs, key=lambda k: (k[0], int(k[1]), k[2])):
                self._check_cancel()
                sheet_rows = self.report["sheets"][role]["rows"]
                if current != (role, year):
                    self._progress(role, year, f"opening {ROLE_PAGES[role]} results")
                    if ctl is None or current is None or current[0] != role:
                        ctl = self._open_role_search(role, params["district"], params["sro"])
                    ctl = self._search_role_year(role, ctl, year, params["name"])
                    scan, at_page, current = self._prepare_results(), 1, (role, year)
                scan = goto_page(scan, page_no)
                tbl = self._best_table(scan)
                if not tbl:
                    raise RuntimeError(f"No results table for {role} {year}.")
                page_rows = self._table_rows(tbl)[1]

                seen_cells: dict[tuple, int] = {}
                for i, m in jobs[(role, year, page_no)]:
                    self._check_cancel()
                    key = tuple(m["cells"])
                    nth = seen_cells.get(key, 0)
                    seen_cells[key] = nth + 1
                    found = self._evaluate(ROW_LINK_JS, [tbl.get("index", 0), m["cells"], nth])
                    if not found or found.get("status") != "ok":
                        self.log(f"Row {i + 1} ({role} {year}): document link not found ({(found or {}).get('status')}).", "warn")
                        continue
                    row_id = str(sheet_rows[i][0]) if i < len(sheet_rows) else f"{role[0].upper()}{year}-row{i + 1}"
                    target = self._download_marked(out, re.sub(r"[^\w\-]+", "_", row_id))   # file = <ID>.pdf
                    done += 1
                    if target:
                        saved.append(str(target))
                        self.log(f"Saved {target.name}")
                    self._progress(role, year, f"downloaded {done}/{total}")
                    self.ui.emit({"type": "downloads", "files": saved, "dir": str(out), "done": False, "total": total})
                    # a LinkButton postback may have re-rendered the grid: make sure we are still on this page
                    scan = self._scan(RESULT_SCAN_ROWS)
                    tbl_now = self._best_table(scan)
                    if not tbl_now or self._table_rows(tbl_now)[1] != page_rows:
                        at_page = MAX_RESULT_PAGES + 1                 # force a restart from page 1
                        scan = goto_page(None, page_no)
                        tbl = self._best_table(scan)
                        if not tbl:
                            raise RuntimeError("Lost the results table after a download.")
                        page_rows = self._table_rows(tbl)[1]
                    else:
                        tbl = tbl_now
        except Aborted:
            if not self._alive:
                raise
            self.log("Download stopped.", "warn")
        self.log(f"Documents: {len(saved)} file(s) saved in {out}")
        self.ui.emit({"type": "downloads", "files": saved, "dir": str(out), "done": True, "total": total})

    def _download_marked(self, out: Path, stem: str) -> Path | None:
        """Click the element marked by ROW_LINK_JS and save the resulting download."""
        page = self._require_page()
        link = page.locator('[data-ereg-dl="1"]').first
        try:
            with page.expect_download(timeout=60_000) as dl_info:
                link.click()
            dl = dl_info.value
            suffix = Path(dl.suggested_filename or "").suffix or ".pdf"
            target = unique_path(out / f"{stem}{suffix}")
            dl.save_as(target)
            return target
        except PWTimeout:
            if len(page.context.pages) > 1:                    # opened in a new tab instead
                newp = page.context.pages[-1]
                newp.wait_for_load_state()
                target = unique_path(out / f"{stem}.pdf")
                target.write_bytes(newp.request.get(newp.url).body())
                newp.close()
                return target
            self.log(f"{stem}: no download was triggered", "warn")
            return None

    # ---- report export (called from the Api thread; report is complete by then) -------------
    def export_report(self, path: str, selection: dict | None) -> dict:
        import report_export  # local module, kept separate so other scripts can use it
        if not self.report:
            raise RuntimeError("No report to save yet.")
        sheets = {}
        for role, sheet in self.report["sheets"].items():
            rows = sheet["rows"]
            if selection and role in selection and selection[role] is not None:
                keep = set(int(i) for i in selection[role])
                rows = [r for i, r in enumerate(rows) if i in keep]
            sheets[ROLE_PAGES[role].split()[0]] = {"header": sheet["header"], "rows": rows}   # "Buyer" / "Seller"
        for name in ("Buyer", "Seller"):
            sheets.setdefault(name, {"header": [], "rows": []})
        return report_export.write_report(path, sheets, self.report["params"])


# ------------------------------------------------------------------ JS API (window.pywebview.api.*)
class Api:
    """Exposed to JavaScript. Methods must return JSON-serialisable values and never block for long."""

    def __init__(self, worker: Worker) -> None:
        self._worker = worker
        self._window = None          # set by main(); "_" names are not exposed to JS

    def defaults(self) -> dict:
        if captcha_solvers is not None:
            captcha_solvers.reload_settings()                  # pick up edits to captcha_settings.json
            solvers = captcha_solvers.list_solvers()
        else:
            solvers = [{"id": "manual", "label": "Type it myself", "available": True, "reason": "", "model": ""}]
        settings = load_settings()
        credentials = settings.get("credentials") or {}
        remember_credentials = bool(credentials.get("remember")) if isinstance(credentials, dict) else False
        saved_username = credentials.get("username", "") if remember_credentials else ""
        saved_password = credentials.get("password", "") if remember_credentials else ""
        public_settings = {key: value for key, value in settings.items() if key != "credentials"}
        return {
            # Environment overrides saved credentials, which override the CLI defaults.
            "username": os.environ.get("UK_EREG_USERNAME") or saved_username or getattr(script, "USERNAME", "") or "",
            "password": os.environ.get("UK_EREG_PASSWORD") or saved_password or getattr(script, "PASSWORD", "") or "",
            "remember_credentials": remember_credentials,
            "solvers": solvers,
            "default_solver": os.environ.get("CAPTCHA_SOLVER") or settings.get("solver") or "manual",
            "download_dir": settings.get("download_dir") or str(DEFAULT_DOWNLOAD_DIR),
            "login_url": script.LOGIN_URL,
            "settings": public_settings,                      # last-used report fields, options
            "frozen": FROZEN,
            "app_dir": str(APP_DIR),
        }

    def save_prefs(self, patch: dict) -> bool:
        """Remember UI preferences and optionally the login credentials."""
        allowed = {"solver", "headed", "preview", "download_dir", "report"}
        clean = {k: v for k, v in (patch or {}).items() if k in allowed}
        if isinstance(patch, dict) and "remember_credentials" in patch:
            has_credentials = isinstance(patch.get("username"), str) and isinstance(patch.get("password"), str)
            if patch["remember_credentials"] and has_credentials:
                clean["credentials"] = {
                    "remember": True,
                    "username": patch["username"].strip(),
                    "password": patch["password"],
                }
            elif not patch["remember_credentials"]:
                clean["credentials"] = {"remember": False, "username": "", "password": ""}
        if clean:
            save_settings(clean)
        return True

    def captcha_config(self) -> dict:
        if captcha_solvers is None:
            return {"ok": False, "error": "Captcha provider settings are unavailable."}
        return {"ok": True, **captcha_solvers.user_settings_for_ui()}

    def save_captcha_config(self, values: dict, clear: list | None = None) -> dict:
        if captcha_solvers is None:
            return {"ok": False, "error": "Captcha provider settings are unavailable."}
        captcha_solvers.save_user_settings(values if isinstance(values, dict) else {}, clear or [])
        return {
            "ok": True,
            **captcha_solvers.user_settings_for_ui(),
            "solvers": captcha_solvers.list_solvers(),
        }

    def download_rows(self, selection: dict, out_dir: str) -> dict:
        n = sum(len(v or []) for v in (selection or {}).values())
        if not n:
            return {"ok": False, "error": "No rows are shown - nothing to download."}
        if out_dir:
            save_settings({"download_dir": out_dir})
        self._worker.post("download_rows", selection=selection or {}, out_dir=out_dir, _what="Downloading documents...")
        return {"ok": True, "count": n}

    def login(self, username: str, password: str, headed: bool = False,
              solver: str = "manual", preview: bool = True) -> dict:
        if not username or not password:
            return {"ok": False, "error": "Username and password are required."}
        known = set(captcha_solvers.SOLVERS) if captcha_solvers is not None else {"manual"}
        self._worker.post("login", username=username, password=password, headed=bool(headed),
                          solver=solver if solver in known else "manual",
                          preview=bool(preview), _what="Logging in...")
        return {"ok": True}

    def submit_captcha(self, text: str) -> bool:
        self._worker.captcha_answers.put(re.sub(r"\s+", "", text or ""))
        return True

    def skip_captcha(self) -> bool:
        self._worker.captcha_answers.put("")
        return True

    def cancel_captcha(self) -> bool:
        self._worker.captcha_answers.put(CANCEL)
        return True

    def set_options(self, opts: dict) -> bool:
        if isinstance(opts, dict) and "preview" in opts:
            self._worker.opts["preview"] = bool(opts["preview"])
        return True

    def set_value(self, selector: str, value, kind: str = "text") -> bool:
        self._worker.post("set_value", selector=selector, value=value, kind=kind, _what="Updating page...")
        return True

    def click(self, selector: str, values: dict | None = None) -> bool:
        self._worker.post("click", selector=selector, values=values or {}, _what="Submitting...")
        return True

    def navigate(self, text: str, hover: str | None = None) -> bool:
        self._worker.post("navigate", text=text, hover=hover, _what="Navigating...")
        return True

    def rescan(self) -> bool:
        self._worker.post("rescan", _what="Reading page...")
        return True

    def preview(self) -> bool:
        self._worker.post("preview", _what="Taking screenshot...")
        return True

    def download_all(self, out_dir: str) -> bool:
        self._worker.post("download_all", out_dir=out_dir, _what="Downloading...")
        return True

    def logout(self) -> bool:
        self._worker.post("logout", _what="Closing browser...")
        return True

    # ---- modes & custom report ----------------------------------------
    def open_mode(self, mode: str) -> bool:
        self._worker.post("open_mode", mode=mode, _what="Opening page...")
        return True

    def menu(self) -> bool:
        self._worker.post("menu", _what="Back to menu...")
        return True

    def run_report(self, params: dict) -> dict:
        try:
            int(params.get("from_year")), int(params.get("to_year"))
        except (TypeError, ValueError):
            return {"ok": False, "error": "Pick From and To years."}
        self._worker.post("run_report", params=params or {}, _what="Running report...")
        return {"ok": True}

    def run_khasra_search(self, params: dict) -> dict:
        params = params if isinstance(params, dict) else {}
        if not khasra_search.parse_khasra_numbers(params.get("khasra_numbers", "")):
            return {"ok": False, "error": "Enter at least one Khasra number."}
        try:
            int(params.get("from_year")), int(params.get("to_year"))
        except (TypeError, ValueError):
            return {"ok": False, "error": "Pick valid From and To years."}
        if not params.get("district") or not params.get("sro"):
            return {"ok": False, "error": "Pick a District and a Sub-Registrar Office first."}
        self._worker.post("run_khasra_search", params=params, _what="Searching Khasra numbers...")
        return {"ok": True}

    def cancel_report(self) -> bool:
        self._worker._cancel.set()
        self._worker.captcha_answers.put(CANCEL)       # also unblocks a pending captcha prompt
        return True

    def save_report(self, default_name: str, selection: dict | None = None) -> dict:
        """Ask where to save, then write the Excel (or CSV fallback) file. Returns {"ok", "path", "format"}."""
        if self._window is None or not self._worker.report:
            return {"ok": False, "error": "Nothing to save yet."}
        file_dialog = getattr(webview, "FileDialog", None)
        dialog_type = file_dialog.SAVE if file_dialog else webview.SAVE_DIALOG  # type: ignore[attr-defined]
        start_dir = Path(load_settings().get("report_dir") or DEFAULT_DOWNLOAD_DIR)
        start_dir.mkdir(parents=True, exist_ok=True)
        result = self._window.create_file_dialog(dialog_type, directory=str(start_dir),
                                                 save_filename=default_name or "report.xlsx")
        if not result:
            return {"ok": False, "cancelled": True}
        path = result[0] if isinstance(result, (list, tuple)) else str(result)
        try:
            info = self._worker.export_report(path, selection)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": first_line(exc)}
        save_settings({"report_dir": str(Path(path).parent)})
        return {"ok": True, **info}

    def open_path(self, path: str) -> bool:
        p = Path(path or "").expanduser()
        if not p.exists():
            return False
        try:
            system = platform.system()
            if system == "Darwin":
                subprocess.Popen(["open", str(p)])
            elif system == "Windows":
                os.startfile(str(p))  # type: ignore[attr-defined]
            else:
                subprocess.Popen(["xdg-open", str(p)])
            return True
        except Exception:  # noqa: BLE001
            return False

    def transliterate(self, text: str, num: int = 5) -> dict:
        """Devanagari candidates for Roman text (hindi_input.js calls this for the word at the caret)."""
        if hindi_input is None:
            return {"ok": False, "error": "hindi_input.py not found next to the GUI", "candidates": []}
        cands = hindi_input.suggest(text or "", int(num or 5))      # Google Input Tools, rules if offline
        return {"ok": True, "candidates": cands, "source": hindi_input.last_source}

    def choose_folder(self, current: str = "") -> str | None:
        if self._window is None:
            return None
        file_dialog = getattr(webview, "FileDialog", None)
        dialog_type = file_dialog.FOLDER if file_dialog else webview.FOLDER_DIALOG  # type: ignore[attr-defined]
        start = current if current and os.path.isdir(current) else str(Path.home())
        result = self._window.create_file_dialog(dialog_type, directory=start)
        if not result:
            return None
        return result[0] if isinstance(result, (list, tuple)) else str(result)

    def open_folder(self, path: str) -> bool:
        p = Path(path or DEFAULT_DOWNLOAD_DIR).expanduser()
        p.mkdir(parents=True, exist_ok=True)
        system = platform.system()
        try:
            if system == "Darwin":
                subprocess.Popen(["open", str(p)])
            elif system == "Windows":
                os.startfile(str(p))  # type: ignore[attr-defined]
            else:
                subprocess.Popen(["xdg-open", str(p)])
            return True
        except Exception:  # noqa: BLE001
            return False


# ------------------------------------------------------------------ misc
def first_line(exc: BaseException) -> str:
    return (str(exc).strip().splitlines() or ["(no details)"])[0]


def load_html() -> str:
    """gui.html with its local <script src="x.js"> files inlined (the page is handed to pywebview as a string)."""
    html = HTML_FILE.read_text(encoding="utf-8")

    def inline(match: re.Match) -> str:
        path = HERE / match.group(1)
        if path.is_file():
            return "<script>\n" + path.read_text(encoding="utf-8") + "\n</script>"
        return match.group(0)

    return re.sub(r'<script src="([A-Za-z0-9_.-]+\.js)"></script>', inline, html)


def unique_path(target: Path) -> Path:
    if not target.exists():
        return target
    stem, suffix = target.stem, target.suffix
    for i in range(2, 1000):
        candidate = target.with_name(f"{stem} ({i}){suffix}")
        if not candidate.exists():
            return candidate
    return target


def main() -> None:
    if not HTML_FILE.exists():
        sys.exit(f"gui.html not found next to this script ({HTML_FILE}).")

    ui = UIBridge()
    worker = Worker(ui)
    worker.start()
    api = Api(worker)

    window = webview.create_window(
        WINDOW_TITLE,
        html=load_html(),
        js_api=api,
        width=1280, height=880, min_size=(900, 620),
        text_select=True,
    )
    ui.attach(window)
    api._window = window

    def on_closing(*_args):
        ui.closing = True             # stop pushing events into a window that is going away
        worker.shutdown(timeout=3.0)  # unblock a pending captcha, close Chromium

    window.events.closing += on_closing

    sys.stdout = StdoutTee(sys.stdout, ui)   # script's print() lines show up in the GUI log
    webview.start(debug="--debug" in sys.argv)


if __name__ == "__main__":
    main()
