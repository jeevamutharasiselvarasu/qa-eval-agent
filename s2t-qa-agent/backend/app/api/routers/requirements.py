"""Jira retrieval, requirements, open questions and the target specification."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select

from ...core.common import audit
from ...models import (
    ColumnMapping,
    JiraIssue,
    LLMCall,
    Requirement,
    RequirementGap,
)
from ...services import runs as run_svc
from ...services import target_spec
from ..deps import DB, Analyst, Viewer, after_change, get_run, row, wf

router = APIRouter()


@router.post("/runs/{run_id}/jira/retrieve")
def jira_retrieve(run_id: str, db: DB, p: Analyst):
    run = get_run(db, run_id, p)
    try:
        issue = run_svc.retrieve_jira(db, run, p.user)
    except run_svc.WorkflowError as e:
        db.commit()  # persist failure state + error detail
        raise HTTPException(e.status, str(e)) from e
    if db.scalars(select(ColumnMapping).where(ColumnMapping.run_id == run_id)).first():  # existing run: re-derive
        after_change(db, run, p.user)
    return row(issue)


@router.get("/runs/{run_id}/jira")
def jira_get(run_id: str, db: DB, p: Viewer):
    get_run(db, run_id, p)
    i = db.scalars(select(JiraIssue).where(JiraIssue.run_id == run_id)).first()
    return row(i) if i else None


@router.get("/runs/{run_id}/requirements")
def requirements(run_id: str, db: DB, p: Viewer):
    get_run(db, run_id, p)
    i = db.scalars(select(JiraIssue).where(JiraIssue.run_id == run_id)).first()
    return {
        "requirements": [row(r) for r in db.scalars(select(Requirement).where(Requirement.run_id == run_id).order_by(Requirement.id))],
        "gaps": [row(g) for g in db.scalars(select(RequirementGap).where(RequirementGap.run_id == run_id).order_by(RequirementGap.id))],
        "warnings": i.warnings if i else [],
        "llm_calls": [row(c) for c in db.scalars(select(LLMCall).where(LLMCall.run_id == run_id))],
    }


class ReqPatch(BaseModel):
    status: Literal["ACCEPTED", "REJECTED", "PROPOSED"] | None = None
    description: str | None = Field(default=None, max_length=2000)
    spec: dict | None = None


@router.patch("/runs/{run_id}/requirements/{req_id}")
def patch_requirement(run_id: str, req_id: str, body: ReqPatch, db: DB, p: Analyst):
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
def clarify(run_id: str, body: Clarification, db: DB, p: Analyst):
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
    if db.scalars(select(ColumnMapping).where(ColumnMapping.run_id == run_id)).first():
        after_change(db, run, p.user)
    return row(g)


@router.get("/runs/{run_id}/target-spec")
def target_spec_get(run_id: str, db: DB, p: Viewer):
    return target_spec.build_spec(db, get_run(db, run_id, p))


class GapResolution(BaseModel):
    status: Literal["OPEN", "CLARIFIED", "ACCEPTED_LIMITATION"] = "CLARIFIED"
    note: str = Field(default="", max_length=4000)


class ColumnRuleBody(BaseModel):
    expression: str = Field(max_length=4000)
    note: str = Field(default="", max_length=2000)
    resolve: dict[str, GapResolution] = Field(default_factory=dict)


@router.put("/runs/{run_id}/target-spec/columns/{target}")
def target_spec_column(run_id: str, target: str, body: ColumnRuleBody, db: DB, p: Analyst):
    run = get_run(db, run_id, p)
    sync = wf(
        target_spec.edit_column, db, run, target, body.expression, p.user, {k: v.model_dump() for k, v in body.resolve.items()}, body.note
    )
    return {"sync": sync, "spec": target_spec.build_spec(db, run)}


class JoinRule(BaseModel):
    type: str
    left: str
    right: str
    keys: list[str]


class FilterRule(BaseModel):
    dataset: str
    column: str
    op: str = "="
    value: str


class TableRulesBody(BaseModel):
    key: list[str] | None = None
    joins: list[JoinRule] | None = None
    filters: list[FilterRule] | None = None
    defaults: dict[str, str | int | float | None] | None = None
    resolve: dict[str, GapResolution] = Field(default_factory=dict)


@router.put("/runs/{run_id}/target-spec/table-rules")
def target_spec_table(run_id: str, body: TableRulesBody, db: DB, p: Analyst):
    run = get_run(db, run_id, p)
    data = body.model_dump()
    data["resolve"] = {k: v.model_dump() for k, v in body.resolve.items()}
    sync = wf(target_spec.edit_table, db, run, data, p.user)
    return {"sync": sync, "spec": target_spec.build_spec(db, run)}


class AnswerBody(BaseModel):
    status: Literal["OPEN", "CLARIFIED", "ACCEPTED_LIMITATION"]
    note: str = Field(default="", max_length=4000)


@router.post("/runs/{run_id}/target-spec/questions/{gap_id}")
def target_spec_answer(run_id: str, gap_id: str, body: AnswerBody, db: DB, p: Analyst):
    run = get_run(db, run_id, p)
    sync = wf(target_spec.answer_question, db, run, gap_id, body.status, body.note, p.user)
    return {"sync": sync, "spec": target_spec.build_spec(db, run)}


class SyncBody(BaseModel):
    force: bool = False


@router.post("/runs/{run_id}/target-spec/sync")
def target_spec_sync(run_id: str, db: DB, p: Analyst, body: SyncBody = SyncBody()):
    """Generate the SQL from the requirements if they are complete (force=True regenerates a hand-edited version)."""
    run = get_run(db, run_id, p)
    target_spec.refresh(db, run, p.user)
    sync = wf(target_spec.sync_sql, db, run, p.user, body.force)
    return {"sync": sync, "spec": target_spec.build_spec(db, run)}
