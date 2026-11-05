"""Shared helpers: hashing, run storage, masking, audit and workflow state."""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Iterable

from sqlalchemy.orm import Session

from ..config import get_settings
from ..models import AuditLog, ErrorDetail, QARun, utcnow

# ---------------------------------------------------------------- hashing


def stable_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, default=str, separators=(",", ":"))


def sha256_text(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_obj(obj: Any) -> str:
    return sha256_text(stable_json(obj))


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return "sha256:" + h.hexdigest()


# ---------------------------------------------------------------- storage

SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")


def safe_filename(name: str) -> str:
    """Strip any path components and unsafe characters (defends against path traversal)."""
    base = Path(name.replace("\\", "/")).name
    base = SAFE_NAME.sub("_", base).strip("._")
    stem, dot, ext = base.rpartition(".")
    if not dot:
        stem, ext = base, ""
    ext = ext[:10]
    stem = stem[: 120 - len(ext) - 1].strip("._") or "file"
    return f"{stem}.{ext}" if ext else stem


def run_dir(run_id: str, *parts: str) -> Path:
    if not re.fullmatch(r"[A-Z]+-[A-F0-9]{10}", run_id):
        raise ValueError("invalid run id")
    root = get_settings().storage_dir / "runs" / run_id
    p = root.joinpath(*parts)
    p.mkdir(parents=True, exist_ok=True)
    return p


SAFE_ALIAS = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")


def to_alias(label: str) -> str:
    a = re.sub(r"[^A-Za-z0-9_]+", "_", Path(label).stem).strip("_").lower()
    if not a or not a[0].isalpha():
        a = "ds_" + a
    return a[:60]


# ---------------------------------------------------------------- masking


def is_sensitive(column: str) -> bool:
    c = column.upper()
    return any(p in c for p in get_settings().sensitive_column_patterns)


def mask_value(v: Any) -> Any:
    if v is None:
        return None
    s = str(v)
    if len(s) <= 2:
        return "**"
    return s[0] + "*" * (len(s) - 2) + s[-1]


def mask_records(records: Iterable[dict]) -> list[dict]:
    out = []
    for r in records:
        out.append({k: (mask_value(v) if is_sensitive(k) else v) for k, v in r.items()})
    return out


# ---------------------------------------------------------------- audit


def audit(db: Session, actor: str, action: str, run_id: str | None = None, entity_type: str = "",
          entity_id: str = "", **details: Any) -> None:
    db.add(AuditLog(run_id=run_id, actor=actor, action=action, entity_type=entity_type,
                    entity_id=str(entity_id), details=json.loads(stable_json(details))))


def record_error(db: Session, run_id: str, stage: str, message: str, detail: str = "",
                 execution_id: str | None = None) -> None:
    db.add(ErrorDetail(run_id=run_id, stage=stage, message=message[:2000], detail=detail[:20000],
                       execution_id=execution_id))


# ---------------------------------------------------------------- workflow states

WORKFLOW_STATES = [
    "RUN_CREATED",
    "JIRA_RETRIEVAL_IN_PROGRESS",
    "JIRA_RETRIEVED",
    "REQUIREMENTS_ANALYZED",
    "DATASETS_UPLOADED",
    "DATASETS_PROFILED",
    "MAPPING_GENERATED",
    "TRANSFORMATION_DESIGNED",
    "TEST_CASES_GENERATED",
    "SYNTHETIC_DATA_GENERATED",
    "AWAITING_APPROVAL",
    "APPROVED",
    "ACTUAL_TRANSFORMATION_IN_PROGRESS",
    "SYNTHETIC_EXECUTION_IN_PROGRESS",
    "VALIDATION_IN_PROGRESS",
    "VALIDATION_COMPLETED",
    "FAILURE_ANALYSIS_COMPLETED",
    "REPORT_GENERATED",
]
EXCEPTION_STATES = {
    "JIRA_RETRIEVAL_FAILED",
    "REQUIREMENT_GAP",
    "DATASET_PARSE_FAILED",
    "PROFILE_FAILED",
    "MAPPING_REVIEW_REQUIRED",
    "APPROVAL_REJECTED",
    "TRANSFORMATION_FAILED",
    "EXECUTION_FAILED",
    "PARTIAL_SUCCESS",
    "CANCELLED",
}


def set_state(db: Session, run: QARun, state: str, actor: str = "system", *, force: bool = False) -> None:
    """Advance workflow state. Forward-only for normal states (non-linear steps never regress the
    headline state) unless force=True. Exception states are tracked separately."""
    if state in EXCEPTION_STATES:
        run.exception_state = state
        audit(db, actor, "STATE_EXCEPTION", run.id, "QA_RUN", run.id, state=state)
        return
    if state not in WORKFLOW_STATES:
        raise ValueError(f"unknown state {state}")
    cur = WORKFLOW_STATES.index(run.status) if run.status in WORKFLOW_STATES else 0
    new = WORKFLOW_STATES.index(state)
    if force or new >= cur:
        if run.status != state:
            audit(db, actor, "STATE_CHANGE", run.id, "QA_RUN", run.id, frm=run.status, to=state)
        run.status = state
    run.updated_at = utcnow()


def clear_exception(db: Session, run: QARun, states: set[str] | None = None) -> None:
    if run.exception_state and (states is None or run.exception_state in states):
        run.exception_state = None
