"""ExecutionManager: approval-gated, idempotent, checkpointed execution of the ACTUAL and SYNTHETIC tracks.
Both tracks call the same transformation engine (studio.run_version) and the same ValidationEngine."""
from __future__ import annotations

import csv
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..core.common import audit, mask_records, record_error, run_dir, set_state, sha256_obj
from ..db import session_scope
from ..models import (Dataset, Evidence, Execution, FailureAnalysis, LLMCall, QARun, RequirementGap, SyntheticScenario,
                      TestCase, TestResult, utcnow)
from .approvals import approval_state, current_version, missing_approvals
from .llm import get_llm, timed
from .studio import actual_inputs, run_version
from .validation import CheckError, ValidationContext, evaluate_guards, run_check

TERMINAL = ("PASS", "FAIL", "BLOCKED", "SKIPPED")
_threads: dict[str, threading.Thread] = {}


class ExecutionError(Exception):
    def __init__(self, message: str, status: int = 409):
        super().__init__(message)
        self.status = status


def idempotency_key(db: Session, run: QARun, track: str) -> str:
    st = approval_state(db, run.id)
    v = current_version(db, run.id)
    fps = sorted((d.alias, d.role, d.fingerprint) for d in db.scalars(select(Dataset).where(
        Dataset.run_id == run.id, Dataset.role.in_(["SOURCE", "TARGET"]))))
    parts = {"track": track, "tv": v.content_hash if v else None, "datasets": fps,
             "tests": st["TEST_CASES"]["current_hash"], "settings": st["EXECUTION_SETTINGS"]["current_hash"],
             "mappings": st["MAPPINGS"]["current_hash"], "reqs": st["REQUIREMENTS"]["current_hash"]}
    if track == "SYNTHETIC":
        parts["synthetic"] = st["SYNTHETIC_DATA"]["current_hash"]
    return sha256_obj(parts)


def start(db: Session, run: QARun, track: str, actor: str, force: bool = False) -> tuple[Execution, bool]:
    """Create (or reuse) an execution. Returns (execution, reused)."""
    if track not in ("ACTUAL", "SYNTHETIC"):
        raise ExecutionError("track must be ACTUAL or SYNTHETIC", 400)
    missing = missing_approvals(db, run.id, track)
    if missing:
        raise ExecutionError("Execution requires current approvals. Not approved: " + "; ".join(missing))
    if track == "ACTUAL" and not db.scalars(select(Dataset).where(Dataset.run_id == run.id, Dataset.role == "TARGET")).first():
        raise ExecutionError("Upload the target dataset first", 400)
    key = idempotency_key(db, run, track)
    running = db.scalars(select(Execution).where(Execution.run_id == run.id, Execution.track == track,
                                                 Execution.status.in_(["QUEUED", "RUNNING"]))).first()
    if running:
        raise ExecutionError(f"Execution {running.id} for this track is already {running.status}; duplicate execution prevented")
    done = db.scalars(select(Execution).where(Execution.run_id == run.id, Execution.idempotency_key == key,
                                              Execution.status == "COMPLETED").order_by(Execution.created_at.desc())).first()
    if done and not force:
        audit(db, actor, "EXECUTION_REUSED", run.id, "EXECUTION", done.id, track=track)
        return done, True
    v = current_version(db, run.id)
    ex = Execution(run_id=run.id, track=track, status="QUEUED", idempotency_key=key, transformation_version_id=v.id,
                   settings=dict(run.settings or {}), started_by=actor,
                   input_fingerprints={d.alias + ":" + d.role: d.fingerprint for d in db.scalars(select(Dataset).where(
                       Dataset.run_id == run.id, Dataset.role.in_(["SOURCE", "TARGET"])))})
    db.add(ex)
    db.flush()
    audit(db, actor, "EXECUTION_STARTED", run.id, "EXECUTION", ex.id, track=track, key=key, forced=force)
    return ex, False


def launch(execution_id: str) -> None:
    t = threading.Thread(target=run_execution, args=(execution_id,), daemon=True, name=f"exec-{execution_id}")
    _threads[execution_id] = t
    t.start()


def retry(db: Session, ex: Execution, actor: str) -> Execution:
    if ex.status not in ("PARTIAL_SUCCESS", "FAILED", "INTERRUPTED", "CANCELLED"):
        raise ExecutionError(f"Only failed, partial, interrupted or cancelled executions can be retried (status {ex.status})")
    run = db.get(QARun, ex.run_id)
    missing = missing_approvals(db, run.id, ex.track)
    if missing:
        raise ExecutionError("Approvals are no longer current: " + "; ".join(missing))
    if idempotency_key(db, run, ex.track) != ex.idempotency_key:
        raise ExecutionError("Inputs, code, tests or settings changed since this execution; start a new execution instead")
    removed = 0
    for r in db.scalars(select(TestResult).where(TestResult.execution_id == ex.id, TestResult.status.in_(["ERROR", "NOT_EXECUTED"]))):
        db.delete(r)
        removed += 1
    ex.status, ex.attempt, ex.cancel_requested, ex.error, ex.ended_at = "QUEUED", ex.attempt + 1, False, None, None
    audit(db, actor, "EXECUTION_RETRY", run.id, "EXECUTION", ex.id, attempt=ex.attempt, reset_results=removed)
    return ex


def cancel(db: Session, ex: Execution, actor: str) -> None:
    if ex.status not in ("QUEUED", "RUNNING"):
        raise ExecutionError(f"Execution is {ex.status}; nothing to cancel")
    ex.cancel_requested = True
    audit(db, actor, "EXECUTION_CANCEL_REQUESTED", ex.run_id, "EXECUTION", ex.id)


def recover_interrupted() -> int:
    """Called at startup: anything RUNNING/QUEUED was interrupted by a restart; completed checkpoints are kept."""
    n = 0
    with session_scope() as db:
        for ex in db.scalars(select(Execution).where(Execution.status.in_(["RUNNING", "QUEUED"]))):
            ex.status, ex.error = "INTERRUPTED", "Service restarted during execution; completed results were preserved. Retry to resume."
            n += 1
    return n


# ------------------------------------------------------------------ worker
def _expected_actual(db: Session, run: QARun, ex: Execution, actor: str) -> dict[str, dict]:
    """Produce (or reuse checkpointed) expected/actual/sources per evaluation unit. Returns unit -> paths."""
    v = current_version(db, run.id)
    units: dict[str, dict] = {}
    ids = dict(ex.expected_dataset_ids or {})
    if ex.track == "ACTUAL":
        set_state(db, run, "ACTUAL_TRANSFORMATION_IN_PROGRESS", actor)
        inputs = actual_inputs(db, run.id)
        target = db.scalars(select(Dataset).where(Dataset.run_id == run.id, Dataset.role == "TARGET")).first()
        exp = db.get(Dataset, ids["ACTUAL"]) if ids.get("ACTUAL") else None
        if not exp or not Path(exp.parquet_path).exists():
            res, exp = run_version(db, run, v, inputs, purpose="ACTUAL_EXPECTED", role="ACTUAL_EXPECTED", track="ACTUAL",
                                   actor=actor, execution_id=ex.id, out_dir=run_dir(run.id, "expected", ex.id))
            if res.status != "SUCCESS":
                raise RuntimeError("TRANSFORMATION_FAILED: " + "; ".join(e.message for e in res.errors))
            ids["ACTUAL"] = exp.id
            ex.expected_dataset_ids = ids
            db.commit()  # checkpoint
        units["ACTUAL"] = {"expected": Path(exp.parquet_path), "actual": Path(target.parquet_path),
                           "sources": {a: Path(d.parquet_path) for a, d in inputs.items()}, "expected_id": exp.id}
    else:
        set_state(db, run, "SYNTHETIC_EXECUTION_IN_PROGRESS", actor)
        for s in db.scalars(select(SyntheticScenario).where(SyntheticScenario.run_id == run.id)):
            if s.status != "GENERATED":
                continue
            if s.transformation_version_id != v.id:
                raise RuntimeError(f"Scenario {s.id} expected target was produced by a different transformation version; regenerate")
            srcs = {a: db.get(Dataset, i) for a, i in s.source_dataset_ids.items()}
            out = db.get(Dataset, ids[s.id]) if ids.get(s.id) else None
            if not out or not Path(out.parquet_path).exists():
                res, out = run_version(db, run, v, srcs, purpose="SYNTHETIC_RERUN", role="SYNTHETIC_OUTPUT", track="SYNTHETIC",
                                       actor=actor, scenario_id=s.id, execution_id=ex.id,
                                       out_dir=run_dir(run.id, "synthetic", s.id, ex.id))
                if res.status != "SUCCESS":
                    units[s.id] = {"error": "; ".join(e.message for e in res.errors)}
                    continue
                ids[s.id] = out.id
                ex.expected_dataset_ids = ids
                db.commit()
            units[s.id] = {"expected": Path(db.get(Dataset, s.expected_dataset_id).parquet_path), "actual": Path(out.parquet_path),
                           "sources": {a: Path(d.parquet_path) for a, d in srcs.items()}, "expected_id": s.expected_dataset_id}
    return units


def _evaluate(tc: dict, unit: dict, open_gaps: set[str], max_rows: int) -> dict:
    t0 = time.perf_counter()
    started = utcnow()
    try:
        ctx = ValidationContext(unit["expected"], unit["actual"], unit["sources"], max_rows)
        try:
            reason = evaluate_guards(ctx, tc["validation"].get("guards", []), open_gaps)
            if reason:
                out = {"status": "BLOCKED", "message": reason}
            else:
                o = run_check(ctx, tc["validation"])
                out = {"status": o.status, "expected": o.expected, "actual": o.actual, "message": o.message,
                       "metrics": o.metrics, "ev_cols": o.evidence_columns, "ev_rows": o.evidence_rows}
        finally:
            ctx.close()
    except CheckError as e:
        out = {"status": "ERROR", "message": str(e)}
    except Exception as e:  # noqa: BLE001 - technical errors are ERROR, never FAIL
        out = {"status": "ERROR", "message": f"{type(e).__name__}: {e}", "trace": traceback.format_exc()[-3000:]}
    out["duration_ms"] = int((time.perf_counter() - t0) * 1000)
    out["started_at"] = started
    out["ended_at"] = utcnow()
    return out


def run_execution(execution_id: str) -> None:
    s = get_settings()
    with session_scope() as db:
        ex = db.get(Execution, execution_id)
        if not ex or ex.status not in ("QUEUED",):
            return
        run = db.get(QARun, ex.run_id)
        actor = ex.started_by
        ex.status, ex.started_at = "RUNNING", ex.started_at or utcnow()
        db.commit()
        try:
            units = _expected_actual(db, run, ex, actor)
            set_state(db, run, "VALIDATION_IN_PROGRESS", actor)
            db.commit()
            tcs = list(db.scalars(select(TestCase).where(TestCase.run_id == run.id, TestCase.track == ex.track).order_by(TestCase.id)))
            done = {r.test_case_id for r in db.scalars(select(TestResult).where(TestResult.execution_id == ex.id))}
            open_gaps = {g.id for g in db.scalars(select(RequirementGap).where(RequirementGap.run_id == run.id,
                                                                                RequirementGap.status.in_(["OPEN", "BLOCKED"])))}
            max_rows = int((ex.settings or {}).get("max_evidence_rows", 200))
            todo = []
            for tc in tcs:
                if tc.id in done:
                    continue  # checkpointed result from a previous attempt
                unit_key = "ACTUAL" if ex.track == "ACTUAL" else tc.scenario_id
                unit = units.get(unit_key)
                if tc.status == "REJECTED":
                    _store(db, ex, run, tc, {"status": "SKIPPED", "message": "Rejected by reviewer"}, None)
                elif tc.state == "BLOCKED":
                    _store(db, ex, run, tc, {"status": "BLOCKED", "message": tc.blocked_reason or "Blocked"}, None)
                elif tc.state == "EXCLUDED":
                    _store(db, ex, run, tc, {"status": "SKIPPED", "message": "Excluded by configuration"}, None)
                elif unit is None:
                    _store(db, ex, run, tc, {"status": "BLOCKED", "message": "Scenario has no generated expected target"}, None)
                elif "error" in unit:
                    _store(db, ex, run, tc, {"status": "ERROR", "message": "Re-execution failed: " + unit["error"]}, None)
                else:
                    todo.append((tc, unit))
            db.commit()
            cancelled = False
            with ThreadPoolExecutor(max_workers=max(1, s.validation_workers)) as pool:
                futs = {pool.submit(_evaluate, {"id": tc.id, "validation": tc.validation}, unit, open_gaps, max_rows): tc
                        for tc, unit in todo}
                for f in as_completed(futs):
                    tc = futs[f]
                    _store(db, ex, run, tc, f.result(), units.get("ACTUAL" if ex.track == "ACTUAL" else tc.scenario_id))
                    db.commit()  # checkpoint every result
                    db.refresh(ex)
                    if ex.cancel_requested and not cancelled:
                        cancelled = True
                        for other in futs:
                            other.cancel()
            if cancelled:
                stored = {r.test_case_id for r in db.scalars(select(TestResult).where(TestResult.execution_id == ex.id))}
                for tc in tcs:
                    if tc.id not in stored:
                        _store(db, ex, run, tc, {"status": "NOT_EXECUTED", "message": "Execution cancelled"}, None)
            _finish(db, ex, run, "CANCELLED" if cancelled else None)
        except Exception as e:  # noqa: BLE001
            db.rollback()
            ex = db.get(Execution, execution_id)
            run = db.get(QARun, ex.run_id)
            msg = str(e)
            ex.status, ex.error, ex.ended_at = "FAILED", msg[:4000], utcnow()
            set_state(db, run, "TRANSFORMATION_FAILED" if msg.startswith("TRANSFORMATION_FAILED") else "EXECUTION_FAILED", actor)
            record_error(db, run.id, "EXECUTION", msg, traceback.format_exc(), ex.id)
            stored = {r.test_case_id for r in db.scalars(select(TestResult).where(TestResult.execution_id == ex.id))}
            for tc in db.scalars(select(TestCase).where(TestCase.run_id == run.id, TestCase.track == ex.track)):
                if tc.id not in stored:
                    db.add(TestResult(execution_id=ex.id, test_case_id=tc.id, status="NOT_EXECUTED",
                                      message="Not executed: " + msg[:300]))
            audit(db, actor, "EXECUTION_FAILED", run.id, "EXECUTION", ex.id, error=msg[:500])


def _store(db: Session, ex: Execution, run: QARun, tc: TestCase, out: dict, unit: dict | None) -> None:
    ev_id = None
    if out.get("ev_rows") or out.get("metrics"):
        path = None
        rows = out.get("ev_rows") or []
        if rows:
            d = run_dir(run.id, "evidence", ex.id)
            path = d / f"{tc.id}.csv"
            cols = out.get("ev_cols") or sorted({k for r in rows for k in r})
            with open(path, "w", newline="") as f:
                w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
                w.writeheader()
                for r in (mask_records(rows) if (ex.settings or {}).get("mask_sensitive_in_reports", True) else rows):
                    w.writerow(r)
        evd = Evidence(execution_id=ex.id, test_case_id=tc.id, kind=tc.validation.get("type", ""), path=str(path) if path else None,
                       summary={"metrics": out.get("metrics", {}), "expected_dataset": unit.get("expected_id") if unit else None,
                                "row_count": len(rows)}, sample=mask_records(rows[:25]))
        db.add(evd)
        db.flush()
        ev_id = evd.id
    if out.get("status") == "ERROR":
        record_error(db, run.id, "VALIDATION", f"{tc.id}: {out.get('message', '')}", out.get("trace", ""), ex.id)
    db.add(TestResult(execution_id=ex.id, test_case_id=tc.id, status=out["status"], expected=str(out.get("expected", ""))[:2000],
                      actual=str(out.get("actual", ""))[:2000], message=str(out.get("message", ""))[:4000],
                      metrics=out.get("metrics", {}), evidence_id=ev_id, duration_ms=out.get("duration_ms", 0),
                      started_at=out.get("started_at"), ended_at=out.get("ended_at")))


def _finish(db: Session, ex: Execution, run: QARun, forced: str | None) -> None:
    results = list(db.scalars(select(TestResult).where(TestResult.execution_id == ex.id)))
    counts: dict[str, int] = {}
    for r in results:
        counts[r.status] = counts.get(r.status, 0) + 1
    total = len(results)
    overall = ("FAIL" if counts.get("FAIL") else "ERROR" if counts.get("ERROR") else
               "BLOCKED" if counts.get("BLOCKED") and not counts.get("PASS") else "PASS_WITH_BLOCKED" if counts.get("BLOCKED") else "PASS")
    ex.summary = {"total": total, **{k: counts.get(k, 0) for k in ("PASS", "FAIL", "BLOCKED", "ERROR", "SKIPPED", "NOT_EXECUTED")},
                  "overall": overall}
    ex.summary = {**ex.summary, "phase": "AI_FAILURE_ANALYSIS"}
    db.commit()  # deterministic results are final and persisted before any AI step
    try:
        analyze_failures(db, ex)
    except Exception as e:  # noqa: BLE001 - AI problems never affect deterministic results
        db.rollback()
        record_error(db, run.id, "FAILURE_ANALYSIS", str(e), traceback.format_exc(), ex.id)
    ex.summary = {k: v for k, v in ex.summary.items() if k != "phase"}
    if forced:
        ex.status = forced
    else:
        ex.status = "PARTIAL_SUCCESS" if counts.get("ERROR") or counts.get("NOT_EXECUTED") else "COMPLETED"
    ex.ended_at = utcnow()
    set_state(db, run, "VALIDATION_COMPLETED", ex.started_by)
    if ex.status == "PARTIAL_SUCCESS":
        set_state(db, run, "PARTIAL_SUCCESS", ex.started_by)
    audit(db, ex.started_by, "EXECUTION_FINISHED", run.id, "EXECUTION", ex.id, status=ex.status, summary=ex.summary)
    db.commit()


# ------------------------------------------------------------------ AI failure analysis (after deterministic execution)
def analyze_failures(db: Session, ex: Execution) -> int:
    s = get_settings()
    llm = get_llm()
    run = db.get(QARun, ex.run_id)
    n = 0
    for old in db.scalars(select(FailureAnalysis).where(FailureAnalysis.execution_id == ex.id)):
        db.delete(old)
    fails = list(db.scalars(select(TestResult).where(TestResult.execution_id == ex.id, TestResult.status == "FAIL")))
    for r in fails:
        tc = db.scalars(select(TestCase).where(
            TestCase.run_id == run.id, TestCase.id == r.test_case_id)).first()
        ev = db.get(Evidence, r.evidence_id) if r.evidence_id else None
        evidence = {"test_case": tc.id, "title": tc.title, "check_type": tc.validation.get("type"), "column": tc.validation.get("column"),
                    "requirements": tc.requirement_ids, "acceptance_criteria": tc.ac_ids, "expected": r.expected, "actual": r.actual,
                    "message": r.message, "metrics": r.metrics, "track": ex.track, "scenario": tc.scenario_id,
                    "facts": [f"Deterministic check {tc.validation.get('type')} returned FAIL: {r.message}",
                              f"Expected: {r.expected}; actual: {r.actual}"]}
        samples = (ev.sample if ev else [])[:10]
        if tc.validation.get("type") == "COLUMN_MATCH" and samples:
            evidence["sample_differences"] = samples
            evidence["source_value_matches"] = _source_matches(db, run, tc, samples)
        elif samples:
            evidence["sample_records"] = samples
        send_samples = llm.name == "mock" or s.llm_allow_data_samples
        payload = evidence if send_samples else {k: v for k, v in evidence.items() if k not in ("sample_differences", "sample_records")}
        out, ms, err = timed(llm.analyze_failure, payload)
        db.add(LLMCall(run_id=run.id, purpose="FAILURE_ANALYSIS", provider=llm.name, sent_data_samples=send_samples and llm.name != "mock",
                       ok=err is None, latency_ms=ms, error=err))
        data = out.model_dump() if out else {"category": "UNAVAILABLE", "suspected_layer": "UNKNOWN", "confirmed_facts": evidence["facts"],
                                             "likely_explanations": [], "unverified_hypotheses": [], "recommended_checks": [],
                                             "error": err}
        data["evidence_id"] = r.evidence_id
        data["deterministic_status"] = r.status  # AI cannot change this
        db.add(FailureAnalysis(execution_id=ex.id, test_result_id=r.id, test_case_id=tc.id, provider=llm.name, payload=data))
        n += 1
    set_state(db, run, "VALIDATION_COMPLETED", ex.started_by)
    set_state(db, run, "FAILURE_ANALYSIS_COMPLETED", ex.started_by)
    audit(db, ex.started_by, "FAILURE_ANALYSIS_COMPLETED", run.id, "EXECUTION", ex.id, analysed=n, provider=llm.name)
    db.commit()
    return n


def _source_matches(db: Session, run: QARun, tc: TestCase, samples: list[dict]) -> list[str]:
    """Deterministic fact-finding for the AI: does |difference| equal a source value for the same key?"""
    import duckdb
    keys = tc.validation.get("keys") or []
    if len(keys) != 1:
        return []
    key = keys[0]
    hits = []
    for alias, d in actual_inputs(db, run.id).items():
        cols = {c["name"].upper(): c for c in d.columns}
        if key.upper() not in cols:
            continue
        numeric = [c["name"] for c in d.columns if any(t in c["type"].upper() for t in ("INT", "DOUBLE", "DECIMAL", "FLOAT"))]
        with duckdb.connect(":memory:") as con:
            con.execute(f"CREATE VIEW t AS SELECT * FROM read_parquet('{d.parquet_path}')")
            for srow in samples:
                diff = srow.get("difference")
                kv = srow.get(key)
                if not isinstance(diff, (int, float)) or diff == 0 or kv is None:
                    continue
                for c in numeric:
                    q = '"' + c + '"'
                    n = con.execute(f'SELECT COUNT(*) FROM t WHERE "{cols[key.upper()]["name"]}" = ? AND ABS({q}) = ABS(?)', [kv, diff]).fetchone()[0]
                    if n:
                        hits.append(f"{key}={kv}: |difference| {abs(diff):g} equals {alias}.{c} in {n} source row(s)")
    return hits[:10]
