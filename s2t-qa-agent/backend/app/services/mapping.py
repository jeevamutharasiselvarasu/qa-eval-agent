"""MappingService: evidence-based source-to-target mapping proposals and the draft transformation they imply.

Explicit Jira/attachment rules -> JIRA_EXPLICIT / ATTACHMENT_DERIVED mappings (ACCEPTED, still subject to bundle
approval). Name/type similarity alone -> AI_INFERRED (PROPOSED, LOW confidence, must be confirmed).
"""
from __future__ import annotations

import re

import sqlglot
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core.common import audit
from ..models import ColumnMapping, Dataset, Requirement, RequirementGap
from .requirements import columns_in, normalize_expr
from .transform.visual import sql_to_expr

NUMERIC_HINT = ("int", "double", "decimal", "float")


def _family(t: str) -> str:
    t = (t or "").lower()
    if any(h in t for h in NUMERIC_HINT):
        return "numeric"
    if "date" in t or "time" in t:
        return "temporal"
    if "bool" in t:
        return "boolean"
    return "string"


class Ctx:
    def __init__(self, db: Session, run_id: str):
        self.sources = list(db.scalars(select(Dataset).where(Dataset.run_id == run_id, Dataset.role == "SOURCE",
                                                             Dataset.feeds_target.is_(True))))
        self.target = db.scalars(select(Dataset).where(Dataset.run_id == run_id, Dataset.role == "TARGET")).first()
        self.reqs = [r for r in db.scalars(select(Requirement).where(Requirement.run_id == run_id)) if r.status != "REJECTED"]
        self.gaps = list(db.scalars(select(RequirementGap).where(RequirementGap.run_id == run_id)))
        self.cols = {d.alias: [c["name"] for c in d.columns] for d in self.sources}
        self.types = {(d.alias, c["name"].upper()): c["type"] for d in self.sources for c in d.columns}

    def match_alias(self, hint: str | None) -> str | None:
        if not hint:
            return None
        h = re.sub(r"[^a-z0-9]+", "_", hint.lower()).strip("_")
        for a in self.cols:
            if a.lower() == h:
                return a
        for a in self.cols:
            if a.lower() in h or h in a.lower() or a.lower().rstrip("s") == h.rstrip("s"):
                return a
        return None

    def resolve(self, column: str, hint: str | None = None) -> tuple[str | None, str]:
        """Return (alias, note)."""
        cands = [a for a, cs in self.cols.items() if column.upper() in (c.upper() for c in cs)]
        h = self.match_alias(hint)
        if h and h in cands:
            return h, ""
        if len(cands) == 1:
            return cands[0], ""
        if not cands:
            return None, f"Column {column} not found in any source dataset"
        return None, f"Column {column} exists in multiple sources ({', '.join(cands)}); confirm which applies"

    def real(self, alias: str, column: str) -> str:
        return next((c for c in self.cols.get(alias, []) if c.upper() == column.upper()), column)


def qualify_expression(expr: str, ctx: Ctx, hint: str | None) -> tuple[str, list[str], list[str]]:
    """Qualify bare columns with dataset aliases. Returns (expr, source_columns, notes)."""
    notes: list[str] = []
    srcs: list[str] = []
    try:
        tree = sqlglot.parse_one(expr, read="duckdb")
    except Exception as e:  # noqa: BLE001
        return expr, srcs, [f"Expression does not parse: {e}"]
    for col in tree.find_all(sqlglot.exp.Column):
        h = col.table or hint
        alias, note = ctx.resolve(col.name, h)
        if alias:
            col.set("table", sqlglot.exp.to_identifier(alias))
            col.set("this", sqlglot.exp.to_identifier(ctx.real(alias, col.name)))
            srcs.append(f"{alias}.{ctx.real(alias, col.name)}")
        elif note:
            notes.append(note)
    return tree.sql("duckdb"), sorted(set(srcs)), notes


def propose(db: Session, run_id: str, actor: str) -> list[ColumnMapping]:
    ctx = Ctx(db, run_id)
    for m in db.scalars(select(ColumnMapping).where(ColumnMapping.run_id == run_id)):
        db.delete(m)
    db.flush()
    out: list[ColumnMapping] = []
    key_cols = {c.upper() for r in ctx.reqs if r.kind in ("BUSINESS_KEY",) for c in r.spec.get("columns", [])}
    if not key_cols:
        key_cols = {c.upper() for r in ctx.reqs if r.kind == "UNIQUE_KEY" for c in r.spec.get("columns", [])}
    defaults = {}
    for r in ctx.reqs:
        if r.kind == "DEFAULT_WHEN_UNMATCHED":
            for k, v in r.spec.get("defaults", {}).items():
                defaults[k.upper()] = (v, r.id)

    target_cols = [c["name"] for c in (ctx.target.columns if ctx.target else [])]
    rule_targets = []
    for r in ctx.reqs:
        t = r.spec.get("target")
        if t and r.kind in ("DERIVED_COLUMN", "DIRECT_MAPPING", "MAPPING_SPEC") and t.upper() not in [x.upper() for x in target_cols + rule_targets]:
            rule_targets.append(t)
    all_targets = target_cols + rule_targets
    used_sources: set[str] = set()
    n = 0

    def nid():
        nonlocal n
        n += 1
        return f"MAP-{n:03d}"

    def gap_notes(cols: list[str]) -> list[str]:
        cu = {c.upper().split(".")[-1] for c in cols}
        return [f"{g.id} ({g.status}): {g.description}" for g in ctx.gaps
                if g.status in ("OPEN", "BLOCKED") and cu & {c.upper() for c in g.affected.get("columns", []) if c}]

    for tgt in all_targets:
        T = tgt.upper()
        ac = [r for r in ctx.reqs if r.kind in ("DERIVED_COLUMN", "DIRECT_MAPPING") and (r.spec.get("target") or "").upper() == T and r.provenance != "AI_INFERRED"]
        att = [r for r in ctx.reqs if r.kind == "MAPPING_SPEC" and (r.spec.get("target") or "").upper() == T]
        notes: list[str] = []
        expr, srcs, mtype, prov, conf, status, evidence, req_ids = "", [], "", "", "HIGH", "ACCEPTED", [], []
        if ac:
            r = ac[0]
            req_ids.append(r.id)
            evidence.append(f"{r.source_ref}: {r.evidence}")
            if r.kind == "DIRECT_MAPPING":
                raw = f"{r.spec['source_dataset']}.{r.spec['source_column']}"
                hint = r.spec["source_dataset"]
            else:
                raw = r.spec["expression"]
                if r.spec.get("precision") is not None and not raw.upper().startswith("ROUND("):
                    raw = f"ROUND({raw}, {r.spec['precision']})"
                hint = None
                if att:
                    hint = att[0].spec.get("source_dataset")
            expr, srcs, notes = qualify_expression(raw, ctx, hint)
            prov = "JIRA_EXPLICIT"
            for a in att:
                req_ids.append(a.id)
                evidence.append(f"{a.source_ref}: {a.description}")
        elif att:
            a = att[0]
            req_ids.append(a.id)
            evidence.append(f"{a.source_ref}: {a.description}")
            raw = a.spec.get("expression") or f"{a.spec['source_dataset']}.{a.spec['source_columns'][0]}"
            expr, srcs, notes = qualify_expression(raw, ctx, a.spec.get("source_dataset"))
            prov = "ATTACHMENT_DERIVED"
        elif grp := [r for r in ctx.reqs if r.kind == "DERIVED_COLUMN" and r.provenance != "AI_INFERRED"
                     and T in [g.upper() for g in r.spec.get("group_by", [])]]:
            # grouping key named in a Jira rule ("... per SALE_DATE, PRODUCT_CODE") is an explicit target dimension;
            # take it from the dataset that the aggregated columns come from
            hint_alias = next((ctx.resolve(c)[0] for c in grp[0].spec.get("columns", []) if ctx.resolve(c)[0]), None)
            alias, note = ctx.resolve(tgt, hint_alias)
            req_ids += [r.id for r in grp]
            evidence += [f"{r.source_ref}: grouping key in '{r.evidence}'" for r in grp]
            prov = "JIRA_EXPLICIT"
            if alias:
                expr = f"{alias}.{ctx.real(alias, tgt)}"
                srcs = [expr]
            else:
                notes.append(note)
                m = ColumnMapping(id=nid(), run_id=run_id, source_columns=[], target_column=tgt, mapping_type="UNMAPPED_TARGET",
                                  expression="", requirement_ids=req_ids, evidence=" | ".join(evidence), provenance=prov,
                                  confidence="MEDIUM", status="PROPOSED", is_key=T in key_cols, notes="; ".join(notes))
                db.add(m)
                out.append(m)
                continue
        else:
            # similarity-only proposal: never auto-accepted
            alias, note = ctx.resolve(tgt)
            tt = next((c["type"] for c in (ctx.target.columns if ctx.target else []) if c["name"].upper() == T), "")
            if alias and _family(ctx.types.get((alias, T), "")) == _family(tt):
                expr = f"{alias}.{ctx.real(alias, tgt)}"
                srcs = [expr]
                prov, conf, status = "AI_INFERRED", "LOW", "PROPOSED"
                mtype = "DIRECT"
                notes.append("Proposed from matching column name and compatible type only; no Jira evidence. Confirm or reject.")
            else:
                if note:
                    notes.append(note)
                m = ColumnMapping(id=nid(), run_id=run_id, source_columns=[], target_column=tgt, mapping_type="UNMAPPED_TARGET",
                                  expression="", requirement_ids=[], evidence="No rule or matching source column found",
                                  provenance="AI_INFERRED", confidence="LOW", status="PROPOSED", is_key=T in key_cols,
                                  notes="; ".join(notes + ["Target column has no mapping rule; clarify in Jira."]))
                db.add(m)
                out.append(m)
                continue
        if T in defaults and expr:
            v, rid = defaults[T]
            expr = f"COALESCE({expr}, {v!r})" if isinstance(v, str) else f"COALESCE({expr}, {v})"
            req_ids.append(rid)
        if not mtype:
            try:
                tree = sqlglot.parse_one(expr, read="duckdb")
                is_col = isinstance(tree, sqlglot.exp.Column)
                agg = any(isinstance(x, sqlglot.exp.AggFunc) for x in tree.walk())
                cond = any(isinstance(x, sqlglot.exp.Case) for x in tree.walk())
                concat = any(isinstance(x, (sqlglot.exp.Concat, sqlglot.exp.DPipe)) for x in tree.walk())
                const = not list(tree.find_all(sqlglot.exp.Column))
            except Exception:  # noqa: BLE001
                is_col = agg = cond = concat = const = False
            if is_col:
                mtype = "DIRECT" if tree.name.upper() == T else "RENAMED"
            elif agg:
                mtype = "AGGREGATED"
            elif cond:
                mtype = "CONDITIONAL"
            elif concat:
                mtype = "CONCATENATION"
            elif const:
                mtype = "CONSTANT"
            else:
                mtype = "DERIVED"
        if notes and any("multiple sources" in x or "not found" in x for x in notes):
            status, conf = "PROPOSED", "MEDIUM"
        notes += gap_notes(srcs + [tgt])
        used_sources.update(s.upper() for s in srcs)
        m = ColumnMapping(id=nid(), run_id=run_id, source_columns=srcs, target_column=tgt, mapping_type=mtype,
                          expression=expr, requirement_ids=req_ids, evidence=" | ".join(evidence) or "Name/type similarity",
                          provenance=prov, confidence=conf, status=status, is_key=T in key_cols, notes="; ".join(notes))
        db.add(m)
        out.append(m)

    for r in ctx.reqs:
        if r.kind == "JOIN":
            l, rr = ctx.match_alias(r.spec.get("left")), ctx.match_alias(r.spec.get("right"))
            keys = r.spec.get("keys", [])
            note = "" if (l and rr) else f"Could not match datasets '{r.spec.get('left')}'/'{r.spec.get('right')}' to uploaded sources"
            m = ColumnMapping(id=nid(), run_id=run_id, source_columns=[f"{l}.{k}" for k in keys] + [f"{rr}.{k}" for k in keys],
                              target_column=None, mapping_type="JOINED",
                              expression=f"{r.spec.get('type')} JOIN {rr} ON " + " AND ".join(f"{l}.{k} = {rr}.{k}" for k in keys),
                              requirement_ids=[r.id], evidence=f"{r.source_ref}: {r.evidence}", provenance=r.provenance,
                              confidence="HIGH" if not note else "MEDIUM", status="ACCEPTED" if not note else "PROPOSED", notes=note)
            used_sources.update(s.upper() for s in m.source_columns)
            db.add(m)
            out.append(m)
        elif r.kind == "FILTER":
            alias, note = ctx.resolve(r.spec["column"], r.spec.get("dataset_hint"))
            val = r.spec["value"]
            lit = f"'{val}'" if isinstance(val, str) and not re.fullmatch(r"-?\d+(\.\d+)?", val) else str(val)
            m = ColumnMapping(id=nid(), run_id=run_id, source_columns=[f"{alias}.{r.spec['column']}"] if alias else [],
                              target_column=None, mapping_type="FILTERED",
                              expression=f"{alias or '?'}.{r.spec['column']} {r.spec['op']} {lit}", requirement_ids=[r.id],
                              evidence=f"{r.source_ref}: {r.evidence}", provenance=r.provenance, confidence="HIGH" if alias else "MEDIUM",
                              status="ACCEPTED" if alias else "PROPOSED", notes=note)
            used_sources.update(s.upper() for s in m.source_columns)
            db.add(m)
            out.append(m)

    for d in ctx.sources:
        for c in d.columns:
            ref = f"{d.alias}.{c['name']}"
            if ref.upper() not in used_sources:
                m = ColumnMapping(id=nid(), run_id=run_id, source_columns=[ref], target_column=None, mapping_type="UNMAPPED_SOURCE",
                                  expression="", requirement_ids=[], evidence="Not referenced by any rule",
                                  provenance="DETERMINISTIC_CHECK", confidence="HIGH", status="ACCEPTED",
                                  notes="Source column not used by the target (informational).")
                db.add(m)
                out.append(m)
    audit(db, actor, "MAPPINGS_GENERATED", run_id, "QA_RUN", run_id, count=len(out))
    return out


def draft_visual_spec(db: Session, run_id: str) -> tuple[dict, list[str]]:
    """Build a visual definition from ACCEPTED mappings. Returns (spec, assumptions)."""
    ctx = Ctx(db, run_id)
    maps = [m for m in db.scalars(select(ColumnMapping).where(ColumnMapping.run_id == run_id)) if m.status == "ACCEPTED"]
    assumptions: list[str] = []
    joins = [m for m in maps if m.mapping_type == "JOINED"]
    filters = [m for m in maps if m.mapping_type == "FILTERED"]
    cols = [m for m in maps if m.target_column and m.mapping_type not in ("UNMAPPED_TARGET",)]
    used = {s.split(".")[0] for m in cols + joins + filters for s in m.source_columns}
    aliases = [a for a in ctx.cols if a in used] or list(ctx.cols)[:1]
    base = aliases[0]
    join_specs = []
    right_of_left: set[str] = set()
    for j in joins:
        mm = re.match(r"(\w+) JOIN (\w+) ON (.+)$", j.expression)
        if not mm:
            continue
        jt, right, on = mm.groups()
        pairs = [p.strip().split(" = ") for p in on.split(" AND ")]
        left_alias = pairs[0][0].split(".")[0]
        base = left_alias if left_alias in ctx.cols else base
        join_specs.append({"right": right, "type": jt, "on": [{"left": a, "right": b} for a, b in pairs]})
        if jt == "LEFT":
            right_of_left.add(right)
    ordered = [base] + [a for a in aliases if a != base]
    sources = [{"alias": a, "as": a, "filters": []} for a in ordered]
    for a in ordered[1:]:
        if not any(j["right"] == a for j in join_specs):
            assumptions.append(f"No approved join rule for '{a}'; it is excluded from the draft.")
    sources = [s for s in sources if s["alias"] == base or any(j["right"] == s["alias"] for j in join_specs)]
    post_filters = []
    for f in filters:
        mm = re.match(r"(\w+)\.(\w+)\s*(=|!=|<>|>=|<=|>|<)\s*(.+)$", f.expression)
        if not mm:
            continue
        a, col, op, val = mm.groups()
        v = val.strip("'") if val.startswith("'") else (float(val) if "." in val else int(val))
        cond = {"column": f"{a}.{col}", "op": op, "value": v}
        src = next((s for s in sources if s["alias"] == a), None)
        if src is not None:
            src["filters"].append(cond)
            if a in right_of_left:
                assumptions.append(f"Filter {f.expression} ({', '.join(f.requirement_ids)}) is applied to '{a}' BEFORE the "
                                   f"LEFT JOIN so unmatched base records are retained.")
        else:
            post_filters.append(cond)
    target_order = [c["name"].upper() for c in (ctx.target.columns if ctx.target else [])]
    cols.sort(key=lambda m: target_order.index(m.target_column.upper()) if m.target_column.upper() in target_order else 999)
    qual = {}
    for a in [s["alias"] for s in sources]:
        for c in ctx.cols[a]:
            qual.setdefault(c.upper(), a)
    columns = [{"target": m.target_column, "expr": sql_to_expr(m.expression, qual), "mapping_id": m.id} for m in cols]
    spec = {"sources": sources, "joins": join_specs, "filters": post_filters, "columns": columns}
    if target_order:
        missing = [c for c in target_order if c not in [m.target_column.upper() for m in cols]]
        if missing:
            assumptions.append(f"Target columns without accepted mappings are not produced: {', '.join(missing)}")
    return spec, assumptions
