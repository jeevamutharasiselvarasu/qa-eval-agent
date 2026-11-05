"""TestCaseGenerator: deterministic test cases from approved requirements, mappings, gaps and profiles.
Unknown behaviour produces BLOCKED tests (or runtime guards), never invented expectations."""
from __future__ import annotations

import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core.common import audit, set_state
from ..models import ColumnMapping, Dataset, QARun, Requirement, RequirementGap, SyntheticScenario, TestCase
from .synthetic import build_model, _base_key

OPEN = ("OPEN", "BLOCKED")


class _Ctx:
    def __init__(self, db: Session, run: QARun):
        self.run = run
        self.reqs = {r.id: r for r in db.scalars(select(Requirement).where(Requirement.run_id == run.id)) if r.status != "REJECTED"}
        self.maps = [m for m in db.scalars(select(ColumnMapping).where(ColumnMapping.run_id == run.id)) if m.status == "ACCEPTED"]
        self.gaps = list(db.scalars(select(RequirementGap).where(RequirementGap.run_id == run.id)))
        self.target = db.scalars(select(Dataset).where(Dataset.run_id == run.id, Dataset.role == "TARGET")).first()
        keyreq = [r for r in self.reqs.values() if r.kind == "BUSINESS_KEY"] or [r for r in self.reqs.values() if r.kind == "UNIQUE_KEY"]
        self.keys = keyreq[0].spec.get("columns", []) if keyreq else [m.target_column for m in self.maps if m.is_key and m.target_column]
        self.key_reqs = [r.id for r in keyreq]
        self.filter_reqs = [r.id for r in self.reqs.values() if r.kind == "FILTER"]
        self.join_reqs = [r.id for r in self.reqs.values() if r.kind == "JOIN"]

    def acs(self, req_ids) -> list[str]:
        return sorted({self.reqs[r].ac_id for r in req_ids if r in self.reqs and self.reqs[r].ac_id})


def _tc(run_id, tid, track, category, title, validation, *, ctx: _Ctx, req_ids=(), map_ids=(), expected="", priority="P2",
        severity="MAJOR", rationale="", state="READY", blocked=None, scenario_id=None, description="") -> TestCase:
    req_ids = sorted(set(req_ids))
    return TestCase(id=tid, run_id=run_id, scenario_id=scenario_id, track=track, category=category, title=title,
                    description=description or title, validation=validation, ac_ids=ctx.acs(req_ids), requirement_ids=req_ids,
                    mapping_ids=sorted(set(map_ids)), expected_result=expected, priority=priority, severity=severity,
                    rationale=rationale, state=state, blocked_reason=blocked, status="ACCEPTED",
                    preconditions="Approved requirements, mappings, transformation and execution settings.",
                    steps=["Execute approved transformation on " + ("actual uploaded sources" if track == "ACTUAL" else "scenario sources"),
                           f"Run deterministic check {validation.get('type')}", "Persist result and evidence"])


def _guards(ctx: _Ctx, m: ColumnMapping) -> tuple[list[dict], str | None]:
    """Gap dependencies for a column test: runtime guards where data decides, static block otherwise."""
    guards, blocked = [], None
    tcols = {m.target_column.upper()} if m.target_column else set()
    scols = {s.split(".")[-1].upper() for s in m.source_columns}
    for g in ctx.gaps:
        if g.status not in OPEN:
            continue
        gcols = {c.upper() for c in g.affected.get("columns", []) if c}
        topic = g.affected.get("topic", g.gap_type)
        if not (gcols & (tcols | scols)):
            continue
        if topic == "NULL_HANDLING":
            refs = [s for s in m.source_columns if s.split(".")[-1].upper() in gcols]
            guards.append({"gap_id": g.id, "when": "NULLS_IN_SOURCE", "columns": refs})
        elif topic == "NEGATIVE_VALUES":
            guards.append({"gap_id": g.id, "when": "NEGATIVE_IN_EXPECTED", "column": m.target_column})
        else:
            blocked = f"Blocked by open requirement gap {g.id}: {g.description}"
    return guards, blocked


def generate_actual_tests(db: Session, run: QARun, actor: str) -> list[TestCase]:
    ctx = _Ctx(db, run)
    if not ctx.target:
        raise ValueError("Upload the target dataset first")
    for t in db.scalars(select(TestCase).where(TestCase.run_id == run.id, TestCase.track == "ACTUAL")):
        db.delete(t)
    db.flush()
    out: list[TestCase] = []
    n = 0

    def nid():
        nonlocal n
        n += 1
        return f"TC-A-{n:03d}"

    col_maps = [m for m in ctx.maps if m.target_column and m.mapping_type != "UNMAPPED_TARGET"]
    all_map_reqs = sorted({r for m in col_maps for r in m.requirement_ids})
    out.append(_tc(run.id, nid(), "ACTUAL", "SCHEMA", "Target schema matches the approved transformation output",
                   {"type": "SCHEMA_MATCH", "case_insensitive": True}, ctx=ctx, req_ids=all_map_reqs, map_ids=[m.id for m in col_maps],
                   expected="Same columns and type families as the generated expected target", priority="P1", severity="CRITICAL"))
    out.append(_tc(run.id, nid(), "ACTUAL", "COUNT", "Uploaded target row count equals generated expected row count",
                   {"type": "ROW_COUNT"}, ctx=ctx, req_ids=ctx.join_reqs + ctx.filter_reqs, expected="Equal counts",
                   priority="P1", severity="MAJOR",
                   description="Counts are compared after the approved joins, filters and aggregations (not against raw source counts)."))
    no_key = None if ctx.keys else "No approved business key; record-level reconciliation cannot be performed"
    for kind, title in (("RECON_MISSING", "No expected records are missing from the uploaded target"),
                        ("RECON_EXTRA", "Uploaded target has no records that are not expected")):
        out.append(_tc(run.id, nid(), "ACTUAL", "RECONCILIATION", title, {"type": kind, "keys": ctx.keys}, ctx=ctx,
                       req_ids=ctx.key_reqs + ctx.join_reqs, expected="0 records", priority="P1", severity="CRITICAL",
                       state="BLOCKED" if no_key else "READY", blocked=no_key))
    for r in ctx.reqs.values():
        if r.kind == "UNIQUE_KEY":
            out.append(_tc(run.id, nid(), "ACTUAL", "KEY", f"{', '.join(r.spec['columns'])} is unique in the uploaded target",
                           {"type": "KEY_UNIQUE", "columns": r.spec["columns"], "table": "actual"}, ctx=ctx, req_ids=[r.id],
                           expected="0 duplicate keys", priority="P1", severity="CRITICAL"))
        elif r.kind == "NOT_NULL":
            out.append(_tc(run.id, nid(), "ACTUAL", "KEY", f"{', '.join(r.spec['columns'])} is never null in the uploaded target",
                           {"type": "NOT_NULL", "columns": r.spec["columns"], "table": "actual"}, ctx=ctx, req_ids=[r.id],
                           expected="0 nulls", priority="P1", severity="CRITICAL"))
    key_up = {k.upper() for k in ctx.keys}
    for m in col_maps:
        if m.target_column.upper() in key_up:
            continue
        reqs = list(m.requirement_ids)
        prec = None
        for rid in reqs:
            r = ctx.reqs.get(rid)
            if r and r.spec.get("precision") is not None:
                prec = r.spec["precision"]
        pm = re.search(r"ROUND\(.*,\s*(\d+)\)", m.expression)
        if prec is None and pm:
            prec = int(pm.group(1))
        tol = 0.5 / (10 ** prec) if prec is not None else float((run.settings or {}).get("default_numeric_tolerance", 0.0))
        cat = {"AGGREGATED": "AGGREGATION", "DIRECT": "DIRECT_MAPPING", "RENAMED": "DIRECT_MAPPING",
               "CONDITIONAL": "TRANSFORMATION", "CONCATENATION": "TRANSFORMATION"}.get(m.mapping_type, "TRANSFORMATION")
        if m.mapping_type == "AGGREGATED":
            reqs += ctx.filter_reqs + ctx.join_reqs
        guards, blocked = _guards(ctx, m)
        sc = (run.settings or {}).get("string_compare", {})
        if no_key:
            blocked = no_key
        out.append(_tc(run.id, nid(), "ACTUAL", cat, f"{m.target_column} matches rule: {m.expression[:120]}",
                       {"type": "COLUMN_MATCH", "column": m.target_column, "keys": ctx.keys, "tolerance": tol,
                        "trim": bool(sc.get("trim")), "case_insensitive": bool(sc.get("case_insensitive")), "guards": guards},
                       ctx=ctx, req_ids=reqs, map_ids=[m.id], priority="P1" if m.mapping_type in ("AGGREGATED", "DERIVED") else "P2",
                       expected=f"Uploaded {m.target_column} equals generated expected value per key" + (f" (tolerance ±{tol:g})" if tol else ""),
                       state="BLOCKED" if blocked else "READY", blocked=blocked))
    model = build_model(db, run.id)
    bk = _base_key(model)
    if model.join and model.join["type"] in ("LEFT", "FULL") and bk and len(ctx.keys) == 1:
        out.append(_tc(run.id, nid(), "ACTUAL", "JOIN", f"Every {model.base}.{bk} appears in the target ({model.join['type']} join preserves base records)",
                       {"type": "KEY_COVERAGE", "base": model.base, "base_key": bk, "key": ctx.keys[0], "table": "actual"},
                       ctx=ctx, req_ids=model.join["req"], expected="0 base keys missing", priority="P1", severity="CRITICAL"))
    for r in ctx.reqs.values():
        if r.kind == "DEFAULT_WHEN_UNMATCHED" and model.join and len(ctx.keys) == 1:
            rf = None
            if model.filter and model.filter["alias"] == model.join["right"]:
                rf = {"column": model.filter["column"], "op": model.filter["op"], "value": model.filter["value"]}
            out.append(_tc(run.id, nid(), "ACTUAL", "BUSINESS_RULE", "Unmatched records carry the required default values: " +
                           ", ".join(f"{k}={v}" for k, v in r.spec["defaults"].items()),
                           {"type": "UNMATCHED_DEFAULTS", "base": model.base, "right": model.join["right"],
                            "join_keys": model.join["keys"], "right_filter": rf, "key": ctx.keys[0], "defaults": r.spec["defaults"],
                            "table": "actual"}, ctx=ctx, req_ids=[r.id] + model.join["req"] + (model.filter["req"] if rf else []),
                           expected="0 violations", priority="P2"))
    out.append(_tc(run.id, nid(), "ACTUAL", "DATA_QUALITY", "Uploaded target contains no fully duplicated rows",
                   {"type": "DUPLICATE_ROWS", "table": "actual"}, ctx=ctx, expected="0 duplicate rows", priority="P3", severity="MINOR",
                   rationale="PROFILING: general data-quality check; not derived from a Jira requirement."))
    for t in out:
        db.add(t)
    set_state(db, run, "TEST_CASES_GENERATED", actor)
    audit(db, actor, "TEST_CASES_GENERATED", run.id, "QA_RUN", run.id, track="ACTUAL", count=len(out),
          blocked=[t.id for t in out if t.state == "BLOCKED"])
    return out


def generate_synthetic_tests(db: Session, run: QARun, actor: str) -> list[TestCase]:
    ctx = _Ctx(db, run)
    for t in db.scalars(select(TestCase).where(TestCase.run_id == run.id, TestCase.track == "SYNTHETIC")):
        db.delete(t)
    db.flush()
    scen = list(db.scalars(select(SyntheticScenario).where(SyntheticScenario.run_id == run.id).order_by(SyntheticScenario.id)))
    model = build_model(db, run.id)
    bk = _base_key(model)
    out = []
    for s in scen:
        k = 0
        num = s.id.split("-")[-1]

        def nid():
            nonlocal k
            k += 1
            return f"TC-S{num}-{k:02d}"
        blocked = s.blocked_reason if s.status in ("BLOCKED", "ERROR") else None
        state = "BLOCKED" if blocked else "READY"
        common = dict(ctx=ctx, scenario_id=s.id, state=state, blocked=blocked)
        out.append(_tc(run.id, nid(), "SYNTHETIC", "SYNTHETIC_" + s.scenario_type.split(":")[0],
                       f"[{s.name}] re-execution reproduces the approved expected target",
                       {"type": "SNAPSHOT_MATCH", "keys": ctx.keys, "tolerance": 0}, req_ids=s.requirement_ids,
                       expected="Identical to approved synthetic expected target", description=s.description, **common))
        for r in ctx.reqs.values():
            if r.kind == "UNIQUE_KEY":
                out.append(_tc(run.id, nid(), "SYNTHETIC", "KEY", f"[{s.name}] {', '.join(r.spec['columns'])} unique in output",
                               {"type": "KEY_UNIQUE", "columns": r.spec["columns"], "table": "actual"}, req_ids=[r.id],
                               expected="0 duplicate keys", severity="CRITICAL", **common))
            elif r.kind == "NOT_NULL":
                out.append(_tc(run.id, nid(), "SYNTHETIC", "KEY", f"[{s.name}] {', '.join(r.spec['columns'])} never null in output",
                               {"type": "NOT_NULL", "columns": r.spec["columns"], "table": "actual"}, req_ids=[r.id],
                               expected="0 nulls", severity="CRITICAL", **common))
            elif r.kind == "DEFAULT_WHEN_UNMATCHED" and model.join and len(ctx.keys) == 1:
                rf = {"column": model.filter["column"], "op": model.filter["op"], "value": model.filter["value"]} \
                    if model.filter and model.filter["alias"] == model.join["right"] else None
                out.append(_tc(run.id, nid(), "SYNTHETIC", "BUSINESS_RULE", f"[{s.name}] unmatched records carry defaults",
                               {"type": "UNMATCHED_DEFAULTS", "base": model.base, "right": model.join["right"],
                                "join_keys": model.join["keys"], "right_filter": rf, "key": ctx.keys[0],
                                "defaults": r.spec["defaults"], "table": "actual"}, req_ids=[r.id], expected="0 violations", **common))
        if model.join and model.join["type"] in ("LEFT", "FULL") and bk and len(ctx.keys) == 1:
            out.append(_tc(run.id, nid(), "SYNTHETIC", "JOIN", f"[{s.name}] every non-null {model.base}.{bk} appears in output",
                           {"type": "KEY_COVERAGE", "base": model.base, "base_key": bk, "key": ctx.keys[0], "table": "actual"},
                           req_ids=model.join["req"], expected="0 base keys missing", **common))
    for t in out:
        db.add(t)
    audit(db, actor, "TEST_CASES_GENERATED", run.id, "QA_RUN", run.id, track="SYNTHETIC", count=len(out))
    return out
