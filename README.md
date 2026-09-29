precedence. Uncheck the option to clear saved credentials. `Api.defaults()` returns settings to the
page; opening
# UK e-Registration Search — desktop GUI

A small desktop app around the Uttarakhand Stamp & Registration **e-Search** portal
(`online.eregistrationukgov.in/E_Search`). It logs in for you, shows every captcha inside the app
(or reads it with an LLM of your choice), exposes the site's Buyer Wise / Seller Wise pages as
native-looking forms, lets you type Hindi names in English, builds multi-year "custom reports"
that you can filter and save as Excel, and downloads the documents behind the rows you keep.

Everything runs locally: a headless Chromium (Playwright) does the browsing, Python drives it, and a
pywebview window shows an HTML/JS front end.

## Download the app (no Python needed)

Every push or merged pull request to `main` automatically increments the patch version in [`VERSION`](VERSION), builds the macOS disk image and Windows executable, and publishes both a versioned release and the **Latest build** release. Pushes to other branches run tests and packaging checks without publishing artifacts.

| Platform | Download |
|---|---|
| macOS (Apple Silicon) | [UK-e-Registration-Search-macos-arm64.dmg](https://github.com/anuragsinhame/eregistration-uk/releases/latest/download/UK-e-Registration-Search-macos-arm64.dmg) |
| Windows (64-bit) | [UK-e-Registration-Search-windows-x64.exe](https://github.com/anuragsinhame/eregistration-uk/releases/latest/download/UK-e-Registration-Search-windows-x64.exe) |
| All builds & history | [Releases page](https://github.com/anuragsinhame/eregistration-uk/releases) · [Actions runs](https://github.com/anuragsinhame/eregistration-uk/actions) |

Chromium is bundled, so nothing else has to be installed. First launch:

* **macOS**: the app is not notarised, so macOS blocks it once. Either System Settings → Privacy &
  Security → **Open Anyway**, or in Terminal: `xattr -dr com.apple.quarantine "UK e-Registration Search.app"`.
  If needed, clear all extended attributes recursively:

  ```bash
  xattr -cr "UK e-Registration Search.app"
  ```
* **Windows**: run `UK-e-Registration-Search-windows-x64.exe`; SmartScreen → *More info* → *Run anyway*.
  Needs the WebView2 runtime (already present on Windows 10/11 with Edge).

The packaged app keeps its settings, captcha keys and downloads under
`~/Library/Application Support/UK e-Registration Search/` (macOS) or `%APPDATA%\UK e-Registration Search\`
(Windows); downloads default to `~/Downloads/uk_ereg`.

---

## 1. Quick start (from source)

New machine, one command (creates `.venv`, installs the requirements, downloads Chromium):

```bash
cd ~/RoxStar/Tools/data_checker
python3 setup_env.py
source .venv/bin/activate                   # Windows: .venv\Scripts\activate
python uk_ereg_gui.py                       # add --debug to get the web inspector
```

Or by hand:

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-gui.txt        # pywebview, playwright, openpyxl, anthropic
python uk_ereg_gui.py
```

`requirements.txt` can't download a browser (pip has no post-install step), so Chromium is fetched
by `setup_env.py` — or, if you skip that, **by the app itself**: the first time the browser is
missing, the worker runs Playwright's installer, shows the download progress in the status bar and
log, and then continues with the login. (The packaged app ships with Chromium and never needs this.)

Then: check the credentials (prefilled from `uk_eregistration.py` / env vars), pick how captchas
should be solved, press **Log in**, and choose **Search for Buyer**, **Search for Seller** or
**Custom report**.

Environment variables (all optional):

| Variable | Purpose |
|---|---|
| `UK_EREG_USERNAME`, `UK_EREG_PASSWORD` | Override the credentials from `uk_eregistration.py` |
| `CAPTCHA_SOLVER` | Pre-select a solver in the login form (`manual`, `claude`, `openai`, `gemini`, `compatible`, `ollama`, `tesseract`) |
| API keys / models for the solvers | See section 5 — or put them in `captcha_settings.json` |

You can also open **Captcha settings** from the app header to save provider keys, models, compatible API endpoints, and local tool paths. Keys are stored in the app's local `captcha_settings.json`; the file is not encrypted. Environment variables take precedence over saved values.

---

## 2. Files

| File | What it is | Edit it when… |
|---|---|---|
| `uk_ereg_gui.py` | The app: Playwright worker thread, page reader (JS), custom report engine, the Python API the page calls, window setup. | …changing what the app does with the site. |
| `gui.html` | The whole front end (HTML + CSS + JS in one file). Renders events from Python, sends calls back. | …changing the look or the UI flow. |
| `hindi_input.js` | Reusable Hindi typing widget for any `<input>`/`<textarea>`: suggestion popup + on-screen Devanagari keyboard. No dependencies. | …changing typing behaviour or reusing it in another page. |
| `hindi_input.py` | Roman → Devanagari candidates: Google Input Tools with an offline phonetic fallback. Also a CLI. | …tuning transliteration rules or the exceptions list. |
| `captcha_solvers.py` | Pluggable captcha readers (Claude, OpenAI, Gemini, any OpenAI-compatible API, Ollama, Tesseract). Also a CLI. | …adding a provider, changing models or the prompt. |
| `captcha_settings.example.json` | Template for `captcha_settings.json` (keys and models). | Copy, rename, fill in. |
| `report_export.py` | Writes the report to `.xlsx` (Buyer / Seller / Summary sheets) or CSV fallback. | …changing the Excel layout. |
| `uk_eregistration.py` | The original command-line script. The GUI imports its URLs, login selectors and `go_to_buyer_wise()`. Still runnable on its own. | …the site changes its login page ids or menu. |
| `setup_env.py` | One-time setup on a new machine: `.venv`, requirements, Chromium, a `captcha_settings.json` from the example. | |
| `build_app.py` | Packages the app with PyInstaller (bundles Chromium); used locally and by CI. See section 9. | …changing what goes into the app. |
| `.github/workflows/build.yml` | GitHub Actions: branch build checks and versioned macOS + Windows releases from `main`. | …changing the release process. |
| `VERSION` | Current project version; main-branch builds increment its patch number. | …starting a new major or minor release series. |
| `requirements-gui.txt` | Python dependencies for the GUI. | |
| `tests/` | Mock-based tests, a mock Buyer Wise page and a browser demo of the GUI. See section 8. | |
| `downloads/` | Default folder for downloaded documents and saved reports (created on first use). | |

Generated / private, safe to delete: `__pycache__/`, `settings.json` (last-used values),
`captcha_settings.json` (your keys), `build/`, `dist/`, `*.spec`, `tests/demo_harness*.html`,
`tests/_tmp_settings.json`.

---

## 3. How it works

### 3.1 Threads and messages

```
 ┌──────────────── pywebview window (main thread) ────────────────┐
 │  gui.html  ── window.pywebview.api.<method>(...) ──►  Api      │   (JS → Python: one call per
 │            ◄── window.app.onEvent({type, ...})  ──   UIBridge  │    user action, returns fast)
 └────────────────────────────────────────────────────────────────┘
                     │ Api.post(...) puts a command on a queue
                     ▼
 ┌──────────── Worker thread (the only one touching Playwright) ──┐
 │  cmd_login / cmd_open_mode / cmd_set_value / cmd_click /       │
 │  cmd_navigate / cmd_download_all / cmd_run_report / ...        │
 │  needs a captcha? ─► emit {type:"captcha", image} ─► wait on   │
 │                      captcha_answers queue (GUI answers)       │
 └────────────────────────────────────────────────────────────────┘
```

* **`Api`** (in `uk_ereg_gui.py`) is the object exposed to JavaScript. Its methods validate input,
  enqueue a command and return immediately, so the window never freezes. A few (`transliterate`,
  `save_report`, `choose_folder`) do their work directly because they don't touch the browser.
* **`Worker`** runs commands one at a time. Every command ends by pushing the current page state
  (`_push_page()`) so the UI re-renders. Exceptions become `{type:"error"}` events.
* **`UIBridge.emit()`** turns a dict into `window.app.onEvent(json)` via `window.run_js`. Events are
  buffered until the DOM is ready; `print()` output (e.g. from `uk_eregistration.py`) is mirrored
  into the log panel by `StdoutTee`.

Event types the page understands (see `onEvent` in `gui.html`): `log`, `error`, `busy`, `status`,
`captcha`, `captcha_done`, `page`, `preview`, `downloads`, `menu`, `report_progress`,
`report_done`, `reset`.

### 3.2 Login and captchas

`cmd_login` reuses the selectors from `uk_eregistration.py` (`SEL_USER`, `SEL_PASS`,
`SEL_CAPTCHA_IMG`, …). It screenshots **only the captcha `<img>`**, gets an answer from
`_get_captcha_answer()`, fills it, clicks Login and checks the URL. Up to `MAX_LOGIN_ATTEMPTS`.

`_get_captcha_answer()` is the single place captchas are handled, for login **and** for any later
page action:

1. If a solver other than *manual* is selected and it hasn't been rejected `SOLVER_MAX_STRIKES`
   times, ask `captcha_solvers.solve(id, png)`.
2. Otherwise emit a `captcha` event (image + prompt) and block until the GUI answers
   (`submit_captcha`, `skip_captcha` → `""`, `cancel_captcha` → abort).

The same box in the UI is reused every time. A wrong answer at login reloads the page and asks again;
on a search page, `CAPTCHA_REJECTED` text on the page triggers a retry with the fresh image.

### 3.3 Reading a page (the "scanner")

`SCAN_JS` runs inside the site's page and returns a JSON description:

* `controls` — every visible `select` / text input / textarea / checkbox / radio group / button,
  each with a Playwright `selector` (`#id`, `[name=…]` or `tag >> nth=i`) and a **label** guessed
  from `<label for>`, aria/placeholder/title, the previous table cell, or the id (`ddlDistrict` →
  "District").
* `captcha` — the captcha image and its text box, if the page has one.
* `tables` — data tables (GridView-style: has `<th>` or an id like `gv…/…Result`, no form controls,
  not a menu). Rows are capped (`maxRows`).
* `messages` — status/error texts (`lblMsg`, "no record found", …).
* `links` — menu links, including hidden sub-menu items with the parent to hover.

`gui.html` renders `controls` as a form (`renderControl`). Changing a dropdown calls
`set_value` → Playwright selects it, waits for the postback (`_settle`), re-scans, re-renders.
Buttons call `click` → fill all text values, handle captcha, click, wait, re-scan.

`_settle()` waits for `load` + `networkidle` and then for a quiet period with **no new main-frame
navigation** (the Buyer Wise page fires an extra postback right after loading). `_evaluate()`
retries a page read if it collides with a navigation.

### 3.4 Modes

After login the worker emits `menu`. `cmd_open_mode(mode)`:

* `buyer` → `script.go_to_buyer_wise()` (the CLI script's tested navigation) → generic form.
* `seller` → home page → hover "Search By Party Name" → click "Seller Wise" → generic form.
* `report` → Buyer Wise page → the **report form** instead of the generic one.

### 3.5 Custom report (`cmd_run_report`)

Parameters from the UI: roles (`buyer`/`seller`), district and SRO **labels** (read from the site's
own dropdowns in the report form), from/to year, name.

For each role: open the role's page → find the controls by label regex (`REPORT_LABELS`) → select
district, then SRO (by option text; `_select_label`) → for each year in range that exists in the
Year dropdown: select it, fill the name, `_click_with_captcha("Search")`, then `_collect_results()`:

1. `_wait_for_results()` polls until a data table or a "no record" message appears.
2. `PAGE_SIZE_JS` switches a "Show N entries" selector to the largest value, if there is one.
3. Loop: take the best table (has download links / most rows), drop pager rows, append rows,
   `NEXT_PAGE_JS` marks the next pager control (`n+1`, "Next", "…"), click it, re-scan.
   Stops when there is no next page, the rows don't change, or `MAX_RESULT_PAGES`.

Each row is stored as `[ID, Year, <site columns…>, Relation, Relative]`:

* **ID** — `B2023-0001`, `S2024-0017`: role letter, year, running number. It is the same in the
  app table, the Excel file and the downloaded document's filename (`B2023-0001.pdf`).
* **Relation / Relative** — parsed from the searched party's column (`party_column_index()`:
  "Buyer/Second Party" for buyer searches, "Seller/First Party" for seller searches) by
  `parse_relation()`: `अनुराग गुप्ता S/O ठाकुर दास गुप्ता , ,` → `S/O` · `ठाकुर दास गुप्ता`.
  Recognised: S/O, W/O, D/O, H/O, C/O (any case, also `s/0`), पुत्र, पुत्री, पत्नी, पति, विधवा, पिता, माता.

Progress goes out as `report_progress`; **Stop** sets `_cancel` (checked between steps and while
waiting on a captcha) and the partial result is still emitted as `report_done`.

### 3.6 Results, filter, Excel

The UI keeps the result set in `state.reportData` and filters client-side. Two kinds of terms:

* **Chips** (S/o, D/o, W/o, H/o, C/o, पुत्र, पुत्री, पत्नी, पति, विधवा, श्री, श्रीमती) must appear as a
  whole word in the **searched party's column** — Buyer/Second Party on the Buyer sheet,
  Seller/First Party on the Seller sheet (`party_index` from the worker, header regex as fallback).
  Case-insensitive, so "S/o" matches the site's "S/O"; whole-word, so "श्री" does not match "श्रीमती".
* **Any other word** is a substring match in "All columns" or the chosen column.

**Save as Excel…** sends the visible row indexes per role to `save_report`, which opens a save
dialog and calls `report_export.write_report()`: sheets **Buyer**, **Seller** (bold frozen header,
auto-filter, widths; ID first) and **Summary** (parameters, counts). Without `openpyxl`, one CSV
per sheet is written instead.

### 3.7 Documents for the shown rows

While collecting, every row remembers where it came from (`report["meta"]`: role, year, result
page, cell texts). **Download documents for shown rows** sends the visible row indexes to
`cmd_download_rows`, which groups them by (role, year, page), re-runs that search (captcha box if
needed), applies the same page size as during collection, walks the pager to the page, finds each
row by its cell texts (`ROW_LINK_JS`, nth match for duplicates), marks its "Download" link and clicks
it (`_download_marked`, with the new-tab fallback). Files are named after the row's **ID**
(`B2023-0001.pdf`), so the Excel row and the document always match. If a download re-renders the
grid, the walker restarts from page 1. Stop works here too.

### 3.8 Remembered settings

`settings.json` (in the project folder, or the per-user app folder when packaged) stores the last
report fields (district, SRO, years, name, roles), the captcha solver, the browser toggles, the
download folder and the last report folder. The login form can also remember the username and
password when **Remember username and password** is selected. Credentials are stored in plaintext
in `settings.json`; environment variables override saved credentials, which override the CLI
defaults. Uncheck the option to clear saved credentials. `Api.defaults()` returns settings to the
page; opening report mode re-applies District → SRO → from-year on the live page so the cascading
lists are right (`_apply_saved_report_prefs`). Delete the file to reset.

### 3.9 Hindi typing

`hindi_input.js` attaches to a field, watches the word at the caret, asks a **provider** for
candidates and shows a popup. Space/Enter/Tab/1-5/click commit, Esc keeps English, words finished
before the answer arrives are swapped in place; `HindiInput.flush()` is awaited before values are
sent to the site. The GUI's provider is `Api.transliterate` → `hindi_input.suggest()`:
Google Input Tools (`hi-t-i0-und`) first, else the phonetic rules in `hindi_input.py`
(`phonetic()`), which produce the most likely spelling and alternates for the common ambiguities.
Words shorter than 2 letters (S/o, W/o) are never converted.

---

## 4. Common changes

* **The site renamed a field or button** — nothing to do for the generic forms (they're read live).
  For the report, adjust the regexes in `REPORT_LABELS` (`uk_ereg_gui.py`); the log tells you which
  control it could not find and lists the labels it saw.
* **Login page ids changed** — update `SEL_*` in `uk_eregistration.py`; the GUI also falls back to
  detecting the captcha image generically.
* **Different menu texts** — `ROLE_PAGES` / `MENU_PARENT` in `uk_ereg_gui.py`;
  `go_to_buyer_wise()` in the script.
* **Which fields get Hindi typing by default** — `HINDI_DEFAULT` regex in `gui.html`.
* **Relation chips** — `RELATION_CHIPS` in `gui.html`.
* **Excel layout** — `report_export.py`.
* **Transliteration mistakes** — add to `_EXCEPTIONS` in `hindi_input.py`, or tweak the cluster
  rules in `_build()`. Run `python tests/test_hindi_input.py` afterwards.
* **Captcha normalisation** — `UPPERCASE` / `_clean()` in `captcha_solvers.py`.
* **Timeouts / retries** — `ACTION_TIMEOUT_MS`, `MAX_CAPTCHA_ATTEMPTS`, `SOLVER_MAX_STRIKES`,
  `MAX_RESULT_PAGES` at the top of `uk_ereg_gui.py`.

---

## 5. Captcha solvers

Pick one in the login form. All of them fall back to the manual box if they fail, and after two
rejected answers in a session the app stops asking the model and asks you.

| Solver | Needs | Notes |
|---|---|---|
| Type it myself | nothing | Default. The image is shown in the app. |
| Claude (Anthropic) | `ANTHROPIC_API_KEY`, model `CAPTCHA_CLAUDE_MODEL` (default `claude-haiku-4-5-20251001`) | Same as the CLI script's solver. |
| ChatGPT (OpenAI) | `OPENAI_API_KEY`, `CAPTCHA_OPENAI_MODEL` (default `gpt-4o-mini`) | Any vision-capable chat model. |
| Gemini (Google) | `GEMINI_API_KEY`, `CAPTCHA_GEMINI_MODEL` (default `gemini-2.5-flash`) | |
| Other OpenAI-compatible API | `CAPTCHA_LLM_BASE_URL`, `CAPTCHA_LLM_MODEL`, optional `CAPTCHA_LLM_API_KEY` | Azure OpenAI, OpenRouter, Groq, LM Studio, vLLM… Base URL up to and including `/v1`. |
| Ollama (local model) | Ollama running; `ollama pull llava` (or `moondream`, `qwen2.5vl`) | Free and offline. `CAPTCHA_OLLAMA_URL`, `CAPTCHA_OLLAMA_MODEL`. |
| Tesseract OCR (offline) | Tesseract executable (`brew install tesseract` on macOS); `pytesseract` and Pillow are included in the app build. | Cheap but weak on distorted captchas. |

Keys and models are read from environment variables first, then from **`captcha_settings.json`**
next to the code (copy `captcha_settings.example.json`; keep that file private). Only the captcha
image and a one-line instruction are ever sent to a provider.

GitHub Copilot has no public API that other programs can send an image to, so it isn't an option;
if your organisation provides Azure OpenAI or a similar gateway, use "Other OpenAI-compatible API".

Compare providers on a saved captcha: `python captcha_solvers.py captcha.png` (or `--list`).

To **add a provider**: write a `solve_xyz(png) -> str` function in `captcha_solvers.py`, add an
entry to `SOLVERS` with a `label`, `fn` and `check` (returns `(available, reason)`), and it appears
in the dropdown automatically.

---

## 6. Privacy and safety notes

* Credentials are only ever sent to the e-registration site by the automated browser.
* Hindi suggestions send the word you are typing to Google Input Tools (unless offline, when the
  local rules are used). To keep everything local, make `Api.transliterate` call
  `hindi_input.phonetic()` instead of `suggest()`.
* Captcha solving sends the captcha image to the provider you chose — nothing else from the page.
* `uk_eregistration.py` contains the password as a fallback constant; prefer `UK_EREG_PASSWORD`.

---

## 7. Troubleshooting

| Symptom | What it means / what to do |
|---|---|
| "Chromium for Playwright is not installed" / installer exited with an error | The automatic download failed (offline, proxy, disk). Run `playwright install chromium` in the venv, or `python setup_env.py`. Behind a proxy set `HTTPS_PROXY` first. |
| Window opens but stays blank / no "Ready" line | pywebview problem; run with `--debug` and check the inspector console. |
| "Could not find the district, sro… control(s) on …" | The report couldn't match a dropdown label. The log lists `kind:label` pairs it saw — adjust `REPORT_LABELS`. |
| "Execution context was destroyed" | A navigation happened during a page read. `_evaluate` retries; if it persists, raise the quiet time in `_settle()`. |
| Report ends with 0 rows but the site shows results | The grid wasn't recognised as a data table. Use *Search for Buyer*, run the same search and press *Rescan*; if no table appears, loosen `isDataCore` in `SCAN_JS`. |
| Hindi suggestions say "using the built-in phonetic rules" | Google Input Tools not reachable (offline or blocked). Typing still works via rules and the keyboard. |
| Excel saved as CSV | `pip install openpyxl`. |
| Solver "unavailable (set …)" | Add the key to `captcha_settings.json` or the environment and restart. |

`--debug` opens the web inspector for the window; **Show browser window** at login runs Chromium
visibly so you can watch what the automation does; **Preview page** takes a screenshot any time.

---

## 8. Tests and the demo

```bash
python tests/run_all.py                 # everything below
python tests/test_flow.py               # worker flows against a fake Playwright page (no network)
python tests/test_captcha_solvers.py    # request/response handling for every provider (mocked HTTP)
python tests/test_hindi_input.py        # phonetic rules
python tests/build_demo.py              # writes tests/demo_harness.html — open it in a browser
```

`test_flow.py` drives the real `Worker` with a `FakePage`: login with a wrong then right captcha,
the menu, Buyer/Seller pages, a cascading dropdown, a captcha-protected search, a two-role
two-year report with paginated results, cancel, Excel export, and the solver fallback.

`tests/demo_harness.html` shows `gui.html` next to `tests/mock_buyerwise.html` with a fake
`pywebview` bridge — handy for UI work without Playwright or the real site (Hindi typing works for
a handful of demo words). Rebuild it after editing `gui.html`, `hindi_input.js` or `SCAN_JS`.

---

## 9. Building the app and the release pipeline

### Locally

```bash
python build_app.py --zip        # builds the local app and a platform-specific zip
python build_app.py --dmg        # macOS disk image
python build_app.py --onefile    # Windows single-file executable
python build_app.py --no-browser # smaller build without Chromium (target machine needs `playwright install chromium`)
```

`build_app.py` installs PyInstaller, downloads Chromium into `build/ms-playwright`, runs PyInstaller
(`--windowed`, one-folder, `--collect-all playwright`, data files `gui.html`, `hindi_input.js`,
`captcha_settings.example.json`) and then copies the browsers into the app at
`playwright/driver/package/.local-browsers` (with `ditto` on macOS so Chromium's framework
symlinks survive; on macOS the real copy lives under `Contents/Resources` with a symlink from
`Contents/Frameworks`). `uk_ereg_gui.py` detects that folder when frozen and sets
`PLAYWRIGHT_BROWSERS_PATH=0`. Drop an `app.icns` / `app.ico` next to the script to get an icon.

Frozen-mode paths (`uk_ereg_gui.py` top): `HERE` = bundle resources (`sys._MEIPASS`),
`APP_DIR` = per-user writable folder, `DEFAULT_DOWNLOAD_DIR` = `~/Downloads/uk_ereg`.

### GitHub Actions (`.github/workflows/build.yml`)

* On every **push to `main`**, including a pull request merge: run `tests/run_all.py`, increment the
  patch version in `VERSION`, build the macOS `.dmg` and Windows `.exe`, then publish both a
  versioned release (`vMAJOR.MINOR.PATCH`) and the rolling `latest` release.
* Pushes to other branches and manual workflow runs execute tests and no-browser packaging checks;
  they do not publish artifacts.
* The main-branch download links above always resolve to the latest published `.dmg` and `.exe`.

The builds are unsigned. Signing/notarising for macOS would need an Apple Developer ID
(`codesign` + `notarytool` steps after `build_app.py`); until then users use "Open Anyway" once.

---

## 10. Ideas not built yet

* Code signing / notarisation for the macOS app.
* A "search history" panel (previous reports and their Excel files).
* Scheduling a report to run unattended (would need a captcha solver configured).
