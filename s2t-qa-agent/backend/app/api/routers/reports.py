"""Reports, traceability and the audit trail."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy import select

from ...models import (
    AuditLog,
    Execution,
    Report,
)
from ...services import reports as report_svc
from ..deps import DB, Analyst, Viewer, get_run, row, wf

router = APIRouter()


class ReportBody(BaseModel):
    execution_ids: list[str] | None = None


@router.post("/runs/{run_id}/reports")
def report_generate(run_id: str, db: DB, p: Analyst, body: ReportBody = ReportBody()):
    run = get_run(db, run_id, p)
    rpt = wf(report_svc.generate, db, run, p.user, body.execution_ids)
    return row(rpt, {"files": sorted((rpt.files or {}).keys())})  # formats only; server paths are never returned


@router.get("/runs/{run_id}/reports")
def reports_list(run_id: str, db: DB, p: Viewer):
    get_run(db, run_id, p)
    return [
        row(r, {"files": sorted((r.files or {}).keys())})
        for r in db.scalars(select(Report).where(Report.run_id == run_id).order_by(Report.created_at.desc()))
    ]


@router.get("/runs/{run_id}/report-data")
def report_data(run_id: str, db: DB, p: Viewer):
    run = get_run(db, run_id, p)
    # show what the latest validation run covered (its selected tests), not a mix of older executions
    last = db.scalars(select(Report).where(Report.run_id == run_id).order_by(Report.created_at.desc())).first()
    newest = db.scalars(
        select(Execution)
        .where(Execution.run_id == run_id, Execution.status.not_in(["QUEUED", "RUNNING"]))
        .order_by(Execution.created_at.desc())
    ).first()
    if last and newest and newest.id in (last.execution_ids or []):
        return report_svc.collect(db, run, last.execution_ids)
    return report_svc.collect(db, run)


@router.get("/reports/{report_id}/download/{fmt}")
def report_download(report_id: str, fmt: Literal["json", "html", "xlsx", "powerbi"], db: DB, p: Viewer, inline: bool = False):
    r = db.get(Report, report_id)
    if not r:
        raise HTTPException(404, "Report not found")
    get_run(db, r.run_id, p)
    path = (r.files or {}).get(fmt)
    if not path or not Path(path).exists():
        raise HTTPException(404, "Report file not available")
    media = {
        "json": "application/json",
        "html": "text/html",
        "powerbi": "application/zip",
        "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    }[fmt]
    name = {"json": "report.json", "html": "report.html", "xlsx": "report.xlsx", "powerbi": "powerbi_tables.zip"}[fmt]
    if inline and fmt == "html":
        return FileResponse(path, media_type=media)
    return FileResponse(path, media_type=media, filename=f"{r.run_id}_{name}")


@router.get("/runs/{run_id}/traceability")
def traceability(run_id: str, db: DB, p: Viewer):
    run = get_run(db, run_id, p)
    ids = [e.id for e in db.scalars(select(Execution).where(Execution.run_id == run_id))]
    return report_svc.build_traceability(db, run, ids)


@router.get("/runs/{run_id}/audit")
def audit_log(run_id: str, db: DB, p: Viewer):
    get_run(db, run_id, p)
    return [row(a) for a in db.scalars(select(AuditLog).where(AuditLog.run_id == run_id).order_by(AuditLog.id.desc()).limit(500))]
