"""Deterministic ValidationEngine. Owns every PASS/FAIL decision. No AI is involved here.

Each check receives registered DuckDB views: `expected`, `actual` and `src_<alias>` (sources), and returns a
CheckOutcome. Technical problems raise and are recorded by the caller as ERROR (never as FAIL).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import duckdb

from .datasets import _jsonable

NUMERIC = ("TINYINT", "SMALLINT", "INTEGER", "BIGINT", "HUGEINT", "FLOAT", "DOUBLE", "DECIMAL", "UTINYINT",
           "USMALLINT", "UINTEGER", "UBIGINT", "REAL")


@dataclass
class CheckOutcome:
    status: str  # PASS | FAIL | BLOCKED | SKIPPED
    expected: str = ""
    actual: str = ""
    message: str = ""
    metrics: dict = field(default_factory=dict)
    evidence_columns: list[str] = field(default_factory=list)
    evidence_rows: list[dict] = field(default_factory=list)


class CheckError(Exception):
    """Technical failure (bad config, missing table) -> recorded as ERROR."""


def q(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def lit(v: Any) -> str:
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, (int, float)):
        return repr(v)
    return "'" + str(v).replace("'", "''") + "'"


class ValidationContext:
    def __init__(self, expected: Path | None, actual: Path | None, sources: dict[str, Path], max_rows: int = 200):
        self.con = duckdb.connect(":memory:")
        self.con.execute("SET threads=2")
        self.max_rows = max_rows
        if expected:
            self.con.execute(f"CREATE VIEW expected AS SELECT * FROM read_parquet({lit(str(expected))})")
        if actual:
            self.con.execute(f"CREATE VIEW actual AS SELECT * FROM read_parquet({lit(str(actual))})")
        for a, p in sources.items():
            self.con.execute(f"CREATE VIEW {q('src_' + a)} AS SELECT * FROM read_parquet({lit(str(p))})")
        self._cols: dict[str, dict[str, str]] = {}

    def close(self):
        self.con.close()

    def cols(self, table: str) -> dict[str, str]:
        if table not in self._cols:
            try:
                rows = self.con.execute(f"DESCRIBE {q(table)}").fetchall()
            except duckdb.Error as e:
                raise CheckError(f"Table '{table}' is not available: {e}") from e
            self._cols[table] = {r[0]: r[1] for r in rows}
        return self._cols[table]

    def col(self, table: str, name: str) -> str | None:
        """Case-insensitive column resolution."""
        for c in self.cols(table):
            if c.upper() == name.upper():
                return c
        return None

    def one(self, sql: str):
        return self.con.execute(sql).fetchone()

    def rows(self, sql: str) -> tuple[list[str], list[dict]]:
        cur = self.con.execute(sql)
        names = [d[0] for d in cur.description]
        return names, [{k: _jsonable(v) for k, v in zip(names, r)} for r in cur.fetchall()]


def _family(t: str) -> str:
    b = t.split("(")[0].upper()
    if b in NUMERIC:
        return "numeric"
    if b.startswith(("DATE", "TIMESTAMP", "TIME")):
        return "temporal"
    if b == "BOOLEAN":
        return "boolean"
    return "string"


def _keys(ctx: ValidationContext, spec: dict, table: str) -> list[str]:
    keys = spec.get("keys") or []
    if not keys:
        raise CheckError("No approved business key configured for this check")
    out = []
    for k in keys:
        c = ctx.col(table, k)
        if not c:
            raise CheckError(f"Key column '{k}' not found in {table}")
        out.append(c)
    return out


def _key_join(ek: list[str], ak: list[str], le: str = "e", la: str = "a") -> str:
    return " AND ".join(f"{le}.{q(x)} IS NOT DISTINCT FROM {la}.{q(y)}" for x, y in zip(ek, ak))


# ------------------------------------------------------------------ checks
def check_schema(ctx: ValidationContext, spec: dict) -> CheckOutcome:
    e, a = ctx.cols("expected"), ctx.cols("actual")
    norm = (lambda s: s.upper()) if spec.get("case_insensitive", True) else (lambda s: s)
    en, an = {norm(k): (k, v) for k, v in e.items()}, {norm(k): (k, v) for k, v in a.items()}
    missing = [en[k][0] for k in en if k not in an]
    extra = [an[k][0] for k in an if k not in en]
    type_mm = [{"column": en[k][0], "expected_type": en[k][1], "actual_type": an[k][1]}
               for k in en if k in an and _family(en[k][1]) != _family(an[k][1])]
    rows = ([{"issue": "MISSING_IN_TARGET", "column": c} for c in missing] + [{"issue": "UNEXPECTED_IN_TARGET", "column": c} for c in extra]
            + [{"issue": "TYPE_FAMILY_MISMATCH", **t} for t in type_mm])
    ok = not missing and not extra and not type_mm
    return CheckOutcome("PASS" if ok else "FAIL", ", ".join(f"{k}:{_family(v)}" for k, v in e.items()),
                        ", ".join(f"{k}:{_family(v)}" for k, v in a.items()),
                        "Schemas match" if ok else f"{len(missing)} missing, {len(extra)} unexpected, {len(type_mm)} type mismatches",
                        {"missing": missing, "unexpected": extra, "type_mismatches": len(type_mm)}, ["issue", "column", "expected_type", "actual_type"], rows)


def check_row_count(ctx: ValidationContext, spec: dict) -> CheckOutcome:
    e = ctx.one("SELECT COUNT(*) FROM expected")[0]
    a = ctx.one("SELECT COUNT(*) FROM actual")[0]
    return CheckOutcome("PASS" if e == a else "FAIL", str(e), str(a),
                        "Counts match" if e == a else f"Generated expected has {e} rows, uploaded target has {a} (difference {a - e:+d})",
                        {"expected_count": e, "actual_count": a, "difference": a - e})


def check_key_unique(ctx: ValidationContext, spec: dict) -> CheckOutcome:
    t = spec.get("table", "actual")
    cols = [ctx.col(t, c) or c for c in spec["columns"]]
    for c, raw in zip(cols, spec["columns"]):
        if c not in ctx.cols(t):
            raise CheckError(f"Column {raw} not found in {t}")
    kc = ", ".join(q(c) for c in cols)
    names, rows = ctx.rows(f"SELECT {kc}, COUNT(*) AS occurrences FROM {q(t)} GROUP BY {kc} HAVING COUNT(*) > 1 ORDER BY occurrences DESC LIMIT {ctx.max_rows}")
    n = ctx.one(f"SELECT COUNT(*) FROM (SELECT {kc} FROM {q(t)} GROUP BY {kc} HAVING COUNT(*) > 1)")[0]
    return CheckOutcome("PASS" if n == 0 else "FAIL", "0 duplicate keys", f"{n} duplicate keys",
                        "Key is unique" if n == 0 else f"{n} key value(s) occur more than once in {t}", {"duplicate_keys": n}, names, rows)


def check_not_null(ctx: ValidationContext, spec: dict) -> CheckOutcome:
    t = spec.get("table", "actual")
    total, details, ev = 0, {}, []
    for raw in spec["columns"]:
        c = ctx.col(t, raw)
        if not c:
            raise CheckError(f"Column {raw} not found in {t}")
        blank = f" OR TRIM(CAST({q(c)} AS VARCHAR)) = ''" if spec.get("include_blank") else ""
        n = ctx.one(f"SELECT COUNT(*) FROM {q(t)} WHERE {q(c)} IS NULL{blank}")[0]
        details[c] = n
        total += n
        if n:
            _, rows = ctx.rows(f"SELECT * FROM {q(t)} WHERE {q(c)} IS NULL{blank} LIMIT {ctx.max_rows}")
            ev.extend({"column": c, **r} for r in rows)
    return CheckOutcome("PASS" if total == 0 else "FAIL", "0 nulls", f"{total} nulls",
                        "No nulls" if total == 0 else f"Null values found: {details}", {"null_counts": details},
                        list(ev[0].keys()) if ev else [], ev[: ctx.max_rows])


def _recon(ctx: ValidationContext, spec: dict, direction: str) -> CheckOutcome:
    E = spec.get("expected_table", "expected")
    ek, ak = _keys(ctx, spec, E), _keys(ctx, spec, "actual")
    src, other, sk, ok_ = (E, "actual", ek, ak) if direction == "missing" else ("actual", E, ak, ek)
    cond = _key_join(sk, ok_, "s", "o")
    base = f"FROM {src} s WHERE NOT EXISTS (SELECT 1 FROM {other} o WHERE {cond})"
    n = ctx.one(f"SELECT COUNT(*) {base}")[0]
    names, rows = ctx.rows(f"SELECT s.* {base} LIMIT {ctx.max_rows}")
    label = "missing from uploaded target" if direction == "missing" else "in uploaded target but not expected"
    return CheckOutcome("PASS" if n == 0 else "FAIL", "0 records", f"{n} records",
                        f"No records {label}" if n == 0 else f"{n} record(s) {label}", {"count": n, "keys": sk}, names, rows)


def check_column_match(ctx: ValidationContext, spec: dict) -> CheckOutcome:
    E = spec.get("expected_table", "expected")
    ek, ak = _keys(ctx, spec, E), _keys(ctx, spec, "actual")
    ec, ac = ctx.col(E, spec["column"]), ctx.col("actual", spec["column"])
    if not ec:
        raise CheckError(f"Column {spec['column']} is not produced by the approved transformation")
    if not ac:
        return CheckOutcome("FAIL", spec["column"], "column missing", f"Column {spec['column']} is missing from the uploaded target",
                            {"missing_column": True})
    fam = _family(ctx.cols(E)[ec])
    ekl, akl = ", ".join(q(k) for k in ek), ", ".join(q(k) for k in ak)
    eu = f"(SELECT * FROM {E} WHERE ({ekl}) IN (SELECT ({ekl}) FROM {E} GROUP BY ALL HAVING COUNT(*) = 1))" if len(ek) > 1 else \
        f"(SELECT * FROM {E} WHERE {q(ek[0])} IN (SELECT {q(ek[0])} FROM {E} GROUP BY 1 HAVING COUNT(*) = 1))"
    au = f"(SELECT * FROM actual WHERE ({akl}) IN (SELECT ({akl}) FROM actual GROUP BY ALL HAVING COUNT(*) = 1))" if len(ak) > 1 else \
        f"(SELECT * FROM actual WHERE {q(ak[0])} IN (SELECT {q(ak[0])} FROM actual GROUP BY 1 HAVING COUNT(*) = 1))"
    tol = float(spec.get("tolerance") or 0.0)
    ev, av = f"e.{q(ec)}", f"a.{q(ac)}"
    if fam == "numeric":
        avn = f"TRY_CAST({av} AS DOUBLE)"
        mismatch = (f"NOT (({ev} IS NULL AND {av} IS NULL) OR ({ev} IS NOT NULL AND {avn} IS NOT NULL AND "
                    f"ABS(CAST({ev} AS DOUBLE) - {avn}) <= {tol} + 1e-9))")
        diff = f"{avn} - CAST({ev} AS DOUBLE)"
    else:
        def norm(x):
            x = f"CAST({x} AS VARCHAR)"
            if fam == "temporal":
                x = f"CAST(TRY_CAST({x} AS TIMESTAMP) AS VARCHAR)"
            if spec.get("trim"):
                x = f"TRIM({x})"
            if spec.get("case_insensitive"):
                x = f"UPPER({x})"
            return x
        mismatch = f"NOT ({norm(ev)} IS NOT DISTINCT FROM {norm(av)})"
        diff = "NULL"
    join = f"FROM {eu} e JOIN {au} a ON {_key_join(ek, ak)}"
    compared = ctx.one(f"SELECT COUNT(*) {join}")[0]
    n = ctx.one(f"SELECT COUNT(*) {join} WHERE {mismatch}")[0]
    dup_excl = ctx.one(f"SELECT (SELECT COUNT(*) FROM {E}) - (SELECT COUNT(*) FROM {eu})")[0]
    keysel = ", ".join(f"e.{q(k)} AS {q(k)}" for k in ek)
    names, rows = ctx.rows(f"SELECT {keysel}, {lit(ec)} AS column_name, {ev} AS expected, {av} AS actual, {diff} AS difference "
                           f"{join} WHERE {mismatch} ORDER BY ALL LIMIT {ctx.max_rows}")
    exp_rows = ctx.one(f"SELECT COUNT(*) FROM {E}")[0]
    metrics = {"compared": compared, "mismatches": n, "tolerance": tol, "family": fam, "duplicate_key_rows_excluded": dup_excl}
    if compared == 0 and exp_rows > 0:
        return CheckOutcome("BLOCKED", "", "", "No records with matching unique keys to compare", metrics)
    msg = f"All {compared} matched records agree" if n == 0 else f"{n} of {compared} matched records differ in {ec}"
    if dup_excl:
        msg += f" ({dup_excl} rows with duplicate keys excluded; see key uniqueness check)"
    return CheckOutcome("PASS" if n == 0 else "FAIL", f"{compared} matching values", f"{n} mismatches", msg, metrics, names, rows)


def check_key_coverage(ctx: ValidationContext, spec: dict) -> CheckOutcome:
    t = spec.get("table", "actual")
    base = "src_" + spec["base"]
    bk = ctx.col(base, spec["base_key"])
    tk = ctx.col(t, spec["key"])
    if not bk or not tk:
        raise CheckError("Key column not found for coverage check")
    sql = f"FROM (SELECT DISTINCT {q(bk)} AS k FROM {q(base)} WHERE {q(bk)} IS NOT NULL) b WHERE NOT EXISTS (SELECT 1 FROM {q(t)} x WHERE x.{q(tk)} = b.k)"
    n = ctx.one(f"SELECT COUNT(*) {sql}")[0]
    total = ctx.one(f"SELECT COUNT(DISTINCT {q(bk)}) FROM {q(base)}")[0]
    names, rows = ctx.rows(f"SELECT b.k AS {q(spec['base_key'])} {sql} ORDER BY 1 LIMIT {ctx.max_rows}")
    return CheckOutcome("PASS" if n == 0 else "FAIL", f"{total} base keys present", f"{n} missing",
                        f"All {total} {spec['base']}.{spec['base_key']} values appear in {t}" if n == 0 else
                        f"{n} of {total} {spec['base']}.{spec['base_key']} values are missing from {t}", {"missing": n, "base_keys": total}, names, rows)


def check_unmatched_defaults(ctx: ValidationContext, spec: dict) -> CheckOutcome:
    t = spec.get("table", "actual")
    base, right = "src_" + spec["base"], "src_" + spec["right"]
    rf = spec.get("right_filter")
    where = ""
    if rf:
        c = ctx.col(right, rf["column"])
        where = f"WHERE {q(c)} {rf['op']} {lit(rf['value'])}"
    jk = spec["join_keys"]
    cond = " AND ".join(f"r.{q(ctx.col(right, k))} = b.{q(ctx.col(base, k))}" for k in jk)
    tk = ctx.col(t, spec["key"])
    bk = ctx.col(base, spec["key"])
    unmatched = f"SELECT DISTINCT b.{q(bk)} AS k FROM {q(base)} b WHERE NOT EXISTS (SELECT 1 FROM (SELECT * FROM {q(right)} {where}) r WHERE {cond})"
    viol = []
    for col, val in spec["defaults"].items():
        c = ctx.col(t, col)
        if not c:
            raise CheckError(f"Column {col} not found in {t}")
        viol.append(f"NOT (x.{q(c)} IS NOT DISTINCT FROM {lit(val)})")
    sel = ", ".join(f"x.{q(ctx.col(t, col))}" for col in spec["defaults"])
    sql = f"FROM {q(t)} x JOIN ({unmatched}) u ON x.{q(tk)} = u.k WHERE {' OR '.join(viol)}"
    n = ctx.one(f"SELECT COUNT(*) {sql}")[0]
    total = ctx.one(f"SELECT COUNT(*) FROM ({unmatched})")[0]
    names, rows = ctx.rows(f"SELECT x.{q(tk)}, {sel} {sql} LIMIT {ctx.max_rows}")
    return CheckOutcome("PASS" if n == 0 else "FAIL", ", ".join(f"{k}={v}" for k, v in spec["defaults"].items()),
                        f"{n} violations", f"{total} unmatched key(s) checked; {n} without the required defaults",
                        {"unmatched_keys": total, "violations": n}, names, rows)


def check_duplicate_rows(ctx: ValidationContext, spec: dict) -> CheckOutcome:
    t = spec.get("table", "actual")
    n = ctx.one(f"SELECT COALESCE(SUM(c - 1), 0) FROM (SELECT COUNT(*) c FROM {q(t)} GROUP BY ALL HAVING COUNT(*) > 1)")[0] \
        if False else ctx.one(f"SELECT COUNT(*) - (SELECT COUNT(*) FROM (SELECT DISTINCT * FROM {q(t)})) FROM {q(t)}")[0]
    return CheckOutcome("PASS" if n == 0 else "FAIL", "0 duplicate rows", f"{n} duplicate rows",
                        "No fully duplicated rows" if n == 0 else f"{n} fully duplicated row(s)", {"duplicate_rows": int(n)})


def check_snapshot(ctx: ValidationContext, spec: dict) -> CheckOutcome:
    """Full comparison (missing / extra / changed) of re-executed output against the approved expected snapshot."""
    ek, ak = _keys(ctx, spec, "expected"), _keys(ctx, spec, "actual")
    sch = check_schema(ctx, {})
    if sch.status == "FAIL":
        return CheckOutcome("FAIL", sch.expected, sch.actual, "Schema differs from snapshot: " + sch.message, sch.metrics,
                            sch.evidence_columns, sch.evidence_rows)
    miss = _recon(ctx, spec, "missing")
    extra = _recon(ctx, spec, "extra")
    ecols = [c for c in ctx.cols("expected") if c not in ek]
    changed = 0
    ev: list[dict] = []
    for c in ecols:
        r = check_column_match(ctx, {"keys": spec["keys"], "column": c, "tolerance": spec.get("tolerance", 0)})
        if r.status == "FAIL":
            changed += r.metrics.get("mismatches", 1)
            ev.extend(r.evidence_rows[:20])
    total = miss.metrics["count"] + extra.metrics["count"] + changed
    rows = [{"issue": "MISSING", **r} for r in miss.evidence_rows[:20]] + [{"issue": "EXTRA", **r} for r in extra.evidence_rows[:20]] + \
        [{"issue": "CHANGED", **r} for r in ev]
    cols = sorted({k for r in rows for k in r}, key=lambda x: (x != "issue", x))
    return CheckOutcome("PASS" if total == 0 else "FAIL", "identical to approved expected snapshot",
                        f"{miss.metrics['count']} missing, {extra.metrics['count']} extra, {changed} changed values",
                        "Re-execution reproduces the approved expected output" if total == 0 else "Output differs from approved snapshot",
                        {"missing": miss.metrics["count"], "extra": extra.metrics["count"], "changed_values": changed}, cols, rows[: ctx.max_rows])


def check_predicate(ctx: ValidationContext, spec: dict) -> CheckOutcome:
    """Business rule: rows of `table` violating a visual-condition predicate."""
    from .transform.visual import compile_expr, norm_cond
    t = spec.get("table", "actual")
    pred = compile_expr(norm_cond(spec["condition"]))
    n = ctx.one(f"SELECT COUNT(*) FROM {q(t)} WHERE NOT ({pred}) OR ({pred}) IS NULL")[0]
    names, rows = ctx.rows(f"SELECT * FROM {q(t)} WHERE NOT ({pred}) OR ({pred}) IS NULL LIMIT {ctx.max_rows}")
    return CheckOutcome("PASS" if n == 0 else "FAIL", f"all rows satisfy {pred}", f"{n} violations",
                        "Rule holds for all rows" if n == 0 else f"{n} row(s) violate the rule", {"violations": n}, names, rows)


PROPERTY_SQL = {  # property: (violation predicate, what every value must be)
    "UPPER": ("{v} IS NOT NULL AND {v} <> UPPER({v})", "upper-case"),
    "LOWER": ("{v} IS NOT NULL AND {v} <> LOWER({v})", "lower-case"),
    "TRIMMED": ("{v} IS NOT NULL AND {v} <> TRIM({v})", "free of leading/trailing spaces"),
    "MAX_DECIMALS": ("{c} IS NOT NULL AND ABS(TRY_CAST({c} AS DOUBLE) - ROUND(TRY_CAST({c} AS DOUBLE), {a})) > 1e-9",
                     "rounded to at most {a} decimal place(s)"),
    "NOT_NULL": ("{c} IS NULL", "non-null"),
    "NON_NEGATIVE": ("TRY_CAST({c} AS DOUBLE) < 0", "0 or more"),
    "NON_NEGATIVE_INTEGER": ("{c} IS NULL OR TRY_CAST({c} AS DOUBLE) < 0 OR TRY_CAST({c} AS DOUBLE) <> FLOOR(TRY_CAST({c} AS DOUBLE))",
                             "whole numbers of 0 or more"),
    "MAX_LENGTH": ("LENGTH({v}) > {a}", "at most {a} characters long"),
}


def check_value_property(ctx: ValidationContext, spec: dict) -> CheckOutcome:
    """A property every value must have because of the transformation logic (UPPER, TRIM, ROUND(x, n), COALESCE, ...)."""
    t = spec.get("table", "actual")
    c = ctx.col(t, spec["column"])
    if not c:
        raise CheckError(f"Column {spec['column']} not found in {t}")
    prop, a = spec["property"], spec.get("arg")
    v = f"CAST({q(c)} AS VARCHAR)"
    if prop == "ALLOWED_VALUES":
        vals = ", ".join(lit(str(x)) for x in a)
        pred = f"{q(c)} IS NULL" if not spec.get("allow_null") else "FALSE"
        pred = f"({pred}) OR ({q(c)} IS NOT NULL AND {v} NOT IN ({vals}))"
        what = "one of " + ", ".join(str(x) for x in a) + (" (or NULL)" if spec.get("allow_null") else "")
    elif prop in PROPERTY_SQL:
        tmpl, what = PROPERTY_SQL[prop]
        pred = tmpl.format(c=q(c), v=v, a=int(a or 0))
        what = what.format(a=a)
    else:
        raise CheckError(f"Unknown property {prop}")
    n = ctx.one(f"SELECT COUNT(*) FROM {q(t)} WHERE {pred}")[0]
    total = ctx.one(f"SELECT COUNT(*) FROM {q(t)}")[0]
    names, rows = ctx.rows(f"SELECT * FROM {q(t)} WHERE {pred} LIMIT {ctx.max_rows}")
    return CheckOutcome("PASS" if n == 0 else "FAIL", f"all {c} values {what}", f"{n} violation(s)",
                        f"All {total} values of {c} are {what}" if n == 0 else f"{n} of {total} values of {c} break the rule (must be {what})",
                        {"violations": n, "rows": total, "property": prop}, names, rows)


def check_key_subset(ctx: ValidationContext, spec: dict) -> CheckOutcome:
    """Every key in `table` must come from the source records the logic allows (e.g. INNER JOIN matches, filtered rows)."""
    t = spec.get("table", "actual")
    k = ctx.col(t, spec["key"])
    if not k:
        raise CheckError(f"Key column {spec['key']} not found in {t}")
    src = spec["source_sql"]
    sql = f"FROM {q(t)} x WHERE x.{q(k)} IS NOT NULL AND x.{q(k)} NOT IN (SELECT * FROM ({src}) s WHERE s IS NOT NULL)"
    try:
        n = ctx.one(f"SELECT COUNT(*) {sql}")[0]
    except duckdb.Error as e:
        raise CheckError(f"Could not evaluate the source records: {e}") from e
    names, rows = ctx.rows(f"SELECT x.* {sql} LIMIT {ctx.max_rows}")
    return CheckOutcome("PASS" if n == 0 else "FAIL", f"only {spec.get('source_desc', 'allowed records')}", f"{n} other record(s)",
                        "Every record comes from " + spec.get("source_desc", "the allowed source records") if n == 0 else
                        f"{n} record(s) do not come from {spec.get('source_desc', 'the allowed source records')}",
                        {"violations": n}, names, rows)


def check_rule_oracle(ctx: ValidationContext, spec: dict) -> CheckOutcome:
    """Compare the transformation output with an independent 'rule oracle': the approved mapping rules for the
    listed columns, compiled separately and evaluated directly on the scenario's source data."""
    from .transform.sql_exec import validate_sql
    sql = spec.get("oracle_sql") or ""
    allowed = {t for t in ctx.con.execute("SELECT view_name FROM duckdb_views() WHERE NOT internal").fetchall() for t in t if t.startswith("src_")}
    errs, _, _ = validate_sql(sql, allowed)
    if errs:
        raise CheckError("Rule oracle failed safety validation: " + "; ".join(e.message for e in errs[:3]))
    try:
        ctx.con.execute(f"CREATE OR REPLACE TEMP VIEW oracle AS {sql}")
    except duckdb.Error as e:
        raise CheckError(f"Rule oracle could not be evaluated: {str(e).splitlines()[0]}") from e
    ctx._cols.pop("oracle", None)
    base = {**spec, "expected_table": "oracle"}
    if spec.get("rows"):
        m = _recon(ctx, base, "missing")
        x = _recon(ctx, base, "extra")
        n_o = ctx.one("SELECT COUNT(*) FROM oracle")[0]
        bad = m.metrics["count"] + x.metrics["count"]
        rows = [{"issue": "EXPECTED_BY_RULE_BUT_MISSING", **r} for r in m.evidence_rows[:50]] + \
               [{"issue": "PRESENT_BUT_NOT_EXPECTED_BY_RULE", **r} for r in x.evidence_rows[:50]]
        return CheckOutcome("PASS" if bad == 0 else "FAIL", f"{n_o} record(s) per the rule",
                            f"{m.metrics['count']} missing, {x.metrics['count']} unexpected",
                            "Output records match the rule" if bad == 0 else "Output records differ from what the rule implies",
                            {"oracle_rows": n_o, "missing": m.metrics["count"], "unexpected": x.metrics["count"]},
                            sorted({k for r in rows for k in r}, key=lambda k: (k != "issue", k)), rows)
    n_oracle = ctx.one("SELECT COUNT(*) FROM oracle")[0]
    n_out = ctx.one("SELECT COUNT(*) FROM actual")[0]
    if n_oracle == 0:
        return CheckOutcome("PASS" if n_out == 0 else "FAIL", "no output rows (the rule excludes every record)",
                            f"{n_out} output row(s)", "The rule expects no output rows for this data, and the output is empty"
                            if n_out == 0 else f"The rule expects no output rows, but the output has {n_out}",
                            {"oracle_rows": 0, "output_rows": n_out})
    fails, compared, ev, per = 0, 0, [], {}
    for col in spec["columns"]:
        r = check_column_match(ctx, {**base, "column": col})
        per[col] = r.status
        compared += r.metrics.get("compared", 0)
        if r.status == "FAIL":
            fails += r.metrics.get("mismatches", 1)
            ev.extend(r.evidence_rows[:50])
        elif r.status == "BLOCKED":
            return CheckOutcome("BLOCKED", "", "", f"{col}: {r.message}", {"per_column": per})
    cols = spec["columns"]
    return CheckOutcome("PASS" if fails == 0 else "FAIL", f"{', '.join(cols)} follow the approved rule",
                        f"{fails} value(s) differ", (f"All {compared} checked value(s) match the rule" if fails == 0 else
                        f"{fails} value(s) differ from the rule ({', '.join(c for c, st in per.items() if st == 'FAIL')})"),
                        {"per_column": per, "mismatches": fails, "compared": compared},
                        list(ev[0].keys()) if ev else [], ev)


CHECKS = {
    "SCHEMA_MATCH": check_schema, "ROW_COUNT": check_row_count, "KEY_UNIQUE": check_key_unique, "NOT_NULL": check_not_null,
    "RECON_MISSING": lambda c, s: _recon(c, s, "missing"), "RECON_EXTRA": lambda c, s: _recon(c, s, "extra"),
    "COLUMN_MATCH": check_column_match, "KEY_COVERAGE": check_key_coverage, "UNMATCHED_DEFAULTS": check_unmatched_defaults,
    "DUPLICATE_ROWS": check_duplicate_rows, "SNAPSHOT_MATCH": check_snapshot, "RULE_PREDICATE": check_predicate,
    "RULE_ORACLE": check_rule_oracle, "VALUE_PROPERTY": check_value_property, "KEY_SUBSET": check_key_subset,
}


def evaluate_guards(ctx: ValidationContext, guards: list[dict], open_gaps: set[str]) -> str | None:
    """Runtime dependency guards: return a BLOCKED reason if an open gap affects data actually present."""
    for g in guards or []:
        if g["gap_id"] not in open_gaps:
            continue
        if g["when"] == "ALWAYS":
            return f"Blocked by unanswered requirement gap {g['gap_id']}"
        if g["when"] == "NULLS_IN_SOURCE":
            for ref in g["columns"]:
                alias, col = ref.split(".", 1)
                t = "src_" + alias
                try:
                    c = ctx.col(t, col)
                except CheckError:
                    continue
                if c and ctx.one(f"SELECT COUNT(*) FROM {q(t)} WHERE {q(c)} IS NULL")[0]:
                    return f"Blocked by unanswered gap {g['gap_id']}: {ref} contains nulls and null handling is not specified"
        if g["when"] == "NEGATIVE_IN_EXPECTED":
            c = ctx.col("expected", g["column"])
            if c and ctx.one(f"SELECT COUNT(*) FROM expected WHERE TRY_CAST({q(c)} AS DOUBLE) < 0")[0]:
                return f"Blocked by unanswered gap {g['gap_id']}: expected {g['column']} contains negative values whose behaviour is unspecified"
    return None


def run_check(ctx: ValidationContext, spec: dict) -> CheckOutcome:
    fn = CHECKS.get(spec.get("type", ""))
    if not fn:
        raise CheckError(f"Unknown check type '{spec.get('type')}'")
    try:
        return fn(ctx, spec)
    except duckdb.Error as e:
        raise CheckError(f"Check execution failed: {str(e).splitlines()[0]}") from e
