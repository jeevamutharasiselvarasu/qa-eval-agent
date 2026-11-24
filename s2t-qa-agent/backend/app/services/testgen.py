"""TestCaseGenerator: test cases organised per Jira acceptance criterion, plus test cases derived from the
transformation logic itself (see logic_tests.py, group TL) and general reconciliation checks (group GEN).

For every acceptance criterion the typed rules it contains are turned into
  * POSITIVE cases - valid data must produce the result the rule describes,
  * NEGATIVE cases - invalid or unexpected data (duplicates, nulls, unmatched joins, filtered-out rows) must be
                     handled the way the rule requires,
  * EDGE cases     - boundaries (zero, rounding, empty sources, special characters, one-to-many joins).
Actual-data cases check the uploaded target; synthetic cases run the approved transformation on generated edge-case
data and check the output against the rule. Rule checks on synthetic data use an independent "rule oracle"
(the approved mapping rule compiled separately and evaluated on the scenario sources), so they do not simply
compare the transformation with itself. Unknown behaviour produces BLOCKED cases, never invented expectations.
"""
from __future__ import annotations

import copy
import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core.common import audit, set_state
from ..models import ColumnMapping, Dataset, QARun, Requirement, RequirementGap, SyntheticScenario, TestCase
from .mapping import draft_visual_spec
from .synthetic import _base_key, build_model
from .transform.visual import VisualSpecError, compile_visual, has_agg

OPEN = ("OPEN", "BLOCKED", "ACCEPTED_LIMITATION")  # a limitation keeps dependent tests BLOCKED


class _Ctx:
    def __init__(self, db: Session, run: QARun):
        self.run = run
        self.reqs = {r.id: r for r in db.scalars(select(Requirement).where(Requirement.run_id == run.id)) if r.status == "ACCEPTED"}
        self.acs = [r for r in db.scalars(select(Requirement).where(Requirement.run_id == run.id,
                                                                    Requirement.kind == "ACCEPTANCE_CRITERION")) if r.status != "REJECTED"]
        self.maps = [m for m in db.scalars(select(ColumnMapping).where(ColumnMapping.run_id == run.id)) if m.status == "ACCEPTED"]
        self.gaps = list(db.scalars(select(RequirementGap).where(RequirementGap.run_id == run.id)))
        self.target = db.scalars(select(Dataset).where(Dataset.run_id == run.id, Dataset.role == "TARGET")).first()
        keyreq = [r for r in self.reqs.values() if r.kind == "BUSINESS_KEY"] or [r for r in self.reqs.values() if r.kind == "UNIQUE_KEY"]
        self.keys = keyreq[0].spec.get("columns", []) if keyreq else [m.target_column for m in self.maps if m.is_key and m.target_column]
        self.key_reqs = [r.id for r in keyreq]
        self.scen = {s.scenario_type: s for s in db.scalars(select(SyntheticScenario).where(SyntheticScenario.run_id == run.id))}
        self.model = build_model(db, run.id)
        try:
            self.spec, _ = draft_visual_spec(db, run.id)
        except Exception:  # noqa: BLE001
            self.spec = None

    def ac_of(self, req_ids) -> list[str]:
        return sorted({self.reqs[r].ac_id for r in req_ids if r in self.reqs and self.reqs[r].ac_id})

    def mapping_for(self, target: str) -> ColumnMapping | None:
        return next((m for m in self.maps if m.target_column and m.target_column.upper() == target.upper()), None)

    def oracle_sql(self, columns: list[str]) -> str | None:
        """The approved mapping rules for `columns` (plus the key), compiled independently over src_<alias> views."""
        if not self.spec or not self.keys:
            return None
        want = {c.upper() for c in self.keys + columns}
        spec = copy.deepcopy(self.spec)
        spec["columns"] = [c for c in spec["columns"] if c["target"].upper() in want]
        if {c["target"].upper() for c in spec["columns"]} != want:
            return None
        for s in spec["sources"]:
            s["as"] = s.get("as") or s["alias"]
            s["alias"] = "src_" + s["alias"]
        try:
            sql = compile_visual(spec)
        except (VisualSpecError, KeyError, TypeError):
            return None
        if not any(has_agg(c["expr"]) for c in spec["columns"]):
            # row-level rule over a one-to-many join: one value per key, exactly as the target holds it
            sql = f"SELECT DISTINCT * FROM (\n{sql}\n) AS __rule"
        return sql


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
            guards.append({"gap_id": g.id, "when": "NULLS_IN_SOURCE",
                           "columns": [s for s in m.source_columns if s.split(".")[-1].upper() in gcols]})
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

    def add(self, group: str, test_type: str, track: str, category: str, title: str, validation: dict, *, req_ids=(),
            map_ids=(), expected: str = "", scenario: str | None = None, rationale: str = "", blocked: str | None = None,
            priority: str = "P2", severity: str = "MAJOR", description: str = "", basis: str | None = None) -> TestCase | None:
        sc = None
        if scenario:
            sc = self.ctx.scen.get(scenario)
            if sc is None:
                return None  # scenario not applicable to these rules
            if sc.status in ("BLOCKED", "ERROR"):
                blocked = blocked or sc.blocked_reason or f"Scenario {sc.id} could not be generated"
        sig = (group, track, validation.get("type"), str(validation.get("columns") or validation.get("column")), scenario)
        if sig in self.seen:
            return None
        self.seen.add(sig)
        n = self.counters[group] = self.counters.get(group, 0) + 1
        tid = f"TC-{group}-{n:02d}"
        req_ids = sorted(set(req_ids))
        ac_ids = [group] if group.startswith("AC") else []  # general checks stay in their own group
        if track == "ACTUAL":
            steps = ["Run the approved transformation on the uploaded source files to build the expected target",
                     f"Check the uploaded target ({validation.get('type')})", "Store the result and evidence"]
            pre = "Approved transformation and test pack; uploaded source and target files."
        else:
            steps = [f"Generate synthetic source data for scenario {sc.id} ({sc.name})",
                     "Run the approved transformation on that data", f"Check the output ({validation.get('type')})",
                     "Store the result and evidence"]
            pre = f"Synthetic scenario {sc.id}: {sc.description[:300]}"
        tc = TestCase(id=tid, run_id=self.ctx.run.id, scenario_id=sc.id if sc else None, track=track, category=category,
                      test_type=test_type, title=title, description=description or title, validation=validation,
                      ac_ids=ac_ids, requirement_ids=req_ids, mapping_ids=sorted(set(map_ids)), expected_result=expected,
                      priority=priority, severity=severity, rationale=rationale, state="BLOCKED" if blocked else "READY",
                      blocked_reason=blocked, status="ACCEPTED", preconditions=pre, steps=steps,
                      basis=basis or ("ACCEPTANCE_CRITERIA" if group.startswith("AC") else "GENERAL"))
        self.out.append(tc)
        return tc

    def oracle(self, group, test_type, scenario, columns, title, expected, req_ids, map_ids=(), rows=False, category="RULE"):
        sql = self.ctx.oracle_sql(columns)
        blocked = None if sql else "No accepted mapping rule (or business key) to evaluate this check against"
        return self.add(group, test_type, "SYNTHETIC", category, title,
                        {"type": "RULE_ORACLE", "columns": columns, "keys": self.ctx.keys, "oracle_sql": sql or "",
                         "rows": rows, "tolerance": 1e-6}, req_ids=req_ids, map_ids=map_ids, expected=expected,
                        scenario=scenario, blocked=blocked)


def _general(b: _Builder, ctx: _Ctx) -> None:
    col_maps = [m for m in ctx.maps if m.target_column and m.mapping_type != "UNMAPPED_TARGET"]
    reqs = sorted({r for m in col_maps for r in m.requirement_ids})
    no_key = None if ctx.keys else "No approved business key; record-level reconciliation cannot be performed"
    G = "GEN"
    b.add(G, "GENERAL", "ACTUAL", "SCHEMA", "Uploaded target has the columns and types the transformation produces",
          {"type": "SCHEMA_MATCH", "case_insensitive": True}, req_ids=reqs, map_ids=[m.id for m in col_maps],
          expected="Same columns and type families", priority="P1", severity="CRITICAL")
    b.add(G, "GENERAL", "ACTUAL", "COUNT", "Uploaded target row count equals the expected row count", {"type": "ROW_COUNT"},
          expected="Equal counts (after approved joins, filters and aggregation)", priority="P1",
          rationale="General reconciliation check.")
    b.add(G, "GENERAL", "ACTUAL", "RECONCILIATION", "No expected record is missing from the uploaded target",
          {"type": "RECON_MISSING", "keys": ctx.keys}, req_ids=ctx.key_reqs, expected="0 missing records", blocked=no_key,
          priority="P1", severity="CRITICAL", rationale="General reconciliation check.")
    b.add(G, "GENERAL", "ACTUAL", "RECONCILIATION", "Uploaded target contains no unexpected records",
          {"type": "RECON_EXTRA", "keys": ctx.keys}, req_ids=ctx.key_reqs, expected="0 unexpected records", blocked=no_key,
          priority="P1", severity="CRITICAL", rationale="General reconciliation check.")
    b.add(G, "GENERAL", "ACTUAL", "DATA_QUALITY", "Uploaded target contains no fully duplicated rows",
          {"type": "DUPLICATE_ROWS", "table": "actual"}, expected="0 duplicate rows", priority="P3", severity="MINOR",
          rationale="General data-quality check; not derived from a Jira requirement.")


def _column_actual(b: _Builder, ctx: _Ctx, g: str, target: str, req_ids: list[str], title: str):
    m = ctx.mapping_for(target)
    if not m:
        b.add(g, "POSITIVE", "ACTUAL", "TRANSFORMATION", title, {"type": "COLUMN_MATCH", "column": target, "keys": ctx.keys},
              req_ids=req_ids, blocked=f"No accepted mapping for {target}")
        return None
    prec = next((ctx.reqs[r].spec.get("precision") for r in m.requirement_ids if r in ctx.reqs and
                 ctx.reqs[r].spec.get("precision") is not None), None)
    pm = re.search(r"ROUND\(.*,\s*(\d+)\)", m.expression)
    if prec is None and pm:
        prec = int(pm.group(1))
    tol = 0.5 / (10 ** prec) if prec is not None else float((ctx.run.settings or {}).get("default_numeric_tolerance", 0.0))
    guards, blocked = _guards(ctx, m)
    if not ctx.keys:
        blocked = "No approved business key; record-level comparison cannot be performed"
    sc = (ctx.run.settings or {}).get("string_compare", {})
    b.add(g, "POSITIVE", "ACTUAL", "AGGREGATION" if m.mapping_type == "AGGREGATED" else "TRANSFORMATION", title,
          {"type": "COLUMN_MATCH", "column": target, "keys": ctx.keys, "tolerance": tol, "trim": bool(sc.get("trim")),
           "case_insensitive": bool(sc.get("case_insensitive")), "guards": guards},
          req_ids=list(set(req_ids) | set(m.requirement_ids)), map_ids=[m.id],
          expected=f"Every uploaded {target} equals the value the rule produces" + (f" (±{tol:g})" if tol else ""),
          blocked=blocked, priority="P1")
    return m


def _rule_cases(b: _Builder, ctx: _Ctx, g: str, r: Requirement) -> None:
    m = ctx.model
    rid = [r.id]
    jl = m.join
    right = jl["right"] if jl else None
    key_ok = len(ctx.keys) == 1
    if r.kind in ("UNIQUE_KEY", "BUSINESS_KEY"):
        cols = r.spec.get("columns", [])
        if r.kind == "BUSINESS_KEY" and any(x.kind == "UNIQUE_KEY" and x.ac_id == r.ac_id for x in ctx.reqs.values()):
            return
        why = "" if r.kind == "UNIQUE_KEY" else "A business key identifies each record, so it must be unique."
        b.add(g, "POSITIVE", "ACTUAL", "KEY", f"{' + '.join(cols)} is unique in the uploaded target",
              {"type": "KEY_UNIQUE", "columns": cols, "table": "actual"}, req_ids=rid, expected="0 duplicate keys",
              priority="P1", severity="CRITICAL", rationale=why)
        b.add(g, "POSITIVE", "SYNTHETIC", "KEY", f"Valid records with distinct keys produce one target row each",
              {"type": "KEY_UNIQUE", "columns": cols, "table": "actual"}, req_ids=rid, scenario="POSITIVE",
              expected="0 duplicate keys", rationale=why)
        b.add(g, "NEGATIVE", "SYNTHETIC", "KEY", f"Duplicate {' + '.join(cols)} in the source does not create duplicate target rows",
              {"type": "KEY_UNIQUE", "columns": cols, "table": "actual"}, req_ids=rid, scenario="DUPLICATE_KEYS",
              expected="0 duplicate keys in the output", severity="CRITICAL", rationale=why)
        if right:
            b.add(g, "EDGE", "SYNTHETIC", "KEY", (f"Many {m.base} records sharing one {right} entry still give one row per key"
                                                  if m.right_unique else f"One-to-many matches in {right} do not duplicate target rows"),
                  {"type": "KEY_UNIQUE", "columns": cols, "table": "actual"}, req_ids=rid + jl["req"], scenario="MULTI_MATCH_JOIN",
                  expected="0 duplicate keys in the output", rationale=why)
    elif r.kind == "NOT_NULL":
        cols = r.spec.get("columns", [])
        b.add(g, "POSITIVE", "ACTUAL", "KEY", f"{', '.join(cols)} is never null in the uploaded target",
              {"type": "NOT_NULL", "columns": cols, "table": "actual"}, req_ids=rid, expected="0 nulls", priority="P1",
              severity="CRITICAL")
        b.add(g, "NEGATIVE", "SYNTHETIC", "KEY", f"A source record with a null {', '.join(cols)} does not reach the target with a null value",
              {"type": "NOT_NULL", "columns": cols, "table": "actual"}, req_ids=rid, scenario="NULL_KEY",
              expected="0 nulls in the output", severity="CRITICAL")
    elif r.kind == "DIRECT_MAPPING":
        t = r.spec["target"]
        mp = _column_actual(b, ctx, g, t, rid, f"{t} in the uploaded target equals {r.spec.get('source_dataset')}.{r.spec.get('source_column')}")
        b.oracle(g, "POSITIVE", "POSITIVE", [t], f"{t} is copied unchanged for valid records", f"Output {t} equals the source value",
                 rid, [mp.id] if mp else [])
    elif r.kind == "DERIVED_COLUMN":
        t = r.spec["target"]
        mp = _column_actual(b, ctx, g, t, rid, f"{t} in the uploaded target follows {r.spec.get('expression')}")
        mids = [mp.id] if mp else []
        if r.spec.get("aggregate"):
            b.oracle(g, "POSITIVE", "POSITIVE", [t], f"{t} is calculated correctly for valid records",
                     f"Output {t} equals the rule's result", rid, mids)
            if right:
                many = (f"{t} is correct when many {m.base} records share one {right} entry" if m.right_unique
                        else f"{t} aggregates all matching {right} records (one-to-many)")
                b.oracle(g, "POSITIVE", "MULTI_MATCH_JOIN", [t], many, f"Output {t} equals the rule's result over every match", rid, mids)
            b.oracle(g, "EDGE", "BOUNDARY_ZERO", [t], f"{t} is correct when numeric inputs are zero", f"Output {t} equals the rule's result",
                     rid, mids)
            if r.spec.get("precision") is not None or "ROUND" in (mp.expression if mp else ""):
                b.oracle(g, "EDGE", "DECIMAL_PRECISION", [t], f"{t} rounds to {r.spec.get('precision', '?')} decimals correctly",
                         "Rounded values match the rule (half-up)", rid + [x.id for x in ctx.reqs.values() if x.kind == "ROUNDING_MODE"], mids)
            if right and jl["type"] in ("LEFT", "FULL"):
                b.oracle(g, "EDGE", "EMPTY_RIGHT_SOURCE", [t], f"{t} when {right} is empty",
                         f"Output {t} equals the rule's result (defaults apply)", rid, mids)
        else:
            b.oracle(g, "POSITIVE", "POSITIVE", [t], f"{t} is derived correctly for valid records",
                     f"Output {t} equals the rule's result", rid, mids)
        if re.search(r"UPPER|LOWER|TRIM|CONCAT|REPLACE", r.spec.get("expression", ""), re.I):
            b.oracle(g, "EDGE", "SPECIAL_CHARACTERS", [t], f"{t} handles accents, apostrophes, tabs and whitespace-only values",
                     f"Output {t} equals the rule's result", rid, mids)
        for col in r.spec.get("columns", []):
            kind = next((k for k in ctx.scen if k.startswith("NULL_VALUES:") and k.split(".")[-1].upper() == col.upper()), None)
            if kind:
                b.oracle(g, "NEGATIVE", kind, [t], f"{t} when {col} is null", f"Output {t} follows the confirmed null-handling rule",
                         rid, mids)
        if "-" in r.spec.get("expression", ""):
            b.oracle(g, "NEGATIVE", "NEGATIVE_RESULT", [t], f"{t} when the subtracted value is larger (negative result)",
                     f"Output {t} follows the confirmed rule for negative results", rid, mids)
    elif r.kind == "FILTER":
        cols = [mm.target_column for mm in ctx.maps if mm.target_column and mm.mapping_type != "UNMAPPED_TARGET"
                and mm.target_column not in ctx.keys
                and any(s.split(".")[0] == (m.filter or {}).get("alias") for s in mm.source_columns)]
        val = r.spec["value"]
        shown = val if re.fullmatch(r"-?\d+(\.\d+)?", str(val)) else f"'{val}'"
        cond = f"{r.spec['column']} {r.spec['op']} {shown}"
        if not cols:
            b.add(g, "POSITIVE", "SYNTHETIC", "FILTER", f"Only records with {cond} are included",
                  {"type": "RULE_ORACLE"}, req_ids=rid, blocked="No target column depends on the filtered data")
            return
        b.oracle(g, "POSITIVE", "POSITIVE", cols, f"Records with {cond} are included",
                 f"{', '.join(cols)} include the qualifying records", rid, category="FILTER")
        b.oracle(g, "NEGATIVE", "FILTER_EXCLUSION", cols, f"Records that fail {cond} are excluded (boundary values included)",
                 f"{', '.join(cols)} ignore the excluded records", rid, category="FILTER")
    elif r.kind == "JOIN":
        jt = r.spec.get("type", "")
        bk = _base_key(m)
        if jt in ("LEFT", "FULL") and bk and key_ok:
            cov = {"type": "KEY_COVERAGE", "base": m.base, "base_key": bk, "key": ctx.keys[0], "table": "actual"}
            b.add(g, "POSITIVE", "ACTUAL", "JOIN", f"Every {m.base}.{bk} appears in the uploaded target", cov, req_ids=rid,
                  expected="0 base keys missing", priority="P1", severity="CRITICAL")
            b.add(g, "NEGATIVE", "SYNTHETIC", "JOIN", f"{m.base} records with no matching {right} are kept", cov, req_ids=rid,
                  scenario="NO_MATCH_JOIN", expected="0 base keys missing")
            b.add(g, "EDGE", "SYNTHETIC", "JOIN", f"All {m.base} records are kept when {right} is empty", cov, req_ids=rid,
                  scenario="EMPTY_RIGHT_SOURCE", expected="0 base keys missing")
        else:
            b.oracle(g, "POSITIVE", "POSITIVE", [], f"Records matching on {', '.join(r.spec.get('keys', []))} are included",
                     "Output records equal the rule's matched records", rid, rows=True, category="JOIN")
            b.oracle(g, "NEGATIVE", "NO_MATCH_JOIN", [], f"{m.base} records with no matching {right} are dropped ({jt} join)",
                     "Output contains only matched records", rid, rows=True, category="JOIN")
            b.oracle(g, "EDGE", "EMPTY_RIGHT_SOURCE", [], f"Output is empty when {right} is empty ({jt} join)",
                     "No output records", rid, rows=True, category="JOIN")
    elif r.kind == "DEFAULT_WHEN_UNMATCHED" and jl and key_ok:
        rf = {"column": m.filter["column"], "op": m.filter["op"], "value": m.filter["value"]} \
            if m.filter and m.filter["alias"] == right else None
        v = {"type": "UNMATCHED_DEFAULTS", "base": m.base, "right": right, "join_keys": jl["keys"], "right_filter": rf,
             "key": ctx.keys[0], "defaults": r.spec["defaults"], "table": "actual"}
        d = ", ".join(f"{k} = {x}" for k, x in r.spec["defaults"].items())
        b.add(g, "POSITIVE", "ACTUAL", "BUSINESS_RULE", f"Unmatched records in the uploaded target have {d}", v, req_ids=rid,
              expected="0 violations")
        b.add(g, "NEGATIVE", "SYNTHETIC", "BUSINESS_RULE", f"{m.base} records with no {right} get {d}", v, req_ids=rid,
              scenario="NO_MATCH_JOIN", expected="0 violations")
        if m.filter and m.filter["alias"] == right:
            b.add(g, "NEGATIVE", "SYNTHETIC", "BUSINESS_RULE", f"Records whose {right} rows are all filtered out get {d}", v,
                  req_ids=rid, scenario="FILTER_EXCLUSION", expected="0 violations")
        b.add(g, "EDGE", "SYNTHETIC", "BUSINESS_RULE", f"All records get {d} when {right} is empty", v, req_ids=rid,
              scenario="EMPTY_RIGHT_SOURCE", expected="0 violations")


def generate_test_pack(db: Session, run: QARun, actor: str) -> list[TestCase]:
    """(Re)build all test cases (actual + synthetic). Synthetic scenarios must already exist (synthetic.generate)."""
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
    order = {"BUSINESS_KEY": 0, "UNIQUE_KEY": 1, "NOT_NULL": 2, "DIRECT_MAPPING": 3, "DERIVED_COLUMN": 4, "FILTER": 5,
             "JOIN": 6, "DEFAULT_WHEN_UNMATCHED": 7}
    for ac in sorted(ctx.acs, key=lambda a: (len(a.ac_id or ""), a.ac_id or "")):
        rules = sorted(by_ac.get(ac.ac_id, []), key=lambda x: (order.get(x.kind, 9), x.id))
        before = len(b.out)
        for r in rules:
            _rule_cases(b, ctx, ac.ac_id, r)
        if len(b.out) == before:
            gap = next((x for x in ctx.gaps if x.gap_type == "UNPARSED_RULE" and ac.ac_id in x.description), None)
            if rules and not ctx.scen:
                why = "The checks for this criterion run on synthetic data, which has not been generated yet. Build the test pack."
            else:
                why = ((f"Open gap {gap.id}: " if gap else "") + "This acceptance criterion could not be turned into an automated "
                       "check. Add a structured rule or verify it manually.")
            b.add(ac.ac_id, "POSITIVE", "ACTUAL", "REQUIREMENT", f"{ac.ac_id}: {ac.description[:150]}", {"type": "MANUAL"},
                  req_ids=[ac.id], blocked=why)
    from . import logic_tests
    from .approvals import current_version
    logic_tests.generate(b, ctx, current_version(db, run.id))
    for t in b.out:
        t.selected = t.id not in deselected  # a rebuilt pack keeps the user's choice (test IDs are stable)
        db.add(t)
    set_state(db, run, "TEST_CASES_GENERATED", actor)
    counts = {k: sum(1 for t in b.out if t.test_type == k) for k in ("POSITIVE", "NEGATIVE", "EDGE", "GENERAL")}
    audit(db, actor, "TEST_PACK_GENERATED", run.id, "QA_RUN", run.id, count=len(b.out), by_type=counts,
          blocked=[t.id for t in b.out if t.state == "BLOCKED"])
    return b.out


# Backwards-compatible names used by the API and older callers
def generate_actual_tests(db: Session, run: QARun, actor: str) -> list[TestCase]:
    return generate_test_pack(db, run, actor)


def generate_synthetic_tests(db: Session, run: QARun, actor: str) -> list[TestCase]:
    return generate_test_pack(db, run, actor)
