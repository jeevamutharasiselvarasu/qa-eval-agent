"""QA runs."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from ...core.common import audit
from ...models import (
    ColumnMapping,
    Dataset,
    Execution,
    QARun,
    Report,
    Requirement,
    RequirementGap,
    TestCase,
)
from ...services import approvals as approval_svc
from ...services import runs as run_svc
from ...services import target_spec
from ..deps import DB, Analyst, Viewer, get_run, row, wf

router = APIRouter()


class RunCreate(BaseModel):
    jira_key: str = Field(min_length=3, max_length=40)
    name: str = Field(default="", max_length=200)
    project: str = Field(default="default", max_length=64, pattern=r"^[A-Za-z0-9_.-]+$")
    settings: dict[str, Any] = Field(default_factory=dict)


@router.post("/runs")
def create_run(body: RunCreate, db: DB, p: Analyst):
    if not p.can_access(body.project):
        raise HTTPException(403, "No access to project")
    run = wf(run_svc.create_run, db, body.jira_key, p.user, body.project, body.name, body.settings)
    return row(run)


@router.get("/runs")
def list_runs(db: DB, p: Viewer, q: str = "", status: str = ""):
    stmt = select(QARun).order_by(QARun.created_at.desc())
    out = []
    for r in db.scalars(stmt):
        if not p.can_access(r.project):
            continue
        if q and q.lower() not in (r.jira_key + " " + r.name + " " + r.id).lower():
            continue
        if status and r.status != status and r.exception_state != status:
            continue
        ex = db.scalars(select(Execution).where(Execution.run_id == r.id).order_by(Execution.created_at.desc())).all()
        out.append(
            row(
                r,
                {
                    "executions": [
                        {"id": e.id, "track": e.track, "status": e.status, "summary": e.summary, "ended_at": e.ended_at} for e in ex[:4]
                    ]
                },
            )
        )
    return out


@router.get("/runs/{run_id}")
def get_run_detail(run_id: str, db: DB, p: Viewer):
    run = get_run(db, run_id, p)
    c = lambda m, *w: db.scalar(select(func.count()).select_from(m).where(*w))  # noqa: E731
    spec = target_spec.build_spec(db, run)
    return row(
        run,
        {
            "requirements_ready": spec["ready"],
            "approvals": approval_svc.approval_state(db, run.id),
            "counts": {
                "requirements": c(Requirement, Requirement.run_id == run.id),
                "open_gaps": c(RequirementGap, RequirementGap.run_id == run.id, RequirementGap.status == "OPEN"),
                "open_items": len(spec["open_items"]),
                "target_columns": len(spec["columns"]),
                "sources": c(Dataset, Dataset.run_id == run.id, Dataset.role == "SOURCE"),
                "targets": c(Dataset, Dataset.run_id == run.id, Dataset.role == "TARGET"),
                "mappings": c(ColumnMapping, ColumnMapping.run_id == run.id),
                "test_cases": c(TestCase, TestCase.run_id == run.id),
                "executions": c(Execution, Execution.run_id == run.id),
                "reports": c(Report, Report.run_id == run.id),
            },
            "transformation_version": (lambda v: v and {"id": v.id, "version": v.version, "mode": v.mode, "origin": v.origin})(
                approval_svc.current_version(db, run.id)
            ),
        },
    )


class SettingsBody(BaseModel):
    settings: dict[str, Any]


@router.put("/runs/{run_id}/settings")
def update_settings(run_id: str, body: SettingsBody, db: DB, p: Analyst):
    run = get_run(db, run_id, p)
    allowed = set(run_svc.DEFAULT_SETTINGS)
    bad = set(body.settings) - allowed
    if bad:
        raise HTTPException(400, f"Unknown settings: {', '.join(sorted(bad))}")
    run.settings = {**(run.settings or {}), **body.settings}
    audit(db, p.user, "SETTINGS_UPDATED", run.id, "QA_RUN", run.id, settings=body.settings)
    return run.settings
