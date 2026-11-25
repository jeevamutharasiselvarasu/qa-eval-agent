"""TestCaseGenerator: test cases organised per Jira acceptance criterion, plus test cases derived from the
transformation logic itself (logic_tests.py, group TL) and general reconciliation checks (group GEN).

Every test checks the UPLOADED target (the developer's output) against the expected target that the approved
reference SQL produces from the uploaded sources. For every acceptance criterion:
  * POSITIVE cases check the whole target against the rule;
  * NEGATIVE and EDGE cases check the target on exactly the records in the uploaded data that hit an edge case
    (no match in a join, several matches, rows the filter excludes, null / zero / negative / high-precision inputs,
    unusual strings, duplicate or null source keys). The records are found with a query over the uploaded sources
    (the test's "scope"). When the uploaded data has no such records the test is SKIPPED ("not in this data"),
    never passed.
Unknown behaviour (an unanswered question) produces BLOCKED cases, never invented expectations.
"""

from __future__ import annotations

import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core.common import audit, set_state
from ..models import ColumnMapping, Dataset, QARun, Requirement, RequirementGap, TestCase
from .rule_model import base_key, build_model, right_key

OPEN = ("OPEN", "BLOCKED", "ACCEPTED_LIMITATION")  # a limitation keeps dependent tests BLOCKED


def _q(n: str) -> str:
    return '"' + str(n).replace('"', '""') + '"'


def _lit(v) -> str:
    if isinstance(v, (int, float)) or re.fullmatch(r"-?\d+(\.\d+)?", str(v)):
        return str(v)
    return "'" + str(v).replace("'", "''") + "'"


class _Ctx:
    def __init__(self, db: Session, run: QARun):
        self.run = run
        self.reqs = {r.id: r for r in db.scalars(select(Requirement).where(Requirement.run_id == run.id)) if r.status == "ACCEPTED"}
        self.acs = [
            r
            for r in db.scalars(select(Requirement).where(Requirement.run_id == run.id, Requirement.kind == "ACCEPTANCE_CRITERION"))
            if r.status != "REJECTED"
        ]
        self.maps = [m for m in db.scalars(select(ColumnMapping).where(ColumnMapping.run_id == run.id)) if m.status == "ACCEPTED"]
        self.gaps = list(db.scalars(select(RequirementGap).where(RequirementGap.run_id == run.id)))
        self.target = db.scalars(select(Dataset).where(Dataset.run_id == run.id, Dataset.role == "TARGET")).first()
        keyreq = [r for r in self.reqs.values() if r.kind == "BUSINESS_KEY"] or [r for r in self.reqs.values() if r.kind == "UNIQUE_KEY"]
        self.keys = keyreq[0].spec.get("columns", []) if keyreq else [m.target_column for m in self.maps if m.is_key and m.target_column]
        self.key_reqs = [r.id for r in keyreq]
        self.model = build_model(db, run.id)

    def ac_of(self, req_ids) -> list[str]:
        return sorted({self.reqs[r].ac_id for r in req_ids if r in self.reqs and self.reqs[r].ac_id})

    def mapping_for(self, target: str) -> ColumnMapping | None:
        return next((m for m in self.maps if m.target_column and m.target_column.upper() == target.upper()), None)

    def unanswered(self, topic: str, cols: set[str]) -> RequirementGap | None:
        cu = {c.upper() for c in cols}
        return next(
            (
                g
                for g in self.ctx_gaps()
                if g.affected.get("topic", g.gap_type) == topic and cu & {c.upper() for c in g.affected.get("columns", []) if c}
            ),
            None,
        )

    def ctx_gaps(self):
        return [g for g in self.gaps if g.status in OPEN]


class _Scopes:
    """Queries over the uploaded sources (src_<alias> views) that return the KEYS of the records hitting an edge case.
    Available only when one business key identifies a record in both the base source and the target."""

    def __init__(self, ctx: _Ctx):
        m = ctx.model
        self.m = m
        self.base = m.base
        self.bk = base_key(m)
        self.tk = ctx.keys[0] if len(ctx.keys) == 1 else None
        self.right = m.join["right"] if m.join else None
        self.rk = right_key(m)
        self.ok = bool(self.bk and self.tk)

    def keys_where(self, alias: str, pred: str) -> str | None:
        """Keys of records whose `alias` rows satisfy pred (pred uses x.<column>)."""
        if alias == self.base:
            return f"SELECT x.{_q(self.bk)} FROM {_q('src_' + alias)} x WHERE {pred}"
        if alias == self.right and self.rk:
            return f"SELECT x.{_q(self.rk)} FROM {_q('src_' + alias)} x WHERE {pred}"
        return None

    def no_match(self) -> str | None:
        if not (self.right and self.rk):
            return None
        return (
            f"SELECT b.{_q(self.bk)} FROM {_q('src_' + self.base)} b WHERE NOT EXISTS (SELECT 1 FROM "
            f"{_q('src_' + self.right)} r WHERE r.{_q(self.rk)} = b.{_q(self.bk)})"
        )

    def multi_match(self) -> str | None:
        if not (self.right and self.rk) or self.m.right_unique:
            return None
        return f"SELECT {_q(self.rk)} FROM {_q('src_' + self.right)} GROUP BY 1 HAVING COUNT(*) > 1"

    def duplicate_base(self) -> str:
        return f"SELECT {_q(self.bk)} FROM {_q('src_' + self.base)} GROUP BY 1 HAVING COUNT(*) > 1"

    def null_base_key(self) -> str:
        return f"SELECT 1 FROM {_q('src_' + self.base)} WHERE {_q(self.bk)} IS NULL"

    def filter_pred(self) -> str | None:
        f = self.m.filter
        return f"x.{_q(f['column'])} {f['op']} {_lit(f['value'])}" if f else None


def _scope(sql: str, desc: str, restrict: dict) -> dict:
    return {"sql": sql, "desc": desc, "restrict": restrict}


def _guards(ctx: _Ctx, m: ColumnMapping) -> tuple[list[dict], str | None]:
    """Gap dependencies for an actual-data column check: runtime guards where the data decides, static block otherwise."""
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
            guards.append(
                {"gap_id": g.id, "when": "NULLS_IN_SOURCE", "columns": [s for s in m.source_columns if s.split(".")[-1].upper() in gcols]}
            )
        elif topic == "NEGATIVE_VALUES":
            guards.append({"gap_id": g.id, "when": "NEGATIVE_IN_EXPECTED", "column": m.target_column})
        else:
            blocked = f"Blocked by unanswered requirement gap {g.id}: {g.description}"
    return guards, blocked


class _Builder:
    def __init__(self, ctx: _Ctx):
        self.ctx = ctx
        self.out: list[TestCase] = []
        self.counters: dict[str, int] = {}
        self.seen: set[tuple] = set()

    def add(
        self,
        group: str,
        test_type: str,
        track: str,
        category: str,
        title: str,
        validation: dict,
        *,
        req_ids=(),
        map_ids=(),
        expected: str = "",
        rationale: str = "",
        blocked: str | None = None,
        priority: str = "P2",
        severity: str = "MAJOR",
        description: str = "",
        basis: str | None = None,
    ) -> TestCase | None:
        scope = validation.get("scope")
        sig = (group, validation.get("type"), str(validation.get("columns") or validation.get("column")), scope["sql"] if scope else None)
        if sig in self.seen:
            return None
        self.seen.add(sig)
        n = self.counters[group] = self.counters.get(group, 0) + 1
        tid = f"TC-{group}-{n:02d}"
        req_ids = sorted(set(req_ids))
        ac_ids = [group] if group.startswith("AC") else []  # general and logic checks stay in their own groups
        steps = ["Run the approved SQL on the uploaded source files to build the expected target"]
        if scope:
            steps.append(f"Find the records in the uploaded data with {scope['desc']} (skipped if there are none)")
        steps += [f"Check the uploaded target ({validation.get('type')})", "Store the result and evidence"]
        tc = TestCase(
            id=tid,
            run_id=self.ctx.run.id,
            scenario_id=None,
            track="ACTUAL",
            category=category,
            test_type=test_type,
            title=title,
            description=description or title,
            validation=validation,
            ac_ids=ac_ids,
            requirement_ids=req_ids,
            mapping_ids=sorted(set(map_ids)),
            expected_result=expected,
            priority=priority,
            severity=severity,
            rationale=rationale,
            state="BLOCKED" if blocked else "READY",
            blocked_reason=blocked,
            status="ACCEPTED",
            preconditions="Approved SQL and test pack; uploaded source and target files.",
            steps=steps,
            basis=basis or ("ACCEPTANCE_CRITERIA" if group.startswith("AC") else "GENERAL"),
        )
        self.out.append(tc)
        return tc


def _general(b: _Builder, ctx: _Ctx) -> None:
    col_maps = [m for m in ctx.maps if m.target_column and m.mapping_type != "UNMAPPED_TARGET"]
    reqs = sorted({r for m in col_maps for r in m.requirement_ids})
    no_key = None if ctx.keys else "No approved business key; record-level reconciliation cannot be performed"
    G = "GEN"
    b.add(
        G,
        "GENERAL",
        "ACTUAL",
        "SCHEMA",
        "Uploaded target has the columns and types the transformation produces",
        {"type": "SCHEMA_MATCH", "case_insensitive": True},
        req_ids=reqs,
        map_ids=[m.id for m in col_maps],
        expected="Same columns and type families",
        priority="P1",
        severity="CRITICAL",
    )
    b.add(
        G,
        "GENERAL",
        "ACTUAL",
        "COUNT",
        "Uploaded target row count equals the expected row count",
        {"type": "ROW_COUNT"},
        expected="Equal counts (after approved joins, filters and aggregation)",
        priority="P1",
        rationale="General reconciliation check.",
    )
    b.add(
        G,
        "GENERAL",
        "ACTUAL",
        "RECONCILIATION",
        "No expected record is missing from the uploaded target",
        {"type": "RECON_MISSING", "keys": ctx.keys},
        req_ids=ctx.key_reqs,
        expected="0 missing records",
        blocked=no_key,
        priority="P1",
        severity="CRITICAL",
        rationale="General reconciliation check.",
    )
    b.add(
        G,
        "GENERAL",
        "ACTUAL",
        "RECONCILIATION",
        "Uploaded target contains no unexpected records",
        {"type": "RECON_EXTRA", "keys": ctx.keys},
        req_ids=ctx.key_reqs,
        expected="0 unexpected records",
        blocked=no_key,
        priority="P1",
        severity="CRITICAL",
        rationale="General reconciliation check.",
    )
    b.add(
        G,
        "GENERAL",
        "ACTUAL",
        "DATA_QUALITY",
        "Uploaded target contains no fully duplicated rows",
        {"type": "DUPLICATE_ROWS", "table": "actual"},
        expected="0 duplicate rows",
        priority="P3",
        severity="MINOR",
        rationale="General data-quality check; not derived from a Jira requirement.",
    )


def _colspec(ctx: _Ctx, target: str) -> tuple[dict | None, ColumnMapping | None, str | None]:
    """COLUMN_MATCH spec for a target column (uploaded target vs the reference SQL), its mapping, and a block reason."""
    m = ctx.mapping_for(target)
    if not m:
        return None, None, f"No accepted rule for {target}"
    prec = next(
        (ctx.reqs[r].spec.get("precision") for r in m.requirement_ids if r in ctx.reqs and ctx.reqs[r].spec.get("precision") is not None),
        None,
    )
    pm = re.search(r"ROUND\(.*,\s*(\d+)\)", m.expression)
    if prec is None and pm:
        prec = int(pm.group(1))
    tol = 0.5 / (10**prec) if prec is not None else float((ctx.run.settings or {}).get("default_numeric_tolerance", 0.0))
    guards, blocked = _guards(ctx, m)
    if not ctx.keys:
        blocked = "No approved business key; record-level comparison cannot be performed"
    sc = (ctx.run.settings or {}).get("string_compare", {})
    return (
        {
            "type": "COLUMN_MATCH",
            "column": target,
            "keys": ctx.keys,
            "tolerance": tol,
            "trim": bool(sc.get("trim")),
            "case_insensitive": bool(sc.get("case_insensitive")),
            "guards": guards,
        },
        m,
        blocked,
    )


def _column_cases(b: _Builder, ctx: _Ctx, g: str, r: Requirement, title: str) -> None:
    """POSITIVE whole-target comparison plus NEGATIVE / EDGE comparisons on the records that hit an edge case."""
    t = r.spec["target"]
    spec, mp, blocked = _colspec(ctx, t)
    rid = [r.id] + (mp.requirement_ids if mp else [])
    mids = [mp.id] if mp else []
    tol = spec["tolerance"] if spec else 0
    b.add(
        g,
        "POSITIVE",
        "ACTUAL",
        "AGGREGATION" if mp and mp.mapping_type == "AGGREGATED" else "TRANSFORMATION",
        title,
        spec or {"type": "COLUMN_MATCH", "column": t},
        req_ids=rid,
        map_ids=mids,
        expected=f"Every uploaded {t} equals the value the rule produces" + (f" (±{tol:g})" if tol else ""),
        blocked=blocked,
        priority="P1",
    )
    S = _Scopes(ctx)
    if not spec or not S.ok:
        return
    restrict = {"actual": S.tk, "expected": S.tk}

    def scoped(ttype, sql, desc, ttl, why="", block=None, extra_req=()):
        if not sql:
            return
        b.add(
            g,
            ttype,
            "ACTUAL",
            "EDGE_CASE",
            ttl,
            {**spec, "scope": _scope(sql, desc, restrict)},
            req_ids=rid + list(extra_req),
            map_ids=mids,
            expected=f"{t} equals the rule's result for those records (skipped if the data has none)",
            rationale=why,
            blocked=block or blocked,
        )

    m = S.m
    agg = bool(r.spec.get("aggregate"))
    srcs = [(s.split(".")[0], s.split(".")[1]) for s in (mp.source_columns if mp else []) if "." in s]
    if agg and S.right:
        scoped(
            "POSITIVE",
            S.multi_match(),
            f"several matching {S.right} rows",
            f"{t} adds up every matching {S.right} row (records with several matches)",
        )
    if S.right and m.join and m.join["type"] in ("LEFT", "FULL"):
        d = next((ctx.reqs[x].spec.get("defaults", {}) for x in ctx.reqs if ctx.reqs[x].kind == "DEFAULT_WHEN_UNMATCHED"), {})
        dv = next((v for k, v in d.items() if k.upper() == t.upper()), None)
        scoped(
            "NEGATIVE",
            S.no_match(),
            f"no matching {S.right} row",
            f"{t} for {S.base} records with no matching {S.right}" + (f" is {dv}" if dv is not None else ""),
        )
    for a, c in srcs:
        if a not in (S.base, S.right):
            continue
        gap = ctx.unanswered("NULL_HANDLING", {c})
        scoped(
            "NEGATIVE",
            S.keys_where(a, f"x.{_q(c)} IS NULL"),
            f"a null {a}.{c}",
            f"{t} when {c} is null",
            block=(f"Null handling for {a}.{c} is not answered (gap {gap.id}); the expected value is unknown" if gap else None),
        )
    num = [rr for rr in m.numeric_rules if rr["target"].upper() == t.upper()]
    for rr in num:
        for a, c in rr["cols"]:
            scoped("EDGE", S.keys_where(a, f"TRY_CAST(x.{_q(c)} AS DOUBLE) = 0"), f"a zero {a}.{c}", f"{t} when {c} is zero")
        for (a1, c1), (a2, c2) in rr["minus"]:
            if a1 == a2:
                gap = ctx.unanswered("NEGATIVE_VALUES", {t, c1, c2})
                scoped(
                    "NEGATIVE",
                    S.keys_where(a1, f"TRY_CAST(x.{_q(c2)} AS DOUBLE) > TRY_CAST(x.{_q(c1)} AS DOUBLE)"),
                    f"{c2} larger than {c1}",
                    f"{t} when {c2} is larger than {c1} (negative result)",
                    block=(f"Behaviour for a negative {t} is not answered (gap {gap.id})" if gap else None),
                )
        p = r.spec.get("precision")
        if p is None:
            pm = re.search(r"ROUND\(.*,\s*(\d+)\)", mp.expression if mp else "")
            p = int(pm.group(1)) if pm else None
        if p is not None:
            for a, c in rr["cols"][:2]:
                scoped(
                    "EDGE",
                    S.keys_where(
                        a, f"x.{_q(c)} IS NOT NULL AND ROUND(TRY_CAST(x.{_q(c)} AS DOUBLE), {p}) <> TRY_CAST(x.{_q(c)} AS DOUBLE)"
                    ),
                    f"a {a}.{c} value with more than {p} decimals",
                    f"{t} rounds to {p} decimals correctly when {c} has more decimals",
                    extra_req=[x.id for x in ctx.reqs.values() if x.kind == "ROUNDING_MODE"],
                )
    if (
        re.search(r"UPPER|LOWER|TRIM|CONCAT|REPLACE", r.spec.get("expression", "") or (mp.expression if mp else ""), re.I)
        or r.kind == "DIRECT_MAPPING"
    ):
        for a, c in srcs:
            if _is_string(ctx, a, c):
                scoped(
                    "EDGE",
                    S.keys_where(
                        a,
                        f"x.{_q(c)} IS NOT NULL AND (x.{_q(c)} <> TRIM(x.{_q(c)}) OR TRIM(x.{_q(c)}) = '' "
                        f"OR regexp_matches(x.{_q(c)}, '[^\\x20-\\x7E]') OR x.{_q(c)} LIKE '%''%')",
                    ),
                    f"unusual {a}.{c} text (surrounding spaces, blank, accents or apostrophes)",
                    f"{t} handles unusual text: spaces, blanks, accents, apostrophes",
                )


def _is_string(ctx: _Ctx, alias: str, col: str) -> bool:
    t = next((c["type"] for c in ctx.model.columns.get(alias, []) if c["name"].upper() == col.upper()), "")
    return not any(x in t.lower() for x in ("int", "double", "decimal", "float", "date", "time", "bool"))


def _rule_cases(b: _Builder, ctx: _Ctx, g: str, r: Requirement) -> None:
    m = ctx.model
    S = _Scopes(ctx)
    rid = [r.id]
    jl = m.join
    right = jl["right"] if jl else None
    if r.kind in ("UNIQUE_KEY", "BUSINESS_KEY"):
        cols = r.spec.get("columns", [])
        if r.kind == "BUSINESS_KEY" and any(x.kind == "UNIQUE_KEY" and x.ac_id == r.ac_id for x in ctx.reqs.values()):
            return
        why = "" if r.kind == "UNIQUE_KEY" else "A business key identifies each record, so it must be unique."
        v = {"type": "KEY_UNIQUE", "columns": cols, "table": "actual"}
        b.add(
            g,
            "POSITIVE",
            "ACTUAL",
            "KEY",
            f"{' + '.join(cols)} is unique in the uploaded target",
            v,
            req_ids=rid,
            expected="0 duplicate keys",
            priority="P1",
            severity="CRITICAL",
            rationale=why,
        )
        if S.ok and len(cols) == 1:
            b.add(
                g,
                "NEGATIVE",
                "ACTUAL",
                "EDGE_CASE",
                f"{S.base} records with a duplicated {S.bk} in the source give one target row",
                {**v, "scope": _scope(S.duplicate_base(), f"a {S.bk} that appears more than once in {S.base}", {"actual": S.tk})},
                req_ids=rid,
                expected="0 duplicate keys for those records (skipped if the data has none)",
                severity="CRITICAL",
            )
            if S.right:
                b.add(
                    g,
                    "EDGE",
                    "ACTUAL",
                    "EDGE_CASE",
                    f"Records with several matching {S.right} rows still have one target row",
                    {**v, "scope": _scope(S.multi_match(), f"several matching {S.right} rows", {"actual": S.tk})},
                    req_ids=rid + jl["req"],
                    expected="0 duplicate keys for those records (skipped if the data has none)",
                )
    elif r.kind == "NOT_NULL":
        cols = r.spec.get("columns", [])
        v = {"type": "NOT_NULL", "columns": cols, "table": "actual"}
        b.add(
            g,
            "POSITIVE",
            "ACTUAL",
            "KEY",
            f"{', '.join(cols)} is never null in the uploaded target",
            v,
            req_ids=rid,
            expected="0 nulls",
            priority="P1",
            severity="CRITICAL",
        )
        if S.ok and [c.upper() for c in cols] == [S.tk.upper()]:
            b.add(
                g,
                "NEGATIVE",
                "ACTUAL",
                "EDGE_CASE",
                f"Source records with a null {S.bk} do not produce a null {cols[0]}",
                {**v, "scope": _scope(S.null_base_key(), f"a null {S.base}.{S.bk}", {})},
                req_ids=rid,
                expected="0 nulls (skipped if the source has no null keys)",
                severity="CRITICAL",
            )
    elif r.kind == "DIRECT_MAPPING":
        t = r.spec["target"]
        _column_cases(b, ctx, g, r, f"{t} in the uploaded target equals {r.spec.get('source_dataset')}.{r.spec.get('source_column')}")
    elif r.kind == "DERIVED_COLUMN":
        t = r.spec["target"]
        _column_cases(b, ctx, g, r, f"{t} in the uploaded target follows {r.spec.get('expression')}")
    elif r.kind == "FILTER":
        f = m.filter
        val = r.spec["value"]
        shown = val if re.fullmatch(r"-?\d+(\.\d+)?", str(val)) else f"'{val}'"
        cond = f"{r.spec['column']} {r.spec['op']} {shown}"
        if not f or not S.ok:
            b.add(
                g,
                "POSITIVE",
                "ACTUAL",
                "FILTER",
                f"Only records with {cond} are included",
                {"type": "MANUAL"},
                req_ids=rid,
                blocked="The filter could not be related to the business key; verify it manually",
            )
            return
        pred = S.filter_pred()
        fail = f"NOT ({pred}) OR x.{_q(f['column'])} IS NULL"
        if f["alias"] == S.base:  # the filter decides which records exist in the target
            passing = S.keys_where(S.base, pred)
            b.add(
                g,
                "POSITIVE",
                "ACTUAL",
                "FILTER",
                f"Every {S.base} record with {cond} is in the uploaded target",
                {
                    "type": "KEY_COVERAGE",
                    "base": S.base,
                    "base_key": S.bk,
                    "key": S.tk,
                    "table": "actual",
                    "scope": _scope(passing, f"{cond}", {"src_" + S.base: S.bk}),
                },
                req_ids=rid,
                expected="0 missing records",
            )
            b.add(
                g,
                "NEGATIVE",
                "ACTUAL",
                "FILTER",
                f"{S.base} records that fail {cond} are not in the uploaded target",
                {
                    "type": "KEY_SUBSET",
                    "table": "actual",
                    "key": S.tk,
                    "source_sql": passing,
                    "source_desc": f"{S.base} records with {cond}",
                },
                req_ids=rid,
                expected="0 records that fail the filter",
            )
            return
        cols = [
            mm.target_column
            for mm in ctx.maps
            if mm.target_column
            and mm.mapping_type != "UNMAPPED_TARGET"
            and mm.target_column not in ctx.keys
            and any(s.split(".")[0] == f["alias"] for s in mm.source_columns)
        ]
        if not cols:
            b.add(
                g,
                "POSITIVE",
                "ACTUAL",
                "FILTER",
                f"Only records with {cond} are included",
                {"type": "MANUAL"},
                req_ids=rid,
                blocked="No target column depends on the filtered data",
            )
            return
        fails = S.keys_where(f["alias"], fail)
        passes = S.keys_where(f["alias"], pred)
        all_fail = f"{fails} EXCEPT {passes}" if fails and passes else None
        restrict = {"actual": S.tk, "expected": S.tk}
        for c in cols:
            spec, mp, blocked = _colspec(ctx, c)
            if not spec:
                continue
            mids = [mp.id]
            b.add(
                g,
                "POSITIVE",
                "ACTUAL",
                "FILTER",
                f"{c} counts the {f['alias']} rows with {cond}",
                {**spec, "scope": _scope(passes, f"{f['alias']} rows with {cond}", restrict)},
                req_ids=rid,
                map_ids=mids,
                expected=f"{c} equals the rule's result for those records",
                blocked=blocked,
            )
            b.add(
                g,
                "NEGATIVE",
                "ACTUAL",
                "FILTER",
                f"{c} ignores {f['alias']} rows that fail {cond}",
                {**spec, "scope": _scope(fails, f"{f['alias']} rows that fail {cond}", restrict)},
                req_ids=rid,
                map_ids=mids,
                expected=f"{c} equals the rule's result for those records (skipped if the data has none)",
                blocked=blocked,
            )
            if all_fail:
                b.add(
                    g,
                    "EDGE",
                    "ACTUAL",
                    "FILTER",
                    f"{c} for records whose {f['alias']} rows all fail {cond}",
                    {**spec, "scope": _scope(all_fail, f"only {f['alias']} rows that fail {cond}", restrict)},
                    req_ids=rid,
                    map_ids=mids,
                    expected=f"{c} equals the rule's result (defaults apply; skipped if the data has none)",
                    blocked=blocked,
                )
    elif r.kind == "JOIN":
        jt = r.spec.get("type", "")
        if not S.ok or not S.right:
            return
        if jt in ("LEFT", "FULL"):
            cov = {"type": "KEY_COVERAGE", "base": S.base, "base_key": S.bk, "key": S.tk, "table": "actual"}
            b.add(
                g,
                "POSITIVE",
                "ACTUAL",
                "JOIN",
                f"Every {S.base}.{S.bk} appears in the uploaded target",
                cov,
                req_ids=rid,
                expected="0 base keys missing",
                priority="P1",
                severity="CRITICAL",
            )
            b.add(
                g,
                "NEGATIVE",
                "ACTUAL",
                "JOIN",
                f"{S.base} records with no matching {S.right} are kept",
                {**cov, "scope": _scope(S.no_match(), f"no matching {S.right} row", {"src_" + S.base: S.bk})},
                req_ids=rid,
                expected="0 of those records missing (skipped if every record has a match)",
            )
            if S.multi_match():
                b.add(
                    g,
                    "EDGE",
                    "ACTUAL",
                    "JOIN",
                    f"{S.base} records with several matching {S.right} rows are kept once",
                    {
                        "type": "KEY_UNIQUE",
                        "columns": [S.tk],
                        "table": "actual",
                        "scope": _scope(S.multi_match(), f"several matching {S.right} rows", {"actual": S.tk}),
                    },
                    req_ids=rid,
                    expected="0 duplicate keys for those records",
                )
        else:
            matched = (
                f"SELECT b.{_q(S.bk)} FROM {_q('src_' + S.base)} b WHERE EXISTS (SELECT 1 FROM {_q('src_' + S.right)} r "
                f"WHERE r.{_q(S.rk)} = b.{_q(S.bk)})"
            )
            b.add(
                g,
                "POSITIVE",
                "ACTUAL",
                "JOIN",
                f"Every {S.base} record with a matching {S.right} is in the uploaded target",
                {
                    "type": "KEY_COVERAGE",
                    "base": S.base,
                    "base_key": S.bk,
                    "key": S.tk,
                    "table": "actual",
                    "scope": _scope(matched, f"a matching {S.right} row", {"src_" + S.base: S.bk}),
                },
                req_ids=rid,
                expected="0 missing records",
            )
            b.add(
                g,
                "NEGATIVE",
                "ACTUAL",
                "JOIN",
                f"{S.base} records with no matching {S.right} are dropped ({jt} join)",
                {
                    "type": "KEY_SUBSET",
                    "table": "actual",
                    "key": S.tk,
                    "source_sql": matched,
                    "source_desc": f"{S.base} records with a matching {S.right}",
                },
                req_ids=rid,
                expected="0 unmatched records",
            )
    elif r.kind == "DEFAULT_WHEN_UNMATCHED" and jl and S.ok:
        rf = (
            {"column": m.filter["column"], "op": m.filter["op"], "value": m.filter["value"]}
            if m.filter and m.filter["alias"] == right
            else None
        )
        v = {
            "type": "UNMATCHED_DEFAULTS",
            "base": m.base,
            "right": right,
            "join_keys": jl["keys"],
            "right_filter": rf,
            "key": ctx.keys[0],
            "defaults": r.spec["defaults"],
            "table": "actual",
        }
        d = ", ".join(f"{k} = {x}" for k, x in r.spec["defaults"].items())
        b.add(
            g,
            "POSITIVE",
            "ACTUAL",
            "BUSINESS_RULE",
            f"Unmatched records in the uploaded target have {d}",
            v,
            req_ids=rid,
            expected="0 violations",
        )
        b.add(
            g,
            "NEGATIVE",
            "ACTUAL",
            "BUSINESS_RULE",
            f"{m.base} records with no {right} at all get {d}",
            {**{**v, "right_filter": None}, "scope": _scope(S.no_match(), f"no {right} row at all", {"actual": S.tk})},
            req_ids=rid,
            expected="0 violations (skipped if every record has a match)",
        )


def generate_test_pack(db: Session, run: QARun, actor: str) -> list[TestCase]:
    """(Re)build all test cases from the acceptance criteria, the transformation logic and the general checks."""
    ctx = _Ctx(db, run)
    if not ctx.target:
        raise ValueError("Upload the target dataset first")
    deselected = set()
    for t in db.scalars(select(TestCase).where(TestCase.run_id == run.id)):
        if t.selected is False:
            deselected.add(t.id)
        db.delete(t)
    db.flush()
    b = _Builder(ctx)
    _general(b, ctx)
    by_ac: dict[str, list[Requirement]] = {}
    for r in ctx.reqs.values():
        if r.ac_id and r.kind not in ("ACCEPTANCE_CRITERION", "MAPPING_SPEC"):
            by_ac.setdefault(r.ac_id, []).append(r)
    order = {
        "BUSINESS_KEY": 0,
        "UNIQUE_KEY": 1,
        "NOT_NULL": 2,
        "DIRECT_MAPPING": 3,
        "DERIVED_COLUMN": 4,
        "FILTER": 5,
        "JOIN": 6,
        "DEFAULT_WHEN_UNMATCHED": 7,
    }
    for ac in sorted(ctx.acs, key=lambda a: (len(a.ac_id or ""), a.ac_id or "")):
        rules = sorted(by_ac.get(ac.ac_id, []), key=lambda x: (order.get(x.kind, 9), x.id))
        before = len(b.out)
        for r in rules:
            _rule_cases(b, ctx, ac.ac_id, r)
        if len(b.out) == before:
            gap = next((x for x in ctx.gaps if x.gap_type == "UNPARSED_RULE" and ac.ac_id in x.description), None)
            why = (
                f"Open gap {gap.id}: " if gap else ""
            ) + "This acceptance criterion could not be turned into an automated check. Add a structured rule or verify it manually."
            b.add(
                ac.ac_id,
                "POSITIVE",
                "ACTUAL",
                "REQUIREMENT",
                f"{ac.ac_id}: {ac.description[:150]}",
                {"type": "MANUAL"},
                req_ids=[ac.id],
                blocked=why,
            )
    from . import logic_tests
    from .approvals import current_version

    logic_tests.generate(b, ctx, current_version(db, run.id))
    for t in b.out:
        t.selected = t.id not in deselected  # a rebuilt pack keeps the user's choice (test IDs are stable)
        db.add(t)
    set_state(db, run, "TEST_CASES_GENERATED", actor)
    counts = {k: sum(1 for t in b.out if t.test_type == k) for k in ("POSITIVE", "NEGATIVE", "EDGE", "GENERAL")}
    audit(
        db,
        actor,
        "TEST_PACK_GENERATED",
        run.id,
        "QA_RUN",
        run.id,
        count=len(b.out),
        by_type=counts,
        blocked=[t.id for t in b.out if t.state == "BLOCKED"],
    )
    return b.out
