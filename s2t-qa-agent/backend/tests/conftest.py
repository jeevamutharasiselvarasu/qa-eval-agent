import os
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


def complete_requirements(client, rid, status="ACCEPTED_LIMITATION"):
    """Answer every open question so the SQL can be generated (a limitation keeps dependent tests BLOCKED)."""
    spec = client.get(f"/api/runs/{rid}/target-spec").json()
    for q in spec["questions"]:
        if q["status"] == "OPEN":
            r = client.post(
                f"/api/runs/{rid}/target-spec/questions/{q['id']}", json={"status": status, "note": "Not specified in Jira; tested later."}
            )
            assert r.status_code == 200, r.text
    return client.get(f"/api/runs/{rid}/target-spec").json()


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
    complete_requirements(client, rid)
    v = client.post(f"/api/runs/{rid}/transformations/generate", json={"mode": mode}).json()
    assert client.post(f"/api/runs/{rid}/test-cases/generate").status_code == 200
    if approve:
        for k in ["REQUIREMENTS", "DATASETS", "MAPPINGS", "EXECUTION_SETTINGS"]:
            r = client.post(f"/api/runs/{rid}/approvals", json={"artifact_type": k, "decision": "APPROVED"})
            assert r.status_code == 200, r.text
        r = client.post(f"/api/runs/{rid}/transformations/{v['id']}/approve", json={"decision": "APPROVED"})
        assert r.status_code == 200, r.text
    return rid, v


# ------------------------------------------------------------------ fake Azure Blob container (no Azure account needed)
class _Blob:
    def __init__(self, name, size):
        self.name, self.size = name, size


class _Download:
    def __init__(self, path):
        self.path = path

    def readinto(self, fh):
        fh.write(self.path.read_bytes())


class FakeContainer:
    """Stands in for azure.storage.blob.ContainerClient: blobs are the files under `root`/<account>/<container>/."""

    def __init__(self, root, ref, error=None):
        self.base, self.error = Path(root) / ref.account / ref.container, error

    def list_blobs(self, name_starts_with=""):
        if self.error:
            raise self.error
        if not self.base.exists():
            raise type("ResourceNotFoundError", (Exception,), {"status_code": 404})("no container")
        for f in sorted(self.base.rglob("*")):
            name = f.relative_to(self.base).as_posix()
            if f.is_file() and name.startswith(name_starts_with):
                yield _Blob(name, f.stat().st_size)

    def get_blob_client(self, name):
        return type("C", (), {"download_blob": lambda _self: _Download(self.base / name)})()


@pytest.fixture
def azure(tmp_path, monkeypatch):
    """Returns put(path_in_storage, data_or_sample_name) and sets the app to read from the fake storage."""
    from app.services import blob

    root = tmp_path / "azure"
    state = {"error": None}
    monkeypatch.setattr(blob, "container_client", lambda ref: FakeContainer(root, ref, state["error"]))

    def put(path, content):
        dest = root / path
        dest.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, str) and "\n" not in content and (SAMPLES / content).is_file():
            content = (SAMPLES / content).read_bytes()
        dest.write_bytes(content if isinstance(content, bytes) else content.encode())
        return dest

    put.state = state
    return put
