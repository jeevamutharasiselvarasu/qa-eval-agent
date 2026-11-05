"""Regression tests for defects found in testing round 2 (second scenario, sandbox and robustness probes)."""
import time

import pytest

from conftest import SAMPLES, wait_exec
from app.core.common import safe_filename
from app.services.datasets import DatasetError, parse_file
from app.services.requirements import analyze
from app.services.transform.python_exec import validate_python


def test_composite_business_key_and_multiword_filter_and_unparsed_gap():
    doc = {"summary": "s", "comments": [], "attachments": [], "description":
           "Acceptance Criteria\nAC1: SALE_DATE and PRODUCT_CODE together form the business key of DAILY_SALES.\n"
           "AC2: Only order lines with QTY > 0 are included.\nAC3: The business key of X is KEY_A + KEY_B.\n"
           "AC4: Revenue should look reasonable to finance.\n"}
    res = analyze(doc)
    keys = [r.spec["columns"] for r in res.requirements if r.kind == "BUSINESS_KEY"]
    assert ["SALE_DATE", "PRODUCT_CODE"] in keys and ["KEY_A", "KEY_B"] in keys
    f = next(r for r in res.requirements if r.kind == "FILTER")
    assert f.spec == {"dataset_hint": "order lines", "column": "QTY", "op": ">", "value": "0"}
    unparsed = [g for g in res.gaps if g["gap_type"] == "UNPARSED_RULE"]
    assert len(unparsed) == 1 and "AC4" in unparsed[0]["description"]


@pytest.mark.parametrize("code", [
    'import operator\ndef transform(s):\n    return operator.attrgetter("x")(s)',
    'from operator import methodcaller\ndef transform(s): pass',
    'import string\ndef transform(s): pass',
    'def transform(s):\n    t = "{0._" + "_class_" + "_}"\n    return t.format(s)',
    'def transform(s):\n    return "{0.real}".format(1)',
    'def transform(s):\n    return "{0[x]}".format(s)',
    'def transform(s):\n    return "{x}".format_map(s)',
])
def test_python_dynamic_attribute_access_blocked(code):
    errs, _ = validate_python(code)
    assert errs and errs[0].kind == "SAFETY"


def test_python_plain_format_still_allowed():
    assert not validate_python('def transform(s):\n    x = "{} rows".format(len(s))\n    return s["customers"]')[0]


def test_ragged_csv_is_rejected_with_line_numbers(tmp_path):
    (tmp_path / "r.csv").write_text("ID,NAME,AMT\n1,a,10\n2,b,20,EXTRA\n3,c\n")
    with pytest.raises(DatasetError) as e:
        parse_file(tmp_path / "r.csv", tmp_path / "r.parquet")
    assert e.value.code == "PARSE_FAILED" and "line 3 has 4 fields" in str(e.value) and "line 4 has 2 fields" in str(e.value)
    (tmp_path / "one.csv").write_text("ONLY\n1\n2\n")  # a genuine single-column file is fine
    assert parse_file(tmp_path / "one.csv", tmp_path / "one.parquet").row_count == 2


def test_long_unicode_filename_keeps_extension():
    n = safe_filename("../ünïcødé 文件 " + "x" * 300 + ".csv")
    assert n.endswith(".csv") and len(n) <= 120 and "/" not in n


def test_unknown_api_path_returns_json_404(client):
    r = client.get("/api/does/not/exist")
    assert r.status_code == 404 and r.headers["content-type"].startswith("application/json")


def test_second_scenario_composite_key_lookup_join_python_mode(client):
    """SALES-77: JSON + XLSX sources, INNER lookup join, base-side filter, composite key, Python transformation approved."""
    rid = client.post("/api/runs", json={"jira_key": "SALES-77"}).json()["id"]
    client.post(f"/api/runs/{rid}/jira/retrieve")
    for name, role, alias in [("order_lines.json", "sources", "order_lines"), ("products.xlsx", "sources", "products"),
                              ("daily_sales.csv", "target", "")]:
        with open(SAMPLES / "sales" / name, "rb") as f:
            assert client.post(f"/api/runs/{rid}/datasets/{role}", files={"file": (name, f)},
                               data={"alias": alias} if alias else {}).status_code == 200
    client.post(f"/api/runs/{rid}/profile")
    maps = {m["target_column"]: m for m in client.post(f"/api/runs/{rid}/mappings/generate").json() if m["target_column"]}
    # grouping keys named in Jira rules are mapped explicitly (previously omitted -> wrong output grain)
    assert maps["SALE_DATE"]["expression"] == "order_lines.SALE_DATE" and maps["SALE_DATE"]["provenance"] == "JIRA_EXPLICIT"
    assert maps["PRODUCT_CODE"]["expression"] == "order_lines.PRODUCT_CODE" and maps["PRODUCT_CODE"]["status"] == "ACCEPTED"
    for r in client.get(f"/api/runs/{rid}/requirements").json()["requirements"]:
        if r["provenance"] == "AI_INFERRED":
            client.patch(f"/api/runs/{rid}/requirements/{r['id']}", json={"status": "REJECTED"})
    rows = {}
    for mode in ("VISUAL", "SQL", "PYTHON"):
        v = client.post(f"/api/runs/{rid}/transformations/generate", json={"mode": mode}).json()
        p = client.post(f"/api/runs/{rid}/transformations/{v['id']}/preview", json={"limit": 100}).json()
        assert p["status"] == "SUCCESS", p["errors"]
        rows[mode] = sorted((str(r["SALE_DATE"]), r["PRODUCT_CODE"], float(r["TOTAL_QTY"]), round(float(r["GROSS_REVENUE"]), 2)) for r in p["preview"]["rows"])
    assert rows["VISUAL"] == rows["SQL"] == rows["PYTHON"] and len(rows["SQL"]) == 7
    tcs = client.post(f"/api/runs/{rid}/test-cases/generate").json()
    assert all(t["state"] == "READY" for t in tcs)
    for k in ["REQUIREMENTS", "DATASETS", "MAPPINGS", "EXECUTION_SETTINGS"]:
        assert client.post(f"/api/runs/{rid}/approvals", json={"artifact_type": k, "decision": "APPROVED"}).status_code == 200
    assert client.post(f"/api/runs/{rid}/transformations/{v['id']}/approve", json={"decision": "APPROVED"}).status_code == 200
    sc = {s["scenario_type"]: s for s in client.post(f"/api/runs/{rid}/synthetic-data/generate", json={"seed": 11}).json()}
    assert sc["FILTER_EXCLUSION"]["status"] == "GENERATED" and "0.0" in sc["FILTER_EXCLUSION"]["description"]
    exp = next(s for s in client.get(f"/api/runs/{rid}/synthetic-data").json() if s["scenario_type"] == "DECIMAL_PRECISION")["expected"]
    revenue = sorted(r["GROSS_REVENUE"] for r in client.get(f"/api/runs/{rid}/datasets/{exp['id']}/preview").json()["rows"])
    assert 10.01 in revenue and 1.03 in revenue  # half-up rounding of 10.005 and 1.025 with QTY held at 1
    for k in ["TEST_CASES", "SYNTHETIC_DATA"]:
        client.post(f"/api/runs/{rid}/approvals", json={"artifact_type": k, "decision": "APPROVED"})
    d = wait_exec(client, client.post(f"/api/runs/{rid}/execute/actual", json={}).json()["id"])
    fails = [r for r in d["results"] if r["status"] == "FAIL"]
    assert [r["title"].split(" matches")[0] for r in fails] == ["TOTAL_QTY"]
    assert fails[0]["evidence"]["sample"][0]["PRODUCT_CODE"] == "P-300"  # off-by-one planted; 0.004 revenue drift is within tolerance
    s = wait_exec(client, client.post(f"/api/runs/{rid}/execute/synthetic", json={}).json()["id"])
    assert s["status"] == "COMPLETED" and s["summary"]["ERROR"] == 0
