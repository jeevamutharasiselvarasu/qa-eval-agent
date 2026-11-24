"""Test pack = synthetic scenarios + all test cases, built from the current requirements, data, mappings and
transformation. Workflow with two approvals:
  1. approve the transformation (Transformation Studio),
  2. approve the test pack (requirements, data, mappings, test cases, synthetic data and settings in one decision).
"""
from __future__ import annotations

import threading
import traceback

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core.common import audit, record_error, set_state, sha256_obj
from ..db import session_scope
from ..models import ColumnMapping, Execution, QARun, SyntheticScenario, TestCase
from . import approvals as approval_svc
from . import execution as exec_svc
from . import reports as report_svc
from . import logic_tests, studio, synthetic, testgen

PACK_ARTIFACTS = ["REQUIREMENTS", "DATASETS", "MAPPINGS", "TEST_CASES", "SYNTHETIC_DATA", "EXECUTION_SETTINGS"]


def basis(db: Session, run: QARun) -> str:
    st = approval_svc.approval_state(db, run.id)
    v = approval_svc.current_version(db, run.id)
    return sha256_obj({k: st[k]["current_hash"] for k in ("REQUIREMENTS", "DATASETS", "MAPPINGS", "EXECUTION_SETTINGS")}
                      | {"transformation": v.content_hash if v else None})


def status(db: Session, run: QARun) -> dict:
    st = approval_svc.approval_state(db, run.id)
    v = approval_svc.current_version(db, run.id)
    scen = list(db.scalars(select(SyntheticScenario).where(SyntheticScenario.run_id == run.id)))
    tcs = list(db.scalars(select(TestCase).where(TestCase.run_id == run.id)))
    built = run.test_pack_basis is not None and bool(tcs)
    stale = built and run.test_pack_basis != basis(db, run)
    by_type = {k: sum(1 for t in tcs if t.test_type == k) for k in ("POSITIVE", "NEGATIVE", "EDGE", "GENERAL")}
    by_basis = {k: sum(1 for t in tcs if t.basis == k) for k in ("ACCEPTANCE_CRITERIA", "TRANSFORMATION_LOGIC", "GENERAL")}
    selected = [t for t in tcs if t.selected is not False]
    ignored_reqs = []
    from ..models import Requirement
    for r in db.scalars(select(Requirement).where(Requirement.run_id == run.id, Requirement.status == "PROPOSED")):
        ignored_reqs.append({"id": r.id, "description": r.description, "provenance": r.provenance})
    ignored_maps = [{"id": m.id, "target": m.target_column, "notes": m.notes} for m in
                    db.scalars(select(ColumnMapping).where(ColumnMapping.run_id == run.id, ColumnMapping.status == "PROPOSED",
                                                           ColumnMapping.mapping_type != "UNMAPPED_SOURCE"))]
    pack_approved = all(st[k]["status"] == "APPROVED" for k in PACK_ARTIFACTS if st[k]["status"] != "NOT_READY")
    blockers = []
    if not v:
        blockers.append("No SQL yet: complete the requirements and it is generated automatically")
    elif st["TRANSFORMATION"]["status"] != "APPROVED":
        blockers.append("Approve the transformation first (Transformation tab)")
    if not built:
        blockers.append("Test pack has not been built")
    elif stale:
        blockers.append("Inputs changed since the test pack was built: rebuild it")
    if v and any(s.status == "GENERATED" and s.transformation_version_id != v.id for s in scen):
        blockers.append("Synthetic data was produced by an older transformation version: rebuild the test pack")
    return {"built": built, "stale": stale, "transformation": st["TRANSFORMATION"]["status"],
            "transformation_version": v.version if v else None, "pack_approved": pack_approved and built and not stale,
            "artifacts": {k: st[k] for k in PACK_ARTIFACTS}, "test_cases": len(tcs), "by_type": by_type, "by_basis": by_basis,
            "logic_note": logic_tests.note(v) if v else None,
            "selected": len(selected), "selected_runnable": sum(1 for t in selected if t.state == "READY"),
            "blocked_tests": sum(1 for t in tcs if t.state == "BLOCKED"),
            "scenarios": {"total": len(scen), "generated": sum(s.status == "GENERATED" for s in scen),
                          "blocked": sum(s.status == "BLOCKED" for s in scen), "error": sum(s.status == "ERROR" for s in scen)},
            "ignored_requirements": ignored_reqs, "ignored_mappings": ignored_maps, "can_approve": not blockers,
            "blockers": blockers}


def build(db: Session, run: QARun, actor: str) -> dict:
    """Create a draft transformation if none exists, then (re)generate synthetic data and all test cases."""
    v = approval_svc.current_version(db, run.id)
    notes = []
    if not v:
        from . import target_spec
        spec = target_spec.build_spec(db, run)
        if not spec["ready"]:
            raise ValueError(f"The requirements are not complete ({len(spec['open_items'])} open item(s)). Complete them on "
                             "the Requirements tab; the SQL and the test pack are then generated automatically.")
        v = studio.ai_generate(db, run, "SQL", "", actor)
        v.requirements_basis = target_spec.requirements_basis(db, run.id)
        set_state(db, run, "TRANSFORMATION_DESIGNED", actor)
        notes.append(f"SQL transformation v{v.version} generated from the requirements")
    if (v.validation or {}).get("valid"):
        synthetic.generate(db, run, actor)
    else:
        notes.append("Current transformation fails validation, so synthetic scenarios were not regenerated")
    tcs = testgen.generate_test_pack(db, run, actor)
    db.flush()
    run.test_pack_basis = basis(db, run)
    audit(db, actor, "TEST_PACK_BUILT", run.id, "QA_RUN", run.id, test_cases=len(tcs), transformation_version=v.version)
    return {"notes": notes, **status(db, run)}


def rebuild_if_built(db: Session, run: QARun, actor: str) -> None:
    """Keep the pack in step with the transformation: called after a new transformation version is saved."""
    if run.test_pack_basis is None:
        return
    try:
        build(db, run, actor)
    except Exception as e:  # noqa: BLE001 - leave the pack marked stale; the UI offers a rebuild
        record_error(db, run.id, "TEST_PACK", f"Automatic test-pack rebuild failed: {e}")


def approve(db: Session, run: QARun, actor: str, decision: str, reason: str = "") -> dict:
    s = status(db, run)
    if decision == "APPROVED" and not s["can_approve"]:
        raise ValueError("The test pack cannot be approved yet: " + "; ".join(s["blockers"]))
    if decision == "REJECTED" and not reason.strip():
        raise ValueError("A rejection reason is required")
    done = []
    for k in PACK_ARTIFACTS:
        if s["artifacts"][k]["status"] == "NOT_READY":
            continue
        approval_svc.decide(db, run.id, k, decision, actor, reason)
        done.append(k)
    set_state(db, run, "APPROVED" if decision == "APPROVED" else "APPROVAL_REJECTED", actor)
    audit(db, actor, f"TEST_PACK_{decision}", run.id, "QA_RUN", run.id, artifacts=done, reason=reason)
    db.flush()
    return status(db, run)


# ------------------------------------------------------------------ one-click validation
def start_validation(db: Session, run: QARun, actor: str, force: bool = False) -> list[Execution]:
    """Run the SELECTED test cases: Track A (uploaded data) and/or Track B (synthetic data), whichever has selected
    tests; a report covering exactly those results is generated at the end."""
    tracks = [t for t in ("ACTUAL", "SYNTHETIC") if exec_svc.selected_ids(db, run, t)]
    if "SYNTHETIC" in tracks and not db.scalars(select(SyntheticScenario).where(
            SyntheticScenario.run_id == run.id, SyntheticScenario.status == "GENERATED")).first():
        tracks.remove("SYNTHETIC")
    if not tracks:
        raise exec_svc.ExecutionError("Select at least one test case to run", 400)
    started, to_run = [], []
    for t in tracks:
        ex, reused = exec_svc.start(db, run, t, actor, force)
        started.append(ex)
        if not reused:
            to_run.append(ex.id)
    db.commit()
    threading.Thread(target=_run_all, args=(run.id, to_run, [e.id for e in started], actor), daemon=True,
                     name=f"validate-{run.id}").start()
    return started


def _run_all(run_id: str, to_run: list[str], all_ids: list[str], actor: str) -> None:
    for eid in to_run:
        exec_svc.run_execution(eid)
    try:
        with session_scope() as db:
            run = db.get(QARun, run_id)
            execs = [db.get(Execution, e) for e in all_ids]
            if all(e and e.status not in ("QUEUED", "RUNNING") for e in execs):
                report_svc.generate(db, run, actor, all_ids)
    except Exception as e:  # noqa: BLE001 - report failure never changes test results
        with session_scope() as db:
            record_error(db, run_id, "REPORT", f"Automatic report generation failed: {e}", traceback.format_exc())
