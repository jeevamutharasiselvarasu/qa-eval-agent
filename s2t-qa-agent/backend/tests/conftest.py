import os
import shutil
import tempfile
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SAMPLES = ROOT / "sample_data"


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    """Every test gets an isolated storage dir + SQLite DB; Jira and LLM are mocked (no live credentials)."""
    monkeypatch.setenv("QA_STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("QA_ENV", "test")
    monkeypatch.setenv("JIRA_MODE", "mock")
    monkeypatch.setenv("LLM_MODE", "mock")
    monkeypatch.delenv("QA_AUTH_TOKENS", raising=False)
    monkeypatch.setenv("QA_PYTHON_TIMEOUT_S", "20")
    pg_url = os.getenv("QA_TEST_DATABASE_URL", "")
    if pg_url:
        monkeypatch.setenv("DATABASE_URL", pg_url)  # run the whole suite against PostgreSQL
    else:
        monkeypatch.delenv("DATABASE_URL", raising=False)
    from app import config, db
    config.reset_settings()
    db.init_engine()
    db.create_all()
    if pg_url:
        from sqlalchemy import text
        names = ", ".join(f'"{t.name}"' for t in db.Base.metadata.sorted_tables)
        with db.get_engine().begin() as conn:
            conn.execute(text("SET lock_timeout = '20s'"))
            conn.execute(text(f"TRUNCATE {names} RESTART IDENTITY CASCADE"))
    yield
    db.get_engine().dispose()


@pytest.fixture
def client():
    from fastapi.testclient import TestClient
    from app.main import create_app
    with TestClient(create_app()) as c:
        yield c


def upload(client, run_id, name, role="sources", **form):
    with open(SAMPLES / name, "rb") as f:
        r = client.post(f"/api/runs/{run_id}/datasets/{role}", files={"file": (name, f)}, data=form)
    return r


def wait_exec(client, eid, timeout=90):
    t0 = time.time()
    while time.time() - t0 < timeout:
        d = client.get(f"/api/executions/{eid}").json()
        if d["status"] not in ("QUEUED", "RUNNING"):
            return d
        time.sleep(0.3)
    raise AssertionError("execution did not finish")


def prepare_run(client, mode="SQL", approve=True):
    """Drive the API from run creation to approved artifacts (actual track)."""
    run = client.post("/api/runs", json={"jira_key": "DATA-1234"}).json()
    rid = run["id"]
    assert client.post(f"/api/runs/{rid}/jira/retrieve").status_code == 200
    reqs = client.get(f"/api/runs/{rid}/requirements").json()["requirements"]
    for r in reqs:
        if r["provenance"] == "AI_INFERRED":
            client.patch(f"/api/runs/{rid}/requirements/{r['id']}", json={"status": "REJECTED"})
    assert upload(client, rid, "customers.csv").status_code == 200
    assert upload(client, rid, "transactions.csv").status_code == 200
    assert upload(client, rid, "customer_master_target.csv", "target").status_code == 200
    assert client.post(f"/api/runs/{rid}/profile").status_code == 200
    assert client.post(f"/api/runs/{rid}/mappings/generate").status_code == 200
    v = client.post(f"/api/runs/{rid}/transformations/generate", json={"mode": mode}).json()
    assert client.post(f"/api/runs/{rid}/test-cases/generate").status_code == 200
    if approve:
        for k in ["REQUIREMENTS", "DATASETS", "MAPPINGS", "EXECUTION_SETTINGS"]:
            r = client.post(f"/api/runs/{rid}/approvals", json={"artifact_type": k, "decision": "APPROVED"})
            assert r.status_code == 200, r.text
        r = client.post(f"/api/runs/{rid}/transformations/{v['id']}/approve", json={"decision": "APPROVED"})
        assert r.status_code == 200, r.text
    return rid, v
