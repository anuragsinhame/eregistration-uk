# CLAUDE.md — project notes for AI-assisted editing

Desktop GUI (pywebview + Playwright) around the Uttarakhand Stamp & Registration e-Search portal.
Read `README.md` for the full story; this file is the short version plus the rules of the road.

## What this project is

- `uk_eregistration.py` — the ORIGINAL CLI script (owner's). The GUI imports its constants
  (`LOGIN_URL`, `SEARCH_URL`, `SEL_*`, `USERNAME`, `PASSWORD`, `MAX_LOGIN_ATTEMPTS`, `DOWNLOAD_DIR`)
  and `go_to_buyer_wise()`. Keep it importable and runnable on its own; don't refactor it casually.
- `uk_ereg_gui.py` — the app. One process, two threads:
  - main thread: pywebview window; `Api` methods are called from JS and must return quickly
    (JSON-serialisable values only; names starting with `_` are not exposed).
  - `Worker` thread: the ONLY code allowed to touch Playwright. Commands arrive on `worker.cmds`
    as `("name", kwargs)` → `cmd_name(**kwargs)`. Every command should end with `_push_page()`
    (or emit its own result event). Captcha answers arrive on `worker.captcha_answers`.
  - `UIBridge.emit(dict)` → `window.app.onEvent(dict)` in the page. Add new event types in
    `onEvent` in `gui.html` at the same time.
- `gui.html` — the entire front end, no framework, no build step. `hindi_input.js` is loaded via
  `<script src>` and inlined by `load_html()` at startup (pywebview gets the page as a string), so
  local script tags must look exactly like `<script src="name.js"></script>`.
- `hindi_input.js` / `hindi_input.py` — Hindi typing (Roman → Devanagari). Standalone; nothing
  site-specific goes in here.
- `captcha_solvers.py` — captcha readers (manual / Claude / OpenAI / Gemini / OpenAI-compatible /
  Ollama / Tesseract). Standard library only; settings via the GUI's **Captcha settings** dialog,
  env vars, or `captcha_settings.json`.
- `report_export.py` — Excel/CSV writer for the custom report. Standalone.
- `khasra_search.py` — standalone Khasra No. Wise navigation, number-list parsing, and year-range search workflow; called by the Playwright worker.
- `setup_env.py` — new-machine setup (venv + requirements + `playwright install chromium`). pip cannot
  run post-install steps, so browsers are never in requirements; the app also self-installs them.
- `build_app.py` — PyInstaller packaging (bundles Chromium into the app). `.github/workflows/build.yml`
  runs tests/build checks on all branch pushes; pushes to `main` bump `VERSION`, build macOS `.dmg`
  and Windows `.exe` artifacts, and refresh the versioned and `latest` releases linked in README.md.
- `tests/` — mock-based tests (`python tests/run_all.py`), the mock Buyer Wise page, and
  `build_demo.py` which builds `tests/demo_harness.html` (GUI on a fake bridge, open in a browser).

Paths: `HERE` = read-only resources (`sys._MEIPASS` when frozen), `APP_DIR` = writable per-user
folder (project dir in development; Application Support / AppData when frozen). `settings.json`
(last-used values) and `captcha_settings.json` live in `APP_DIR`; downloads default to
`APP_DIR/downloads` or `~/Downloads/uk_ereg` when frozen. Never write next to `HERE` when frozen.

## Key flows (where to look)

| Flow | Code |
|---|---|
| Browser start / first run | `_ensure_browser` → on Playwright's "Executable doesn't exist" (`BROWSER_MISSING`) → `_install_browsers` runs `playwright_install_command()` (the package's own Node driver, so it also works frozen) with progress in the status bar, then retries. `setup_env.py` does the same up front. |
| Login + captcha | `Worker.cmd_login`, `_get_captcha_answer` (single place for ALL captchas) |
| Reading a page | `SCAN_JS` (runs in the site's page) → `_scan()` → `page` event → `renderPage()` in gui.html |
| Waiting for postbacks | `_settle()` (load + networkidle + quiet period with no main-frame navigation), `_evaluate()` retries |
| Modes after login | `cmd_open_mode` (`buyer` / `seller` / `report`), `cmd_menu` |
| Custom report | `cmd_run_report` → `_report_controls` (label regexes `REPORT_LABELS`) → `_select_label` → `_click_with_captcha` → `_collect_results` (`PAGE_SIZE_JS`, `NEXT_PAGE_JS`). Row shape: `[ID, Year, *site cols, Relation, Relative]`; ID = `B2023-0001` (role letter, year, running no.); `party_column_index()` + `parse_relation()` fill the last two from the searched party's column (`PARTY_COLUMN`, `RELATION_RE`). Sheet carries `party_index` / `party_column`. |
| Khasra search | `cmd_run_khasra_search` delegates to `khasra_search.py`; selects District/SRO and each available year, then searches the parsed Khasra list using the shared captcha and result-pagination helpers. |
| Filter + Excel | gui.html `renderReportTable`/`visibleRows`/`rowMatches`: chip terms (`RELATION_CHIPS`) = whole-word match in the party column, other words = substring in the chosen scope; `Api.save_report` → `Worker.export_report` → `report_export.write_report` |
| Documents for shown rows | `report["meta"]` (role/year/page/cells per row, filled by `_collect_results`) → `Api.download_rows` → `cmd_download_rows` (`_open_role_search`, `_search_role_year`, `_prepare_results`, `_next_result_page`, `ROW_LINK_JS`, `_download_marked`); file name = the row's ID |
| Remembered settings | `load_settings`/`save_settings` (settings.json); `Api.defaults()` returns them; `Api.save_prefs`; `_apply_saved_report_prefs` in `cmd_open_mode("report")`; `cmd_run_report` saves the report fields |
| Hindi typing | `HindiInput.attach(...)` in gui.html; provider = `Api.transliterate` → `hindi_input.suggest` |
| Captcha solvers | `captcha_solvers.SOLVERS` registry; `_get_captcha_answer` tries the chosen solver, `SOLVER_MAX_STRIKES` rejections → manual |

## Conventions

- Python 3.14 on the owner's Mac (venv in `.venv`); code is written to also run on 3.10+.
  Only add dependencies to `requirements-gui.txt` when really needed; prefer `urllib` over SDKs.
- Playwright sync API: never call it from the main thread or from `Api`; post a command instead.
- Every Playwright wait goes through `_settle()`; every `page.evaluate` goes through `_evaluate()`.
- Site text is Hindi (Devanagari) — keep everything UTF-8, `ensure_ascii=True` when embedding JSON in JS.
- Log with `self.log(msg, level)` (`"info"`/`"warn"`) — it shows in the GUI log panel; `print()` is
  mirrored there too (`StdoutTee`).
- The UI uses the `.hidden` class for its own elements; `hindi_input.js` uses the `hidden`
  attribute and includes its own `[hidden]{display:none!important}` rule.
- Don't put real API keys or passwords in code. Credentials come from `uk_eregistration.py`
  (owner's choice) or `UK_EREG_USERNAME` / `UK_EREG_PASSWORD`; solver keys from
  `captcha_settings.json` (git-ignored) or env vars.

## Testing

```
python tests/run_all.py          # must pass before finishing a change
python tests/build_demo.py       # regenerate the browser demo after UI changes
```

`tests/test_flow.py` fakes Playwright (`FakePage`) and drives the real `Worker`; extend it when
adding commands or events. Real-site behaviour can only be checked by the owner running
`python uk_ereg_gui.py` (use "Show browser window" and the log panel).

## Known site facts (from real runs)

- Login page ids: `#MainContent_txtUsername`, `#MainContent_txtPassword`,
  `#MainContent_imgCaptcha`, `#MainContent_txtCaptcha`, `#MainContent_btnLogin`.
- Buyer Wise page: labels "District", "Sub-Registrar Office", "Registration Year",
  "Enter Buyer Name" (textarea), a "Search" button, plus a "Search..." text box that is NOT the
  submit button. The page fires a second postback right after loading (prefills the SRO list) —
  that is why `_settle()` waits for a navigation-free quiet period.
- The menu item for buyers is "Buyer Wise" under "Search By Party Name" (hover to reveal);
  the seller page is assumed to be "Seller Wise" (`ROLE_PAGES`). Menu URL ends up as
  `Buyer_Wise.aspx#`.
- No captcha was seen on the search pages so far; the code handles one if it appears.
- Result grid columns (Buyer Wise): "…Details", "Seller/First Party", "Buyer/Second Party",
  "Buyer/Second Gender", "SRO Name", "Trans Value", "Market Value", "Download Document". Party
  cells look like `अनुराग गुप्ता S/O ठाकुर दास गुप्ता , ,` (relation in upper case, trailing commas);
  HUF entries: `... एच0यू0एफ0 द्वारा कर्ता श्री <name> S/O <father> , ,`.

## Packaging notes

- `python build_app.py --zip` on the target OS. Chromium is copied into the bundle AFTER PyInstaller
  (PyInstaller's data collection would flatten Chromium.app's symlinks on macOS); on macOS the real
  copy goes under `Contents/Resources/...` with a symlink from `Contents/Frameworks/...` because Node
  resolves the driver's real path. Keep that in sync with the `.local-browsers` check in `uk_ereg_gui.py`.
- Builds are unsigned; README explains the "Open Anyway" / SmartScreen step.

## Not done yet / ideas

- macOS code signing / notarisation in the workflow.
- Report history panel; unattended/scheduled report runs (needs a configured captcha solver).
