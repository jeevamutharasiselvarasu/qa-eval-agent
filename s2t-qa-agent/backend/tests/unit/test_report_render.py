"""Unit tests: the client-facing report (HTML and Excel) built from collected report data."""

import copy

import pytest
from openpyxl import load_workbook

from app.services.report_render import ac_rows, html_context, render_html, render_xlsx, verdict

DATA = {
    "summary": {
        "qa_run_id": "RUN-1",
        "run_name": "Customer master",
        "jira_ticket": "DATA-1",
        "jira_source": "MOCK",
        "generated_for_executions": ["EXE-1"],
        "executions": [{"execution_id": "EXE-1", "track": "ACTUAL", "status": "COMPLETED"}],
        "source_datasets": [{"alias": "customers", "rows": 3}],
        "target_dataset": [{"file": "target.csv", "rows": 3}],
        "selection": {"selected": 3, "in_pack": 4, "not_selected": ["TC-GEN-02"]},
        "counts": {"total": 3, "PASS": 1, "FAIL": 1, "BLOCKED": 0, "ERROR": 0, "SKIPPED": 1, "NOT_EXECUTED": 0},
        "overall": "FAIL",
        "open_gaps": [],
    },
    "traceability": [
        {
            "acceptance_criterion": "AC1",
            "criterion_text": "NAME is upper-case",
            "coverage": "FAIL",
            "test_case_ids": ["TC-AC1-01", "TC-AC1-02", "TC-GEN-01"],
        }
    ],
    "test_results": [
        {
            "test_case_id": "TC-AC1-01",
            "title": "NAME follows UPPER(NAME)",
            "status": "FAIL",
            "expected": "3 matching values",
            "actual": "1 mismatch",
            "message": "1 of 3 records differ",
            "acceptance_criteria": "AC1",
            "basis": "ACCEPTANCE_CRITERIA",
            "test_type": "POSITIVE",
            "scope": "",
        },
        {
            "test_case_id": "TC-AC1-02",
            "title": "NAME when NAME is null",
            "status": "SKIPPED",
            "expected": "",
            "actual": "",
            "message": "Not applicable",
            "acceptance_criteria": "AC1",
            "basis": "ACCEPTANCE_CRITERIA",
            "test_type": "NEGATIVE",
            "scope": "a null customers.NAME",
        },
        {
            "test_case_id": "TC-GEN-01",
            "title": "Schema matches",
            "status": "PASS",
            "expected": "",
            "actual": "",
            "message": "ok",
            "acceptance_criteria": "",
            "basis": "GENERAL",
            "test_type": "GENERAL",
            "scope": "",
        },
    ],
    "reconciliation": {"missing_records": 0, "extra_records": 1},
    "column_differences": [
        {"test_case_id": "TC-AC1-01", "business_key": "C1", "column": "NAME", "expected": "ANITA", "actual": "Anita", "difference": None}
    ],
    "failure_analysis": [
        {
            "test_case_id": "TC-AC1-01",
            "category": "VALUE_MISMATCH",
            "suspected_layer": "TARGET_DATA",
            "confirmed_facts": ["1 value differs"],
            "likely_explanations": [],
            "recommended_checks": ["Check UPPER"],
            "provider": "mock",
        }
    ],
    "mappings": [
        {
            "target_column": "NAME",
            "source_columns": "customers.NAME",
            "rule": "UPPER(customers.NAME)",
            "type": "DERIVED",
            "provenance": "JIRA_EXPLICIT",
        },
        {"target_column": None, "rule": "LEFT JOIN"},
    ],
    "transformation": {
        "version": 2,
        "mode": "SQL",
        "origin": "AI_GENERATED",
        "approval": "APPROVED",
        "content_hash": "sha256:abc",
        "code": "SELECT UPPER(NAME) AS NAME FROM customers",
    },
    "audit": {
        "approvals": [{"artifact_type": "TRANSFORMATION", "version": "v2", "decision": "APPROVED", "reviewer": "dev", "at": "now"}],
        "events": [{"at": "now", "actor": "dev", "action": "RUN_CREATED", "entity": "QA_RUN:RUN-1"}],
    },
}


@pytest.mark.parametrize(
    ("overall", "title"),
    [
        ("PASS", "Passed"),
        ("FAIL", "Failed"),
        ("PASS_WITH_BLOCKED", "Passed with open items"),
        ("ERROR", "Incomplete"),
        ("NO_EXECUTION", "Not run"),
    ],
)
def test_verdict_titles(overall, title):
    d = copy.deepcopy(DATA)
    d["summary"]["overall"] = overall
    assert verdict(d)[0] == title


def test_acceptance_criterion_counts_use_the_same_tests_as_its_verdict():
    (row,) = ac_rows(DATA)
    assert row["tests"] == 3 and row["FAIL"] == 1 and row["PASS"] == 1 and row["SKIPPED"] == 1 and row["coverage"] == "FAIL"


def test_html_context_numbers():
    c = html_context(DATA)
    assert c["pass_rate"] == "50%"  # skipped tests are not part of the pass rate
    assert [k["value"] for k in c["kpis"]] == [3, 1, 1, 0, 0, 1]
    assert sum(float(seg["w"]) for seg in c["bar"]) == pytest.approx(100, abs=0.01)  # widths are rounded to 3 decimals
    assert [r["scope"] for r in c["results"]] == ["Whole target", "Only records with a null customers.NAME", "Whole target"]
    assert [m["target_column"] for m in c["column_rules"]] == ["NAME"]


def test_html_report_contains_the_story_and_escapes_untrusted_text():
    d = copy.deepcopy(DATA)
    d["summary"]["run_name"] = "<script>alert(1)</script>"
    d["test_results"][0]["message"] = "<img src=x onerror=alert(1)>"
    out = render_html(d)
    assert "<script>alert(1)</script>" not in out and "&lt;script&gt;" in out
    assert "<img src=x" not in out
    for text in (
        "QA validation report",
        "Failed",
        "TC-AC1-01",
        "Not in data",
        "Print / save as PDF",
        "SELECT UPPER(NAME)",
        "3 of 4 test cases",
    ):
        assert text in out, text
    assert out.count("<h2 id=") == 9


def test_html_report_without_failures_or_transformation():
    d = copy.deepcopy(DATA)
    d["test_results"] = [r for r in d["test_results"] if r["status"] != "FAIL"]
    d["failure_analysis"], d["transformation"], d["column_differences"] = [], None, []
    out = render_html(d)
    assert "No failed tests." in out and "No failures to analyse." in out and "No column differences." in out


def test_excel_workbook_has_a_summary_and_formatted_tables(tmp_path):
    path = tmp_path / "r.xlsx"
    render_xlsx(DATA, path)
    wb = load_workbook(path)
    assert wb.sheetnames == [
        "Summary",
        "Acceptance criteria",
        "Test results",
        "Column differences",
        "Reconciliation",
        "Failure analysis",
        "Column rules",
        "Approvals",
        "Audit events",
    ]
    s = wb["Summary"]
    assert s["A1"].value == "QA validation report" and s["B4"].value == "Failed"
    tr = wb["Test results"]
    assert [c.value for c in tr[1]][:4] == ["Test", "AC", "From", "Type"]
    assert tr["D2"].value == "Positive" and tr["F3"].value == "Only records with a null customers.NAME"
    assert tr.freeze_panes == "A2" and tr.tables and tr.page_setup.orientation == "landscape"
    status_col = [c.value for c in tr[1]].index("Result") + 1
    assert tr.cell(row=2, column=status_col).fill.fgColor.rgb.endswith("FDECEA")  # failed result highlighted


def test_excel_empty_sheet_says_so(tmp_path):
    d = copy.deepcopy(DATA)
    d["column_differences"] = []
    path = tmp_path / "r.xlsx"
    render_xlsx(d, path)
    assert load_workbook(path)["Column differences"]["A1"].value == "No rows."
