"""Test cases, selection, the test pack (approval 2), approvals and one-click validation."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select

from ...core.common import audit
from ...models import (
    Requirement,
    TestCase,
)
from ...services import approvals as approval_svc
from ...services import testgen, testpack
from ..deps import DB, Analyst, Approver, DecisionBody, ExecBody, Viewer, get_run, row, wf

router = APIRouter()


@router.post("/runs/{run_id}/test-cases/generate")
def tests_generate(run_id: str, db: DB, p: Analyst):
    run = get_run(db, run_id, p)
    out = [row(t) for t in wf(testgen.generate_test_pack, db, run, p.user)]
    db.flush()
    run.test_pack_basis = testpack.basis(db, run)
    return out


@router.get("/runs/{run_id}/test-cases")
def tests_list(run_id: str, db: DB, p: Viewer):
    get_run(db, run_id, p)
    tcs = [row(t) for t in db.scalars(select(TestCase).where(TestCase.run_id == run_id).order_by(TestCase.track, TestCase.id))]
    reqs = list(db.scalars(select(Requirement).where(Requirement.run_id == run_id, Requirement.kind == "ACCEPTANCE_CRITERION")))
    covered = {a for t in tcs for a in t["ac_ids"]}
    no_req = [t["id"] for t in tcs if not t["requirement_ids"] and not t["rationale"]]
    return {
        "test_cases": tcs,
        "coverage": {
            "acceptance_criteria": [{"ac_id": r.ac_id, "text": r.description, "covered": r.ac_id in covered} for r in reqs],
            "tests_without_requirement": no_req,
        },
    }


class TcPatch(BaseModel):
    status: Literal["ACCEPTED", "REJECTED", "PROPOSED"] | None = None
    tolerance: float | None = Field(default=None, ge=0)
    trim: bool | None = None
    case_insensitive: bool | None = None
    expected_result: str | None = Field(default=None, max_length=2000)
    priority: Literal["P1", "P2", "P3"] | None = None
    excluded: bool | None = None


class SelectionBody(BaseModel):
    only: list[str] | None = None  # select exactly these
    select: list[str] = Field(default_factory=list)
    deselect: list[str] = Field(default_factory=list)


@router.put("/runs/{run_id}/test-cases/selection")
def tests_selection(run_id: str, body: SelectionBody, db: DB, p: Analyst):
    """Choose the test cases for the next validation run. Not part of the approved content: no re-approval needed."""
    get_run(db, run_id, p)
    tcs = list(db.scalars(select(TestCase).where(TestCase.run_id == run_id)))
    ids = {t.id for t in tcs}
    unknown = (set(body.only or []) | set(body.select) | set(body.deselect)) - ids
    if unknown:
        raise HTTPException(400, f"Unknown test cases: {', '.join(sorted(unknown)[:10])}")
    for t in tcs:
        if body.only is not None:
            t.selected = t.id in set(body.only)
        if t.id in body.select:
            t.selected = True
        if t.id in body.deselect:
            t.selected = False
    n = sum(1 for t in tcs if t.selected)
    audit(db, p.user, "TEST_SELECTION_CHANGED", run_id, "QA_RUN", run_id, selected=n, total=len(tcs))
    return {"selected": sorted(t.id for t in tcs if t.selected), "total": len(tcs)}


@router.patch("/runs/{run_id}/test-cases/{tc_id}")
def tests_patch(run_id: str, tc_id: str, body: TcPatch, db: DB, p: Analyst):
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


@router.get("/runs/{run_id}/test-pack")
def test_pack_status(run_id: str, db: DB, p: Viewer):
    return testpack.status(db, get_run(db, run_id, p))


@router.post("/runs/{run_id}/test-pack")
def test_pack_build(run_id: str, db: DB, p: Analyst):
    return wf(testpack.build, db, get_run(db, run_id, p), p.user)


@router.post("/runs/{run_id}/test-pack/approve")
def test_pack_approve(run_id: str, body: DecisionBody, db: DB, p: Approver):
    return wf(testpack.approve, db, get_run(db, run_id, p), p.user, body.decision, body.reason)


@router.post("/runs/{run_id}/run-validation")
def run_validation(run_id: str, db: DB, p: Analyst, body: ExecBody = ExecBody()):
    run = get_run(db, run_id, p)
    execs = wf(testpack.start_validation, db, run, p.user, body.force)
    return [row(e) for e in execs]


class ApprovalBody(BaseModel):
    artifact_type: str
    decision: Literal["APPROVED", "REJECTED"]
    reason: str = Field(default="", max_length=4000)
    expected_hash: str | None = None


@router.get("/runs/{run_id}/approvals")
def approvals_get(run_id: str, db: DB, p: Viewer):
    get_run(db, run_id, p)
    from ...models import Approval

    return {
        "state": approval_svc.approval_state(db, run_id),
        "required": approval_svc.REQUIRED,
        "history": [row(a) for a in db.scalars(select(Approval).where(Approval.run_id == run_id).order_by(Approval.created_at.desc()))],
    }


@router.post("/runs/{run_id}/approvals")
def approvals_post(run_id: str, body: ApprovalBody, db: DB, p: Approver):
    run = get_run(db, run_id, p)
    if body.artifact_type == "TRANSFORMATION":
        v = approval_svc.current_version(db, run_id)
        if body.decision == "APPROVED" and v and not v.validation.get("valid"):
            raise HTTPException(400, "A version that fails validation cannot be approved")
    a = wf(approval_svc.decide, db, run_id, body.artifact_type, body.decision, p.user, body.reason, body.expected_hash)
    from ...core.common import set_state

    if body.decision == "REJECTED":
        set_state(db, run, "APPROVAL_REJECTED", p.user)
    else:
        st = approval_svc.approval_state(db, run_id)
        if all(st[k]["status"] == "APPROVED" for k in approval_svc.REQUIRED["ACTUAL"]):
            set_state(db, run, "APPROVED", p.user)
            from ...core.common import clear_exception

            clear_exception(db, run, {"APPROVAL_REJECTED"})
        else:
            set_state(db, run, "AWAITING_APPROVAL", p.user)
    return row(a)
