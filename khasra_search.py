"""Khasra-number search workflow for the Uttarakhand e-Search portal."""

from __future__ import annotations

import re
from collections.abc import Callable


MENU_PARENT = "Search By Property No"
MENU_ITEM = "Khasra No. Wise"


def parse_khasra_numbers(value: str | list | tuple) -> list[str]:
    """Accept a comma-, semicolon-, or newline-separated string, or a sequence."""
    values = value if isinstance(value, (list, tuple)) else re.split(r"[,;\r\n]+", str(value or ""))
    found: list[str] = []
    seen: set[str] = set()
    for item in values:
        number = re.sub(r"\s+", " ", str(item or "")).strip()
        if number and number.casefold() not in seen:
            seen.add(number.casefold())
            found.append(number)
    return found


def open_page(page, search_url: str, goto: Callable, settle: Callable, log: Callable) -> None:
    """Open the portal's Khasra No. Wise page from its property-search menu."""
    goto(search_url)
    menu = page.get_by_text(MENU_PARENT, exact=False).first
    try:
        menu.hover(timeout=5_000)
    except Exception:  # noqa: BLE001
        pass
    link = page.locator("a").filter(has_text=re.compile(rf"^\s*{re.escape(MENU_ITEM)}\s*$", re.I)).first
    try:
        link.click(timeout=5_000)
    except Exception:  # noqa: BLE001
        try:
            page.get_by_text(MENU_ITEM, exact=True).first.click(timeout=5_000)
        except Exception:  # noqa: BLE001
            link.evaluate("el => el.click()")
    settle()
    log(f"[nav] {MENU_ITEM} -> {page.url}")


def run_search(
    page,
    params: dict,
    *,
    scan: Callable,
    select_label: Callable,
    click_search: Callable,
    collect_results: Callable,
    cancelled: Callable,
    progress: Callable,
    log: Callable,
) -> dict:
    """Search every Khasra number for each available registration year in range."""
    numbers = parse_khasra_numbers(params.get("khasra_numbers", ""))
    if not numbers:
        raise ValueError("Enter at least one Khasra number.")
    try:
        first_year, last_year = sorted((int(params["from_year"]), int(params["to_year"])))
    except (KeyError, TypeError, ValueError):
        raise ValueError("Pick valid From and To years.") from None
    district, sro = str(params.get("district") or "").strip(), str(params.get("sro") or "").strip()
    if not district or not sro:
        raise ValueError("Pick a District and a Sub-Registrar Office first.")

    controls = _find_controls(scan())
    select_label(controls["district"]["selector"], district)
    controls = _find_controls(scan())
    select_label(controls["sro"]["selector"], sro)
    controls = _find_controls(scan())
    available_years = sorted(
        {o["label"].strip() for o in controls["year"].get("options", [])
         if o.get("label", "").strip().isdigit()
         and first_year <= int(o["label"].strip()) <= last_year},
        key=int,
    )
    if not available_years:
        raise ValueError(f"No registration years available between {first_year} and {last_year}.")

    rows: list[list[str]] = []
    header: list[str] = []
    total = len(available_years) * len(numbers)
    completed = 0
    for year in available_years:
        for number in numbers:
            if cancelled():
                return {"params": params, "header": header, "rows": rows, "partial": True}
            controls = _find_controls(scan())
            select_label(controls["year"]["selector"], year)
            controls = _find_controls(scan())
            page.locator(controls["khasra"]["selector"]).first.fill(number)
            click_search(controls["search"]["selector"])
            result_header, result_rows, _ = collect_results("khasra", year)
            if result_header and not header:
                header = ["Khasra No.", "Year", *result_header]
            rows.extend([[number, year, *row] for row in result_rows])
            completed += 1
            progress(year, number, completed, total, len(rows))
            log(f"Khasra {number}, {year}: {len(result_rows)} row(s)")
    return {"params": {**params, "from_year": first_year, "to_year": last_year,
                       "khasra_numbers": numbers},
            "header": header, "rows": rows, "partial": False}


def _find_controls(scan: dict) -> dict:
    controls = scan.get("controls", [])

    def find(kind: str, pattern: str) -> dict | None:
        return next((c for c in controls if c.get("kind") == kind and
                     re.search(pattern, c.get("label") or "", re.I)), None)

    found = {
        "district": find("select", r"district|जनपद"),
        "sro": find("select", r"sub.?registrar|s\.?r\.?o\b|registrar|उप.?निबंधक"),
        "year": find("select", r"registration\s*year|year|पंजीकरण\s*वर्ष"),
        "khasra": next((c for c in controls if c.get("kind") in ("textarea", "text") and
                         re.search(r"khasra|खसरा", c.get("label") or "", re.I)), None),
        "search": find("button", r"^\s*search\s*$|खोज"),
    }
    missing = [key for key, value in found.items() if value is None]
    if missing:
        labels = ", ".join(f"{c.get('kind')}:{c.get('label')}" for c in controls)
        raise RuntimeError(f"Could not find Khasra search control(s): {', '.join(missing)}. Controls seen: {labels}")
    return found