"""Target specification: requirements expressed per TARGET column (how each one is built from the sources) plus the
table-level rules (business key, joins, filters, defaults for unmatched records).

Workflow:
* The Jira analysis and mapping proposal fill the spec automatically.
* Anything missing or unclear (no rule, an unconfirmed suggestion, an open question, no key, no join between several
  sources) is an *open item*. The user edits the requirement in the app; every edit is stored as a USER_CONFIRMED
  requirement that supersedes the Jira-derived one for the same target column or table rule and survives Jira re-retrieval.
* When there are no open items the SQL transformation is generated from the requirements automatically
  (``sync_sql``) and the test pack is built. Until then SQL generation is refused.
"""

from __future__ import annotations

import re

import sqlglot
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core.common import audit, record_error, set_state, sha256_obj
from ..models import ColumnMapping, Dataset, QARun, Requirement, RequirementGap
from . import approvals as approval_svc
from . import mapping as mapping_svc
from .requirements import columns_in

OPEN = ("OPEN", "BLOCKED")
COLUMN_KINDS = ("DERIVED_COLUMN", "DIRECT_MAPPING", "MAPPING_SPEC")
# a user-confirmed rule of the key kind supersedes every Jira rule of the kinds listed
# (NO_JOIN / NO_FILTER / NO_DEFAULTS record an explicit "none" so the Jira rule of that kind stops applying)
TABLE_SUPERSEDES = {
    "BUSINESS_KEY": ("BUSINESS_KEY", "UNIQUE_KEY"),
    "JOIN": ("JOIN",),
    "NO_JOIN": ("JOIN",),
    "FILTER": ("FILTER",),
    "NO_FILTER": ("FILTER",),
    "DEFAULT_WHEN_UNMATCHED": ("DEFAULT_WHEN_UNMATCHED",),
    "NO_DEFAULTS": ("DEFAULT_WHEN_UNMATCHED",),
}
EDIT_SECTIONS = {
    "BUSINESS_KEY": ("BUSINESS_KEY",),
    "JOIN": ("JOIN", "NO_JOIN"),
    "FILTER": ("FILTER", "NO_FILTER"),
    "DEFAULT_WHEN_UNMATCHED": ("DEFAULT_WHEN_UNMATCHED", "NO_DEFAULTS"),
}
OPS = ("=", "!=", "<>", ">", ">=", "<", "<=")
JOIN_TYPES = ("INNER", "LEFT", "RIGHT", "FULL")


class SpecError(ValueError):
    pass


# ------------------------------------------------------------------ helpers
def _reqs(db: Session, run_id: str) -> list[Requirement]:
    return list(db.scalars(select(Requirement).where(Requirement.run_id == run_id).order_by(Requirement.id)))


def _next_req_id(db: Session, run_id: str) -> str:
    nums = [int(m.group(1)) for r in _reqs(db, run_id) if (m := re.match(r"REQ-(\d+)$", r.id))]
    return f"REQ-{(max(nums) if nums else 0) + 1:03d}"


def _target(db: Session, run_id: str) -> Dataset | None:
    return db.scalars(select(Dataset).where(Dataset.run_id == run_id, Dataset.role == "TARGET")).first()


def _sources(db: Session, run_id: str) -> list[Dataset]:
    return list(db.scalars(select(Dataset).where(Dataset.run_id == run_id, Dataset.role == "SOURCE", Dataset.feeds_target.is_(True))))


def _lit(v) -> str:
    if isinstance(v, str) and not re.fullmatch(r"-?\d+(\.\d+)?", v):
        return "'" + v.replace("'", "''") + "'"
    return str(v)


def _defaults(reqs: list[Requirement]) -> dict[str, object]:
    out = {}
    for r in reqs:
        if r.kind == "DEFAULT_WHEN_UNMATCHED" and r.status == "ACCEPTED":
            out.update({k.upper(): v for k, v in r.spec.get("defaults", {}).items()})
    return out


def _strip_default(expr: str, default) -> str:
    """propose() wraps a column that has an 'unmatched default' as COALESCE(<rule>, <default>); show the rule itself."""
    suffix = f", {default!r})" if isinstance(default, str) else f", {default})"
    if default is not None and expr.startswith("COALESCE(") and expr.endswith(suffix):
        return expr[len("COALESCE(") : -len(suffix)]
    return expr


def _summary(mtype: str, expr: str, srcs: list[str], group_by: list[str]) -> str:
    per = f", per {', '.join(group_by)}" if group_by else ""
    if mtype == "DIRECT":
        return f"Copied as-is from {srcs[0]}" if srcs else "Copied as-is"
    if mtype == "RENAMED":
        return f"Copied from {srcs[0]} (renamed)" if srcs else "Copied (renamed)"
    if mtype == "AGGREGATED":
        return f"Aggregated from {', '.join(srcs)}{per}"
    if mtype == "CONSTANT":
        return "Constant value"
    if mtype == "CONDITIONAL":
        return f"Conditional (CASE) on {', '.join(srcs)}"
    if mtype == "CONCATENATION":
        return f"Concatenation of {', '.join(srcs)}"
    return f"Calculated from {', '.join(srcs)}" if srcs else "Calculated"


# ------------------------------------------------------------------ overrides
def apply_overrides(db: Session, run_id: str, actor: str = "system") -> None:
    """User-confirmed requirements supersede the Jira-derived ones for the same target column / table rule, and
    resolve the structural questions they answer. Called after every analysis and every edit."""
    reqs = _reqs(db, run_id)
    user = [r for r in reqs if r.provenance == "USER_CONFIRMED" and r.status == "ACCEPTED"]
    user_targets = {(r.spec.get("target") or "").upper() for r in user if r.kind in COLUMN_KINDS} - {""}
    superseded_kinds = {k for r in user for k in TABLE_SUPERSEDES.get(r.kind, ())}
    for r in reqs:
        if r.provenance in ("USER_CONFIRMED", "AI_INFERRED") or r.status not in ("ACCEPTED", "SUPERSEDED"):
            continue
        hit = (r.kind in COLUMN_KINDS and (r.spec.get("target") or "").upper() in user_targets) or r.kind in superseded_kinds
        r.status = "SUPERSEDED" if hit else "ACCEPTED"
    kinds = {r.kind for r in user}
    for g in db.scalars(select(RequirementGap).where(RequirementGap.run_id == run_id, RequirementGap.status == "OPEN")):
        cols = {c.upper() for c in g.affected.get("columns", []) if c}
        why = None
        if g.gap_type == "NO_BUSINESS_KEY" and "BUSINESS_KEY" in kinds:
            why = "the business key was set on the Requirements tab"
        elif g.gap_type == "JOIN_TYPE" and not cols and "JOIN" in kinds:
            why = "the join was set on the Requirements tab"
        elif g.gap_type in ("PRECISION", "CONFLICT") and cols & user_targets:
            why = f"the rule for {', '.join(sorted(cols & user_targets))} was confirmed on the Requirements tab"
        if why:
            g.status, g.updated_by = "CLARIFIED", actor
            g.clarification = f"Resolved automatically: {why}."
    db.flush()


def refresh(db: Session, run: QARun, actor: str) -> None:
    """Re-derive mappings from the effective requirements (only once both sources and requirements exist)."""
    apply_overrides(db, run.id, actor)
    if _sources(db, run.id) and _reqs(db, run.id):
        mapping_svc.propose(db, run.id, actor)
        db.flush()
    from .runs import _gap_state

    _gap_state(db, run, actor)


# ------------------------------------------------------------------ the spec view
def build_spec(db: Session, run: QARun) -> dict:
    reqs = _reqs(db, run.id)
    gaps = list(db.scalars(select(RequirementGap).where(RequirementGap.run_id == run.id).order_by(RequirementGap.id)))
    maps = list(db.scalars(select(ColumnMapping).where(ColumnMapping.run_id == run.id).order_by(ColumnMapping.id)))
    target, sources = _target(db, run.id), _sources(db, run.id)
    req_by_id = {r.id: r for r in reqs}
    defaults = _defaults(reqs)
    open_gaps = [g for g in gaps if g.status in OPEN]
    accepted = [r for r in reqs if r.status == "ACCEPTED"]

    key_req = next((r for r in accepted if r.kind == "BUSINESS_KEY"), None) or next((r for r in accepted if r.kind == "UNIQUE_KEY"), None)
    key_cols = [c for c in (key_req.spec.get("columns", []) if key_req else [])]
    not_null = sorted({c for r in accepted if r.kind == "NOT_NULL" for c in r.spec.get("columns", [])})

    tcols = [(c["name"], c["type"]) for c in (target.columns if target else [])]
    names = {n.upper() for n, _ in tcols}
    for m in maps:  # rule targets that are not in the uploaded target file
        if m.target_column and m.target_column.upper() not in names:
            tcols.append((m.target_column, ""))
            names.add(m.target_column.upper())
    by_target = {m.target_column.upper(): m for m in maps if m.target_column}
    claimed: set[str] = set()
    columns, open_items = [], []
    for name, typ in tcols:
        T = name.upper()
        m = by_target.get(T)
        gov = None
        if m:
            gov = next((req_by_id[i] for i in m.requirement_ids if i in req_by_id and req_by_id[i].kind in COLUMN_KINDS), None)
        srcs = list(m.source_columns) if m else []
        bare = {s.split(".")[-1].upper() for s in srcs} | {T}
        related = [g for g in open_gaps if bare & {c.upper() for c in g.affected.get("columns", []) if c}]
        claimed.update(g.id for g in related)
        expr = m.expression if m else ""
        rule = _strip_default(expr, defaults.get(T)) if expr else ""
        group_by = (gov.spec.get("group_by") or []) if gov else []
        notes = [n for n in (m.notes.split("; ") if m and m.notes else []) if n and not re.match(r"GAP-\w+ \(", n)]
        problem = [n for n in notes if re.search(r"not found|multiple sources|does not parse|no mapping rule", n, re.I)]
        if not m or m.mapping_type == "UNMAPPED_TARGET" or not expr:
            state, msg = "MISSING", "No rule says how this column is built"
        elif problem:
            state, msg = "INVALID", problem[0]
        elif m.status != "ACCEPTED":
            state, msg = "SUGGESTED", "Suggested from a matching source column name only: confirm or change it"
        elif related:
            state, msg = "QUESTION", f"{len(related)} open question(s) affect this column"
        else:
            state, msg = "CLEAR", ""
        ai_notes = [
            r.description
            for r in reqs
            if r.provenance == "AI_INFERRED" and r.status == "PROPOSED" and (r.spec.get("target") or "").upper() == T
        ]
        jira_rule = next(
            (r for r in reqs if r.status == "SUPERSEDED" and r.kind in COLUMN_KINDS and (r.spec.get("target") or "").upper() == T), None
        )
        col = {
            "target": name,
            "type": typ,
            "in_target_file": bool(target and T in {c["name"].upper() for c in target.columns}),
            "is_key": T in {k.upper() for k in key_cols},
            "not_null": T in {c.upper() for c in not_null},
            "source_columns": srcs,
            "rule": rule,
            "expression": expr,
            "mapping_type": m.mapping_type if m else "UNMAPPED_TARGET",
            "summary": _summary(m.mapping_type, expr, srcs, group_by) if m and expr else "",
            "group_by": group_by,
            "default_when_unmatched": defaults.get(T),
            "origin": (
                "EDITED"
                if gov and gov.provenance == "USER_CONFIRMED"
                else "JIRA"
                if m and m.provenance in ("JIRA_EXPLICIT", "ATTACHMENT_DERIVED")
                else "SUGGESTED"
                if m and expr
                else "NONE"
            ),
            "evidence": (m.evidence if m else ""),
            "requirement_id": gov.id if gov else None,
            "ac_id": gov.ac_id if gov else None,
            "jira_rule": jira_rule.description if jira_rule else None,
            "state": state,
            "message": msg,
            "notes": [n for n in notes if n not in problem],
            "ai_notes": ai_notes,
            "open_questions": [{"id": g.id, "description": g.description, "gap_type": g.gap_type} for g in related],
        }
        columns.append(col)
        if state != "CLEAR":
            open_items.append({"kind": "COLUMN", "ref": name, "state": state, "message": msg, "action": "edit_column"})

    # table-level rules
    joins = [
        {"id": m.id, "expression": m.expression, "status": m.status, "notes": m.notes, "requirement_ids": m.requirement_ids}
        for m in maps
        if m.mapping_type == "JOINED"
    ]
    filters = [
        {"id": m.id, "expression": m.expression, "status": m.status, "notes": m.notes, "requirement_ids": m.requirement_ids}
        for m in maps
        if m.mapping_type == "FILTERED"
    ]

    def raw(kind):
        return [{"id": r.id, "spec": r.spec, "description": r.description, "provenance": r.provenance} for r in accepted if r.kind == kind]

    table = {
        "key": key_cols,
        "key_requirement": key_req.id if key_req else None,
        "key_origin": ("EDITED" if key_req and key_req.provenance == "USER_CONFIRMED" else "JIRA" if key_req else None),
        "not_null": not_null,
        "joins": joins,
        "filters": filters,
        "defaults": {k: v for r in accepted if r.kind == "DEFAULT_WHEN_UNMATCHED" for k, v in r.spec.get("defaults", {}).items()},
        "rounding": next((r.spec.get("mode") for r in accepted if r.kind == "ROUNDING_MODE"), None),
        "join_specs": [r["spec"] for r in raw("JOIN")],
        "filter_specs": [r["spec"] for r in raw("FILTER")],
    }
    if not key_cols:
        open_items.append(
            {
                "kind": "TABLE",
                "ref": "Business key",
                "state": "MISSING",
                "message": "No business key: say which target column(s) identify a record",
                "action": "edit_table",
            }
        )
    used = {s.split(".")[0] for c in columns for s in c["source_columns"]}
    joined = {a for j in joins if j["status"] == "ACCEPTED" for a in re.findall(r"\b(\w+)\.\w+", j["expression"])}
    if len(used) > 1:
        missing = sorted(used - joined) if joined else sorted(used)
        if not joined or (missing and len(joined) < len(used)):
            open_items.append(
                {
                    "kind": "TABLE",
                    "ref": "Join",
                    "state": "MISSING",
                    "message": f"Columns come from {len(used)} sources ({', '.join(sorted(used))}) but no join "
                    f"connects {', '.join(missing) if joined else 'them'}",
                    "action": "edit_table",
                }
            )
    for x in joins + filters:
        if x["status"] != "ACCEPTED":
            open_items.append(
                {
                    "kind": "TABLE",
                    "ref": x["expression"],
                    "state": "INVALID",
                    "message": x["notes"] or "Rule could not be matched to the uploaded sources",
                    "action": "edit_table",
                }
            )
    for g in open_gaps:
        if g.id not in claimed:
            open_items.append({"kind": "QUESTION", "ref": g.id, "state": "QUESTION", "message": g.description, "action": "answer_gap"})
    if not target:
        open_items.insert(
            0, {"kind": "DATA", "ref": "Target file", "state": "MISSING", "message": "Upload the target file", "action": "data"}
        )
    if not sources:
        open_items.insert(
            0, {"kind": "DATA", "ref": "Source files", "state": "MISSING", "message": "Upload at least one source file", "action": "data"}
        )

    other_notes = [
        {"id": r.id, "description": r.description, "evidence": r.evidence}
        for r in reqs
        if r.provenance == "AI_INFERRED" and r.status == "PROPOSED" and not r.spec.get("target")
    ]
    ready = not open_items
    return {
        "ready": ready,
        "open_items": open_items,
        "columns": columns,
        "table": table,
        "questions": [
            {
                "id": g.id,
                "gap_type": g.gap_type,
                "description": g.description,
                "detail": g.detail,
                "evidence": g.evidence,
                "status": g.status,
                "clarification": g.clarification,
                "columns": g.affected.get("columns", []),
                "updated_by": g.updated_by,
            }
            for g in gaps
        ],
        "other_notes": other_notes,
        "sources": [{"alias": d.alias, "columns": [c["name"] for c in d.columns]} for d in sources],
        "sql": sql_state(db, run, ready),
    }


# ------------------------------------------------------------------ SQL generation
def requirements_basis(db: Session, run_id: str) -> str:
    maps = [
        [m.target_column, m.mapping_type, m.expression]
        for m in db.scalars(
            select(ColumnMapping)
            .where(ColumnMapping.run_id == run_id, ColumnMapping.status == "ACCEPTED", ColumnMapping.mapping_type != "UNMAPPED_SOURCE")
            .order_by(ColumnMapping.id)
        )
    ]
    ds = sorted([d.alias, d.fingerprint] for d in _sources(db, run_id))
    return sha256_obj({"maps": maps, "sources": ds})


def sql_state(db: Session, run: QARun, ready: bool) -> dict:
    v = approval_svc.current_version(db, run.id)
    if not v:
        return {"state": "READY_TO_GENERATE" if ready else "WAITING_FOR_REQUIREMENTS", "version": None}
    outdated = v.requirements_basis != requirements_basis(db, run.id)
    generated = v.origin == "AI_GENERATED" and v.requirements_basis is not None
    state = (
        "CURRENT" if not outdated else "WAITING_FOR_REQUIREMENTS" if not ready else "WILL_REGENERATE" if generated else "MANUAL_OUTDATED"
    )
    return {"state": state, "version": v.version, "origin": v.origin, "generated_from_requirements": generated, "outdated": outdated}


def sync_sql(db: Session, run: QARun, actor: str, force: bool = False) -> dict:
    """If every requirement is clear, (re)generate the SQL from them and build the test pack.

    A version that a person wrote or edited is never replaced automatically; ``force`` regenerates it on request."""
    from . import studio, testpack

    spec = build_spec(db, run)
    if not spec["ready"]:
        if force:
            raise SpecError(
                "The requirements are not complete yet: " + "; ".join(f"{i['ref']}: {i['message']}" for i in spec["open_items"][:6])
            )
        return {"generated": False, "ready": False, "open_items": spec["open_items"]}
    cur = approval_svc.current_version(db, run.id)
    basis = requirements_basis(db, run.id)
    if cur and not force:
        if cur.requirements_basis == basis:
            return {"generated": False, "ready": True, "version": cur.version, "reason": "SQL is already up to date"}
        if not (cur.origin == "AI_GENERATED" and cur.requirements_basis is not None):
            return {
                "generated": False,
                "ready": True,
                "version": cur.version,
                "reason": "The current SQL was written or edited by a person; regenerate it on the Transformation tab",
            }
    v = studio.ai_generate(db, run, "SQL", "", actor)
    v.requirements_basis = basis
    if not v.comment.startswith("Generated from the requirements"):
        v.comment = ("Generated from the requirements (target columns and table rules). " + (v.comment or ""))[:2000]
    set_state(db, run, "TRANSFORMATION_DESIGNED", actor)
    audit(db, actor, "SQL_GENERATED_FROM_REQUIREMENTS", run.id, "TRANSFORMATION_VERSION", v.id, version=v.version, basis=basis)
    db.flush()
    pack = None
    try:
        pack = testpack.build(db, run, actor)
    except Exception as e:  # noqa: BLE001 - SQL is still saved; the UI offers a rebuild
        record_error(db, run.id, "TEST_PACK", f"Test pack build after SQL generation failed: {e}")
    return {"generated": True, "ready": True, "version": v.version, "test_pack_built": bool(pack)}


# ------------------------------------------------------------------ edits
def _resolve_gaps(db: Session, run_id: str, resolve: dict[str, dict], actor: str, default_note: str) -> list[str]:
    done = []
    for gid, d in (resolve or {}).items():
        g = db.get(RequirementGap, {"id": gid, "run_id": run_id})
        if not g:
            raise SpecError(f"Unknown question {gid}")
        st = d.get("status", "CLARIFIED")
        if st not in ("OPEN", "CLARIFIED", "ACCEPTED_LIMITATION"):
            raise SpecError(f"Invalid status for {gid}")
        note = (d.get("note") or "").strip() or (
            default_note if st == "CLARIFIED" else f"Accepted as a known limitation by {actor}" if st == "ACCEPTED_LIMITATION" else ""
        )
        g.status, g.clarification, g.updated_by = st, (note or None), actor
        audit(db, actor, "GAP_UPDATED", run_id, "REQUIREMENT_GAP", gid, status=st, clarification=note)
        done.append(gid)
    return done


def _check_expression(expr: str, ctx: mapping_svc.Ctx) -> tuple[str, list[str], bool, int | None]:
    expr = (expr or "").strip().rstrip(";")
    if not expr:
        raise SpecError("Enter the rule (a SQL expression over the source columns)")
    if len(expr) > 4000:
        raise SpecError("Rule is too long")
    try:
        tree = sqlglot.parse_one(expr, read="duckdb")
    except Exception as e:  # noqa: BLE001
        raise SpecError(f"The rule is not a valid SQL expression: {str(e).splitlines()[0][:200]}") from e
    if isinstance(tree, (sqlglot.exp.Query, sqlglot.exp.DDL, sqlglot.exp.DML)) or list(
        tree.find_all(sqlglot.exp.Subquery, sqlglot.exp.Select)
    ):
        raise SpecError("Write a single expression (for example UPPER(TRIM(customers.NAME))), not a query")
    q, srcs, notes = mapping_svc.qualify_expression(expr, ctx, None)
    bad = [n for n in notes if "not found" in n or "multiple sources" in n or "does not parse" in n]
    if bad:
        raise SpecError(bad[0] + ". Use alias.COLUMN with one of: " + ", ".join(ctx.cols))
    agg = any(isinstance(x, sqlglot.exp.AggFunc) for x in sqlglot.parse_one(q, read="duckdb").walk())
    p = re.match(r"^ROUND\(.*,\s*(\d+)\)$", q, re.I | re.S)
    return q, srcs, agg, int(p.group(1)) if p else None


def edit_column(
    db: Session, run: QARun, target: str, expression: str, actor: str, resolve: dict[str, dict] | None = None, note: str = ""
) -> dict:
    ctx = mapping_svc.Ctx(db, run.id)
    if not ctx.sources:
        raise SpecError("Upload the source files first")
    tnames = [c["name"] for c in (ctx.target.columns if ctx.target else [])]
    reqs = _reqs(db, run.id)
    known = {t.upper(): t for t in tnames} | {(r.spec.get("target") or "").upper(): r.spec["target"] for r in reqs if r.spec.get("target")}
    if target.upper() not in known:
        raise SpecError(f"{target} is not a target column")
    target = known[target.upper()]
    q, srcs, agg, precision = _check_expression(expression, ctx)
    T = target.upper()
    previous = [r for r in reqs if r.kind in COLUMN_KINDS and (r.spec.get("target") or "").upper() == T and r.provenance != "AI_INFERRED"]
    orig = next((r for r in previous if r.provenance != "USER_CONFIRMED"), None)
    group_by = []
    if agg:
        group_by = next((r.spec.get("group_by") for r in previous if r.spec.get("group_by")), None) or []
        if not group_by:
            group_by = (
                next((r.spec.get("columns") for r in reqs if r.kind in ("BUSINESS_KEY", "UNIQUE_KEY") and r.status == "ACCEPTED"), []) or []
            )
    spec = {"target": target, "expression": q, "columns": columns_in(q), "aggregate": agg, "parsed": True, "source_columns": srcs}
    if group_by:
        spec["group_by"] = group_by
    if precision is not None:
        spec["precision"] = precision
    desc = f"{target} = {q}" + (f" per {', '.join(group_by)}" if group_by else "")
    user = next((r for r in previous if r.provenance == "USER_CONFIRMED"), None)
    if user:
        user.spec, user.description, user.status, user.kind = spec, desc, "ACCEPTED", "DERIVED_COLUMN"
        user.evidence = f"Edited on the Requirements tab by {actor}" + (f": {note}" if note else "")
        user.version += 1
        rid = user.id
    else:
        rid = _next_req_id(db, run.id)
        db.add(
            Requirement(
                id=rid,
                run_id=run.id,
                ac_id=orig.ac_id if orig else None,
                kind="DERIVED_COLUMN",
                description=desc,
                spec=spec,
                provenance="USER_CONFIRMED",
                confidence="HIGH",
                source_ref="user:requirements-tab",
                evidence=f"Edited on the Requirements tab by {actor}" + (f": {note}" if note else ""),
                status="ACCEPTED",
            )
        )
    db.flush()
    resolved = _resolve_gaps(db, run.id, resolve or {}, actor, f"Handled by the rule for {target}: {q} (set by {actor})")
    audit(
        db,
        actor,
        "TARGET_RULE_EDITED",
        run.id,
        "REQUIREMENT",
        rid,
        target=target,
        expression=q,
        replaces=[r.id for r in previous],
        resolved=resolved,
    )
    refresh(db, run, actor)
    return sync_sql(db, run, actor)


def edit_table(db: Session, run: QARun, body: dict, actor: str) -> dict:
    ctx = mapping_svc.Ctx(db, run.id)
    reqs = _reqs(db, run.id)
    tnames = {c["name"].upper(): c["name"] for c in (ctx.target.columns if ctx.target else [])}

    def ac_of(kinds):
        return next((r.ac_id for r in reqs if r.kind in kinds and r.provenance != "USER_CONFIRMED" and r.ac_id), None)

    new: list[tuple[str, str, dict, str | None]] = []
    replace: set[str] = set()
    if body.get("key") is not None:
        key = [k.strip() for k in body["key"] if k and k.strip()]
        if not key:
            raise SpecError("Choose at least one key column")
        bad = [k for k in key if tnames and k.upper() not in tnames]
        if bad:
            raise SpecError(f"Not target columns: {', '.join(bad)}")
        key = [tnames.get(k.upper(), k) for k in key]
        replace.add("BUSINESS_KEY")
        new.append(
            (
                "BUSINESS_KEY",
                f"{' + '.join(key)} {'is' if len(key) == 1 else 'form'} the business key",
                {"columns": key},
                ac_of(("BUSINESS_KEY", "UNIQUE_KEY")),
            )
        )
        have_nn = {c.upper() for r in reqs if r.kind == "NOT_NULL" and r.status == "ACCEPTED" for c in r.spec.get("columns", [])}
        for k in key:
            if k.upper() not in have_nn:
                new.append(("NOT_NULL", f"{k} cannot be null", {"columns": [k]}, ac_of(("BUSINESS_KEY", "UNIQUE_KEY"))))
    if body.get("joins") is not None:
        replace.add("JOIN")
        for j in body["joins"]:
            jt = (j.get("type") or "").upper()
            left, r_, keys = j.get("left"), j.get("right"), [k.strip() for k in j.get("keys", []) if k.strip()]
            if jt not in JOIN_TYPES:
                raise SpecError(f"Join type must be one of {', '.join(JOIN_TYPES)}")
            if left not in ctx.cols or r_ not in ctx.cols or left == r_:
                raise SpecError("Choose two different source files for the join")
            if not keys:
                raise SpecError("Choose the join key column(s)")
            for k in keys:
                for a in (left, r_):
                    if k.upper() not in {c.upper() for c in ctx.cols[a]}:
                        raise SpecError(f"Join key {k} is not a column of {a}")
            new.append(
                (
                    "JOIN",
                    f"{jt} JOIN {left} to {r_} on {', '.join(keys)}",
                    {"type": jt, "left": left, "right": r_, "keys": keys},
                    ac_of(("JOIN",)),
                )
            )
    if body.get("filters") is not None:
        replace.add("FILTER")
        for f in body["filters"]:
            a, col, op, val = f.get("dataset"), (f.get("column") or "").strip(), f.get("op", "="), f.get("value")
            if a not in ctx.cols or col.upper() not in {c.upper() for c in ctx.cols[a]}:
                raise SpecError(f"Filter column {a}.{col} not found")
            if op not in OPS:
                raise SpecError(f"Operator must be one of {' '.join(OPS)}")
            if val is None or str(val).strip() == "":
                raise SpecError("Enter the filter value")
            col = ctx.real(a, col)
            new.append(
                (
                    "FILTER",
                    f"Only {a} with {col} {op} {_lit(val)} are included",
                    {"dataset_hint": a, "column": col, "op": op, "value": str(val)},
                    ac_of(("FILTER",)),
                )
            )
    if body.get("defaults") is not None:
        replace.add("DEFAULT_WHEN_UNMATCHED")
        d = {}
        for k, v in body["defaults"].items():
            if tnames and k.upper() not in tnames:
                raise SpecError(f"{k} is not a target column")
            if v is None or str(v).strip() == "":
                continue
            v = str(v).strip()
            d[tnames.get(k.upper(), k)] = (float(v) if "." in v else int(v)) if re.fullmatch(r"-?\d+(\.\d+)?", v) else v.strip("'")
        if d:
            new.append(
                (
                    "DEFAULT_WHEN_UNMATCHED",
                    "Defaults for records without matches: " + ", ".join(f"{k} = {_lit(v)}" for k, v in d.items()),
                    {"defaults": d},
                    ac_of(("DEFAULT_WHEN_UNMATCHED",)),
                )
            )
        else:
            new.append(("NO_DEFAULTS", "No defaults for records without matches", {}, None))
    if body.get("filters") is not None and not body["filters"]:
        new.append(("NO_FILTER", "No row filter: all source rows are used", {}, None))
    if body.get("joins") is not None and not body["joins"]:
        new.append(("NO_JOIN", "No join between the sources", {}, None))

    for r in reqs:  # previous user rules of the edited kinds are replaced
        if r.provenance == "USER_CONFIRMED" and any(r.kind in EDIT_SECTIONS[k] for k in replace):
            db.delete(r)
    db.flush()
    ids = []
    for kind, desc, spec, ac in new:
        rid = _next_req_id(db, run.id)
        db.add(
            Requirement(
                id=rid,
                run_id=run.id,
                ac_id=ac,
                kind=kind,
                description=desc,
                spec=spec,
                provenance="USER_CONFIRMED",
                confidence="HIGH",
                source_ref="user:requirements-tab",
                evidence=f"Edited on the Requirements tab by {actor}",
                status="ACCEPTED",
            )
        )
        db.flush()
        ids.append(rid)
    resolved = _resolve_gaps(db, run.id, body.get("resolve") or {}, actor, f"Handled by the table rules set by {actor}")
    audit(db, actor, "TABLE_RULES_EDITED", run.id, "QA_RUN", run.id, sections=sorted(replace), requirements=ids, resolved=resolved)
    refresh(db, run, actor)
    return sync_sql(db, run, actor)


def answer_question(db: Session, run: QARun, gap_id: str, status: str, note: str, actor: str) -> dict:
    if status in ("CLARIFIED", "ACCEPTED_LIMITATION") and not note.strip():
        raise SpecError("Write the answer (or why it is accepted as a limitation)")
    _resolve_gaps(db, run.id, {gap_id: {"status": status, "note": note}}, actor, "")
    refresh(db, run, actor)
    return sync_sql(db, run, actor)
