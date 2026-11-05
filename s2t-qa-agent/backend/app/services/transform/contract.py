"""Common transformation execution contract shared by SQL, PYTHON and VISUAL modes and by both tracks."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class TransformationDefinition:
    transformation_id: str
    qa_run_id: str
    mode: str  # SQL | PYTHON | VISUAL
    inputs: dict[str, Path]  # alias -> parquet path (run-scoped, read-only)
    input_dataset_ids: dict[str, str] = field(default_factory=dict)
    code: str = ""
    visual_spec: dict | None = None
    version: int = 0
    version_id: str = ""
    content_hash: str = ""
    approval_status: str = "PENDING"
    referenced_requirement_ids: list[str] = field(default_factory=list)
    execution_configuration: dict = field(default_factory=dict)  # timeout_s, memory, preview_rows


@dataclass
class ExecError:
    message: str
    line: int | None = None
    column: int | None = None
    kind: str = "RUNTIME"  # SYNTAX | SAFETY | RUNTIME | TIMEOUT | CONTRACT

    def to_dict(self) -> dict:
        return {"message": self.message, "line": self.line, "column": self.column, "kind": self.kind}


@dataclass
class TransformationResult:
    execution_id: str
    transformation_id: str
    status: str  # SUCCESS | FAILED
    output_path: Path | None = None
    output_schema: list[dict] = field(default_factory=list)
    row_count: int = 0
    duration_ms: int = 0
    logs: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    errors: list[ExecError] = field(default_factory=list)
    code_version: str = ""
    input_fingerprints: dict[str, str] = field(default_factory=dict)
    compiled_sql: str | None = None

    def to_dict(self) -> dict:
        return {
            "execution_id": self.execution_id, "transformation_id": self.transformation_id, "status": self.status,
            "output_schema": self.output_schema, "row_count": self.row_count, "duration_ms": self.duration_ms,
            "logs": self.logs, "warnings": self.warnings, "errors": [e.to_dict() for e in self.errors],
            "code_version": self.code_version, "input_fingerprints": self.input_fingerprints,
            "compiled_sql": self.compiled_sql,
        }
