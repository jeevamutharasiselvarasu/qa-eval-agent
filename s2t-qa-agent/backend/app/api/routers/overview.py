"""Dashboard, metadata, Jira connection settings and ticket preview."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select

from ...config import get_settings
from ...core.common import EXCEPTION_STATES, WORKFLOW_STATES, audit
from ...models import (
    Execution,
    QARun,
)
from ...services import studio
from ...services.jira import JiraError
from ..deps import DB, Admin, Analyst, CurrentUser, Viewer, row, wf

router = APIRouter()


@router.get("/meta")
def meta(p: CurrentUser):
    s = get_settings()
    from ...services.settings_svc import get_jira_config

    jcfg = get_jira_config()
    return {
        "user": p.user,
        "role": p.role,
        "jira_mode": jcfg["mode"],
        "jira_site": jcfg["base_url"] if jcfg["mode"] == "cloud" else "",
        "llm_mode": s.llm_mode,
        "llm_allow_data_samples": s.llm_allow_data_samples,
        "workflow_states": WORKFLOW_STATES,
        "exception_states": sorted(EXCEPTION_STATES),
        "blob_storage": {"provider": "azure", "auth": "machine identity", "allowed_accounts": s.blob_allowed_accounts},
        "supported_formats": ["csv", "txt", "tsv", "xlsx", "json", "ndjson", "parquet"],
        "transformation_modes": list(studio.MODES),
        "sql_dialect": "DuckDB",
    }


@router.get("/dashboard")
def dashboard(db: DB, p: Viewer):
    runs = [r for r in db.scalars(select(QARun).order_by(QARun.created_at.desc())) if p.can_access(r.project)]
    ids = [r.id for r in runs]
    execs = list(db.scalars(select(Execution).where(Execution.run_id.in_(ids)).order_by(Execution.created_at)))
    totals = {"PASS": 0, "FAIL": 0, "BLOCKED": 0, "ERROR": 0}
    trend = []
    for e in execs:
        s = e.summary or {}
        for k in totals:
            totals[k] += s.get(k, 0)
        if e.ended_at:
            trend.append(
                {
                    "execution_id": e.id,
                    "run_id": e.run_id,
                    "track": e.track,
                    "at": e.ended_at.isoformat(),
                    "pass": s.get("PASS", 0),
                    "fail": s.get("FAIL", 0),
                    "blocked": s.get("BLOCKED", 0),
                    "error": s.get("ERROR", 0),
                }
            )
    by_status: dict[str, int] = {}
    for r in runs:
        by_status[r.status] = by_status.get(r.status, 0) + 1
    latest = {}
    for e in execs:
        latest.setdefault(e.run_id, {})[e.track] = {"id": e.id, "status": e.status, "summary": e.summary}
    return {
        "run_count": len(runs),
        "execution_count": len(execs),
        "totals": totals,
        "runs_by_status": by_status,
        "trend": trend[-30:],
        "recent_runs": [row(r, {"latest": latest.get(r.id, {})}) for r in runs[:8]],
    }


class JiraSettingsBody(BaseModel):
    mode: Literal["mock", "cloud"] = "mock"
    base_url: str | None = Field(default=None, max_length=300)
    email: str | None = Field(default=None, max_length=254)
    api_token: str | None = Field(default=None, max_length=2000)  # write-only; never returned
    clear_token: bool = False
    ac_custom_fields: str | list[str] | None = None


@router.get("/settings/jira")
def jira_settings_get(db: DB, p: Analyst):
    from ...services.settings_svc import get_jira_config, public_jira

    return public_jira(get_jira_config(db))


@router.put("/settings/jira")
def jira_settings_put(body: JiraSettingsBody, db: DB, p: Admin):
    from ...services.settings_svc import save_jira

    return wf(save_jira, db, body.model_dump(), p.user)


@router.post("/settings/jira/test")
def jira_settings_test(body: JiraSettingsBody, db: DB, p: Admin):
    """Test the submitted (or saved) Jira Cloud credentials without saving them."""
    from ...services.jira import JiraCloudConnector
    from ...services.settings_svc import merged_jira

    cfg = wf(merged_jira, db, {**body.model_dump(), "mode": "cloud"})
    try:
        info = JiraCloudConnector(cfg).myself()
    except JiraError as e:
        hints = {
            "UNAUTHORIZED": "Check the email and API token (create one at id.atlassian.com → Security → API tokens).",
            "NOT_FOUND": "Check the site URL, e.g. https://your-site.atlassian.net.",
            "TIMEOUT": "The site did not respond. Check the URL and your network or proxy.",
            "UPSTREAM": "Could not reach the site. Check the URL and your network or proxy.",
        }
        return {"ok": False, "code": e.code, "message": str(e), "hint": hints.get(e.code, "")}
    audit(db, p.user, "JIRA_CONNECTION_TESTED", None, "SETTINGS", "jira", ok=True, site=cfg["base_url"])
    return {"ok": True, **info}


class TicketBody(BaseModel):
    jira_key: str = Field(min_length=3, max_length=40)


@router.post("/jira/preview")
def jira_preview(body: TicketBody, p: Analyst):
    """Look up a ticket before creating a run (summary, status and how many acceptance criteria were found)."""
    from ...services.jira import get_connector
    from ...services.requirements import collect_units

    try:
        doc = get_connector().fetch(body.jira_key)
    except JiraError as e:
        raise HTTPException(400 if e.code not in ("TIMEOUT", "UPSTREAM", "RATE_LIMITED") else 502, f"{e.code}: {e}") from e
    acs, _, comments = collect_units(doc.to_dict())
    return {
        "key": doc.key,
        "source": doc.source,
        "summary": doc.summary,
        "status": doc.status,
        "issue_type": doc.issue_type,
        "acceptance_criteria": [{"id": u.ac_id, "text": u.text} for u in acs],
        "comments": len(comments),
        "attachments": len(doc.attachments),
        "project": doc.project,
    }
