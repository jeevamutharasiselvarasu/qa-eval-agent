"""TransformationStudioService: versions, AI assistance and execution of transformation definitions."""
from __future__ import annotations

from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..core.common import audit, run_dir, sha256_file, sha256_obj
from ..models import (Dataset, LLMCall, QARun, Requirement, Transformation, TransformationExecution,
                      TransformationVersion, new_id)
from . import mapping as mapping_svc
from .approvals import current_version, primary_transformation
from .datasets import duck_columns, preview_rows, schema_fingerprint
from .llm import get_llm, timed
from .transform.contract import TransformationDefinition, TransformationResult
from .transform.engine import execute, validate_definition
from .transform.visual import PandasUnsupported, compile_visual, compile_visual_to_pandas

MODES = ("SQL", "PYTHON", "VISUAL")


def ensure_transformation(db: Session, run_id: str) -> Transformation:
    t = primary_transformation(db, run_id)
    if not t:
        t = Transformation(run_id=run_id, name="Primary S2T transformation", is_primary=True, current_version=0)
        db.add(t)
        db.flush()
    return t


def actual_inputs(db: Session, run_id: str) -> dict[str, Dataset]:
    return {d.alias: d for d in db.scalars(select(Dataset).where(Dataset.run_id == run_id, Dataset.role == "SOURCE",
                                                                 Dataset.feeds_target.is_(True)))}


def save_version(db: Session, run_id: str, mode: str, code: str, visual_spec: dict | None, origin: str, actor: str,
                 comment: str = "") -> TransformationVersion:
    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}")
    t = ensure_transformation(db, run_id)
    aliases = sorted(actual_inputs(db, run_id))
    content = {"mode": mode, "code": code if mode != "VISUAL" else "", "visual": visual_spec if mode == "VISUAL" else None,
               "inputs": aliases}
    h = sha256_obj(content)
    cur = current_version(db, run_id)
    if cur and cur.content_hash == h:
        return cur
    if origin == "USER_WRITTEN" and cur and cur.origin in ("AI_GENERATED", "AI_EDITED", "USER_EDITED_AI") and cur.mode == mode:
        origin = "USER_EDITED_AI"
    defn = TransformationDefinition(t.id, run_id, mode, {a: Path(d.parquet_path) for a, d in actual_inputs(db, run_id).items()},
                                    code=code, visual_spec=visual_spec)
    validation = validate_definition(defn)
    reqs = []
    if mode == "VISUAL" and visual_spec:
        mids = {c.get("mapping_id") for c in visual_spec.get("columns", [])}
        from ..models import ColumnMapping
        for m in db.scalars(select(ColumnMapping).where(ColumnMapping.run_id == run_id)):
            if m.id in mids or m.mapping_type in ("JOINED", "FILTERED"):
                reqs.extend(m.requirement_ids)
    else:
        reqs = [r.id for r in db.scalars(select(Requirement).where(Requirement.run_id == run_id, Requirement.status == "ACCEPTED",
                                                                  Requirement.kind != "ACCEPTANCE_CRITERION"))]
    t.current_version += 1
    v = TransformationVersion(transformation_id=t.id, version=t.current_version, mode=mode,
                              code=code if mode != "VISUAL" else (validation.get("compiled_sql") or ""),
                              visual_spec=visual_spec if mode == "VISUAL" else None, origin=origin, input_aliases=aliases,
                              requirement_ids=sorted(set(reqs)), content_hash=h, validation=validation, comment=comment,
                              created_by=actor)
    db.add(v)
    db.flush()
    audit(db, actor, "TRANSFORMATION_VERSION_SAVED", run_id, "TRANSFORMATION", t.id, version=v.version, mode=mode,
          origin=origin, hash=h, valid=validation["valid"])
    return v


def restore_version(db: Session, run_id: str, version: int, actor: str) -> TransformationVersion:
    t = ensure_transformation(db, run_id)
    old = db.scalars(select(TransformationVersion).where(TransformationVersion.transformation_id == t.id,
                                                         TransformationVersion.version == version)).first()
    if not old:
        raise ValueError("version not found")
    return save_version(db, run_id, old.mode, old.code, old.visual_spec, old.origin, actor, f"Restored from v{version}")


def definition(version: TransformationVersion, run_id: str, inputs: dict[str, Dataset]) -> TransformationDefinition:
    s = get_settings()
    return TransformationDefinition(
        transformation_id=version.transformation_id, qa_run_id=run_id, mode=version.mode,
        inputs={a: Path(d.parquet_path) for a, d in inputs.items()}, input_dataset_ids={a: d.id for a, d in inputs.items()},
        code=version.code, visual_spec=version.visual_spec, version=version.version, version_id=version.id,
        content_hash=version.content_hash, referenced_requirement_ids=version.requirement_ids,
        execution_configuration={"timeout_s": s.sql_timeout_s if version.mode != "PYTHON" else s.python_timeout_s})


def run_version(db: Session, run: QARun, version: TransformationVersion, inputs: dict[str, Dataset], *, purpose: str,
                role: str, track: str, actor: str, limit: int | None = None, scenario_id: str | None = None,
                execution_id: str | None = None, out_dir: Path | None = None) -> tuple[TransformationResult, Dataset | None]:
    """Execute through the common interface; persist the execution and (on success) an immutable output dataset."""
    missing = [a for a in version.input_aliases if a not in inputs]
    out_dir = out_dir or run_dir(run.id, "outputs")
    out_path = out_dir / f"{purpose.lower()}_{new_id('OUT')}.parquet"
    defn = definition(version, run.id, {a: d for a, d in inputs.items() if a in version.input_aliases or not version.input_aliases})
    fps = {a: d.fingerprint for a, d in inputs.items()}
    if missing:
        from .transform.contract import ExecError
        res = TransformationResult(new_id("TEX"), version.transformation_id, "FAILED",
                                   errors=[ExecError(f"Inputs missing for aliases: {', '.join(missing)}", kind="CONTRACT")],
                                   input_fingerprints=fps)
    else:
        res = execute(defn, out_path, limit=limit, input_fingerprints=fps)
    tex = TransformationExecution(id=res.execution_id, run_id=run.id, transformation_version_id=version.id, purpose=purpose,
                                  scenario_id=scenario_id, status=res.status, output_schema=res.output_schema,
                                  row_count=res.row_count, duration_ms=res.duration_ms, logs=res.logs, warnings=res.warnings,
                                  errors=[e.to_dict() for e in res.errors], input_fingerprints=fps, executed_by=actor)
    db.add(tex)
    ds = None
    if res.status == "SUCCESS":
        out_path.chmod(0o440)
        cols = duck_columns(out_path)
        ds = Dataset(run_id=run.id, role=role, track=track, alias=f"{purpose.lower()}", label=f"{purpose} (v{version.version})",
                     filename=out_path.name, file_format="parquet", size_bytes=out_path.stat().st_size, parquet_path=str(out_path),
                     fingerprint=sha256_file(out_path), schema_fingerprint=schema_fingerprint(cols), row_count=res.row_count,
                     columns=cols, scenario_id=scenario_id, execution_id=execution_id)
        db.add(ds)
        db.flush()
        tex.output_dataset_id = ds.id
    audit(db, actor, "TRANSFORMATION_EXECUTED", run.id, "TRANSFORMATION_VERSION", version.id, purpose=purpose,
          status=res.status, rows=res.row_count, scenario=scenario_id)
    return res, ds


def preview(db: Session, run: QARun, version: TransformationVersion, actor: str, limit: int | None = None) -> dict:
    limit = limit or get_settings().preview_rows
    res, ds = run_version(db, run, version, actual_inputs(db, run.id), purpose="PREVIEW", role="PREVIEW", track="ACTUAL",
                          actor=actor, limit=limit, out_dir=run_dir(run.id, "previews"))
    out = res.to_dict()
    out["preview"] = preview_rows(Path(ds.parquet_path), limit) if ds else None
    return out


# ------------------------------------------------------------------ AI assistance
def _context(db: Session, run_id: str) -> dict:
    inputs = actual_inputs(db, run_id)
    spec, assumptions = mapping_svc.draft_visual_spec(db, run_id)
    ctx = {"datasets": {a: [c["name"] + ":" + c["type"] for c in d.columns] for a, d in inputs.items()},
           "visual_spec": spec, "assumptions": assumptions,
           "known_columns": sorted({c["name"] for d in inputs.values() for c in d.columns})}
    from ..models import ColumnMapping
    ctx["mappings"] = [{"target": m.target_column, "type": m.mapping_type, "expression": m.expression}
                       for m in db.scalars(select(ColumnMapping).where(ColumnMapping.run_id == run_id, ColumnMapping.status == "ACCEPTED"))]
    ctx["requirements"] = [r.description for r in db.scalars(select(Requirement).where(Requirement.run_id == run_id,
                                                                                         Requirement.status == "ACCEPTED"))]
    try:
        ctx["compiled_sql"] = "-- Draft generated from accepted mappings. Review before approving.\n" + "".join(
            f"-- Assumption: {a}\n" for a in assumptions) + compile_visual(spec)
    except Exception as e:  # noqa: BLE001
        ctx["compile_error"] = str(e)
    try:
        ctx["compiled_python"] = ("# Draft generated from accepted mappings. Review before approving.\n" + "".join(
            f"# Assumption: {a}\n" for a in assumptions) + compile_visual_to_pandas(spec, {a: [c['name'] for c in d.columns] for a, d in inputs.items()}))
    except (PandasUnsupported, KeyError, StopIteration) as e:
        ctx["python_error"] = f"Python draft not available for this definition: {e}"
    return ctx


def ai_generate(db: Session, run: QARun, mode: str, instruction: str, actor: str) -> TransformationVersion:
    ctx = _context(db, run.id)
    if mode == "VISUAL":
        if not ctx["visual_spec"]["columns"]:
            raise ValueError("No accepted column mappings to build a visual definition from")
        v = save_version(db, run.id, "VISUAL", "", ctx["visual_spec"], "AI_GENERATED", actor,
                         "Draft from accepted mappings. " + " ".join(ctx["assumptions"]))
        return v
    cur = current_version(db, run.id)
    if instruction and cur and cur.mode == mode:
        ctx["current_code"] = cur.code
    llm = get_llm()
    out, ms, err = timed(llm.generate_code, mode, ctx, instruction)
    db.add(LLMCall(run_id=run.id, purpose=f"GENERATE_{mode}", provider=llm.name, ok=err is None, latency_ms=ms, error=err))
    if err:
        raise ValueError(f"AI generation failed: {err}")
    origin = "AI_EDITED" if instruction and ctx.get("current_code") else "AI_GENERATED"
    return save_version(db, run.id, mode, out.code, None, origin, actor, (out.explanation or "")[:2000])


def ai_explain(db: Session, run: QARun, mode: str, code: str) -> str:
    llm = get_llm()
    out, ms, err = timed(llm.explain_code, mode, code, {})
    db.add(LLMCall(run_id=run.id, purpose="EXPLAIN", provider=llm.name, ok=err is None, latency_ms=ms, error=err))
    if err:
        raise ValueError(err)
    return out.explanation


def ai_fix(db: Session, run: QARun, mode: str, code: str, error: str) -> dict:
    llm = get_llm()
    ctx = {"known_columns": sorted({c["name"] for d in actual_inputs(db, run.id).values() for c in d.columns})}
    out, ms, err = timed(llm.fix_code, mode, code, error, ctx)
    db.add(LLMCall(run_id=run.id, purpose="FIX", provider=llm.name, ok=err is None, latency_ms=ms, error=err))
    if err:
        raise ValueError(err)
    return {"code": out.code, "explanation": out.explanation, "provider": llm.name}
