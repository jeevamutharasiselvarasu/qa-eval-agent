"""Unit tests: secrets, Jira URL guard, role checks and API helpers."""

import pytest
from fastapi import HTTPException

from app.api.deps import row, wf
from app.core.auth import Principal, _parse_tokens
from app.models import Dataset
from app.services.execution import ExecutionError
from app.services.jira import JiraError
from app.services.runs import WorkflowError
from app.services.settings_svc import normalize_base_url, public_jira


def test_secret_round_trip_and_tamper_detection(monkeypatch):
    from cryptography.fernet import Fernet

    from app.core import secrets

    monkeypatch.setenv("QA_SECRET_KEY", Fernet.generate_key().decode())
    token = secrets.encrypt("api-token-value")
    assert token != "api-token-value" and secrets.decrypt(token) == "api-token-value"
    assert secrets.decrypt(token[:-4] + "AAAA") is None
    monkeypatch.setenv("QA_SECRET_KEY", Fernet.generate_key().decode())  # a different key cannot read it
    assert secrets.decrypt(token) is None


def test_secret_key_file_is_created_once_and_private(tmp_path, monkeypatch):
    from app import config
    from app.core import secrets

    monkeypatch.delenv("QA_SECRET_KEY", raising=False)
    monkeypatch.setenv("QA_STORAGE_DIR", str(tmp_path))
    config.reset_settings()
    t = secrets.encrypt("x")
    key = tmp_path / ".secret.key"
    assert key.exists() and (key.stat().st_mode & 0o777) == 0o600
    assert secrets.decrypt(t) == "x"


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("acme.atlassian.net", "https://acme.atlassian.net"),
        ("https://acme.atlassian.net/", "https://acme.atlassian.net"),
        ("https://jira.example.com:8443", "https://jira.example.com:8443"),
        ("", ""),
    ],
)
def test_jira_url_is_normalised(url, expected):
    assert normalize_base_url(url) == expected


@pytest.mark.parametrize(
    ("url", "message"),
    [
        ("http://acme.atlassian.net", "https://"),
        ("https://acme.atlassian.net/jira", "no path"),
        ("https://acme.atlassian.net?x=1", "no path"),
        ("https://user@acme.atlassian.net", "no path"),
        ("https://127.0.0.1", "disallowed"),
        ("https://169.254.169.254", "disallowed"),
        ("https://localhost", "disallowed"),
    ],
)
def test_jira_url_guard_refuses_unsafe_addresses(url, message):
    with pytest.raises(ValueError, match=message):
        normalize_base_url(url)


def test_public_jira_never_returns_the_token():
    out = public_jira({"mode": "cloud", "api_token": "secret"})
    assert "api_token" not in out and out["token_set"] is True


def test_tokens_roles_and_projects():
    tokens = _parse_tokens("tA:alice:approver:retail|finance, bad, tB:bob:wizard:x, tC:carol:viewer:*")
    assert set(tokens) == {"tA", "tC"}
    alice = tokens["tA"]
    assert alice.has("analyst") and alice.has("approver") and not alice.has("admin")
    assert alice.can_access("retail") and not alice.can_access("hr")
    assert tokens["tC"].can_access("anything") and not tokens["tC"].has("analyst")
    assert Principal("root", "admin", set()).can_access("hr")


def test_row_hides_server_paths():
    d = Dataset(
        id="DS-1",
        run_id="R",
        role="SOURCE",
        alias="c",
        filename="c.csv",
        parquet_path="/srv/x.parquet",
        original_path="/srv/c.csv",
        fingerprint="f",
        row_count=1,
        columns=[],
    )
    out = row(d, {"extra": 1})
    assert "parquet_path" not in out and "original_path" not in out and out["alias"] == "c" and out["extra"] == 1


@pytest.mark.parametrize(
    ("exc", "status"),
    [
        (WorkflowError("x", 409), 409),
        (ExecutionError("x", 409), 409),
        (JiraError("NOT_FOUND", "x"), 400),
        (ValueError("bad"), 400),
    ],
)
def test_business_errors_become_http_errors(exc, status):
    def boom():
        raise exc

    with pytest.raises(HTTPException) as e:
        wf(boom)
    assert e.value.status_code == status
