#!/usr/bin/env python3
"""
report_export.py - write tabular report data to Excel (.xlsx, one sheet per section) or CSV.

    from report_export import write_report
    write_report("report.xlsx",
                 {"Buyer": {"header": ["Year", "Reg. No.", "Buyer"], "rows": [["2023", "1234", "..."]]},
                  "Seller": {"header": [...], "rows": [...]}},
                 params={"district": "BAGESHWAR", "name": "..."})      # optional "Summary" sheet

Excel needs `openpyxl` (pip install openpyxl). Without it, one UTF-8 CSV per sheet is written next to
the requested path (report_Buyer.csv, report_Seller.csv) and the result says format="csv".
Returns {"path": ..., "format": "xlsx" | "csv", "files": [...], "counts": {sheet: rows}}.
"""
from __future__ import annotations

import csv
import time
from pathlib import Path


def write_report(path: str | Path, sheets: dict[str, dict], params: dict | None = None) -> dict:
    path = Path(path).expanduser()
    if path.suffix.lower() not in (".xlsx", ".csv"):
        path = path.with_suffix(".xlsx")
    counts = {name: len(sheet.get("rows", [])) for name, sheet in sheets.items()}
    if path.suffix.lower() == ".xlsx":
        try:
            import openpyxl  # noqa: F401
        except ImportError:
            path = path.with_suffix(".csv")
        else:
            _write_xlsx(path, sheets, params or {})
            return {"path": str(path), "format": "xlsx", "files": [str(path)], "counts": counts}
    files = _write_csv(path, sheets)
    return {"path": str(files[0]) if files else str(path), "format": "csv", "files": files, "counts": counts}


def _write_xlsx(path: Path, sheets: dict[str, dict], params: dict) -> None:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    wb = Workbook()
    wb.remove(wb.active)
    head_font = Font(bold=True)
    head_fill = PatternFill("solid", fgColor="DDE6F2")

    for name, sheet in sheets.items():
        ws = wb.create_sheet(title=name[:31] or "Sheet")
        header = list(sheet.get("header") or [])
        rows = sheet.get("rows") or []
        if not header and rows:
            header = [f"Column {i + 1}" for i in range(len(rows[0]))]
        if header:
            ws.append(header)
            for cell in ws[1]:
                cell.font = head_font
                cell.fill = head_fill
                cell.alignment = Alignment(vertical="center", wrap_text=True)
            ws.freeze_panes = "A2"
            ws.auto_filter.ref = f"A1:{get_column_letter(len(header))}{max(len(rows) + 1, 1)}"
        for row in rows:
            ws.append([_cell(v) for v in row])
        # column widths from content (capped so Hindi names stay readable without huge columns)
        widths: dict[int, int] = {}
        for r in ws.iter_rows(min_row=1, max_row=min(ws.max_row, 500)):
            for c in r:
                if c.value is not None:
                    widths[c.column] = max(widths.get(c.column, 0), len(str(c.value)))
        for col, w in widths.items():
            ws.column_dimensions[get_column_letter(col)].width = min(max(w + 2, 8), 60)
        if not rows:
            ws.cell(row=2 if header else 1, column=1, value="(no records)")

    ws = wb.create_sheet(title="Summary")
    ws.append(["Field", "Value"])
    for cell in ws[1]:
        cell.font = head_font
        cell.fill = head_fill
    for key in ("district", "sro", "from_year", "to_year", "name", "roles", "started"):
        if key in params:
            val = params[key]
            ws.append([key.replace("_", " ").title(), ", ".join(val) if isinstance(val, list) else str(val)])
    for name, sheet in sheets.items():
        ws.append([f"{name} rows", len(sheet.get("rows") or [])])
    ws.append(["Saved", time.strftime("%Y-%m-%d %H:%M")])
    ws.column_dimensions["A"].width = 16
    ws.column_dimensions["B"].width = 50

    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(path)


def _write_csv(path: Path, sheets: dict[str, dict]) -> list[str]:
    path.parent.mkdir(parents=True, exist_ok=True)
    files: list[str] = []
    for name, sheet in sheets.items():
        target = path.with_name(f"{path.stem}_{name}.csv")
        with target.open("w", newline="", encoding="utf-8-sig") as fh:   # BOM so Excel reads Hindi correctly
            writer = csv.writer(fh)
            if sheet.get("header"):
                writer.writerow(sheet["header"])
            writer.writerows(sheet.get("rows") or [])
        files.append(str(target))
    return files


def _cell(value):
    """Keep numbers as text when they look like identifiers (leading zeros, long digit runs)."""
    if isinstance(value, str):
        s = value.strip()
        if s.isdigit() and not s.startswith("0") and len(s) <= 9:
            return int(s)
        return value
    return value


if __name__ == "__main__":
    demo = {"Buyer": {"header": ["Year", "Reg. No.", "Buyer", "Seller"],
                      "rows": [["2023", "1234", "आशुतोष जौहरी", "रमेश सिंह"]]},
            "Seller": {"header": ["Year", "Reg. No.", "Buyer", "Seller"], "rows": []}}
    print(write_report("demo_report.xlsx", demo, {"district": "BAGESHWAR", "from_year": 2023, "to_year": 2023}))
