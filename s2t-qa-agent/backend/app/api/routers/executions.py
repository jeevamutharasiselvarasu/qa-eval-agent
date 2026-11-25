"""Executions, results, evidence and comparison."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from sqlalchemy import func, select

from ...models import (
    Evidence,
    Execution,
    FailureAnalysis,
    TestCase,
    TestResult,
    TransformationExecution,
)
from ...services import execution as exec_svc
from ..deps import DB, Analyst, ExecBody, Viewer, get_run, row, wf

router = APIRouter()


def _start(run_id, track, body, db, p):
    run = get_run(db, run_id, p)
    ex, reused = wf(exec_svc.start, db, run, track, p.user, body.force)
    db.commit()
    if not reused:
        exec_svc.launch(ex.id)
    return row(ex, {"reused": reused})


@router.post("/runs/{run_id}/execute/actual")
def execute_actual(run_id: str, db: DB, p: Analyst, body: ExecBody = ExecBody()):
    return _start(run_id, "ACTUAL", body, db, p)


@router.get("/runs/{run_id}/executions")
def executions(run_id: str, db: DB, p: Viewer):
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
def execution_detail(execution_id: str, db: DB, p: Viewer):
    e = _exec(db, execution_id, p)
    tcs = {t.id: t for t in db.scalars(select(TestCase).where(TestCase.run_id == e.run_id, TestCase.track == e.track))}
    res = list(db.scalars(select(TestResult).where(TestResult.execution_id == e.id)))
    ev = {x.id: x for x in db.scalars(select(Evidence).where(Evidence.execution_id == e.id))}
    fa = {
        f.test_case_id: f.payload | {"provider": f.provider}
        for f in db.scalars(select(FailureAnalysis).where(FailureAnalysis.execution_id == e.id))
    }
    texs = [
        row(t)
        for t in db.scalars(
            select(TransformationExecution)
            .where(TransformationExecution.run_id == e.run_id)
            .where(TransformationExecution.purpose == "ACTUAL_EXPECTED")
        )
        if t.output_dataset_id in (e.expected_dataset_ids or {}).values()
    ]
    results = []
    for r in sorted(res, key=lambda r: r.test_case_id):
        t = tcs.get(r.test_case_id)
        results.append(
            row(
                r,
                {
                    "title": t.title if t else "",
                    "category": t.category if t else "",
                    "scope": ((t.validation or {}).get("scope") or {}).get("desc") if t else None,
                    "ac_ids": t.ac_ids if t else [],
                    "requirement_ids": t.requirement_ids if t else [],
                    "evidence": (
                        lambda x: x and {"id": x.id, "kind": x.kind, "summary": x.summary, "sample": x.sample, "has_file": bool(x.path)}
                    )(ev.get(r.evidence_id)),
                    "analysis": fa.get(r.test_case_id),
                },
            )
        )
    from ...models import ErrorDetail

    errs = [row(x) for x in db.scalars(select(ErrorDetail).where(ErrorDetail.execution_id == e.id))]
    return row(
        e, {"results": results, "transformation_executions": texs, "errors": errs, "progress": {"done": len(res), "total": len(tcs)}}
    )


@router.post("/executions/{execution_id}/retry")
def execution_retry(execution_id: str, db: DB, p: Analyst):
    e = _exec(db, execution_id, p)
    wf(exec_svc.retry, db, e, p.user)
    db.commit()
    exec_svc.launch(e.id)
    return row(e)


@router.post("/executions/{execution_id}/cancel")
def execution_cancel(execution_id: str, db: DB, p: Analyst):
    e = _exec(db, execution_id, p)
    wf(exec_svc.cancel, db, e, p.user)
    if e.status == "QUEUED":
        e.status = "CANCELLED"
    return row(e)


@router.post("/executions/{execution_id}/analyze")
def execution_analyze(execution_id: str, db: DB, p: Analyst):
    e = _exec(db, execution_id, p)
    if e.status in ("QUEUED", "RUNNING"):
        raise HTTPException(409, "Failure analysis runs only after deterministic execution finishes")
    return {"analysed": exec_svc.analyze_failures(db, e)}


@router.get("/executions/{execution_id}/evidence/{evidence_id}")
def evidence_download(execution_id: str, evidence_id: str, db: DB, p: Viewer):
    e = _exec(db, execution_id, p)
    ev = db.get(Evidence, evidence_id)
    if not ev or ev.execution_id != e.id or not ev.path:
        raise HTTPException(404, "Evidence file not found")
    return FileResponse(ev.path, media_type="text/csv", filename=f"{e.id}_{ev.test_case_id}_evidence.csv")


@router.get("/compare")
def compare(a: str, b: str, db: DB, p: Viewer):
    ea, eb = _exec(db, a, p), _exec(db, b, p)
    ra = {r.test_case_id: r.status for r in db.scalars(select(TestResult).where(TestResult.execution_id == ea.id))}
    rb = {r.test_case_id: r.status for r in db.scalars(select(TestResult).where(TestResult.execution_id == eb.id))}
    rows = [{"test_case_id": k, "a": ra.get(k), "b": rb.get(k), "changed": ra.get(k) != rb.get(k)} for k in sorted(set(ra) | set(rb))]
    return {"a": row(ea), "b": row(eb), "rows": rows, "changed": sum(r["changed"] for r in rows)}
