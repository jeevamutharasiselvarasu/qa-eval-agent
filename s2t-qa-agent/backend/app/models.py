"""Persistence model (metadata, state, approvals, traceability and audit).

Large datasets and report artifacts live in run-scoped file storage; tables hold references,
fingerprints and metadata only.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10].upper()}"


class ProjectConfig(Base):
    __tablename__ = "qa_project_config"
    project: Mapped[str] = mapped_column(String(64), primary_key=True)
    settings: Mapped[dict] = mapped_column(JSON, default=dict)
    retention_days: Mapped[int] = mapped_column(Integer, default=90)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class QARun(Base):
    __tablename__ = "qa_runs"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: new_id("RUN"))
    project: Mapped[str] = mapped_column(String(64), index=True, default="default")
    name: Mapped[str] = mapped_column(String(200), default="")
    jira_key: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(48), default="RUN_CREATED", index=True)
    exception_state: Mapped[str | None] = mapped_column(String(48), nullable=True)
    settings: Mapped[dict] = mapped_column(JSON, default=dict)
    created_by: Mapped[str] = mapped_column(String(128), default="system")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class JiraIssue(Base):
    __tablename__ = "qa_jira_issues"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: new_id("JIRA"))
    run_id: Mapped[str] = mapped_column(ForeignKey("qa_runs.id", ondelete="CASCADE"), index=True)
    issue_key: Mapped[str] = mapped_column(String(64))
    source: Mapped[str] = mapped_column(String(16))  # MOCK | LIVE
    payload: Mapped[dict] = mapped_column(JSON)
    warnings: Mapped[list] = mapped_column(JSON, default=list)
    retrieved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Requirement(Base):
    __tablename__ = "qa_requirements"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("qa_runs.id", ondelete="CASCADE"), primary_key=True, index=True)
    ac_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    kind: Mapped[str] = mapped_column(String(48))  # ACCEPTANCE_CRITERION | DERIVED_COLUMN | UNIQUE_KEY | ...
    description: Mapped[str] = mapped_column(Text)
    spec: Mapped[dict] = mapped_column(JSON, default=dict)
    provenance: Mapped[str] = mapped_column(String(32))  # JIRA_EXPLICIT | ATTACHMENT_DERIVED | AI_INFERRED | USER_CONFIRMED
    confidence: Mapped[str] = mapped_column(String(16), default="HIGH")  # HIGH | MEDIUM | LOW
    source_ref: Mapped[str] = mapped_column(String(200), default="")
    evidence: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(24), default="PROPOSED")  # PROPOSED | ACCEPTED | REJECTED
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class RequirementGap(Base):
    __tablename__ = "qa_requirement_gaps"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("qa_runs.id", ondelete="CASCADE"), primary_key=True, index=True)
    description: Mapped[str] = mapped_column(Text)
    detail: Mapped[str] = mapped_column(Text, default="")
    gap_type: Mapped[str] = mapped_column(String(48), default="AMBIGUOUS")
    affected: Mapped[dict] = mapped_column(JSON, default=dict)  # {requirements, columns, scenarios}
    evidence: Mapped[str] = mapped_column(Text, default="")
    clarification: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="OPEN")  # OPEN | CLARIFIED | ACCEPTED_LIMITATION | BLOCKED
    provenance: Mapped[str] = mapped_column(String(32), default="DETERMINISTIC_CHECK")
    updated_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class Dataset(Base):
    __tablename__ = "qa_datasets"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: new_id("DS"))
    run_id: Mapped[str] = mapped_column(ForeignKey("qa_runs.id", ondelete="CASCADE"), index=True)
    # SOURCE | TARGET | ACTUAL_EXPECTED | SYNTHETIC_SOURCE | SYNTHETIC_EXPECTED | SYNTHETIC_OUTPUT | PREVIEW
    role: Mapped[str] = mapped_column(String(32), index=True)
    track: Mapped[str] = mapped_column(String(16), default="ACTUAL")  # ACTUAL | SYNTHETIC
    alias: Mapped[str] = mapped_column(String(64))  # SQL table name / python sources key
    label: Mapped[str] = mapped_column(String(200), default="")
    filename: Mapped[str] = mapped_column(String(255), default="")
    file_format: Mapped[str] = mapped_column(String(16), default="parquet")
    sheet_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)
    original_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    parquet_path: Mapped[str] = mapped_column(Text)
    fingerprint: Mapped[str] = mapped_column(String(80))
    schema_fingerprint: Mapped[str] = mapped_column(String(80), default="")
    row_count: Mapped[int] = mapped_column(Integer, default=0)
    columns: Mapped[list] = mapped_column(JSON, default=list)  # [{name,type}]
    scenario_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    execution_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    feeds_target: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class DatasetProfile(Base):
    __tablename__ = "qa_dataset_profiles"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: new_id("PROF"))
    dataset_id: Mapped[str] = mapped_column(ForeignKey("qa_datasets.id", ondelete="CASCADE"), index=True)
    profile: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ColumnMapping(Base):
    __tablename__ = "qa_column_mappings"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("qa_runs.id", ondelete="CASCADE"), primary_key=True, index=True)
    source_columns: Mapped[list] = mapped_column(JSON, default=list)  # ["alias.COL", ...]
    target_column: Mapped[str | None] = mapped_column(String(128), nullable=True)
    mapping_type: Mapped[str] = mapped_column(String(32))
    expression: Mapped[str] = mapped_column(Text, default="")
    requirement_ids: Mapped[list] = mapped_column(JSON, default=list)
    evidence: Mapped[str] = mapped_column(Text, default="")
    provenance: Mapped[str] = mapped_column(String(32))
    confidence: Mapped[str] = mapped_column(String(16), default="MEDIUM")
    status: Mapped[str] = mapped_column(String(24), default="PROPOSED")  # PROPOSED | ACCEPTED | REJECTED
    is_key: Mapped[bool] = mapped_column(Boolean, default=False)
    version: Mapped[int] = mapped_column(Integer, default=1)
    notes: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class Transformation(Base):
    __tablename__ = "qa_transformation_rules"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: new_id("TRF"))
    run_id: Mapped[str] = mapped_column(ForeignKey("qa_runs.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    is_primary: Mapped[bool] = mapped_column(Boolean, default=True)
    current_version: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    versions: Mapped[list["TransformationVersion"]] = relationship(
        back_populates="transformation", order_by="TransformationVersion.version", cascade="all, delete-orphan"
    )


class TransformationVersion(Base):
    __tablename__ = "qa_transformation_versions"
    __table_args__ = (UniqueConstraint("transformation_id", "version"),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: new_id("TRV"))
    transformation_id: Mapped[str] = mapped_column(ForeignKey("qa_transformation_rules.id", ondelete="CASCADE"))
    version: Mapped[int] = mapped_column(Integer)
    mode: Mapped[str] = mapped_column(String(16))  # SQL | PYTHON | VISUAL
    code: Mapped[str] = mapped_column(Text, default="")
    visual_spec: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    origin: Mapped[str] = mapped_column(String(24))  # USER_WRITTEN | AI_GENERATED | AI_EDITED | USER_EDITED_AI
    input_aliases: Mapped[list] = mapped_column(JSON, default=list)
    requirement_ids: Mapped[list] = mapped_column(JSON, default=list)
    content_hash: Mapped[str] = mapped_column(String(80))
    validation: Mapped[dict] = mapped_column(JSON, default=dict)
    comment: Mapped[str] = mapped_column(Text, default="")
    created_by: Mapped[str] = mapped_column(String(128), default="system")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    transformation: Mapped[Transformation] = relationship(back_populates="versions")


class TransformationExecution(Base):
    __tablename__ = "qa_transformation_executions"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: new_id("TEX"))
    run_id: Mapped[str] = mapped_column(ForeignKey("qa_runs.id", ondelete="CASCADE"), index=True)
    transformation_version_id: Mapped[str] = mapped_column(ForeignKey("qa_transformation_versions.id"))
    purpose: Mapped[str] = mapped_column(String(32))  # PREVIEW | ACTUAL_EXPECTED | SYNTHETIC_EXPECTED | SYNTHETIC_RERUN
    scenario_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    status: Mapped[str] = mapped_column(String(16))
    output_dataset_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    output_schema: Mapped[list] = mapped_column(JSON, default=list)
    row_count: Mapped[int] = mapped_column(Integer, default=0)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    logs: Mapped[list] = mapped_column(JSON, default=list)
    warnings: Mapped[list] = mapped_column(JSON, default=list)
    errors: Mapped[list] = mapped_column(JSON, default=list)
    input_fingerprints: Mapped[dict] = mapped_column(JSON, default=dict)
    executed_by: Mapped[str] = mapped_column(String(128), default="system")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class SyntheticScenario(Base):
    __tablename__ = "qa_test_scenarios"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("qa_runs.id", ondelete="CASCADE"), primary_key=True, index=True)
    scenario_type: Mapped[str] = mapped_column(String(48))
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text, default="")
    seed: Mapped[int] = mapped_column(Integer, default=42)
    records: Mapped[int] = mapped_column(Integer, default=10)
    status: Mapped[str] = mapped_column(String(24), default="PENDING")  # PENDING | GENERATED | BLOCKED | ERROR
    blocked_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    gap_ids: Mapped[list] = mapped_column(JSON, default=list)
    requirement_ids: Mapped[list] = mapped_column(JSON, default=list)
    source_dataset_ids: Mapped[dict] = mapped_column(JSON, default=dict)  # alias -> dataset id
    expected_dataset_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    transformation_version_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    explanation: Mapped[list] = mapped_column(JSON, default=list)
    assumptions: Mapped[list] = mapped_column(JSON, default=list)
    generation_method: Mapped[str] = mapped_column(String(64), default="SEEDED_RULE_BASED")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class TestCase(Base):
    __tablename__ = "qa_test_cases"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("qa_runs.id", ondelete="CASCADE"), primary_key=True, index=True)
    scenario_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    track: Mapped[str] = mapped_column(String(16))  # ACTUAL | SYNTHETIC
    category: Mapped[str] = mapped_column(String(48))
    title: Mapped[str] = mapped_column(String(300))
    description: Mapped[str] = mapped_column(Text, default="")
    preconditions: Mapped[str] = mapped_column(Text, default="")
    steps: Mapped[list] = mapped_column(JSON, default=list)
    expected_result: Mapped[str] = mapped_column(Text, default="")
    validation: Mapped[dict] = mapped_column(JSON, default=dict)  # deterministic check spec
    ac_ids: Mapped[list] = mapped_column(JSON, default=list)
    requirement_ids: Mapped[list] = mapped_column(JSON, default=list)
    mapping_ids: Mapped[list] = mapped_column(JSON, default=list)
    rationale: Mapped[str] = mapped_column(Text, default="")  # e.g. PROFILING rationale when no requirement
    priority: Mapped[str] = mapped_column(String(8), default="P2")
    severity: Mapped[str] = mapped_column(String(16), default="MAJOR")
    state: Mapped[str] = mapped_column(String(16), default="READY")  # READY | BLOCKED | EXCLUDED
    blocked_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(24), default="PROPOSED")  # PROPOSED | ACCEPTED | REJECTED
    version: Mapped[int] = mapped_column(Integer, default=1)


class Approval(Base):
    __tablename__ = "qa_approvals"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: new_id("APR"))
    run_id: Mapped[str] = mapped_column(ForeignKey("qa_runs.id", ondelete="CASCADE"), index=True)
    artifact_type: Mapped[str] = mapped_column(String(32))
    artifact_id: Mapped[str] = mapped_column(String(64))
    artifact_version: Mapped[str] = mapped_column(String(64), default="")
    content_hash: Mapped[str] = mapped_column(String(80))
    decision: Mapped[str] = mapped_column(String(16))  # APPROVED | REJECTED
    reviewer: Mapped[str] = mapped_column(String(128))
    reason: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


Index("ix_approvals_artifact", Approval.run_id, Approval.artifact_type, Approval.artifact_id)


class Execution(Base):
    __tablename__ = "qa_executions"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: new_id("EXE"))
    run_id: Mapped[str] = mapped_column(ForeignKey("qa_runs.id", ondelete="CASCADE"), index=True)
    track: Mapped[str] = mapped_column(String(16))  # ACTUAL | SYNTHETIC
    status: Mapped[str] = mapped_column(String(24))  # QUEUED | RUNNING | COMPLETED | PARTIAL_SUCCESS | FAILED | INTERRUPTED | CANCELLED
    idempotency_key: Mapped[str] = mapped_column(String(80), index=True)
    transformation_version_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    expected_dataset_ids: Mapped[dict] = mapped_column(JSON, default=dict)
    summary: Mapped[dict] = mapped_column(JSON, default=dict)
    settings: Mapped[dict] = mapped_column(JSON, default=dict)
    input_fingerprints: Mapped[dict] = mapped_column(JSON, default=dict)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    attempt: Mapped[int] = mapped_column(Integer, default=1)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    started_by: Mapped[str] = mapped_column(String(128), default="system")
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class TestResult(Base):
    __tablename__ = "qa_test_results"
    __table_args__ = (UniqueConstraint("execution_id", "test_case_id"),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: new_id("RES"))
    execution_id: Mapped[str] = mapped_column(ForeignKey("qa_executions.id", ondelete="CASCADE"), index=True)
    test_case_id: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(16))  # PASS | FAIL | BLOCKED | NOT_EXECUTED | ERROR | SKIPPED
    expected: Mapped[str] = mapped_column(Text, default="")
    actual: Mapped[str] = mapped_column(Text, default="")
    message: Mapped[str] = mapped_column(Text, default="")
    metrics: Mapped[dict] = mapped_column(JSON, default=dict)
    evidence_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Evidence(Base):
    __tablename__ = "qa_evidence"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: new_id("EVD"))
    execution_id: Mapped[str] = mapped_column(ForeignKey("qa_executions.id", ondelete="CASCADE"), index=True)
    test_case_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    kind: Mapped[str] = mapped_column(String(32))
    path: Mapped[str | None] = mapped_column(Text, nullable=True)
    summary: Mapped[dict] = mapped_column(JSON, default=dict)
    sample: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ErrorDetail(Base):
    __tablename__ = "qa_error_details"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: new_id("ERR"))
    run_id: Mapped[str] = mapped_column(ForeignKey("qa_runs.id", ondelete="CASCADE"), index=True)
    execution_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    stage: Mapped[str] = mapped_column(String(48))
    message: Mapped[str] = mapped_column(Text)
    detail: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class FailureAnalysis(Base):
    __tablename__ = "qa_failure_analysis"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: new_id("FA"))
    execution_id: Mapped[str] = mapped_column(ForeignKey("qa_executions.id", ondelete="CASCADE"), index=True)
    test_result_id: Mapped[str] = mapped_column(String(32))
    test_case_id: Mapped[str] = mapped_column(String(32))
    provider: Mapped[str] = mapped_column(String(32))
    payload: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Report(Base):
    __tablename__ = "qa_reports"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: new_id("RPT"))
    run_id: Mapped[str] = mapped_column(ForeignKey("qa_runs.id", ondelete="CASCADE"), index=True)
    execution_ids: Mapped[list] = mapped_column(JSON, default=list)
    files: Mapped[dict] = mapped_column(JSON, default=dict)
    overall: Mapped[str] = mapped_column(String(24), default="")
    created_by: Mapped[str] = mapped_column(String(128), default="system")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AuditLog(Base):
    __tablename__ = "qa_audit_log"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str | None] = mapped_column(String(32), index=True, nullable=True)
    actor: Mapped[str] = mapped_column(String(128))
    action: Mapped[str] = mapped_column(String(64))
    entity_type: Mapped[str] = mapped_column(String(48), default="")
    entity_id: Mapped[str] = mapped_column(String(64), default="")
    details: Mapped[dict] = mapped_column(JSON, default=dict)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class LLMCall(Base):
    """Every AI interaction is logged (purpose, provider, whether data samples were sent)."""

    __tablename__ = "qa_llm_calls"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: new_id("LLM"))
    run_id: Mapped[str | None] = mapped_column(String(32), index=True, nullable=True)
    purpose: Mapped[str] = mapped_column(String(48))
    provider: Mapped[str] = mapped_column(String(32))
    sent_data_samples: Mapped[bool] = mapped_column(Boolean, default=False)
    ok: Mapped[bool] = mapped_column(Boolean, default=True)
    latency_ms: Mapped[float] = mapped_column(Float, default=0)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
