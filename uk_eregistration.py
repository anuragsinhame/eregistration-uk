"""
Uttarakhand Stamp & Registration e-Search automation.

Flow:
  1. Open login page (headless Chromium via Playwright)
  2. Fill username / password
  3. Screenshot ONLY the captcha <img>, send to Claude Haiku 4.5, type the answer
  4. Login (retries on captcha failure)
  5. Search By Party Name -> Buyer Wise
  6. District / SRO / Year / Buyer name -> Search
  7. Click "Download Document" on every result row and save the files

Setup:
  pip install playwright anthropic
  playwright install chromium
  export ANTHROPIC_API_KEY=sk-ant-...
  export UK_EREG_PASSWORD='...'          # or edit PASSWORD below

Run:
  python uk_eregistration.py
  python uk_eregistration.py --show           # headed browser, for debugging
  python uk_eregistration.py --dump           # print form controls on the search page, then exit
  python uk_eregistration.py --buyer "आशुतोष जौहरी" --year 2023
"""

import argparse
import base64
import os
import re
import sys
import time
from pathlib import Path

import anthropic
from playwright.sync_api import Page, TimeoutError as PWTimeout, sync_playwright

# ------------------------------------------------------------------ config
BASE = "https://online.eregistrationukgov.in/E_Search"
LOGIN_URL = f"{BASE}/esearchLogin.aspx"
SEARCH_URL = f"{BASE}/Default2.aspx"

USERNAME = ""
PASSWORD = os.environ.get("UK_EREG_PASSWORD", "")   # <-- fill in / export

DISTRICT = "BAGESHWAR"
SRO = "BAGESHWAR"
YEAR = "2023"
BUYER_NAME = "आशुतोष जौहरी"   # default from your screenshot; override with --buyer

DOWNLOAD_DIR = Path("downloads")
CAPTCHA_MODEL = "claude-haiku-4-5-20251001"
MAX_LOGIN_ATTEMPTS = 4

# Login page selectors (verified on the live page)
SEL_USER = "#MainContent_txtUsername"
SEL_PASS = "#MainContent_txtPassword"
SEL_CAPTCHA_IMG = "#MainContent_imgCaptcha"
SEL_CAPTCHA_TXT = "#MainContent_txtCaptcha"
SEL_LOGIN_BTN = "#MainContent_btnLogin"


# ------------------------------------------------------------------ captcha
def solve_captcha(png_bytes: bytes) -> str:
    client = anthropic.Anthropic()
    resp = client.messages.create(
        model=CAPTCHA_MODEL,
        max_tokens=20,
        messages=[{
            "role": "user",
            "content": [
                {"type": "image", "source": {
                    "type": "base64", "media_type": "image/png",
                    "data": base64.b64encode(png_bytes).decode()}},
                {"type": "text", "text":
                    "Read the characters in this captcha image. Reply with ONLY the "
                    "characters, uppercase, no spaces, no punctuation, nothing else."},
            ],
        }],
    )
    text = "".join(b.text for b in resp.content if b.type == "text")
    return re.sub(r"[^A-Za-z0-9]", "", text).upper()


# ------------------------------------------------------------------ login
def login(page: Page) -> None:
    for attempt in range(1, MAX_LOGIN_ATTEMPTS + 1):
        page.goto(LOGIN_URL, wait_until="networkidle")
        page.fill(SEL_USER, USERNAME)
        page.fill(SEL_PASS, PASSWORD)

        img = page.locator(SEL_CAPTCHA_IMG)
        img.wait_for(state="visible")
        png = img.screenshot(type="png")          # snip just the captcha
        code = solve_captcha(png)
        print(f"[login] attempt {attempt}: captcha -> {code}")

        page.fill(SEL_CAPTCHA_TXT, code)
        page.click(SEL_LOGIN_BTN)
        page.wait_for_load_state("networkidle")

        if "esearchLogin" not in page.url.lower():
            print(f"[login] success -> {page.url}")
            return

        # still on login page: read any error text for the log
        body = page.inner_text("body")
        err = next((l for l in body.splitlines() if "captcha" in l.lower() or "invalid" in l.lower()), "")
        print(f"[login] failed: {err.strip() or 'unknown reason'}")

    sys.exit("Login failed after max attempts")


# ------------------------------------------------------------------ helpers
def select_by_label_or_text(page: Page, label_regex: str, value: str, nth_fallback: int | None = None):
    """Pick <option> whose text matches `value` in the <select> next to a label."""
    row = page.locator("tr, div").filter(has_text=re.compile(label_regex, re.I)).locator("select").first
    if row.count() == 0 and nth_fallback is not None:
        row = page.locator("select").nth(nth_fallback)
    row.select_option(label=value)
    page.wait_for_load_state("networkidle")   # ASP.NET postback repopulates dependent dropdowns
    time.sleep(0.5)


def dump_controls(page: Page) -> None:
    js = """() => [...document.querySelectorAll('input,select,textarea,a')]
        .map(e => `${e.tagName} id=${e.id} name=${e.name||''} type=${e.type||''} text=${(e.innerText||e.value||'').trim().slice(0,40)}`)
        .join('\\n')"""
    print(page.evaluate(js))


# ------------------------------------------------------------------ search
def go_to_buyer_wise(page: Page) -> None:
    page.goto(SEARCH_URL, wait_until="networkidle")
    # Hover the menu then click sub-item; fall back to direct click if it's already visible.
    menu = page.get_by_text("Search By Party Name", exact=False).first
    menu.hover()
    buyer = page.get_by_text("Buyer Wise", exact=True).first
    try:
        buyer.click(timeout=5000)
    except PWTimeout:
        menu.click()
        page.get_by_text("Buyer Wise", exact=True).first.click()
    page.wait_for_load_state("networkidle")
    print(f"[nav] buyer-wise page -> {page.url}")


def fill_and_search(page: Page, buyer_name: str, year: str) -> None:
    select_by_label_or_text(page, r"District", DISTRICT, nth_fallback=0)
    select_by_label_or_text(page, r"Sub-?Registrar", SRO, nth_fallback=1)
    select_by_label_or_text(page, r"Year", year, nth_fallback=2)

    # Buyer name is a <textarea>
    ta = page.locator("textarea").first
    ta.fill(buyer_name)

    page.get_by_role("button", name=re.compile(r"^Search$", re.I)).first.click()
    page.wait_for_load_state("networkidle")
    # Wait for result table or "no record" text
    page.wait_for_selector("table >> text=Download", timeout=30000)
    print("[search] results loaded")


def download_all(page: Page, out_dir: Path) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    links = page.get_by_text("Download Document", exact=False)
    n = links.count()
    print(f"[download] {n} document link(s)")
    saved = []
    for i in range(n):
        link = page.get_by_text("Download Document", exact=False).nth(i)
        try:
            with page.expect_download(timeout=60000) as dl_info:
                link.click()
            dl = dl_info.value
            target = out_dir / (dl.suggested_filename or f"document_{i+1}.pdf")
            dl.save_as(target)
            saved.append(target)
            print(f"[download] saved {target}")
        except PWTimeout:
            # Some sites open the PDF in a new tab instead of downloading
            if len(page.context.pages) > 1:
                newp = page.context.pages[-1]
                newp.wait_for_load_state()
                pdf_bytes = newp.request.get(newp.url).body()
                target = out_dir / f"document_{i+1}.pdf"
                target.write_bytes(pdf_bytes)
                saved.append(target)
                newp.close()
                print(f"[download] saved (from new tab) {target}")
            else:
                print(f"[download] row {i+1}: no download triggered")
    return saved


# ------------------------------------------------------------------ main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--show", action="store_true", help="run headed")
    ap.add_argument("--dump", action="store_true", help="print controls on buyer-wise page and exit")
    ap.add_argument("--buyer", default=BUYER_NAME)
    ap.add_argument("--year", default=YEAR)
    ap.add_argument("--out", default=str(DOWNLOAD_DIR))
    args = ap.parse_args()

    if not PASSWORD:
        sys.exit("Set UK_EREG_PASSWORD env var (or PASSWORD in the script).")
    if not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit("Set ANTHROPIC_API_KEY for captcha solving.")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not args.show)
        ctx = browser.new_context(accept_downloads=True, locale="en-IN")
        page = ctx.new_page()

        login(page)
        go_to_buyer_wise(page)

        if args.dump:
            dump_controls(page)
            browser.close()
            return

        fill_and_search(page, args.buyer, args.year)
        files = download_all(page, Path(args.out))
        page.screenshot(path=str(Path(args.out) / "results.png"), full_page=True)
        print(f"\nDone. {len(files)} file(s) in {args.out}/")
        browser.close()


if __name__ == "__main__":
    main()
