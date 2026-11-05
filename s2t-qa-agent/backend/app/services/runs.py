"""Run-level workflow operations (used by the API and by tests)."""
from __future__ import annotations

import shutil
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core.common import (audit, clear_exception, record_error, run_dir, safe_filename, set_state,
                           to_alias, SAFE_ALIAS)
from ..models import Dataset, DatasetProfile, JiraIssue, QARun, Requirement, RequirementGap
from . import mapping as mapping_svc
from .datasets import DatasetError, parse_file, profile_parquet, schema_fingerprint, store_upload
from .jira import JiraError, get_connector
from .requirements import persist_analysis, profile_gaps

DEFAULT_SETTINGS = {
    "default_numeric_tolerance": 0.0,
    "string_compare": {"trim": False, "case_insensitive": False},
    "max_evidence_rows": 200,
    "synthetic_records_per_scenario": 12,
    "synthetic_seed": 20261103,
    "mask_sensitive_in_reports": True,
}


class WorkflowError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def create_run(db: Session, jira_key: str, actor: str, project: str = "default", name: str = "",
               settings: dict | None = None) -> QARun:
    from .jira import validate_key
    key = validate_key(jira_key)
    run = QARun(jira_key=key, project=project, name=name or f"{key} S2T validation", created_by=actor,
                settings={**DEFAULT_SETTINGS, **(settings or {})})
    db.add(run)
    db.flush()
    audit(db, actor, "RUN_CREATED", run.id, "QA_RUN", run.id, jira_key=key, project=project)
    return run


def retrieve_jira(db: Session, run: QARun, actor: str) -> JiraIssue:
    set_state(db, run, "JIRA_RETRIEVAL_IN_PROGRESS", actor)
    try:
        doc = get_connector().fetch(run.jira_key)
    except JiraError as e:
        set_state(db, run, "JIRA_RETRIEVAL_FAILED", actor)
        record_error(db, run.id, "JIRA", f"{e.code}: {e}")
        raise WorkflowError(f"Jira retrieval failed ({e.code}): {e}", 502 if e.code in ("TIMEOUT", "UPSTREAM", "RATE_LIMITED") else 400) from e
    for old in db.scalars(select(JiraIssue).where(JiraIssue.run_id == run.id)):
        db.delete(old)
    issue = JiraIssue(run_id=run.id, issue_key=doc.key, source=doc.source, payload=doc.to_dict())
    db.add(issue)
    clear_exception(db, run, {"JIRA_RETRIEVAL_FAILED"})
    set_state(db, run, "JIRA_RETRIEVED", actor)
    res = persist_analysis(db, run.id, doc.to_dict(), actor)
    issue.warnings = res.warnings
    set_state(db, run, "REQUIREMENTS_ANALYZED", actor)
    db.flush()
    _gap_state(db, run, actor)
    audit(db, actor, "JIRA_RETRIEVED", run.id, "JIRA_ISSUE", doc.key, source=doc.source, comments=len(doc.comments),
          attachments=len(doc.attachments))
    return issue


def _gap_state(db: Session, run: QARun, actor: str) -> None:
    open_gaps = db.scalars(select(RequirementGap).where(RequirementGap.run_id == run.id, RequirementGap.status == "OPEN")).first()
    if open_gaps:
        set_state(db, run, "REQUIREMENT_GAP", actor)
    else:
        clear_exception(db, run, {"REQUIREMENT_GAP"})


def upload_dataset(db: Session, run: QARun, tmp_path: Path, original_name: str, role: str, actor: str,
                   label: str = "", alias: str = "", options: dict | None = None) -> Dataset:
    if role not in ("SOURCE", "TARGET"):
        raise WorkflowError("role must be SOURCE or TARGET")
    fname = safe_filename(original_name)
    alias = alias or to_alias(label or fname)
    if not SAFE_ALIAS.match(alias):
        raise WorkflowError("Alias must be a valid identifier (letters, digits, underscore)")
    existing = list(db.scalars(select(Dataset).where(Dataset.run_id == run.id, Dataset.role.in_(["SOURCE", "TARGET"]))))
    if role == "SOURCE" and any(d.alias == alias and d.role == "SOURCE" for d in existing):
        raise WorkflowError(f"A source with alias '{alias}' already exists in this run", 409)
    up = run_dir(run.id, "uploads", role.lower())
    staged = up / f".staging_{fname}"
    shutil.move(str(tmp_path), staged)
    try:
        original, fp, size = store_upload(staged, up, fname)
    except Exception:
        staged.unlink(missing_ok=True)
        raise
    if any(d.fingerprint == fp and d.role == role for d in existing):
        original.chmod(0o640)
        original.unlink()
        raise WorkflowError("This exact file was already uploaded for this role (duplicate upload)", 409)
    out = run_dir(run.id, "normalized") / f"{role.lower()}_{alias}_{fp[7:19]}.parquet"
    try:
        parsed = parse_file(original, out, options or {})
    except DatasetError as e:
        set_state(db, run, "DATASET_PARSE_FAILED", actor)
        record_error(db, run.id, "UPLOAD", f"{e.code}: {e}")
        original.chmod(0o640)
        original.unlink(missing_ok=True)
        raise WorkflowError(f"{e.code}: {e}", 422) from e
    if role == "TARGET":
        for d in existing:
            if d.role == "TARGET":  # exactly one target per run: replace
                db.delete(d)
                audit(db, actor, "TARGET_REPLACED", run.id, "DATASET", d.id)
    out.chmod(0o440)
    ds = Dataset(run_id=run.id, role=role, track="ACTUAL", alias=alias if role == "SOURCE" else "target",
                 label=label or fname, filename=fname, file_format=parsed.file_format, sheet_name=parsed.sheet_name,
                 size_bytes=size, original_path=str(original), parquet_path=str(out), fingerprint=fp,
                 schema_fingerprint=schema_fingerprint(parsed.columns), row_count=parsed.row_count, columns=parsed.columns)
    db.add(ds)
    db.flush()
    clear_exception(db, run, {"DATASET_PARSE_FAILED"})
    roles = {d.role for d in db.scalars(select(Dataset).where(Dataset.run_id == run.id))}
    if {"SOURCE", "TARGET"} <= roles:
        set_state(db, run, "DATASETS_UPLOADED", actor)
    audit(db, actor, "DATASET_UPLOADED", run.id, "DATASET", ds.id, role=role, alias=ds.alias, fingerprint=fp,
          rows=parsed.row_count, format=parsed.file_format)
    return ds


def profile_all(db: Session, run: QARun, actor: str) -> list[DatasetProfile]:
    dss = list(db.scalars(select(Dataset).where(Dataset.run_id == run.id, Dataset.role.in_(["SOURCE", "TARGET"]))))
    if not any(d.role == "SOURCE" for d in dss) or not any(d.role == "TARGET" for d in dss):
        raise WorkflowError("Upload at least one source dataset and exactly one target dataset first")
    out = []
    profiles = {}
    try:
        for d in dss:
            for old in db.scalars(select(DatasetProfile).where(DatasetProfile.dataset_id == d.id)):
                db.delete(old)
            p = profile_parquet(Path(d.parquet_path))
            prof = DatasetProfile(dataset_id=d.id, profile={**p, "file_name": d.filename, "format": d.file_format,
                                                            "size_bytes": d.size_bytes, "sheet_name": d.sheet_name})
            db.add(prof)
            out.append(prof)
            if d.role == "SOURCE" and d.feeds_target:
                profiles[d.alias] = p
    except Exception as e:  # noqa: BLE001
        set_state(db, run, "PROFILE_FAILED", actor)
        record_error(db, run.id, "PROFILE", str(e))
        raise WorkflowError(f"Profiling failed: {e}", 500) from e
    clear_exception(db, run, {"PROFILE_FAILED"})
    created = profile_gaps(db, run.id, profiles, {})
    set_state(db, run, "DATASETS_PROFILED", actor)
    _gap_state(db, run, actor)
    audit(db, actor, "DATASETS_PROFILED", run.id, "QA_RUN", run.id, datasets=len(out), new_gaps=created)
    return out


def generate_mappings(db: Session, run: QARun, actor: str):
    if not db.scalars(select(Requirement).where(Requirement.run_id == run.id)).first():
        raise WorkflowError("Retrieve Jira requirements first")
    if not db.scalars(select(Dataset).where(Dataset.run_id == run.id, Dataset.role == "SOURCE")).first():
        raise WorkflowError("Upload source datasets first")
    maps = mapping_svc.propose(db, run.id, actor)
    set_state(db, run, "MAPPING_GENERATED", actor)
    if any(m.status == "PROPOSED" and m.mapping_type != "UNMAPPED_SOURCE" for m in maps):
        set_state(db, run, "MAPPING_REVIEW_REQUIRED", actor)
    return maps
