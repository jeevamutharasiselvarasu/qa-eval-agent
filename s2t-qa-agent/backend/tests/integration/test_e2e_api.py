"""End-to-end tests through the HTTP API with the mock Jira fixture and mock LLM (no live credentials)."""

import json

from conftest import prepare_run, wait_exec


def _approve(client, rid, *kinds):
    for k in kinds:
        r = client.post(f"/api/runs/{rid}/approvals", json={"artifact_type": k, "decision": "APPROVED"})
        assert r.status_code == 200, r.text


def test_full_actual_track_finds_planted_defects(client):
    rid, v = prepare_run(client)
    # execution is blocked until test cases are approved
    r = client.post(f"/api/runs/{rid}/execute/actual", json={})
    assert r.status_code == 409 and "TEST_CASES" in r.json()["detail"]
    _approve(client, rid, "TEST_CASES")
    ex = client.post(f"/api/runs/{rid}/execute/actual", json={}).json()
    d = wait_exec(client, ex["id"])
    assert d["status"] == "COMPLETED"
    by_title = {r["title"].split(" in the uploaded target")[0]: r for r in d["results"] if " in the uploaded target " in r["title"]}
    assert by_title["NET_AMOUNT"]["status"] == "FAIL"
    diff = by_title["NET_AMOUNT"]["evidence"]["sample"][0]
    assert diff["CUSTOMER_ID"] == "C102" and diff["expected"] == 1800 and diff["actual"] == 1700 and diff["difference"] == -100
    assert by_title["CUSTOMER_NAME"]["status"] == "FAIL"
    assert by_title["SEGMENT"]["status"] == "PASS" and by_title["TXN_COUNT"]["status"] == "PASS"
    statuses = {r["validation"] if False else r["category"]: r["status"] for r in d["results"]}
    recon = [r for r in d["results"] if r["category"] == "RECONCILIATION"]
    assert all(r["status"] == "FAIL" for r in recon)  # C108 missing, C999 extra
    missing = next(r for r in recon if "missing" in r["title"])
    assert missing["evidence"]["sample"][0]["CUSTOMER_ID"] == "C108"
    assert statuses["SCHEMA"] == "PASS" and statuses["COUNT"] == "PASS"
    # AI analysis exists for failures and never changes the deterministic status
    for r in d["results"]:
        if r["status"] == "FAIL":
            assert r["analysis"]["deterministic_status"] == "FAIL"
            assert r["analysis"]["confirmed_facts"]
    # duplicate execution is prevented / reused
    again = client.post(f"/api/runs/{rid}/execute/actual", json={}).json()
    assert again["reused"] is True and again["id"] == ex["id"]
    # reports
    rpt = client.post(f"/api/runs/{rid}/reports", json={}).json()
    for fmt in ("json", "html", "xlsx", "powerbi"):
        f = client.get(f"/api/reports/{rpt['id']}/download/{fmt}")
        assert f.status_code == 200 and len(f.content) > 200
    data = json.loads(client.get(f"/api/reports/{rpt['id']}/download/json").content)
    assert data["summary"]["overall"] == "FAIL"
    assert data["reconciliation"]["missing_records"] == 1 and data["reconciliation"]["extra_records"] == 1
    trace = {t["acceptance_criterion"]: t for t in data["traceability"]}
    assert trace["AC5"]["coverage"] == "FAIL" and trace["AC3"]["coverage"] == "PASS"  # skipped edge cases don't lower it
    assert all(t["test_case_ids"] for t in data["traceability"]), "every AC has at least one test"


def test_edge_cases_run_on_the_uploaded_records_that_hit_them(client):
    """Negative / edge cases check the uploaded target on exactly the records with that situation; absent -> SKIPPED."""
    rid, v = prepare_run(client)
    _approve(client, rid, "TEST_CASES")
    d = wait_exec(client, client.post(f"/api/runs/{rid}/execute/actual", json={}).json()["id"])
    tcs = {t["id"]: t for t in client.get(f"/api/runs/{rid}/test-cases").json()["test_cases"]}
    assert all(t["track"] == "ACTUAL" for t in tcs.values())
    res = {r["test_case_id"]: r for r in d["results"]}

    def by(text):
        return next(res[i] for i, t in tcs.items() if text in t["title"] and i in res)

    nomatch = by("records with no matching transactions are kept")
    assert nomatch["status"] == "PASS" and "Checked on the 1 record(s) with no matching transactions row" in nomatch["message"]
    assert by("adds up every matching transactions row")["status"] == "FAIL"  # C102 has several transactions
    dup = by("duplicated CUSTOMER_ID in the source")
    assert dup["status"] == "SKIPPED" and "no record in the uploaded data has" in dup["message"]
    unusual = by("CUSTOMER_NAME handles unusual text")
    assert unusual["status"] == "FAIL"  # the untrimmed C105 name is exactly such a record
    scoped = [t for t in tcs.values() if t["validation"].get("scope")]
    assert scoped and all("src_" in t["validation"]["scope"]["sql"] for t in scoped)
    assert {"NEGATIVE", "EDGE"} <= {t["test_type"] for t in scoped}


def test_gap_clarification_requires_reapproval(client):
    rid, v = prepare_run(client)
    gaps = client.get(f"/api/runs/{rid}/requirements").json()["gaps"]
    neg = next(g for g in gaps if g["gap_type"] == "NEGATIVE_VALUES")
    assert client.get(f"/api/runs/{rid}/approvals").json()["state"]["REQUIREMENTS"]["status"] == "APPROVED"
    r = client.post(
        f"/api/runs/{rid}/requirements/clarifications",
        json={"gap_id": neg["id"], "status": "CLARIFIED", "clarification": "Negative NET_AMOUNT is allowed as-is."},
    )
    assert r.status_code == 200
    assert client.get(f"/api/runs/{rid}/approvals").json()["state"]["REQUIREMENTS"]["status"] == "STALE"


def test_code_change_invalidates_transformation_approval(client):
    rid, v = prepare_run(client)
    assert client.get(f"/api/runs/{rid}/approvals").json()["state"]["TRANSFORMATION"]["status"] == "APPROVED"
    new = client.post(f"/api/runs/{rid}/transformations", json={"mode": "SQL", "code": v["code"] + "\n-- edited"}).json()
    assert new["version"] == v["version"] + 1 and new["origin"] == "USER_EDITED_AI"
    st = client.get(f"/api/runs/{rid}/approvals").json()["state"]["TRANSFORMATION"]
    assert st["status"] == "STALE"
    _approve(client, rid, "TEST_CASES")
    r = client.post(f"/api/runs/{rid}/execute/actual", json={})
    assert r.status_code == 409 and "TRANSFORMATION" in r.json()["detail"]
    # cannot approve an old version
    r = client.post(f"/api/runs/{rid}/transformations/{v['id']}/approve", json={"decision": "APPROVED"})
    assert r.status_code == 409


def test_all_three_modes_produce_equivalent_expected_targets(client):
    rid, _ = prepare_run(client, approve=False)
    outs = {}
    for mode in ("VISUAL", "SQL", "PYTHON"):
        v = client.post(f"/api/runs/{rid}/transformations/generate", json={"mode": mode}).json()
        assert v["validation"]["valid"], (mode, v["validation"])
        p = client.post(f"/api/runs/{rid}/transformations/{v['id']}/preview", json={"limit": 100}).json()
        assert p["status"] == "SUCCESS", (mode, p["errors"])
        outs[mode] = sorted(
            (r["CUSTOMER_ID"], r["CUSTOMER_NAME"], r["SEGMENT"], round(float(r["NET_AMOUNT"]), 2), int(r["TXN_COUNT"]))
            for r in p["preview"]["rows"]
        )
    assert outs["VISUAL"] == outs["SQL"] == outs["PYTHON"]
    assert ("C102", "VIKRAM SINGH", "CORPORATE", 1800.0, 1) in outs["SQL"]
    assert ("C110", "KARTHIK S", "SME", 0.0, 0) in outs["SQL"]


def test_user_written_python_and_ai_explain_fix(client):
    rid, _ = prepare_run(client, approve=False)
    code = (
        "import pandas as pd\n\ndef transform(sources):\n    c = sources['customers']\n"
        "    return c[['CUSTOMER_ID']].assign(NAME=c['CUSTOMER_NAME'].str.strip())\n"
    )
    v = client.post(f"/api/runs/{rid}/transformations", json={"mode": "PYTHON", "code": code}).json()
    assert v["origin"] == "USER_WRITTEN" and v["validation"]["valid"]
    p = client.post(f"/api/runs/{rid}/transformations/{v['id']}/preview", json={"limit": 5}).json()
    assert p["status"] == "SUCCESS" and p["row_count"] == 5
    bad = client.post(f"/api/runs/{rid}/transformations/validate", json={"mode": "SQL", "code": "SELECT CUSTOMR_ID FROM customers"}).json()
    assert bad["valid"]  # parses; unknown column is a runtime error
    v2 = client.post(f"/api/runs/{rid}/transformations", json={"mode": "SQL", "code": "SELECT CUSTOMR_ID FROM customers"}).json()
    p2 = client.post(f"/api/runs/{rid}/transformations/{v2['id']}/preview", json={"limit": 5}).json()
    assert p2["status"] == "FAILED" and p2["errors"][0]["line"] == 1
    fix = client.post(
        f"/api/runs/{rid}/transformations/fix",
        json={"mode": "SQL", "code": "SELECT CUSTOMR_ID FROM customers", "error": p2["errors"][0]["message"]},
    ).json()
    assert "CUSTOMER_ID" in fix["code"]
    ex = client.post(f"/api/runs/{rid}/transformations/explain", json={"mode": "SQL", "code": "SELECT a FROM customers WHERE b > 1"}).json()
    assert "customers" in ex["explanation"]


def test_retry_and_restart_recovery(client):
    from app.db import session_scope
    from app.models import Execution, TestResult
    from app.services import execution as exsvc

    rid, v = prepare_run(client)
    _approve(client, rid, "TEST_CASES")
    ex = client.post(f"/api/runs/{rid}/execute/actual", json={}).json()
    d = wait_exec(client, ex["id"])
    n_done = len(d["results"])
    # simulate a crash mid-run: drop some results, mark RUNNING, then "restart"
    with session_scope() as db:
        e = db.get(Execution, ex["id"])
        rs = list(db.query(TestResult).filter(TestResult.execution_id == e.id))
        for r in rs[:4]:
            db.delete(r)
        e.status = "RUNNING"
    assert exsvc.recover_interrupted() == 1
    assert client.get(f"/api/executions/{ex['id']}").json()["status"] == "INTERRUPTED"
    r = client.post(f"/api/executions/{ex['id']}/retry")
    assert r.status_code == 200
    d2 = wait_exec(client, ex["id"])
    assert d2["status"] == "COMPLETED" and len(d2["results"]) == n_done and d2["attempt"] == 2
    assert {r["status"] for r in d2["results"]} == {r["status"] for r in d["results"]}


def test_technical_error_is_error_not_fail(client):
    from app.db import session_scope
    from app.models import TestCase

    rid, v = prepare_run(client)
    with session_scope() as db:
        t = db.query(TestCase).filter(TestCase.run_id == rid, TestCase.category == "KEY").first()
        t.validation = {**t.validation, "columns": ["DOES_NOT_EXIST"]}
    _approve(client, rid, "TEST_CASES")
    ex = client.post(f"/api/runs/{rid}/execute/actual", json={}).json()
    d = wait_exec(client, ex["id"])
    errs = [r for r in d["results"] if r["status"] == "ERROR"]
    assert len(errs) == 1 and "not found" in errs[0]["message"]
    assert d["status"] == "PARTIAL_SUCCESS"


def test_rbac_and_project_boundaries(client, monkeypatch):
    from app import config

    monkeypatch.setenv(
        "QA_AUTH_TOKENS",
        "tA:alice:approver:retail,tB:bob:analyst:retail,tV:vic:viewer:retail,tC:carl:admin:finance|*,tX:xena:analyst:finance",
    )
    config.reset_settings()
    H = lambda t: {"Authorization": f"Bearer {t}"}  # noqa: E731
    assert client.get("/api/runs").status_code == 401
    assert client.get("/api/runs", headers=H("bogus")).status_code == 401
    r = client.post("/api/runs", json={"jira_key": "DATA-1234", "project": "retail"}, headers=H("tB"))
    assert r.status_code == 200
    rid = r.json()["id"]
    assert client.post("/api/runs", json={"jira_key": "DATA-1234", "project": "retail"}, headers=H("tV")).status_code == 403
    assert client.get(f"/api/runs/{rid}", headers=H("tX")).status_code == 404  # other project cannot see it
    client.post(f"/api/runs/{rid}/jira/retrieve", headers=H("tB"))
    r = client.post(f"/api/runs/{rid}/approvals", json={"artifact_type": "EXECUTION_SETTINGS", "decision": "APPROVED"}, headers=H("tB"))
    assert r.status_code == 403  # analyst cannot approve
    r = client.post(f"/api/runs/{rid}/approvals", json={"artifact_type": "EXECUTION_SETTINGS", "decision": "APPROVED"}, headers=H("tA"))
    assert r.status_code == 200 and r.json()["reviewer"] == "alice"


def test_upload_validation(client, tmp_path):
    rid = client.post("/api/runs", json={"jira_key": "DATA-1234"}).json()["id"]
    # malicious filename is sanitised
    r = client.post(f"/api/runs/{rid}/datasets/sources", files={"file": ("../../etc/evil name.csv", b"A,B\n1,2\n")})
    assert r.status_code == 200 and r.json()["filename"] == "evil_name.csv"
    assert client.post(f"/api/runs/{rid}/datasets/sources", files={"file": ("x.exe", b"MZ..")}).status_code == 422
    assert client.post(f"/api/runs/{rid}/datasets/sources", files={"file": ("empty.csv", b"")}).status_code == 422
    assert client.post(f"/api/runs/{rid}/datasets/sources", files={"file": ("bad.xlsx", b"not a zip")}).status_code == 422
    dup = client.post(f"/api/runs/{rid}/datasets/sources", files={"file": ("again.csv", b"A,B\n1,2\n")}, data={"alias": "again"})
    assert dup.status_code == 409  # same content
    r = client.post(f"/api/runs/{rid}/profile")
    assert r.status_code == 400  # no target yet
    # second target replaces the first (exactly one target per run)
    client.post(f"/api/runs/{rid}/datasets/target", files={"file": ("t1.csv", b"A\n1\n")})
    client.post(f"/api/runs/{rid}/datasets/target", files={"file": ("t2.csv", b"A\n2\n")})
    assert len([d for d in client.get(f"/api/runs/{rid}/datasets").json() if d["role"] == "TARGET"]) == 1
    # oversized
    import os

    from app import config

    os.environ["QA_MAX_UPLOAD_BYTES"] = "10"
    config.reset_settings()
    try:
        r = client.post(f"/api/runs/{rid}/datasets/sources", files={"file": ("big.csv", b"A,B\n" + b"1,2\n" * 50)}, data={"alias": "big"})
        assert r.status_code == 413
    finally:
        del os.environ["QA_MAX_UPLOAD_BYTES"]
        config.reset_settings()


def test_jira_errors_are_recorded(client):
    rid = client.post("/api/runs", json={"jira_key": "DATA-9999"}).json()["id"]
    r = client.post(f"/api/runs/{rid}/jira/retrieve")
    assert r.status_code == 400 and "FORBIDDEN" in r.json()["detail"]
    assert client.get(f"/api/runs/{rid}").json()["exception_state"] == "JIRA_RETRIEVAL_FAILED"
    rid2 = client.post("/api/runs", json={"jira_key": "DATA-4040"}).json()["id"]
    assert "NOT_FOUND" in client.post(f"/api/runs/{rid2}/jira/retrieve").json()["detail"]
    assert client.post("/api/runs", json={"jira_key": "not a key"}).status_code == 400


def test_prompt_injection_in_jira_is_ignored(client):
    rid = client.post("/api/runs", json={"jira_key": "DATA-1234"}).json()["id"]
    client.post(f"/api/runs/{rid}/jira/retrieve")
    data = client.get(f"/api/runs/{rid}/requirements").json()
    assert any("prompt-injection" in w for w in data["warnings"])
    assert not any("PASS" in r["description"] and "mark" in r["description"].lower() for r in data["requirements"])


def test_compare_and_history(client):
    rid, v = prepare_run(client)
    _approve(client, rid, "TEST_CASES")
    a = client.post(f"/api/runs/{rid}/execute/actual", json={}).json()
    wait_exec(client, a["id"])
    b = client.post(f"/api/runs/{rid}/execute/actual", json={"force": True}).json()
    assert b["id"] != a["id"]
    wait_exec(client, b["id"])
    cmp = client.get(f"/api/compare?a={a['id']}&b={b['id']}").json()
    assert cmp["changed"] == 0 and cmp["rows"]
    runs = client.get("/api/runs?q=DATA-1234").json()
    assert runs and runs[0]["executions"]
    dash = client.get("/api/dashboard").json()
    assert dash["execution_count"] == 2
    assert client.get(f"/api/runs/{rid}/audit").json()
