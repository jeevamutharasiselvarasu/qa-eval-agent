"""TransformationOrchestrator: the single deterministic execution entry point for SQL, PYTHON and VISUAL
definitions. Used by previews, actual expected-target generation and synthetic expected-target generation."""
from __future__ import annotations

import time
import uuid
from pathlib import Path

from .contract import ExecError, TransformationDefinition, TransformationResult
from .python_exec import execute_python, validate_python
from .sql_exec import execute_sql, validate_sql
from .visual import VisualSpecError, compile_visual


def compile_definition(defn: TransformationDefinition) -> tuple[str | None, list[ExecError]]:
    if defn.mode == "VISUAL":
        try:
            return compile_visual(defn.visual_spec or {}, {a: [] for a in defn.inputs}), []
        except VisualSpecError as e:
            return None, [ExecError(str(e), kind="CONTRACT")]
        except (KeyError, TypeError) as e:
            return None, [ExecError(f"Malformed visual definition: {e}", kind="CONTRACT")]
    return defn.code, []


def validate_definition(defn: TransformationDefinition) -> dict:
    """Static validation only (no execution)."""
    if defn.mode == "PYTHON":
        errs, warns = validate_python(defn.code)
        return {"valid": not errs, "errors": [e.to_dict() for e in errs], "warnings": warns, "compiled_sql": None}
    sql, errs = compile_definition(defn)
    if errs:
        return {"valid": False, "errors": [e.to_dict() for e in errs], "warnings": [], "compiled_sql": None}
    errs, warns, refs = validate_sql(sql or "", set(defn.inputs))
    return {"valid": not errs, "errors": [e.to_dict() for e in errs], "warnings": warns,
            "compiled_sql": sql if defn.mode == "VISUAL" else None, "referenced_datasets": sorted(refs)}


def execute(defn: TransformationDefinition, output_path: Path, *, limit: int | None = None,
            input_fingerprints: dict[str, str] | None = None) -> TransformationResult:
    res = TransformationResult(execution_id="TEX-" + uuid.uuid4().hex[:10].upper(), transformation_id=defn.transformation_id,
                               status="FAILED", code_version=f"v{defn.version}:{defn.content_hash}",
                               input_fingerprints=input_fingerprints or {})
    t0 = time.perf_counter()
    cfg = defn.execution_configuration or {}
    if defn.mode in ("SQL", "VISUAL"):
        sql, errs = compile_definition(defn)
        res.compiled_sql = sql if defn.mode == "VISUAL" else None
        if errs:
            res.errors = errs
        else:
            _, warns, _ = validate_sql(sql or "", set(defn.inputs))
            res.warnings.extend(warns)
            errs, logs, rows, schema = execute_sql(sql or "", defn.inputs, output_path, limit=limit, timeout_s=cfg.get("timeout_s"))
            res.errors, res.logs, res.row_count, res.output_schema = errs, logs, rows, schema
    elif defn.mode == "PYTHON":
        errs, logs, rows, schema, warns = execute_python(defn.code, defn.inputs, output_path, limit=limit, timeout_s=cfg.get("timeout_s"))
        res.errors, res.logs, res.row_count, res.output_schema = errs, logs, rows, schema
        res.warnings.extend(warns)
    else:
        res.errors = [ExecError(f"Unknown mode {defn.mode}", kind="CONTRACT")]
    if not res.errors:
        if not res.output_schema:
            res.errors.append(ExecError("Transformation produced no columns", kind="CONTRACT"))
        else:
            res.status = "SUCCESS"
            res.output_path = output_path
    res.duration_ms = int((time.perf_counter() - t0) * 1000)
    return res
