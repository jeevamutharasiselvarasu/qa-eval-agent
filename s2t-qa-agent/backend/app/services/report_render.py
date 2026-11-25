"""Presentation of a QA report: a client-ready HTML document (printable to PDF) and a formatted Excel workbook.

Both take the dictionary built by ``reports.collect`` and never change it, so the JSON export, the HTML and the
workbook always tell the same story.
"""

from __future__ import annotations

import json
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape
from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.table import Table, TableStyleInfo

STATUSES = ("PASS", "FAIL", "BLOCKED", "ERROR", "SKIPPED", "NOT_EXECUTED")
STATUS_LABEL = {
    "PASS": "Passed",
    "FAIL": "Failed",
    "BLOCKED": "Blocked",
    "ERROR": "Error",
    "SKIPPED": "Not in data",
    "NOT_EXECUTED": "Not executed",
}
COLOR = {"PASS": "#137a3e", "FAIL": "#b42318", "BLOCKED": "#9a5b05", "ERROR": "#7a1fa2", "SKIPPED": "#7a8296", "NOT_EXECUTED": "#9aa1b2"}
BG = {"PASS": "#e6f5ec", "FAIL": "#fdecea", "BLOCKED": "#fff3dd", "ERROR": "#f4e8fa", "SKIPPED": "#eef0f4", "NOT_EXECUTED": "#eef0f4"}
VERDICT = {
    "PASS": ("Passed", "Every selected test passed.", "PASS"),
    "PASS_WITH_BLOCKED": ("Passed with open items", "No test failed, but some could not be evaluated.", "BLOCKED"),
    "FAIL": ("Failed", "At least one test found a difference between the target and the requirements.", "FAIL"),
    "ERROR": ("Incomplete", "Some tests could not run because of a technical error.", "ERROR"),
    "NO_EXECUTION": ("Not run", "No finished execution was found.", "SKIPPED"),
}
BASIS = {"ACCEPTANCE_CRITERIA": "Acceptance criteria", "TRANSFORMATION_LOGIC": "Transformation logic", "GENERAL": "General checks"}


def _s(v) -> str:
    return "" if v is None else str(v)


def _pct(n: int, d: int) -> str:
    return f"{(100 * n / d):.0f}%" if d else "–"


def verdict(data: dict) -> tuple[str, str, str]:
    return VERDICT.get(data["summary"]["overall"], (data["summary"]["overall"], "", "SKIPPED"))


def ac_rows(data: dict) -> list[dict]:
    """Per acceptance criterion: text, counts per status and the coverage verdict, over the same tests as the traceability."""
    status_of = {r["test_case_id"]: r["status"] for r in data["test_results"]}
    out = []
    for t in data["traceability"]:
        c = Counter(status_of[i] for i in t["test_case_ids"] if i in status_of)
        out.append(
            {
                "ac": t["acceptance_criterion"],
                "text": t["criterion_text"],
                "coverage": t["coverage"],
                "tests": sum(c.values()),
                **{k: c.get(k, 0) for k in STATUSES},
            }
        )
    return out


# ------------------------------------------------------------------ HTML
TEMPLATES = Path(__file__).resolve().parents[1] / "templates"
_env = Environment(loader=FileSystemLoader(TEMPLATES), autoescape=select_autoescape(["html", "j2"]), trim_blocks=True, lstrip_blocks=True)
COVERAGE = {
    "PASS": ("PASS", "Passed"),
    "FAIL": ("FAIL", "Failed"),
    "BLOCKED": ("BLOCKED", "Blocked"),
    "PARTIAL": ("BLOCKED", "Partial"),
    "NOT_IN_DATA": ("SKIPPED", "Not in data"),
    "NOT_EXECUTED": ("NOT_EXECUTED", "Not run"),
    "NOT_COVERED": ("ERROR", "No tests"),
}
LOGO = (
    "<svg width='26' height='26' viewBox='0 0 32 32' aria-hidden='true'><rect width='32' height='32' rx='8' fill='#3f51d8'/>"
    "<path d='M8 10h16M8 16h7M8 22h5' stroke='#fff' stroke-opacity='.55' stroke-width='2' stroke-linecap='round'/>"
    "<path d='M17 20.5l3 3 5.5-6.5' fill='none' stroke='#fff' stroke-width='2.4' stroke-linecap='round' stroke-linejoin='round'/>"
    "</svg>"
)
SECTIONS = [
    ("summary", "Summary"),
    ("criteria", "Acceptance criteria"),
    ("findings", "Findings"),
    ("reconciliation", "Reconciliation"),
    ("differences", "Column differences"),
    ("analysis", "Failure analysis"),
    ("results", "All test results"),
    ("logic", "Transformation"),
    ("audit", "Approvals and audit"),
]


def _bar(counts: dict) -> list[dict]:
    total = sum(counts.get(k, 0) for k in STATUSES) or 1
    x, out = 0.0, []
    for k in STATUSES:
        n = counts.get(k, 0)
        if n:
            w = 100 * n / total
            out.append({"x": f"{x:.3f}", "w": f"{w:.3f}", "color": COLOR[k], "label": STATUS_LABEL[k], "n": n})
            x += w
    return out


def html_context(data: dict, generated_at: datetime | None = None) -> dict:
    """Everything the template shows, derived from the collected report data."""
    s = data["summary"]
    c = s["counts"]
    title, explanation, vst = verdict(data)
    tr = data.get("transformation") or {}
    total = c.get("total", 0)
    return {
        "data": data,
        "s": s,
        "c": c,
        "total": total,
        "tr": tr,
        "generated": (generated_at or datetime.now(UTC)).strftime("%d %b %Y, %H:%M UTC"),
        "verdict": {"title": title, "explanation": explanation, "status": vst},
        "pass_rate": _pct(c.get("PASS", 0), c.get("PASS", 0) + c.get("FAIL", 0)),
        "kpis": [
            {"status": st, "value": c.get(st, 0) if st else total, "label": lbl}
            for st, lbl in [
                ("", "Tests run"),
                ("PASS", "Passed"),
                ("FAIL", "Failed"),
                ("BLOCKED", "Blocked"),
                ("ERROR", "Errors"),
                ("SKIPPED", "Not in data"),
            ]
        ],
        "bar": _bar(c),
        "selection": s.get("selection") or {"selected": total, "in_pack": total, "not_selected": []},
        "sources": ", ".join(f"{d['alias']} ({d['rows']} rows)" for d in s["source_datasets"]) or "–",
        "target": ", ".join(f"{d['file']} ({d['rows']} rows)" for d in s["target_dataset"]) or "–",
        "sql_version": f"v{tr.get('version')} · {_s(tr.get('approval')).lower()}" if tr else "–",
        "failures": [r for r in data["test_results"] if r["status"] in ("FAIL", "ERROR")],
        "blocked_count": sum(1 for r in data["test_results"] if r["status"] == "BLOCKED"),
        "ac_rows": ac_rows(data),
        "reconciliation": [
            {"measure": k.replace("_", " ").capitalize(), "value": v} for k, v in (data.get("reconciliation") or {}).items()
        ],
        "results": [
            {
                **r,
                "basis": BASIS.get(r.get("basis") or "", r.get("basis") or ""),
                "test_type": _s(r.get("test_type")).capitalize(),
                "scope": ("Only records with " + r["scope"]) if r.get("scope") else "Whole target",
            }
            for r in data["test_results"]
        ],
        "column_rules": [m for m in data["mappings"] if m.get("target_column")],
        "sections": SECTIONS,
        "section_no": {k: i + 1 for i, (k, _) in enumerate(SECTIONS)},
        "color": COLOR,
        "bg": BG,
        "status_label": STATUS_LABEL,
        "coverage": COVERAGE,
        "css": (TEMPLATES / "report.css").read_text(encoding="utf-8"),
        "logo": LOGO,
    }


def render_html(data: dict, generated_at: datetime | None = None) -> str:
    return _env.get_template("report.html.j2").render(**html_context(data, generated_at))


# ------------------------------------------------------------------ Excel
HEAD_FILL = PatternFill("solid", fgColor="1B2232")
HEAD_FONT = Font(bold=True, color="FFFFFF")
XFILL = {k: PatternFill("solid", fgColor=v.lstrip("#").upper()) for k, v in BG.items()}
XFONT = {k: Font(bold=True, color=v.lstrip("#").upper()) for k, v in COLOR.items()}
THIN = Border(bottom=Side(style="thin", color="E4E7EF"))
WRAP_COLS = {
    "title",
    "message",
    "text",
    "criterion_text",
    "description",
    "rule",
    "expected",
    "actual",
    "scope",
    "confirmed_facts",
    "likely_explanations",
    "recommended_checks",
    "unverified_hypotheses",
    "reason",
    "source_columns",
}


def _cell_value(v):
    if isinstance(v, (dict, list)):
        return json.dumps(v, default=str)
    return v


def _sheet(wb: Workbook, title: str, rows: list[dict], cols: list[tuple[str, str]] | None = None) -> None:
    ws = wb.create_sheet(title[:31])
    if cols is None:
        keys = list(rows[0].keys()) if rows else []
        cols = [(k, k.replace("_", " ").capitalize()) for k in keys]
    if not rows:
        ws.append(["No rows."])
        ws["A1"].font = Font(italic=True, color="7A8296")
        return
    ws.append([label for _, label in cols])
    for r in rows:
        ws.append([_cell_value(r.get(k)) for k, _ in cols])
    for cell in ws[1]:
        cell.fill, cell.font = HEAD_FILL, HEAD_FONT
        cell.alignment = Alignment(vertical="center")
    keys = [k for k, _ in cols]
    for row in ws.iter_rows(min_row=2):
        for k, cell in zip(keys, row, strict=True):
            cell.border = THIN
            cell.alignment = Alignment(vertical="top", wrap_text=k in WRAP_COLS)
            if k in ("status", "coverage") and cell.value in XFILL:
                cell.fill, cell.font = XFILL[cell.value], XFONT[cell.value]
    for i, k in enumerate(keys, 1):
        longest = max([len(_s(ws.cell(row=r, column=i).value)) for r in range(1, min(ws.max_row, 200) + 1)] or [10])
        ws.column_dimensions[get_column_letter(i)].width = min(60 if k in WRAP_COLS else 40, max(10, longest + 2))
    ws.freeze_panes = "A2"
    ref = f"A1:{get_column_letter(len(cols))}{ws.max_row}"
    name = "T_" + "".join(ch for ch in title if ch.isalnum())[:25] + str(len(wb.worksheets))
    tbl = Table(displayName=name, ref=ref)
    tbl.tableStyleInfo = TableStyleInfo(name="TableStyleLight9", showRowStripes=True)
    ws.add_table(tbl)


def render_xlsx(data: dict, path: Path, generated_at: datetime | None = None) -> None:
    s = data["summary"]
    c = s["counts"]
    title, explanation, vst = verdict(data)
    wb = Workbook()
    ws = wb.active
    ws.title = "Summary"
    ws["A1"] = "QA validation report"
    ws["A1"].font = Font(bold=True, size=18, color="1B2232")
    ws["A2"] = s["run_name"]
    ws["A2"].font = Font(size=12, color="4B5468")
    ws["A4"], ws["B4"] = "Result", title
    ws["B4"].fill, ws["B4"].font = XFILL[vst], Font(bold=True, size=13, color=COLOR[vst].lstrip("#").upper())
    ws["A5"], ws["B5"] = "", explanation
    meta = [
        ("Jira ticket", s["jira_ticket"]),
        ("Run", s["qa_run_id"]),
        ("Generated", (generated_at or datetime.now(UTC)).strftime("%Y-%m-%d %H:%M UTC")),
        ("Executions", ", ".join(s["generated_for_executions"])),
        (
            "Selected tests",
            f"{(s.get('selection') or {}).get('selected', c.get('total'))} of {(s.get('selection') or {}).get('in_pack', c.get('total'))}",
        ),
        ("Source files", ", ".join(f"{d['alias']} ({d['rows']} rows)" for d in s["source_datasets"])),
        ("Target file", ", ".join(f"{d['file']} ({d['rows']} rows)" for d in s["target_dataset"])),
    ]
    for i, (k, v) in enumerate(meta, 7):
        ws.cell(row=i, column=1, value=k).font = Font(color="7A8296")
        ws.cell(row=i, column=2, value=v)
    r0 = 7 + len(meta) + 1
    ws.cell(row=r0, column=1, value="Test results").font = Font(bold=True, size=12)
    for j, k in enumerate(["total", *STATUSES], 0):
        label = "Tests run" if k == "total" else STATUS_LABEL[k]
        ws.cell(row=r0 + 1 + j, column=1, value=label)
        cell = ws.cell(row=r0 + 1 + j, column=2, value=c.get(k, 0))
        if k in XFONT:
            cell.font = XFONT[k]
    ws.column_dimensions["A"].width, ws.column_dimensions["B"].width = 22, 70
    for row in ws.iter_rows():
        for cell in row:
            cell.alignment = Alignment(vertical="top", wrap_text=cell.column == 2)

    _sheet(
        wb,
        "Acceptance criteria",
        ac_rows(data),
        [
            ("ac", "AC"),
            ("text", "Criterion"),
            ("tests", "Tests"),
            ("PASS", "Passed"),
            ("FAIL", "Failed"),
            ("BLOCKED", "Blocked"),
            ("SKIPPED", "Not in data"),
            ("coverage", "Result"),
        ],
    )
    _sheet(
        wb,
        "Test results",
        [
            {
                **r,
                "basis": BASIS.get(r.get("basis") or "", r.get("basis")),
                "test_type": _s(r.get("test_type")).capitalize(),
                "scope": ("Only records with " + r["scope"]) if r.get("scope") else "Whole target",
            }
            for r in data["test_results"]
        ],
        [
            ("test_case_id", "Test"),
            ("acceptance_criteria", "AC"),
            ("basis", "From"),
            ("test_type", "Type"),
            ("title", "Test case"),
            ("scope", "Records checked"),
            ("status", "Result"),
            ("expected", "Expected"),
            ("actual", "Found"),
            ("message", "Details"),
        ],
    )
    _sheet(
        wb,
        "Column differences",
        data["column_differences"],
        [
            ("test_case_id", "Test"),
            ("business_key", "Business key"),
            ("column", "Column"),
            ("expected", "Expected"),
            ("actual", "Uploaded target"),
            ("difference", "Difference"),
        ],
    )
    _sheet(
        wb,
        "Reconciliation",
        [{"measure": k.replace("_", " ").capitalize(), "value": v} for k, v in (data.get("reconciliation") or {}).items()],
        [("measure", "Measure"), ("value", "Value")],
    )
    _sheet(
        wb,
        "Failure analysis",
        data["failure_analysis"],
        [
            ("test_case_id", "Test"),
            ("category", "Category"),
            ("suspected_layer", "Suspected layer"),
            ("confirmed_facts", "Confirmed facts"),
            ("likely_explanations", "Likely explanations"),
            ("recommended_checks", "Recommended checks"),
            ("provider", "Provider"),
        ],
    )
    _sheet(
        wb,
        "Column rules",
        [m for m in data["mappings"] if m.get("target_column")],
        [
            ("target_column", "Target column"),
            ("source_columns", "Source columns"),
            ("rule", "Rule"),
            ("type", "Type"),
            ("provenance", "From"),
        ],
    )
    _sheet(
        wb,
        "Approvals",
        data["audit"]["approvals"],
        [
            ("artifact_type", "Item"),
            ("version", "Version"),
            ("decision", "Decision"),
            ("reviewer", "Reviewer"),
            ("at", "When"),
            ("reason", "Reason"),
        ],
    )
    _sheet(wb, "Audit events", data["audit"]["events"])
    for sheet in wb.worksheets:  # printable: fit every sheet to the page width, header row repeated
        sheet.sheet_properties.pageSetUpPr.fitToPage = True
        sheet.page_setup.fitToWidth, sheet.page_setup.fitToHeight = 1, 0
        sheet.page_setup.orientation = "portrait" if sheet.title == "Summary" else "landscape"
        sheet.page_setup.paperSize = sheet.PAPERSIZE_A4
        if sheet.title != "Summary" and sheet.max_row > 1:
            sheet.print_title_rows = "1:1"
    wb.save(path)
