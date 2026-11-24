"""Jira connection settings, the streamlined two-approval workflow, and AC-based positive/negative/edge test cases."""
import time

import httpx
import pytest

from conftest import complete_requirements, SAMPLES, wait_exec


# ------------------------------------------------------------------ Jira settings
def test_jira_settings_save_mask_and_encrypt(client):
    r = client.get("/api/settings/jira").json()
    assert r["mode"] == "mock" and r["source"] == "env" and r["token_set"] is False
    bad = client.put("/api/settings/jira", json={"mode": "cloud", "base_url": "https://acme.atlassian.net", "email": "a@b.com"})
    assert bad.status_code == 400 and "API token" in bad.json()["detail"]
    for url in ["http://acme.atlassian.net", "https://acme.atlassian.net/jira/path", "https://127.0.0.1", "https://localhost"]:
        r = client.put("/api/settings/jira", json={"mode": "cloud", "base_url": url, "email": "a@b.com", "api_token": "x"})
        assert r.status_code == 400, url
    r = client.put("/api/settings/jira", json={"mode": "cloud", "base_url": "acme.atlassian.net/", "email": " qa@acme.com ",
                                                "api_token": "secret-token-123", "ac_custom_fields": "customfield_1, customfield_2"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["base_url"] == "https://acme.atlassian.net" and body["email"] == "qa@acme.com" and body["token_set"]
    assert body["ac_custom_fields"] == ["customfield_1", "customfield_2"] and body["source"] == "settings"
    assert "secret-token-123" not in r.text and "api_token" not in body  # token is never returned
    from app.db import session_scope
    from app.models import AppSetting
    with session_scope() as db:
        row = db.get(AppSetting, "jira")
        assert row.secret_enc and "secret-token-123" not in row.secret_enc  # stored encrypted
    # saving again without a token keeps the stored one; switching back to mock keeps the details
    r = client.put("/api/settings/jira", json={"mode": "mock"}).json()
    assert r["mode"] == "mock" and r["token_set"] and client.get("/api/meta").json()["jira_mode"] == "mock"
    r = client.put("/api/settings/jira", json={"mode": "cloud"}).json()
    assert r["mode"] == "cloud" and client.get("/api/meta").json()["jira_site"] == "https://acme.atlassian.net"
    r = client.put("/api/settings/jira", json={"mode": "mock", "clear_token": True}).json()
    assert r["token_set"] is False


def test_jira_connection_test_and_settings_drive_the_connector(client, monkeypatch):
    calls = []

    def handler(req: httpx.Request):
        calls.append((req.url.path, req.headers.get("authorization", "")))
        if req.url.path.endswith("/myself"):
            return httpx.Response(200, json={"displayName": "QA Bot", "active": True})
        if req.url.path.endswith("/serverInfo"):
            return httpx.Response(200, json={"deploymentType": "Cloud", "baseUrl": "https://acme.atlassian.net"})
        if req.url.path.endswith("/comment"):
            return httpx.Response(200, json={"comments": []})
        if "/issue/" in req.url.path:
            return httpx.Response(200, json={"key": "ACME-7", "id": "7", "names": {}, "fields": {
                "summary": "Live ticket", "description": "Acceptance Criteria\nAC1: ORDER_ID must be unique.\n",
                "issuetype": {"name": "Story"}, "status": {"name": "To Do"}, "project": {"key": "ACME", "name": "Acme"}}})
        return httpx.Response(404)

    real_client = httpx.Client
    monkeypatch.setattr("app.services.jira.httpx.Client", lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw))
    r = client.post("/api/settings/jira/test", json={"mode": "cloud", "base_url": "https://acme.atlassian.net",
                                                      "email": "qa@acme.com", "api_token": "secret"}).json()
    assert r["ok"] and r["display_name"] == "QA Bot" and r["deployment"] == "Cloud"
    assert client.get("/api/settings/jira").json()["mode"] == "mock"  # testing never saves
    client.put("/api/settings/jira", json={"mode": "cloud", "base_url": "https://acme.atlassian.net", "email": "qa@acme.com",
                                           "api_token": "secret"})
    p = client.post("/api/jira/preview", json={"jira_key": "acme-7"}).json()
    assert p["source"] == "LIVE" and p["summary"] == "Live ticket" and p["acceptance_criteria"][0]["id"] == "AC1"
    rid = client.post("/api/runs", json={"jira_key": "ACME-7"}).json()["id"]
    assert client.post(f"/api/runs/{rid}/jira/retrieve").json()["source"] == "LIVE"


def test_jira_connection_test_reports_bad_credentials(client, monkeypatch):
    real_client = httpx.Client
    monkeypatch.setattr("app.services.jira.httpx.Client",
                        lambda **kw: real_client(transport=httpx.MockTransport(lambda r: httpx.Response(401)), **kw))
    r = client.post("/api/settings/jira/test", json={"base_url": "https://acme.atlassian.net", "email": "x@y.z", "api_token": "bad"}).json()
    assert r["ok"] is False and r["code"] == "UNAUTHORIZED" and "API token" in r["hint"]


def test_only_admin_can_change_jira_settings(client, monkeypatch):
    from app import config
    monkeypatch.setenv("QA_AUTH_TOKENS", "tA:alice:approver:*,tZ:zed:admin:*")
    config.reset_settings()
    body = {"mode": "mock"}
    assert client.put("/api/settings/jira", json=body, headers={"Authorization": "Bearer tA"}).status_code == 403
    assert client.put("/api/settings/jira", json=body, headers={"Authorization": "Bearer tZ"}).status_code == 200


def test_existing_database_gets_new_columns(tmp_path, monkeypatch):
    """Upgrading: tables created before a column existed get it added on start-up (light migration)."""
    import sqlite3
    from app import config, db
    dbfile = tmp_path / "old.db"
    con = sqlite3.connect(dbfile)
    con.execute("CREATE TABLE qa_runs (id VARCHAR(32) PRIMARY KEY, project VARCHAR(64), name VARCHAR(200), jira_key VARCHAR(64), "
                "status VARCHAR(48), exception_state VARCHAR(48), settings JSON, created_by VARCHAR(128), created_at DATETIME, updated_at DATETIME)")
    con.commit()
    con.close()
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{dbfile}")
    config.reset_settings()
    db.init_engine()
    db.create_all()
    cols = [r[1] for r in sqlite3.connect(dbfile).execute("PRAGMA table_info(qa_runs)")]
    assert "test_pack_basis" in cols


# ------------------------------------------------------------------ streamlined workflow + AC-based tests
def _new_run(client, buggy_sql: str | None = None):
    rid = client.post("/api/runs", json={"jira_key": "DATA-1234"}).json()["id"]
    client.post(f"/api/runs/{rid}/jira/retrieve")
    for f, role in [("customers.csv", "sources"), ("transactions.csv", "sources"), ("customer_master_target.csv", "target")]:
        with open(SAMPLES / f, "rb") as fh:
            client.post(f"/api/runs/{rid}/datasets/{role}", files={"file": (f, fh)})
    client.post(f"/api/runs/{rid}/profile")
    client.post(f"/api/runs/{rid}/mappings/generate")
    r = client.post(f"/api/runs/{rid}/test-pack")  # refused: the requirements still have open questions
    assert r.status_code == 400 and "not complete" in r.json()["detail"]
    complete_requirements(client, rid)  # -> SQL generated from the requirements and the test pack built
    pack = client.get(f"/api/runs/{rid}/test-pack").json()
    if buggy_sql:
        client.post(f"/api/runs/{rid}/transformations", json={"mode": "SQL", "code": buggy_sql})
    return rid, pack


def _approve_and_run(client, rid):
    tv = client.get(f"/api/runs/{rid}/transformations").json()["current"]
    assert client.post(f"/api/runs/{rid}/transformations/{tv['id']}/approve", json={"decision": "APPROVED"}).status_code == 200
    r = client.post(f"/api/runs/{rid}/test-pack/approve", json={"decision": "APPROVED"})
    assert r.status_code == 200, r.text
    execs = client.post(f"/api/runs/{rid}/run-validation", json={}).json()
    done = [wait_exec(client, e["id"], 240) for e in execs]
    return done


def test_test_pack_has_positive_negative_and_edge_cases_per_ac(client):
    rid, pack = _new_run(client)
    assert pack["built"]
    assert client.get(f"/api/runs/{rid}/target-spec").json()["sql"]["state"] == "CURRENT"
    assert pack["by_type"]["POSITIVE"] > 0 and pack["by_type"]["NEGATIVE"] > 0 and pack["by_type"]["EDGE"] > 0
    tcs = client.get(f"/api/runs/{rid}/test-cases").json()["test_cases"]
    by_ac = {}
    for t in tcs:
        for ac in t["ac_ids"]:
            by_ac.setdefault(ac, set()).add(t["test_type"])
    assert set(by_ac) == {f"AC{i}" for i in range(1, 8)}
    for ac in ("AC1", "AC4", "AC5", "AC7"):
        assert {"POSITIVE", "NEGATIVE"} <= by_ac[ac], (ac, by_ac[ac])
    assert "EDGE" in by_ac["AC5"] and "EDGE" in by_ac["AC2"]
    # unspecified behaviour is BLOCKED, never guessed
    null_disc = next(t for t in tcs if "DISCOUNT is null" in t["title"])
    assert null_disc["state"] == "BLOCKED" and "not specified" in null_disc["blocked_reason"]
    # synthetic rule checks use an independent oracle compiled from the mapping rules over the scenario's sources
    oracle = next(t for t in tcs if t["validation"]["type"] == "RULE_ORACLE" and t["state"] == "READY")
    assert "src_" in oracle["validation"]["oracle_sql"]


def test_two_approval_gate_and_one_click_validation_with_report(client):
    rid, pack = _new_run(client)
    assert pack["blockers"] == ["Approve the transformation first (Transformation tab)"]
    r = client.post(f"/api/runs/{rid}/test-pack/approve", json={"decision": "APPROVED"})
    assert r.status_code == 400 and "transformation" in r.json()["detail"].lower()
    st = client.get(f"/api/runs/{rid}/test-pack").json()
    assert len(st["ignored_requirements"]) == 2  # undecided AI proposals are listed as not used, and don't block
    done = _approve_and_run(client, rid)
    assert {d["track"] for d in done} == {"ACTUAL", "SYNTHETIC"} and all(d["status"] == "COMPLETED" for d in done)
    for _ in range(40):
        reps = client.get(f"/api/runs/{rid}/reports").json()
        if reps:
            break
        time.sleep(0.3)
    assert reps and set(reps[0]["files"]) == {"html", "json", "powerbi", "xlsx"}
    # a later edit makes the pack stale and blocks a new validation run until both approvals are renewed
    v = client.get(f"/api/runs/{rid}/transformations").json()["current"]
    client.post(f"/api/runs/{rid}/transformations", json={"mode": "SQL", "code": v["code"] + "\n-- edit"})
    st = client.get(f"/api/runs/{rid}/test-pack").json()
    assert st["transformation"] == "STALE" and not st["can_approve"]
    assert client.post(f"/api/runs/{rid}/run-validation", json={}).status_code == 409


def test_synthetic_edge_cases_catch_a_buggy_transformation(client):
    """A hand-written transformation with three classic defects passes nothing it shouldn't."""
    bug = """SELECT c.CUSTOMER_ID, UPPER(c.CUSTOMER_NAME) AS CUSTOMER_NAME, c.SEGMENT,
       COALESCE(ROUND(SUM(t.AMOUNT), 2), 0) AS NET_AMOUNT, COUNT(t.TXN_ID) AS TXN_COUNT
FROM customers c LEFT JOIN transactions t ON c.CUSTOMER_ID = t.CUSTOMER_ID
WHERE t.STATUS = 'COMPLETED'
GROUP BY ALL"""
    rid, _ = _new_run(client, buggy_sql=bug)
    done = _approve_and_run(client, rid)
    syn = next(d for d in done if d["track"] == "SYNTHETIC")
    failed = {r["title"] for r in syn["results"] if r["status"] == "FAIL"}
    assert any("accents" in t for t in failed)                      # missing TRIM
    assert any("NET_AMOUNT is calculated correctly" in t for t in failed)  # discount ignored
    assert any("no matching transactions are kept" in t for t in failed)   # filter after LEFT JOIN drops customers
