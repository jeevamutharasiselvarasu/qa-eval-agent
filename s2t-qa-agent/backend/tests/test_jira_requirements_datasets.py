"""Jira adapters (mock + Cloud via mocked HTTP), requirement intelligence, parsing and profiling."""
import json
from pathlib import Path

import httpx
import openpyxl
import pytest

from app import config
from app.services.datasets import DatasetError, parse_file, profile_parquet
from app.services.jira import JiraCloudConnector, JiraError, MockJiraConnector, adf_to_text, validate_key
from app.services.requirements import analyze

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "jira" / "DATA-1234.json"


# ------------------------------------------------------------------ Jira
def test_mock_connector_and_key_validation():
    d = MockJiraConnector().fetch("data-1234")
    assert d.source == "MOCK" and d.key == "DATA-1234" and len(d.comments) == 3 and d.attachments[0]["content"]
    for bad in ["", "1234", "DATA_1234", "DATA-", "a" * 50]:
        with pytest.raises(JiraError):
            validate_key(bad)
    with pytest.raises(JiraError) as e:
        MockJiraConnector().fetch("DATA-9999")
    assert e.value.code == "FORBIDDEN"


def test_adf_conversion():
    adf = {"type": "doc", "content": [{"type": "paragraph", "content": [{"type": "text", "text": "Acceptance Criteria"}]},
                                      {"type": "bulletList", "content": [{"type": "listItem", "content": [
                                          {"type": "paragraph", "content": [{"type": "text", "text": "X must be unique"}]}]}]}]}
    assert "Acceptance Criteria" in adf_to_text(adf) and "- X must be unique" in adf_to_text(adf)


def _cloud(monkeypatch, handler):
    monkeypatch.setenv("JIRA_BASE_URL", "https://example.atlassian.net")
    monkeypatch.setenv("JIRA_EMAIL", "qa@example.com")
    monkeypatch.setenv("JIRA_API_TOKEN", "token")
    config.reset_settings()
    return JiraCloudConnector(client=httpx.Client(transport=httpx.MockTransport(handler)))


def test_cloud_connector_success(monkeypatch):
    def handler(req: httpx.Request):
        p = req.url.path
        if p.endswith("/comment"):
            return httpx.Response(200, json={"comments": [{"id": "1", "author": {"displayName": "BA"},
                                                           "body": {"type": "doc", "content": [{"type": "paragraph", "content": [{"type": "text", "text": "What about nulls?"}]}]}}]})
        if "/attachment/content/" in p:
            return httpx.Response(200, content=b"source_dataset,source_column,target_column,rule\na,X,X,direct\n")
        return httpx.Response(200, json={"key": "DATA-1", "id": "10", "names": {"customfield_1": "Acceptance Criteria"}, "fields": {
            "summary": "S", "description": {"type": "doc", "content": [{"type": "paragraph", "content": [{"type": "text", "text": "AC1: X must be unique."}]}]},
            "customfield_1": "Y cannot be null", "issuetype": {"name": "Story"}, "status": {"name": "Open"}, "labels": ["l"],
            "components": [{"name": "c"}], "project": {"key": "DATA", "name": "D"},
            "attachment": [{"id": "5", "filename": "m.csv", "size": 50, "mimeType": "text/csv"},
                           {"id": "6", "filename": "big.png", "size": 50, "mimeType": "image/png"}],
            "issuelinks": [{"type": {"name": "blocks"}, "outwardIssue": {"key": "DATA-2", "fields": {"summary": "x"}}}]}})
    d = _cloud(monkeypatch, handler).fetch("DATA-1")
    assert d.source == "LIVE" and "AC1" in d.description and d.custom_fields["Acceptance Criteria"] == "Y cannot be null"
    assert d.comments[0]["body"].strip() == "What about nulls?"
    assert d.attachments[0]["content"].startswith("source_dataset") and d.attachments[1]["skipped_reason"]
    assert d.links[0]["key"] == "DATA-2"


@pytest.mark.parametrize("status,code", [(401, "UNAUTHORIZED"), (403, "FORBIDDEN"), (404, "NOT_FOUND"), (429, "RATE_LIMITED"), (500, "UPSTREAM")])
def test_cloud_connector_errors(monkeypatch, status, code):
    c = _cloud(monkeypatch, lambda req: httpx.Response(status, json={}))
    with pytest.raises(JiraError) as e:
        c.fetch("DATA-1")
    assert e.value.code == code


def test_cloud_connector_timeout_and_config(monkeypatch):
    def boom(req):
        raise httpx.ReadTimeout("slow")
    with pytest.raises(JiraError) as e:
        _cloud(monkeypatch, boom).fetch("DATA-1")
    assert e.value.code == "TIMEOUT"
    monkeypatch.setenv("JIRA_BASE_URL", "")
    config.reset_settings()
    with pytest.raises(JiraError) as e:
        JiraCloudConnector()
    assert e.value.code == "NOT_CONFIGURED"


# ------------------------------------------------------------------ requirements
def test_requirement_extraction_provenance_and_gaps():
    res = analyze(json.loads(FIX.read_text()))
    kinds = {(r.kind, r.ac_id) for r in res.requirements}
    for k in [("BUSINESS_KEY", "AC1"), ("UNIQUE_KEY", "AC1"), ("NOT_NULL", "AC1"), ("DERIVED_COLUMN", "AC2"), ("DIRECT_MAPPING", "AC3"),
              ("FILTER", "AC4"), ("DERIVED_COLUMN", "AC5"), ("JOIN", "AC7"), ("DEFAULT_WHEN_UNMATCHED", "AC7")]:
        assert k in kinds, k
    net = next(r for r in res.requirements if r.kind == "DERIVED_COLUMN" and r.spec["target"] == "NET_AMOUNT")
    assert net.spec["precision"] == 2 and net.spec["group_by"] == ["CUSTOMER_ID"] and net.provenance == "JIRA_EXPLICIT"
    assert "NET_AMOUNT = SUM(AMOUNT - DISCOUNT)" in net.evidence
    att = [r for r in res.requirements if r.kind == "MAPPING_SPEC"]
    assert len(att) == 5 and all(r.provenance == "ATTACHMENT_DERIVED" for r in att)
    topics = {g["gap_type"] for g in res.gaps}
    assert topics == {"NULL_HANDLING", "NEGATIVE_VALUES"}  # attachment agrees with ACs -> no conflict
    assert any("prompt-injection" in w for w in res.warnings)


def test_missing_rules_become_gaps_not_inventions():
    doc = {"summary": "x", "description": "Acceptance Criteria\nAC1: TOTAL = PRICE * QTY.\n", "comments": [],
           "attachments": [{"filename": "m.csv", "content": "source_dataset,source_column,target_column,rule\na,PRICE,TOTAL,PRICE * QTY * 2\nb,Z,Z,direct\n"}]}
    res = analyze(doc)
    types = {g["gap_type"] for g in res.gaps}
    assert {"NO_BUSINESS_KEY", "PRECISION", "CONFLICT", "JOIN_TYPE"} <= types


def test_missing_acceptance_criteria():
    res = analyze({"summary": "x", "description": "Just a sentence.", "comments": [], "attachments": []})
    assert not [r for r in res.requirements if r.kind == "ACCEPTANCE_CRITERION"]
    assert any(g["gap_type"] == "NO_BUSINESS_KEY" for g in res.gaps)


# ------------------------------------------------------------------ parsing & profiling
def test_parsers(tmp_path):
    (tmp_path / "a.csv").write_text("ID,AMT\n1,10.5\n2,\n")
    (tmp_path / "b.txt").write_text("ID|NAME\n1|x\n")
    (tmp_path / "c.json").write_text(json.dumps([{"ID": 1, "N": "a"}, {"ID": 2, "N": "b"}]))
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Data"
    ws.append(["ID", "AMT", "D"])
    ws.append([1, 2.5, "2026-01-01"])
    ws.append([2, None, "2026-01-02"])
    wb.create_sheet("Other").append(["Q"])
    wb.save(tmp_path / "d.xlsx")
    assert parse_file(tmp_path / "a.csv", tmp_path / "a.parquet").row_count == 2
    assert parse_file(tmp_path / "b.txt", tmp_path / "b.parquet", {"delimiter": "|"}).columns[1]["name"] == "NAME"
    assert parse_file(tmp_path / "c.json", tmp_path / "c.parquet").row_count == 2
    x = parse_file(tmp_path / "d.xlsx", tmp_path / "d.parquet")
    assert x.sheet_name == "Data" and x.row_count == 2
    assert parse_file(tmp_path / "d.xlsx", tmp_path / "d2.parquet", {"sheet_name": "Other"}).columns[0]["name"] == "Q"
    assert parse_file(tmp_path / "a.parquet", tmp_path / "e.parquet").row_count == 2  # parquet round-trip


@pytest.mark.parametrize("name,content,code", [("x.csv", b"", "EMPTY_FILE"), ("x.xlsx", b"garbage", "CORRUPTED"),
                                               ("x.parquet", b"garbage", "CORRUPTED"), ("x.exe", b"MZ", "UNSUPPORTED_FORMAT"),
                                               ("x.json", b"{not json", "PARSE_FAILED")])
def test_parser_errors(tmp_path, name, content, code):
    (tmp_path / name).write_bytes(content)
    with pytest.raises(DatasetError) as e:
        parse_file(tmp_path / name, tmp_path / "o.parquet")
    assert e.value.code == code


def test_profiler(tmp_path):
    (tmp_path / "p.csv").write_text("ID,NAME,AMT,EMAIL,D\n1, a ,10,x@y.com,2026-01-01\n2,b,-5,z@y.com,2026-01-05\n3,,0,,2026-02-01\n3,,0,,2026-02-01\n")
    parse_file(tmp_path / "p.csv", tmp_path / "p.parquet")
    p = profile_parquet(tmp_path / "p.parquet")
    cols = {c["name"]: c for c in p["columns"]}
    assert p["row_count"] == 4 and p["duplicate_row_count"] == 1
    assert cols["NAME"]["null_count"] == 2 and cols["NAME"]["format_issues"]["leading_trailing_whitespace"] == 1
    assert cols["AMT"]["negative_count"] == 1 and cols["AMT"]["zero_count"] == 2 and cols["AMT"]["min"] == "-5"
    assert cols["ID"]["duplicate_value_count"] == 1
    assert cols["EMAIL"]["sensitive"] and all("*" in s for s in cols["EMAIL"]["sample_values"])  # masked
    assert cols["D"]["min"].startswith("2026-01-01")
    assert p["candidate_keys"] == []  # no unique key because of the duplicate row
