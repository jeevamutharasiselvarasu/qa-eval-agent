"""SQL / Python / visual transformation versions: generate, validate, preview, approve."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select

from ...models import (
    Dataset,
    TransformationExecution,
    TransformationVersion,
)
from ...services import approvals as approval_svc
from ...services import studio, target_spec, testpack
from ...services.transform.contract import TransformationDefinition
from ...services.transform.engine import validate_definition
from ...services.transform.visual import VisualSpecError, compile_visual
from ..deps import DB, Analyst, Approver, DecisionBody, Viewer, get_run, row, wf

router = APIRouter()


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
def transformations(run_id: str, db: DB, p: Viewer):
    get_run(db, run_id, p)
    t = approval_svc.primary_transformation(db, run_id)
    st = approval_svc.approval_state(db, run_id)["TRANSFORMATION"]
    if not t:
        return {"transformation": None, "versions": [], "current": None, "approval": st, "executions": []}
    vers = [row(v) for v in t.versions]
    execs = [
        row(e)
        for e in db.scalars(
            select(TransformationExecution)
            .where(TransformationExecution.run_id == run_id)
            .order_by(TransformationExecution.created_at.desc())
            .limit(20)
        )
    ]
    return {
        "transformation": row(t),
        "versions": vers,
        "current": next((v for v in vers if v["version"] == t.current_version), None),
        "approval": st,
        "executions": execs,
    }


@router.get("/runs/{run_id}/transformations/schema")
def transformation_schema(run_id: str, db: DB, p: Viewer):
    get_run(db, run_id, p)
    tgt = db.scalars(select(Dataset).where(Dataset.run_id == run_id, Dataset.role == "TARGET")).first()
    return {
        "sources": {a: d.columns for a, d in studio.actual_inputs(db, run_id).items()},
        "target": tgt.columns if tgt else [],
        "python_contract": "def transform(sources: dict[str, pandas.DataFrame]) -> pandas.DataFrame",
    }


@router.post("/runs/{run_id}/transformations/generate")
def transformation_generate(run_id: str, body: GenBody, db: DB, p: Analyst):
    run = get_run(db, run_id, p)
    if not (body.instruction.strip() and approval_svc.current_version(db, run_id)):
        spec = target_spec.build_spec(db, run)
        if not spec["ready"]:
            raise HTTPException(
                409,
                f"The requirements are not complete ({len(spec['open_items'])} open item(s)): "
                + "; ".join(f"{i['ref']}: {i['message']}" for i in spec["open_items"][:5])
                + ". Edit them on the Requirements tab first.",
            )
    v = wf(studio.ai_generate, db, run, body.mode, body.instruction, p.user)
    if not body.instruction.strip():
        v.requirements_basis = target_spec.requirements_basis(db, run_id)
    from ...core.common import set_state

    set_state(db, run, "TRANSFORMATION_DESIGNED", p.user)
    testpack.rebuild_if_built(db, run, p.user)
    return row(v)


@router.post("/runs/{run_id}/transformations")
def transformation_save(run_id: str, body: SaveBody, db: DB, p: Analyst):
    run = get_run(db, run_id, p)
    v = wf(studio.save_version, db, run.id, body.mode, body.code, body.visual_spec, "USER_WRITTEN", p.user, body.comment)
    from ...core.common import set_state

    set_state(db, run, "TRANSFORMATION_DESIGNED", p.user)
    testpack.rebuild_if_built(db, run, p.user)
    return row(v)


@router.post("/runs/{run_id}/transformations/validate")
def transformation_validate_draft(run_id: str, body: CodeBody, db: DB, p: Viewer):
    get_run(db, run_id, p)
    defn = TransformationDefinition(
        "draft",
        run_id,
        body.mode,
        {a: Path(d.parquet_path) for a, d in studio.actual_inputs(db, run_id).items()},
        code=body.code,
        visual_spec=body.visual_spec,
    )
    return validate_definition(defn)


@router.post("/runs/{run_id}/transformations/compile-visual")
def compile_visual_ep(run_id: str, body: CodeBody, db: DB, p: Viewer):
    get_run(db, run_id, p)
    try:
        return {"sql": compile_visual(body.visual_spec or {})}
    except (VisualSpecError, KeyError, TypeError) as e:
        raise HTTPException(400, f"Visual definition error: {e}") from e


@router.post("/runs/{run_id}/transformations/{version_id}/validate")
def transformation_validate(run_id: str, version_id: str, db: DB, p: Viewer):
    get_run(db, run_id, p)
    v = _tv(db, run_id, version_id)
    return validate_definition(studio.definition(v, run_id, studio.actual_inputs(db, run_id)))


class PreviewBody(BaseModel):
    limit: int = Field(default=100, ge=1, le=5000)


@router.post("/runs/{run_id}/transformations/{version_id}/preview")
def transformation_preview(run_id: str, version_id: str, body: PreviewBody, db: DB, p: Analyst):
    run = get_run(db, run_id, p)
    v = _tv(db, run_id, version_id)
    if not v.validation.get("valid"):
        raise HTTPException(400, "This version failed safety/syntax validation and cannot be executed")
    return studio.preview(db, run, v, p.user, body.limit)


@router.post("/runs/{run_id}/transformations/{version_id}/approve")
def transformation_approve(run_id: str, version_id: str, body: DecisionBody, db: DB, p: Approver):
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
def transformation_restore(run_id: str, body: RestoreBody, db: DB, p: Analyst):
    run = get_run(db, run_id, p)
    v = wf(studio.restore_version, db, run_id, body.version, p.user)
    testpack.rebuild_if_built(db, run, p.user)
    return row(v)


@router.post("/runs/{run_id}/transformations/explain")
def transformation_explain(run_id: str, body: CodeBody, db: DB, p: Viewer):
    run = get_run(db, run_id, p)
    code = body.code
    if body.mode == "VISUAL":
        code = wf(compile_visual, body.visual_spec or {})
    return {"explanation": wf(studio.ai_explain, db, run, "SQL" if body.mode == "VISUAL" else body.mode, code)}


@router.post("/runs/{run_id}/transformations/fix")
def transformation_fix(run_id: str, body: CodeBody, db: DB, p: Analyst):
    run = get_run(db, run_id, p)
    return wf(studio.ai_fix, db, run, body.mode, body.code, body.error)
