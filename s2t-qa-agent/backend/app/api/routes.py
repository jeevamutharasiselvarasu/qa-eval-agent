"""REST API. Every run-scoped endpoint checks the caller's role and project access."""
from __future__ import annotations

import csv
import io
import shutil
import tempfile
from pathlib import Path
from typing import Any, Literal

import duckdb
from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..core.auth import Principal, get_principal, require
from ..core.common import WORKFLOW_STATES, EXCEPTION_STATES, audit, mask_records
from ..db import get_db
from ..models import (AuditLog, ColumnMapping, Dataset, DatasetProfile, Evidence, Execution, FailureAnalysis, JiraIssue,
                      LLMCall, QARun, Report, Requirement, RequirementGap, SyntheticScenario, TestCase, TestResult,
                      Transformation, TransformationExecution, TransformationVersion)
from ..services import approvals as approval_svc
from ..services import execution as exec_svc
from ..services import reports as report_svc
from ..services import runs as run_svc
from ..services import studio, synthetic, testgen
from ..services.datasets import preview_rows
from ..services.jira import JiraError
from ..services.transform.engine import validate_definition
from ..services.transform.contract import TransformationDefinition
from ..services.transform.visual import VisualSpecError, compile_visual

router = APIRouter(prefix="/api")
HIDDEN = {"original_path", "parquet_path", "path"}


def row(obj, extra: dict | None = None) -> dict:
    d = {c.name: getattr(obj, c.name) for c in obj.__table__.columns if c.name not in HIDDEN}
    if extra:
        d.update(extra)
    return d


def get_run(db: Session, run_id: str, p: Principal) -> QARun:
    run = db.get(QARun, run_id)
    if not run or not p.can_access(run.project):
        raise HTTPException(404, "Run not found")
    return run


def wf(fn, *a, **kw):
    try:
        return fn(*a, **kw)
    except run_svc.WorkflowError as e:
        raise HTTPException(e.status, str(e)) from e
    except exec_svc.ExecutionError as e:
        raise HTTPException(e.status, str(e)) from e
    except JiraError as e:
        raise HTTPException(400, f"{e.code}: {e}") from e
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


# ---------------------------------------------------------------- meta / dashboard
@router.get("/meta")
def meta(p: Principal = Depends(get_principal)):
    s = get_settings()
    return {"user": p.user, "role": p.role, "jira_mode": s.jira_mode, "llm_mode": s.llm_mode,
            "llm_allow_data_samples": s.llm_allow_data_samples, "workflow_states": WORKFLOW_STATES,
            "exception_states": sorted(EXCEPTION_STATES), "supported_formats": ["csv", "txt", "tsv", "xlsx", "json", "ndjson", "parquet"],
            "transformation_modes": list(studio.MODES), "sql_dialect": "DuckDB", "scenarios": synthetic.SCENARIOS}


@router.get("/dashboard")
def dashboard(db: Session = Depends(get_db), p: Principal = Depends(require("viewer"))):
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
            trend.append({"execution_id": e.id, "run_id": e.run_id, "track": e.track, "at": e.ended_at.isoformat(),
                          "pass": s.get("PASS", 0), "fail": s.get("FAIL", 0), "blocked": s.get("BLOCKED", 0), "error": s.get("ERROR", 0)})
    by_status: dict[str, int] = {}
    for r in runs:
        by_status[r.status] = by_status.get(r.status, 0) + 1
    latest = {}
    for e in execs:
        latest.setdefault(e.run_id, {})[e.track] = {"id": e.id, "status": e.status, "summary": e.summary}
    return {"run_count": len(runs), "execution_count": len(execs), "totals": totals, "runs_by_status": by_status,
            "trend": trend[-30:], "recent_runs": [row(r, {"latest": latest.get(r.id, {})}) for r in runs[:8]]}


# ---------------------------------------------------------------- runs
class RunCreate(BaseModel):
    jira_key: str = Field(min_length=3, max_length=40)
    name: str = Field(default="", max_length=200)
    project: str = Field(default="default", max_length=64, pattern=r"^[A-Za-z0-9_.-]+$")
    settings: dict[str, Any] = Field(default_factory=dict)


@router.post("/runs")
def create_run(body: RunCreate, db: Session = Depends(get_db), p: Principal = Depends(require("analyst"))):
    if not p.can_access(body.project):
        raise HTTPException(403, "No access to project")
    run = wf(run_svc.create_run, db, body.jira_key, p.user, body.project, body.name, body.settings)
    return row(run)


@router.get("/runs")
def list_runs(q: str = "", status: str = "", db: Session = Depends(get_db), p: Principal = Depends(require("viewer"))):
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
        out.append(row(r, {"executions": [{"id": e.id, "track": e.track, "status": e.status, "summary": e.summary,
                                            "ended_at": e.ended_at} for e in ex[:4]]}))
    return out


@router.get("/runs/{run_id}")
def get_run_detail(run_id: str, db: Session = Depends(get_db), p: Principal = Depends(require("viewer"))):
    run = get_run(db, run_id, p)
    c = lambda m, *w: db.scalar(select(func.count()).select_from(m).where(*w))  # noqa: E731
    return row(run, {
        "approvals": approval_svc.approval_state(db, run.id),
        "counts": {"requirements": c(Requirement, Requirement.run_id == run.id),
                   "open_gaps": c(RequirementGap, RequirementGap.run_id == run.id, RequirementGap.status == "OPEN"),
                   "sources": c(Dataset, Dataset.run_id == run.id, Dataset.role == "SOURCE"),
                   "targets": c(Dataset, Dataset.run_id == run.id, Dataset.role == "TARGET"),
                   "mappings": c(ColumnMapping, ColumnMapping.run_id == run.id),
                   "test_cases": c(TestCase, TestCase.run_id == run.id),
                   "scenarios": c(SyntheticScenario, SyntheticScenario.run_id == run.id),
                   "executions": c(Execution, Execution.run_id == run.id),
                   "reports": c(Report, Report.run_id == run.id)},
        "transformation_version": (lambda v: v and {"id": v.id, "version": v.version, "mode": v.mode, "origin": v.origin})(
            approval_svc.current_version(db, run.id)),
    })


class SettingsBody(BaseModel):
    settings: dict[str, Any]


@router.put("/runs/{run_id}/settings")
def update_settings(run_id: str, body: SettingsBody, db: Session = Depends(get_db), p: Principal = Depends(require("analyst"))):
    run = get_run(db, run_id, p)
    allowed = set(run_svc.DEFAULT_SETTINGS)
    bad = set(body.settings) - allowed
    if bad:
        raise HTTPException(400, f"Unknown settings: {', '.join(sorted(bad))}")
    run.settings = {**(run.settings or {}), **body.settings}
    audit(db, p.user, "SETTINGS_UPDATED", run.id, "QA_RUN", run.id, settings=body.settings)
    return run.settings


# ---------------------------------------------------------------- jira & requirements
@router.post("/runs/{run_id}/jira/retrieve")
def jira_retrieve(run_id: str, db: Session = Depends(get_db), p: Principal = Depends(require("analyst"))):
    run = get_run(db, run_id, p)
    try:
        issue = run_svc.retrieve_jira(db, run, p.user)
    except run_svc.WorkflowError as e:
        db.commit()  # persist failure state + error detail
        raise HTTPException(e.status, str(e)) from e
    return row(issue)


@router.get("/runs/{run_id}/jira")
def jira_get(run_id: str, db: Session = Depends(get_db), p: Principal = Depends(require("viewer"))):
    get_run(db, run_id, p)
    i = db.scalars(select(JiraIssue).where(JiraIssue.run_id == run_id)).first()
    return row(i) if i else None


@router.get("/runs/{run_id}/requirements")
def requirements(run_id: str, db: Session = Depends(get_db), p: Principal = Depends(require("viewer"))):
    get_run(db, run_id, p)
    i = db.scalars(select(JiraIssue).where(JiraIssue.run_id == run_id)).first()
    return {"requirements": [row(r) for r in db.scalars(select(Requirement).where(Requirement.run_id == run_id).order_by(Requirement.id))],
            "gaps": [row(g) for g in db.scalars(select(RequirementGap).where(RequirementGap.run_id == run_id).order_by(RequirementGap.id))],
            "warnings": i.warnings if i else [], "llm_calls": [row(c) for c in db.scalars(select(LLMCall).where(LLMCall.run_id == run_id))]}


class ReqPatch(BaseModel):
    status: Literal["ACCEPTED", "REJECTED", "PROPOSED"] | None = None
    description: str | None = Field(default=None, max_length=2000)
    spec: dict | None = None


@router.patch("/runs/{run_id}/requirements/{req_id}")
def patch_requirement(run_id: str, req_id: str, body: ReqPatch, db: Session = Depends(get_db), p: Principal = Depends(require("analyst"))):
    get_run(db, run_id, p)
    r = db.get(Requirement, {"id": req_id, "run_id": run_id})
    if not r:
        raise HTTPException(404, "Requirement not found")
    changed = {}
    if body.status:
        changed["status"] = (r.status, body.status)
        r.status = body.status
    if body.description is not None and body.description != r.description:
        changed["description"] = (r.description, body.description)
        r.description, r.provenance = body.description, "USER_CONFIRMED"
    if body.spec is not None and body.spec != r.spec:
        changed["spec"] = (r.spec, body.spec)
        r.spec, r.provenance = body.spec, "USER_CONFIRMED"
    if body.status == "ACCEPTED" and r.provenance == "AI_INFERRED":
        r.provenance = "USER_CONFIRMED"
    r.version += 1
    audit(db, p.user, "REQUIREMENT_UPDATED", run_id, "REQUIREMENT", req_id, changes=changed)
    return row(r)


class Clarification(BaseModel):
    gap_id: str
    clarification: str = Field(default="", max_length=4000)
    status: Literal["OPEN", "CLARIFIED", "ACCEPTED_LIMITATION", "BLOCKED"]


@router.post("/runs/{run_id}/requirements/clarifications")
def clarify(run_id: str, body: Clarification, db: Session = Depends(get_db), p: Principal = Depends(require("analyst"))):
    run = get_run(db, run_id, p)
    g = db.get(RequirementGap, {"id": body.gap_id, "run_id": run_id})
    if not g:
        raise HTTPException(404, "Gap not found")
    if body.status in ("CLARIFIED", "ACCEPTED_LIMITATION") and not body.clarification.strip():
        raise HTTPException(400, "A clarification or limitation note is required")
    g.clarification, g.status, g.updated_by = body.clarification, body.status, p.user
    audit(db, p.user, "GAP_UPDATED", run_id, "REQUIREMENT_GAP", g.id, status=body.status, clarification=body.clarification)
    db.flush()
    run_svc._gap_state(db, run, p.user)
    return row(g)


# ---------------------------------------------------------------- datasets
def _upload(run_id, role, file: UploadFile, label, alias, delimiter, sheet_name, db, p):
    run = get_run(db, run_id, p)
    s = get_settings()
    with tempfile.NamedTemporaryFile(delete=False, dir=s.storage_dir) as tmp:
        size = 0
        while chunk := file.file.read(1 << 20):
            size += len(chunk)
            if size > s.max_upload_bytes:
                Path(tmp.name).unlink(missing_ok=True)
                raise HTTPException(413, "File exceeds the configured upload limit")
            tmp.write(chunk)
    opts = {k: v for k, v in {"delimiter": delimiter, "sheet_name": sheet_name}.items() if v}
    try:
        ds = run_svc.upload_dataset(db, run, Path(tmp.name), file.filename or "upload", role, p.user, label, alias, opts)
    except run_svc.WorkflowError as e:
        Path(tmp.name).unlink(missing_ok=True)
        db.commit()
        raise HTTPException(e.status, str(e)) from e
    return row(ds)


@router.post("/runs/{run_id}/datasets/sources")
def upload_source(run_id: str, file: UploadFile = File(...), label: str = Form(""), alias: str = Form(""),
                  delimiter: str = Form(""), sheet_name: str = Form(""), db: Session = Depends(get_db),
                  p: Principal = Depends(require("analyst"))):
    return _upload(run_id, "SOURCE", file, label, alias, delimiter, sheet_name, db, p)


@router.post("/runs/{run_id}/datasets/target")
def upload_target(run_id: str, file: UploadFile = File(...), label: str = Form(""), delimiter: str = Form(""),
                  sheet_name: str = Form(""), db: Session = Depends(get_db), p: Principal = Depends(require("analyst"))):
    return _upload(run_id, "TARGET", file, label, "", delimiter, sheet_name, db, p)


@router.get("/runs/{run_id}/datasets")
def datasets(run_id: str, track: str = "", db: Session = Depends(get_db), p: Principal = Depends(require("viewer"))):
    get_run(db, run_id, p)
    stmt = select(Dataset).where(Dataset.run_id == run_id).order_by(Dataset.created_at)
    if track:
        stmt = stmt.where(Dataset.track == track)
    return [row(d) for d in db.scalars(stmt)]


class DatasetPatch(BaseModel):
    feeds_target: bool | None = None
    label: str | None = Field(default=None, max_length=200)


@router.patch("/runs/{run_id}/datasets/{ds_id}")
def patch_dataset(run_id: str, ds_id: str, body: DatasetPatch, db: Session = Depends(get_db), p: Principal = Depends(require("analyst"))):
    get_run(db, run_id, p)
    d = db.get(Dataset, ds_id)
    if not d or d.run_id != run_id or d.role != "SOURCE":
        raise HTTPException(404, "Source dataset not found")
    if body.feeds_target is not None:
        d.feeds_target = body.feeds_target
    if body.label is not None:
        d.label = body.label
    audit(db, p.user, "DATASET_UPDATED", run_id, "DATASET", ds_id, feeds_target=d.feeds_target, label=d.label)
    return row(d)


@router.delete("/runs/{run_id}/datasets/{ds_id}")
def delete_dataset(run_id: str, ds_id: str, db: Session = Depends(get_db), p: Principal = Depends(require("analyst"))):
    get_run(db, run_id, p)
    d = db.get(Dataset, ds_id)
    if not d or d.run_id != run_id or d.role not in ("SOURCE", "TARGET"):
        raise HTTPException(404, "Dataset not found")
    db.delete(d)
    audit(db, p.user, "DATASET_REMOVED", run_id, "DATASET", ds_id, role=d.role, fingerprint=d.fingerprint)
    return {"deleted": ds_id}


@router.get("/runs/{run_id}/datasets/{ds_id}/preview")
def dataset_preview(run_id: str, ds_id: str, limit: int = Query(50, le=500), db: Session = Depends(get_db),
                    p: Principal = Depends(require("viewer"))):
    get_run(db, run_id, p)
    d = db.get(Dataset, ds_id)
    if not d or d.run_id != run_id:
        raise HTTPException(404, "Dataset not found")
    return preview_rows(Path(d.parquet_path), limit)


@router.get("/runs/{run_id}/datasets/{ds_id}/download")
def dataset_download(run_id: str, ds_id: str, db: Session = Depends(get_db), p: Principal = Depends(require("viewer"))):
    get_run(db, run_id, p)
    d = db.get(Dataset, ds_id)
    if not d or d.run_id != run_id or d.role in ("SOURCE", "TARGET"):
        raise HTTPException(404, "Only generated datasets can be downloaded")
    with duckdb.connect(":memory:") as c:
        cur = c.execute("SELECT * FROM read_parquet(?)", [d.parquet_path])
        names = [x[0] for x in cur.description]
        rows = [dict(zip(names, r)) for r in cur.fetchall()]
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=names)
    w.writeheader()
    for r in mask_records(rows):
        w.writerow(r)
    name = f"{d.track.lower()}_{d.scenario_id or ''}_{d.role.lower()}_{d.alias}.csv".replace("__", "_")
    audit(db, p.user, "DATASET_DOWNLOADED", run_id, "DATASET", ds_id)
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv",
                             headers={"Content-Disposition": f'attachment; filename="{name}"'})


@router.post("/runs/{run_id}/profile")
def profile(run_id: str, db: Session = Depends(get_db), p: Principal = Depends(require("analyst"))):
    run = get_run(db, run_id, p)
    try:
        profs = run_svc.profile_all(db, run, p.user)
    except run_svc.WorkflowError as e:
        db.commit()
        raise HTTPException(e.status, str(e)) from e
    return [row(x) for x in profs]


@router.get("/runs/{run_id}/profiles")
def profiles(run_id: str, db: Session = Depends(get_db), p: Principal = Depends(require("viewer"))):
    get_run(db, run_id, p)
    out = []
    for d in db.scalars(select(Dataset).where(Dataset.run_id == run_id, Dataset.role.in_(["SOURCE", "TARGET"]))):
        pr = db.scalars(select(DatasetProfile).where(DatasetProfile.dataset_id == d.id).order_by(DatasetProfile.created_at.desc())).first()
        out.append({"dataset": row(d), "profile": pr.profile if pr else None})
    return out


# ---------------------------------------------------------------- mappings
@router.post("/runs/{run_id}/mappings/generate")
def mappings_generate(run_id: str, db: Session = Depends(get_db), p: Principal = Depends(require("analyst"))):
    run = get_run(db, run_id, p)
    return [row(m) for m in wf(run_svc.generate_mappings, db, run, p.user)]


@router.get("/runs/{run_id}/mappings")
def mappings(run_id: str, db: Session = Depends(get_db), p: Principal = Depends(require("viewer"))):
    get_run(db, run_id, p)
    return [row(m) for m in db.scalars(select(ColumnMapping).where(ColumnMapping.run_id == run_id).order_by(ColumnMapping.id))]


class MappingPatch(BaseModel):
    status: Literal["ACCEPTED", "REJECTED", "PROPOSED"] | None = None
    expression: str | None = Field(default=None, max_length=4000)
    target_column: str | None = None
    mapping_type: str | None = None
    source_columns: list[str] | None = None
    is_key: bool | None = None
    notes: str | None = Field(default=None, max_length=4000)


@router.patch("/runs/{run_id}/mappings/{mapping_id}")
def patch_mapping(run_id: str, mapping_id: str, body: MappingPatch, db: Session = Depends(get_db), p: Principal = Depends(require("analyst"))):
    get_run(db, run_id, p)
    m = db.get(ColumnMapping, {"id": mapping_id, "run_id": run_id})
    if not m:
        raise HTTPException(404, "Mapping not found")
    changes = {}
    for f in ("status", "expression", "target_column", "mapping_type", "source_columns", "is_key", "notes"):
        v = getattr(body, f)
        if v is not None and v != getattr(m, f):
            changes[f] = [getattr(m, f), v]
            setattr(m, f, v)
    if set(changes) - {"status", "notes"}:
        m.provenance = "USER_CONFIRMED"
    if body.status == "ACCEPTED" and m.provenance == "AI_INFERRED":
        m.provenance = "USER_CONFIRMED"
    if changes:
        m.version += 1
    audit(db, p.user, "MAPPING_UPDATED", run_id, "MAPPING", mapping_id, changes=changes)
    return row(m)


class MappingCreate(BaseModel):
    target_column: str
    expression: str = Field(max_length=4000)
    mapping_type: str = "DERIVED"
    source_columns: list[str] = Field(default_factory=list)
    notes: str = ""


@router.post("/runs/{run_id}/mappings")
def create_mapping(run_id: str, body: MappingCreate, db: Session = Depends(get_db), p: Principal = Depends(require("analyst"))):
    get_run(db, run_id, p)
    n = db.scalar(select(func.count()).select_from(ColumnMapping).where(ColumnMapping.run_id == run_id)) + 1
    mid = f"MAP-{n:03d}"
    while db.get(ColumnMapping, {"id": mid, "run_id": run_id}):
        n += 1
        mid = f"MAP-{n:03d}"
    m = ColumnMapping(id=mid, run_id=run_id, target_column=body.target_column, expression=body.expression, mapping_type=body.mapping_type,
                      source_columns=body.source_columns, provenance="USER_CONFIRMED", confidence="HIGH", status="ACCEPTED",
                      evidence=f"Created by {p.user}", notes=body.notes)
    db.add(m)
    audit(db, p.user, "MAPPING_CREATED", run_id, "MAPPING", mid, expression=body.expression)
    return row(m)


# ---------------------------------------------------------------- transformation studio
class GenBody(BaseModel):
    mode: Literal["SQL", "PYTHON", "VISUAL"]
    instruction: str = Field(default="", max_length=4000)


class SaveBody(BaseModel):
    mode: Literal["SQL", "PYTHON", "VISUAL"]
    code: str = Field(default="", max_length=200_000)
    visual_spec: dict | None = None
    comment: str = Field(default="", max_length=2000)


class CodeBody(BaseModel):
    mode: Literal["SQL", "PYTHON", "VISUAL"]
    code: str = Field(default="", max_length=200_000)
    visual_spec: dict | None = None
    error: str = Field(default="", max_length=8000)


def _tv(db, run_id, version_id) -> TransformationVersion:
    v = db.get(TransformationVersion, version_id)
    if not v or v.transformation.run_id != run_id:
        raise HTTPException(404, "Transformation version not found")
    return v


@router.get("/runs/{run_id}/transformations")
def transformations(run_id: str, db: Session = Depends(get_db), p: Principal = Depends(require("viewer"))):
    get_run(db, run_id, p)
    t = approval_svc.primary_transformation(db, run_id)
    st = approval_svc.approval_state(db, run_id)["TRANSFORMATION"]
    if not t:
        return {"transformation": None, "versions": [], "current": None, "approval": st, "executions": []}
    vers = [row(v) for v in t.versions]
    execs = [row(e) for e in db.scalars(select(TransformationExecution).where(TransformationExecution.run_id == run_id)
                                        .order_by(TransformationExecution.created_at.desc()).limit(20))]
    return {"transformation": row(t), "versions": vers, "current": next((v for v in vers if v["version"] == t.current_version), None),
            "approval": st, "executions": execs}


@router.get("/runs/{run_id}/transformations/schema")
def transformation_schema(run_id: str, db: Session = Depends(get_db), p: Principal = Depends(require("viewer"))):
    get_run(db, run_id, p)
    tgt = db.scalars(select(Dataset).where(Dataset.run_id == run_id, Dataset.role == "TARGET")).first()
    return {"sources": {a: d.columns for a, d in studio.actual_inputs(db, run_id).items()},
            "target": tgt.columns if tgt else [],
            "python_contract": "def transform(sources: dict[str, pandas.DataFrame]) -> pandas.DataFrame"}


@router.post("/runs/{run_id}/transformations/generate")
def transformation_generate(run_id: str, body: GenBody, db: Session = Depends(get_db), p: Principal = Depends(require("analyst"))):
    run = get_run(db, run_id, p)
    v = wf(studio.ai_generate, db, run, body.mode, body.instruction, p.user)
    from ..core.common import set_state
    set_state(db, run, "TRANSFORMATION_DESIGNED", p.user)
    return row(v)


@router.post("/runs/{run_id}/transformations")
def transformation_save(run_id: str, body: SaveBody, db: Session = Depends(get_db), p: Principal = Depends(require("analyst"))):
    run = get_run(db, run_id, p)
    v = wf(studio.save_version, db, run.id, body.mode, body.code, body.visual_spec, "USER_WRITTEN", p.user, body.comment)
    from ..core.common import set_state
    set_state(db, run, "TRANSFORMATION_DESIGNED", p.user)
    return row(v)


@router.post("/runs/{run_id}/transformations/validate")
def transformation_validate_draft(run_id: str, body: CodeBody, db: Session = Depends(get_db), p: Principal = Depends(require("viewer"))):
    get_run(db, run_id, p)
    defn = TransformationDefinition("draft", run_id, body.mode, {a: Path(d.parquet_path) for a, d in studio.actual_inputs(db, run_id).items()},
                                    code=body.code, visual_spec=body.visual_spec)
    return validate_definition(defn)


@router.post("/runs/{run_id}/transformations/compile-visual")
def compile_visual_ep(run_id: str, body: CodeBody, db: Session = Depends(get_db), p: Principal = Depends(require("viewer"))):
    get_run(db, run_id, p)
    try:
        return {"sql": compile_visual(body.visual_spec or {})}
    except (VisualSpecError, KeyError, TypeError) as e:
        raise HTTPException(400, f"Visual definition error: {e}") from e


@router.post("/runs/{run_id}/transformations/{version_id}/validate")
def transformation_validate(run_id: str, version_id: str, db: Session = Depends(get_db), p: Principal = Depends(require("viewer"))):
    get_run(db, run_id, p)
    v = _tv(db, run_id, version_id)
    return validate_definition(studio.definition(v, run_id, studio.actual_inputs(db, run_id)))


class PreviewBody(BaseModel):
    limit: int = Field(default=100, ge=1, le=5000)


@router.post("/runs/{run_id}/transformations/{version_id}/preview")
def transformation_preview(run_id: str, version_id: str, body: PreviewBody, db: Session = Depends(get_db),
                           p: Principal = Depends(require("analyst"))):
    run = get_run(db, run_id, p)
    v = _tv(db, run_id, version_id)
    if not v.validation.get("valid"):
        raise HTTPException(400, "This version failed safety/syntax validation and cannot be executed")
    return studio.preview(db, run, v, p.user, body.limit)


class DecisionBody(BaseModel):
    decision: Literal["APPROVED", "REJECTED"]
    reason: str = Field(default="", max_length=4000)


@router.post("/runs/{run_id}/transformations/{version_id}/approve")
def transformation_approve(run_id: str, version_id: str, body: DecisionBody, db: Session = Depends(get_db),
                           p: Principal = Depends(require("approver"))):
    get_run(db, run_id, p)
    v = _tv(db, run_id, version_id)
    cur = approval_svc.current_version(db, run_id)
    if cur.id != v.id:
        raise HTTPException(409, "Only the current version can be approved; restore it first")
    if body.decision == "APPROVED" and not v.validation.get("valid"):
        raise HTTPException(400, "A version that fails validation cannot be approved")
    a = wf(approval_svc.decide, db, run_id, "TRANSFORMATION", body.decision, p.user, body.reason)
    return row(a)


class RestoreBody(BaseModel):
    version: int


@router.post("/runs/{run_id}/transformations/restore")
def transformation_restore(run_id: str, body: RestoreBody, db: Session = Depends(get_db), p: Principal = Depends(require("analyst"))):
    get_run(db, run_id, p)
    return row(wf(studio.restore_version, db, run_id, body.version, p.user))


@router.post("/runs/{run_id}/transformations/explain")
def transformation_explain(run_id: str, body: CodeBody, db: Session = Depends(get_db), p: Principal = Depends(require("viewer"))):
    run = get_run(db, run_id, p)
    code = body.code
    if body.mode == "VISUAL":
        code = wf(compile_visual, body.visual_spec or {})
    return {"explanation": wf(studio.ai_explain, db, run, "SQL" if body.mode == "VISUAL" else body.mode, code)}


@router.post("/runs/{run_id}/transformations/fix")
def transformation_fix(run_id: str, body: CodeBody, db: Session = Depends(get_db), p: Principal = Depends(require("analyst"))):
    run = get_run(db, run_id, p)
    return wf(studio.ai_fix, db, run, body.mode, body.code, body.error)


# ---------------------------------------------------------------- synthetic & tests
class SynthBody(BaseModel):
    scenarios: list[str] | None = None
    records: int | None = Field(default=None, ge=3, le=500)
    seed: int | None = None


@router.post("/runs/{run_id}/synthetic-data/generate")
def synthetic_generate(run_id: str, body: SynthBody, db: Session = Depends(get_db), p: Principal = Depends(require("analyst"))):
    run = get_run(db, run_id, p)
    return [row(s) for s in wf(synthetic.generate, db, run, p.user, body.scenarios, body.records, body.seed)]


@router.get("/runs/{run_id}/synthetic-data")
def synthetic_list(run_id: str, db: Session = Depends(get_db), p: Principal = Depends(require("viewer"))):
    get_run(db, run_id, p)
    out = []
    ds = {d.id: d for d in db.scalars(select(Dataset).where(Dataset.run_id == run_id, Dataset.track == "SYNTHETIC"))}
    tcs = list(db.scalars(select(TestCase).where(TestCase.run_id == run_id, TestCase.track == "SYNTHETIC")))
    for s in db.scalars(select(SyntheticScenario).where(SyntheticScenario.run_id == run_id).order_by(SyntheticScenario.id)):
        out.append(row(s, {"sources": [row(ds[i]) for i in s.source_dataset_ids.values() if i in ds],
                           "expected": row(ds[s.expected_dataset_id]) if s.expected_dataset_id in ds else None,
                           "test_case_ids": [t.id for t in tcs if t.scenario_id == s.id]}))
    return out


@router.post("/runs/{run_id}/test-cases/generate")
def tests_generate(run_id: str, db: Session = Depends(get_db), p: Principal = Depends(require("analyst"))):
    run = get_run(db, run_id, p)
    return [row(t) for t in wf(testgen.generate_actual_tests, db, run, p.user)]


@router.get("/runs/{run_id}/test-cases")
def tests_list(run_id: str, db: Session = Depends(get_db), p: Principal = Depends(require("viewer"))):
    get_run(db, run_id, p)
    tcs = [row(t) for t in db.scalars(select(TestCase).where(TestCase.run_id == run_id).order_by(TestCase.track, TestCase.id))]
    reqs = list(db.scalars(select(Requirement).where(Requirement.run_id == run_id, Requirement.kind == "ACCEPTANCE_CRITERION")))
    covered = {a for t in tcs for a in t["ac_ids"]}
    no_req = [t["id"] for t in tcs if not t["requirement_ids"] and not t["rationale"]]
    return {"test_cases": tcs, "coverage": {"acceptance_criteria": [{"ac_id": r.ac_id, "text": r.description, "covered": r.ac_id in covered}
                                                                    for r in reqs],
                                            "tests_without_requirement": no_req}}


class TcPatch(BaseModel):
    status: Literal["ACCEPTED", "REJECTED", "PROPOSED"] | None = None
    tolerance: float | None = Field(default=None, ge=0)
    trim: bool | None = None
    case_insensitive: bool | None = None
    expected_result: str | None = Field(default=None, max_length=2000)
    priority: Literal["P1", "P2", "P3"] | None = None
    excluded: bool | None = None


@router.patch("/runs/{run_id}/test-cases/{tc_id}")
def tests_patch(run_id: str, tc_id: str, body: TcPatch, db: Session = Depends(get_db), p: Principal = Depends(require("analyst"))):
    get_run(db, run_id, p)
    t = db.get(TestCase, {"id": tc_id, "run_id": run_id})
    if not t:
        raise HTTPException(404, "Test case not found")
    v = dict(t.validation)
    for k in ("tolerance", "trim", "case_insensitive"):
        if getattr(body, k) is not None:
            v[k] = getattr(body, k)
    t.validation = v
    if body.status:
        t.status = body.status
    if body.expected_result is not None:
        t.expected_result = body.expected_result
    if body.priority:
        t.priority = body.priority
    if body.excluded is not None and t.state != "BLOCKED":
        t.state = "EXCLUDED" if body.excluded else "READY"
    t.version += 1
    audit(db, p.user, "TEST_CASE_UPDATED", run_id, "TEST_CASE", tc_id, body=body.model_dump(exclude_none=True))
    return row(t)


# ---------------------------------------------------------------- approvals
class ApprovalBody(BaseModel):
    artifact_type: str
    decision: Literal["APPROVED", "REJECTED"]
    reason: str = Field(default="", max_length=4000)
    expected_hash: str | None = None


@router.get("/runs/{run_id}/approvals")
def approvals_get(run_id: str, db: Session = Depends(get_db), p: Principal = Depends(require("viewer"))):
    get_run(db, run_id, p)
    from ..models import Approval
    return {"state": approval_svc.approval_state(db, run_id), "required": approval_svc.REQUIRED,
            "history": [row(a) for a in db.scalars(select(Approval).where(Approval.run_id == run_id).order_by(Approval.created_at.desc()))]}


@router.post("/runs/{run_id}/approvals")
def approvals_post(run_id: str, body: ApprovalBody, db: Session = Depends(get_db), p: Principal = Depends(require("approver"))):
    run = get_run(db, run_id, p)
    if body.artifact_type == "TRANSFORMATION":
        v = approval_svc.current_version(db, run_id)
        if body.decision == "APPROVED" and v and not v.validation.get("valid"):
            raise HTTPException(400, "A version that fails validation cannot be approved")
    a = wf(approval_svc.decide, db, run_id, body.artifact_type, body.decision, p.user, body.reason, body.expected_hash)
    from ..core.common import set_state
    if body.decision == "REJECTED":
        set_state(db, run, "APPROVAL_REJECTED", p.user)
    else:
        st = approval_svc.approval_state(db, run_id)
        if all(st[k]["status"] == "APPROVED" for k in approval_svc.REQUIRED["ACTUAL"]):
            set_state(db, run, "APPROVED", p.user)
            from ..core.common import clear_exception
            clear_exception(db, run, {"APPROVAL_REJECTED"})
        else:
            set_state(db, run, "AWAITING_APPROVAL", p.user)
    return row(a)


# ---------------------------------------------------------------- execution
class ExecBody(BaseModel):
    force: bool = False


def _start(run_id, track, body, db, p):
    run = get_run(db, run_id, p)
    ex, reused = wf(exec_svc.start, db, run, track, p.user, body.force)
    db.commit()
    if not reused:
        exec_svc.launch(ex.id)
    return row(ex, {"reused": reused})


@router.post("/runs/{run_id}/execute/actual")
def execute_actual(run_id: str, body: ExecBody = ExecBody(), db: Session = Depends(get_db), p: Principal = Depends(require("analyst"))):
    return _start(run_id, "ACTUAL", body, db, p)


@router.post("/runs/{run_id}/execute/synthetic")
def execute_synthetic(run_id: str, body: ExecBody = ExecBody(), db: Session = Depends(get_db), p: Principal = Depends(require("analyst"))):
    return _start(run_id, "SYNTHETIC", body, db, p)


@router.get("/runs/{run_id}/executions")
def executions(run_id: str, db: Session = Depends(get_db), p: Principal = Depends(require("viewer"))):
    get_run(db, run_id, p)
    out = []
    for e in db.scalars(select(Execution).where(Execution.run_id == run_id).order_by(Execution.created_at.desc())):
        n = db.scalar(select(func.count()).select_from(TestResult).where(TestResult.execution_id == e.id))
        total = db.scalar(select(func.count()).select_from(TestCase).where(TestCase.run_id == run_id, TestCase.track == e.track))
        out.append(row(e, {"progress": {"done": n, "total": total}}))
    return out


def _exec(db, execution_id, p) -> Execution:
    e = db.get(Execution, execution_id)
    if not e:
        raise HTTPException(404, "Execution not found")
    get_run(db, e.run_id, p)
    return e


@router.get("/executions/{execution_id}")
def execution_detail(execution_id: str, db: Session = Depends(get_db), p: Principal = Depends(require("viewer"))):
    e = _exec(db, execution_id, p)
    tcs = {t.id: t for t in db.scalars(select(TestCase).where(TestCase.run_id == e.run_id, TestCase.track == e.track))}
    res = list(db.scalars(select(TestResult).where(TestResult.execution_id == e.id)))
    ev = {x.id: x for x in db.scalars(select(Evidence).where(Evidence.execution_id == e.id))}
    fa = {f.test_case_id: f.payload | {"provider": f.provider} for f in db.scalars(select(FailureAnalysis).where(FailureAnalysis.execution_id == e.id))}
    texs = [row(t) for t in db.scalars(select(TransformationExecution).where(TransformationExecution.run_id == e.run_id)
                                       .where(TransformationExecution.purpose.in_(["ACTUAL_EXPECTED", "SYNTHETIC_RERUN"])))
            if t.output_dataset_id in (e.expected_dataset_ids or {}).values()]
    results = []
    for r in sorted(res, key=lambda r: r.test_case_id):
        t = tcs.get(r.test_case_id)
        results.append(row(r, {"title": t.title if t else "", "category": t.category if t else "", "scenario_id": t.scenario_id if t else None,
                               "ac_ids": t.ac_ids if t else [], "requirement_ids": t.requirement_ids if t else [],
                               "evidence": (lambda x: x and {"id": x.id, "kind": x.kind, "summary": x.summary, "sample": x.sample,
                                                              "has_file": bool(x.path)})(ev.get(r.evidence_id)),
                               "analysis": fa.get(r.test_case_id)}))
    from ..models import ErrorDetail
    errs = [row(x) for x in db.scalars(select(ErrorDetail).where(ErrorDetail.execution_id == e.id))]
    return row(e, {"results": results, "transformation_executions": texs, "errors": errs,
                   "progress": {"done": len(res), "total": len(tcs)}})


@router.post("/executions/{execution_id}/retry")
def execution_retry(execution_id: str, db: Session = Depends(get_db), p: Principal = Depends(require("analyst"))):
    e = _exec(db, execution_id, p)
    wf(exec_svc.retry, db, e, p.user)
    db.commit()
    exec_svc.launch(e.id)
    return row(e)


@router.post("/executions/{execution_id}/cancel")
def execution_cancel(execution_id: str, db: Session = Depends(get_db), p: Principal = Depends(require("analyst"))):
    e = _exec(db, execution_id, p)
    wf(exec_svc.cancel, db, e, p.user)
    if e.status == "QUEUED":
        e.status = "CANCELLED"
    return row(e)


@router.post("/executions/{execution_id}/analyze")
def execution_analyze(execution_id: str, db: Session = Depends(get_db), p: Principal = Depends(require("analyst"))):
    e = _exec(db, execution_id, p)
    if e.status in ("QUEUED", "RUNNING"):
        raise HTTPException(409, "Failure analysis runs only after deterministic execution finishes")
    return {"analysed": exec_svc.analyze_failures(db, e)}


@router.get("/executions/{execution_id}/evidence/{evidence_id}")
def evidence_download(execution_id: str, evidence_id: str, db: Session = Depends(get_db), p: Principal = Depends(require("viewer"))):
    e = _exec(db, execution_id, p)
    ev = db.get(Evidence, evidence_id)
    if not ev or ev.execution_id != e.id or not ev.path:
        raise HTTPException(404, "Evidence file not found")
    return FileResponse(ev.path, media_type="text/csv", filename=f"{e.id}_{ev.test_case_id}_evidence.csv")


@router.get("/compare")
def compare(a: str, b: str, db: Session = Depends(get_db), p: Principal = Depends(require("viewer"))):
    ea, eb = _exec(db, a, p), _exec(db, b, p)
    ra = {r.test_case_id: r.status for r in db.scalars(select(TestResult).where(TestResult.execution_id == ea.id))}
    rb = {r.test_case_id: r.status for r in db.scalars(select(TestResult).where(TestResult.execution_id == eb.id))}
    rows = [{"test_case_id": k, "a": ra.get(k), "b": rb.get(k), "changed": ra.get(k) != rb.get(k)} for k in sorted(set(ra) | set(rb))]
    return {"a": row(ea), "b": row(eb), "rows": rows, "changed": sum(r["changed"] for r in rows)}


# ---------------------------------------------------------------- reports, traceability, audit
class ReportBody(BaseModel):
    execution_ids: list[str] | None = None


@router.post("/runs/{run_id}/reports")
def report_generate(run_id: str, body: ReportBody = ReportBody(), db: Session = Depends(get_db), p: Principal = Depends(require("analyst"))):
    run = get_run(db, run_id, p)
    return row(wf(report_svc.generate, db, run, p.user, body.execution_ids), {"files": None})


@router.get("/runs/{run_id}/reports")
def reports_list(run_id: str, db: Session = Depends(get_db), p: Principal = Depends(require("viewer"))):
    get_run(db, run_id, p)
    return [row(r, {"files": sorted((r.files or {}).keys())}) for r in
            db.scalars(select(Report).where(Report.run_id == run_id).order_by(Report.created_at.desc()))]


@router.get("/runs/{run_id}/report-data")
def report_data(run_id: str, db: Session = Depends(get_db), p: Principal = Depends(require("viewer"))):
    run = get_run(db, run_id, p)
    return report_svc.collect(db, run)


@router.get("/reports/{report_id}/download/{fmt}")
def report_download(report_id: str, fmt: Literal["json", "html", "xlsx", "powerbi"], inline: bool = False, db: Session = Depends(get_db),
                    p: Principal = Depends(require("viewer"))):
    r = db.get(Report, report_id)
    if not r:
        raise HTTPException(404, "Report not found")
    get_run(db, r.run_id, p)
    path = (r.files or {}).get(fmt)
    if not path or not Path(path).exists():
        raise HTTPException(404, "Report file not available")
    media = {"json": "application/json", "html": "text/html", "powerbi": "application/zip",
             "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"}[fmt]
    name = {"json": "report.json", "html": "report.html", "xlsx": "report.xlsx", "powerbi": "powerbi_tables.zip"}[fmt]
    if inline and fmt == "html":
        return FileResponse(path, media_type=media)
    return FileResponse(path, media_type=media, filename=f"{r.run_id}_{name}")


@router.get("/runs/{run_id}/traceability")
def traceability(run_id: str, db: Session = Depends(get_db), p: Principal = Depends(require("viewer"))):
    run = get_run(db, run_id, p)
    ids = [e.id for e in db.scalars(select(Execution).where(Execution.run_id == run_id))]
    return report_svc.build_traceability(db, run, ids)


@router.get("/runs/{run_id}/audit")
def audit_log(run_id: str, db: Session = Depends(get_db), p: Principal = Depends(require("viewer"))):
    get_run(db, run_id, p)
    return [row(a) for a in db.scalars(select(AuditLog).where(AuditLog.run_id == run_id).order_by(AuditLog.id.desc()).limit(500))]
