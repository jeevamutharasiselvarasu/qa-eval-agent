"""ApprovalService. An approval is valid only while the artifact's current content hash equals the hash that was
approved, so any material change automatically invalidates the approval (STALE) and requires re-approval."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core.common import audit, sha256_obj
from ..models import (Approval, ColumnMapping, Dataset, QARun, Requirement, RequirementGap, SyntheticScenario,
                      TestCase, Transformation, TransformationVersion)

ARTIFACT_TYPES = ["REQUIREMENTS", "DATASETS", "MAPPINGS", "TRANSFORMATION", "TEST_CASES", "SYNTHETIC_DATA", "EXECUTION_SETTINGS"]
REQUIRED = {
    "ACTUAL": ["REQUIREMENTS", "DATASETS", "MAPPINGS", "TRANSFORMATION", "TEST_CASES", "EXECUTION_SETTINGS"],
    "SYNTHETIC": ["REQUIREMENTS", "DATASETS", "MAPPINGS", "TRANSFORMATION", "TEST_CASES", "SYNTHETIC_DATA", "EXECUTION_SETTINGS"],
}


def primary_transformation(db: Session, run_id: str) -> Transformation | None:
    return db.scalars(select(Transformation).where(Transformation.run_id == run_id, Transformation.is_primary.is_(True))).first()


def current_version(db: Session, run_id: str) -> TransformationVersion | None:
    t = primary_transformation(db, run_id)
    if not t or not t.current_version:
        return None
    return db.scalars(select(TransformationVersion).where(TransformationVersion.transformation_id == t.id,
                                                          TransformationVersion.version == t.current_version)).first()


def artifact_content(db: Session, run_id: str, kind: str) -> tuple[str, str, object]:
    """Return (artifact_id, artifact_version, content) for hashing."""
    if kind == "REQUIREMENTS":
        reqs = [[r.id, r.kind, r.description, r.spec, r.status, r.provenance] for r in
                db.scalars(select(Requirement).where(Requirement.run_id == run_id).order_by(Requirement.id))]
        gaps = [[g.id, g.status, g.clarification] for g in
                db.scalars(select(RequirementGap).where(RequirementGap.run_id == run_id).order_by(RequirementGap.id))]
        return run_id, f"{len(reqs)} reqs/{len(gaps)} gaps", {"reqs": reqs, "gaps": gaps}
    if kind == "DATASETS":
        ds = [[d.id, d.role, d.alias, d.fingerprint, d.feeds_target] for d in
              db.scalars(select(Dataset).where(Dataset.run_id == run_id, Dataset.role.in_(["SOURCE", "TARGET"])).order_by(Dataset.id))]
        return run_id, f"{len(ds)} datasets", ds
    if kind == "MAPPINGS":
        ms = [[m.id, m.source_columns, m.target_column, m.mapping_type, m.expression, m.status, m.is_key, m.version] for m in
              db.scalars(select(ColumnMapping).where(ColumnMapping.run_id == run_id).order_by(ColumnMapping.id))]
        return run_id, f"{len(ms)} mappings", ms
    if kind == "TRANSFORMATION":
        v = current_version(db, run_id)
        if not v:
            return "", "", None
        return v.id, f"v{v.version}", v.content_hash
    if kind == "TEST_CASES":
        tcs = [[t.id, t.track, t.validation, t.state, t.status, t.expected_result, t.version] for t in
               db.scalars(select(TestCase).where(TestCase.run_id == run_id).order_by(TestCase.id))]
        return run_id, f"{len(tcs)} test cases", tcs or None
    if kind == "SYNTHETIC_DATA":
        sc = [[s.id, s.status, s.seed, s.records, s.source_dataset_ids, s.expected_dataset_id, s.transformation_version_id]
              for s in db.scalars(select(SyntheticScenario).where(SyntheticScenario.run_id == run_id).order_by(SyntheticScenario.id))]
        fps = {d.id: d.fingerprint for d in db.scalars(select(Dataset).where(
            Dataset.run_id == run_id, Dataset.role.in_(["SYNTHETIC_SOURCE", "SYNTHETIC_EXPECTED"])))}
        return run_id, f"{len(sc)} scenarios", ({"scenarios": sc, "fps": fps} if sc else None)
    if kind == "EXECUTION_SETTINGS":
        run = db.get(QARun, run_id)
        return run_id, "settings", run.settings or {}
    raise ValueError(kind)


def approval_state(db: Session, run_id: str) -> dict[str, dict]:
    out = {}
    for kind in ARTIFACT_TYPES:
        aid, aver, content = artifact_content(db, run_id, kind)
        if content is None:
            out[kind] = {"status": "NOT_READY", "artifact_id": aid, "artifact_version": aver, "current_hash": None, "last": None}
            continue
        h = sha256_obj(content)
        last = db.scalars(select(Approval).where(Approval.run_id == run_id, Approval.artifact_type == kind)
                          .order_by(Approval.created_at.desc(), Approval.id.desc())).first()
        if not last:
            st = "PENDING"
        elif last.content_hash != h:
            st = "STALE"
        else:
            st = last.decision
        out[kind] = {"status": st, "artifact_id": aid, "artifact_version": aver, "current_hash": h,
                     "last": None if not last else {"id": last.id, "decision": last.decision, "reviewer": last.reviewer,
                                                    "reason": last.reason, "at": last.created_at.isoformat(),
                                                    "artifact_version": last.artifact_version}}
    return out


def decide(db: Session, run_id: str, kind: str, decision: str, reviewer: str, reason: str = "",
           expected_hash: str | None = None) -> Approval:
    if kind not in ARTIFACT_TYPES:
        raise ValueError(f"Unknown artifact type {kind}")
    if decision not in ("APPROVED", "REJECTED"):
        raise ValueError("decision must be APPROVED or REJECTED")
    if decision == "REJECTED" and not reason.strip():
        raise ValueError("A rejection reason is required")
    aid, aver, content = artifact_content(db, run_id, kind)
    if content is None:
        raise ValueError(f"{kind} is not ready for review")
    h = sha256_obj(content)
    if expected_hash and expected_hash != h:
        raise ValueError("The artifact changed since you loaded it; reload and review again")
    if kind == "REQUIREMENTS" and decision == "APPROVED":
        pending_ai = db.scalars(select(Requirement).where(Requirement.run_id == run_id, Requirement.provenance == "AI_INFERRED",
                                                          Requirement.status == "PROPOSED")).all()
        if pending_ai:
            raise ValueError(f"{len(pending_ai)} AI-inferred requirement(s) must be accepted or rejected before approval")
    if kind == "MAPPINGS" and decision == "APPROVED":
        pending = db.scalars(select(ColumnMapping).where(ColumnMapping.run_id == run_id, ColumnMapping.status == "PROPOSED",
                                                         ColumnMapping.mapping_type != "UNMAPPED_SOURCE")).all()
        if pending:
            raise ValueError(f"{len(pending)} proposed mapping(s) must be accepted or rejected before approval")
    a = Approval(run_id=run_id, artifact_type=kind, artifact_id=aid, artifact_version=aver, content_hash=h,
                 decision=decision, reviewer=reviewer, reason=reason)
    db.add(a)
    audit(db, reviewer, f"APPROVAL_{decision}", run_id, kind, aid, version=aver, hash=h, reason=reason)
    return a


def missing_approvals(db: Session, run_id: str, track: str) -> list[str]:
    st = approval_state(db, run_id)
    return [f"{k}: {st[k]['status']}" for k in REQUIRED[track] if st[k]["status"] != "APPROVED"]
