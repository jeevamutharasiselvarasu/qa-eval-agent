"""Test cases derived from the transformation logic itself (the approved SQL), in addition to the ones derived from
the Jira acceptance criteria.

The SQL is parsed (sqlglot, DuckDB dialect) and every construct that implies a checkable property of the target
becomes a test on the uploaded target:

* column functions: UPPER / LOWER -> letter case, TRIM -> no surrounding spaces, ROUND(x, n) -> at most n decimals,
  COALESCE / IFNULL with a literal fallback -> never null, COUNT -> non-negative integer, ABS -> non-negative,
  LEFT / SUBSTRING with a length -> maximum length, CASE with literal results -> allowed values;
* output columns the requirements do not cover -> value-by-value comparison with the transformation output;
* LEFT JOIN -> every base record is kept; INNER JOIN -> only records with a match appear;
* a filter on the base table -> no record that fails it appears;
* GROUP BY -> one row per group.

The properties follow from the SQL, so a target that violates one cannot have been produced by that logic. Each
test names the SQL fragment it came from.
"""

from __future__ import annotations

import re

import sqlglot
from sqlglot import exp

PY_NOTE = "Transformation-logic tests are derived from SQL; this transformation is written in Python."


# ------------------------------------------------------------------ SQL analysis
def _from(sel: exp.Select):
    return sel.args.get("from_") or sel.args.get("from")


def _src(node) -> tuple[str | None, str, list[str]]:
    """(source table, alias, filter conditions inside a derived table) for a FROM / JOIN item."""
    if isinstance(node, exp.Table):
        return node.name, node.alias_or_name, []
    if isinstance(node, exp.Subquery):
        inner = node.this
        if isinstance(inner, exp.Select):
            f = _from(inner)
            t = f.this if f else None
            if isinstance(t, exp.Table) and not inner.args.get("joins") and not inner.args.get("group"):
                conds = []
                w = inner.args.get("where")
                if w is not None:
                    c = w.this.copy()
                    for col in c.find_all(exp.Column):  # qualify with the derived table's own alias
                        col.set("table", exp.to_identifier(node.alias_or_name))
                    conds.append(c.sql("duckdb"))
                return t.name, node.alias_or_name, conds
    return None, getattr(node, "alias_or_name", ""), []


def _lit_props(e: exp.Literal) -> dict:
    if e.is_string:
        v = e.this
        p = {"NOT_NULL": True}
        if v == v.upper():
            p["UPPER"] = True
        if v == v.lower():
            p["LOWER"] = True
        if v == v.strip():
            p["TRIMMED"] = True
        return p
    try:
        f = float(e.this)
    except ValueError:
        return {}
    dec = len(e.this.split(".")[1]) if "." in e.this else 0
    p = {"NOT_NULL": True, "MAX_DECIMALS": dec}
    if f >= 0:
        p["NON_NEGATIVE"] = True
        if f == int(f):
            p["NON_NEGATIVE_INTEGER"] = True
    return p


def _meet(ps: list[dict]) -> dict:
    """Properties that hold for every branch."""
    if not ps:
        return {}
    out = {}
    for k in set.intersection(*[set(p) for p in ps]):
        vals = [p[k] for p in ps]
        out[k] = max(vals) if k in ("MAX_DECIMALS", "MAX_LENGTH") else vals[0]
    return out


def props(e: exp.Expression, depth: int = 0) -> dict:
    """Properties every value of expression `e` is guaranteed to have."""
    if depth > 20 or e is None:
        return {}
    if isinstance(e, (exp.Alias, exp.Paren)):
        return props(e.this, depth + 1)
    if isinstance(e, exp.Literal):
        return _lit_props(e)
    if isinstance(e, exp.Upper):
        inner = props(e.this, depth + 1)
        return {"UPPER": True, **{k: v for k, v in inner.items() if k in ("TRIMMED", "MAX_LENGTH", "NOT_NULL")}}
    if isinstance(e, exp.Lower):
        inner = props(e.this, depth + 1)
        return {"LOWER": True, **{k: v for k, v in inner.items() if k in ("TRIMMED", "MAX_LENGTH", "NOT_NULL")}}
    if isinstance(e, exp.Trim) and not e.args.get("expression") and e.args.get("position") in (None, "BOTH"):
        inner = props(e.this, depth + 1)
        return {"TRIMMED": True, **{k: v for k, v in inner.items() if k in ("UPPER", "LOWER", "NOT_NULL")}}
    if isinstance(e, exp.Round):
        d = e.args.get("decimals")
        n = int(d.this) if isinstance(d, exp.Literal) and not d.is_string and re.fullmatch(r"\d+", d.this) else (0 if d is None else None)
        inner = props(e.this, depth + 1)
        out = {k: v for k, v in inner.items() if k in ("NOT_NULL", "NON_NEGATIVE")}
        if n is not None:
            out["MAX_DECIMALS"] = n
        return out
    if isinstance(e, exp.Coalesce):
        args = [e.this] + list(e.expressions)
        ps = [props(a, depth + 1) for a in args]
        out = _meet(ps)
        out.pop("NOT_NULL", None)
        if any(p.get("NOT_NULL") for p in ps):
            out["NOT_NULL"] = True
            out["_FALLBACK"] = args[-1].sql("duckdb")
        return out
    if isinstance(e, exp.Count):
        return {"NOT_NULL": True, "NON_NEGATIVE": True, "NON_NEGATIVE_INTEGER": True, "MAX_DECIMALS": 0}
    if isinstance(e, exp.Abs):
        return {"NON_NEGATIVE": True}
    if isinstance(e, (exp.Left, exp.Substring)):
        ln = e.args.get("expression") if isinstance(e, exp.Left) else e.args.get("length")
        if isinstance(ln, exp.Literal) and not ln.is_string and re.fullmatch(r"\d+", ln.this):
            return {"MAX_LENGTH": int(ln.this)}
        return {}
    if isinstance(e, exp.Case):
        branches = [i.args.get("true") for i in e.args.get("ifs", [])]
        default = e.args.get("default")
        vals = branches + ([default] if default is not None else [])
        out = (
            _meet([props(v, depth + 1) for v in vals])
            if default is not None
            else {k: v for k, v in _meet([props(v, depth + 1) for v in vals]).items() if k != "NOT_NULL"}
        )
        if vals and all(isinstance(v, exp.Literal) for v in vals):
            out["ALLOWED_VALUES"] = sorted({v.this for v in vals})
            out["_ALLOW_NULL"] = default is None
        return out
    if isinstance(e, exp.Cast):
        return {k: v for k, v in props(e.this, depth + 1).items() if k == "NOT_NULL"}
    return {}


class Logic:
    """What the outer SELECT of the transformation does."""

    def __init__(self, code: str):
        self.error = None
        self.columns: list[tuple[str, exp.Expression]] = []
        self.base: tuple[str | None, str, list[str]] = (None, "", [])
        self.joins: list[dict] = []
        self.where: str | None = None
        self.group: list[str] | None = None
        self.distinct = False
        try:
            tree = sqlglot.parse_one(code, read="duckdb")
        except Exception as e:  # noqa: BLE001
            self.error = f"The transformation SQL could not be parsed: {str(e).splitlines()[0][:200]}"
            return
        sel = tree if isinstance(tree, exp.Select) else None
        if sel is None:
            self.error = (
                "The transformation combines several SELECTs (e.g. UNION); logic tests cover a single SELECT only"
                if isinstance(tree, exp.SetOperation)
                else "The transformation SQL is not a SELECT statement"
            )
            return
        with_ = sel.args.get("with_") or sel.args.get("with")  # the argument name differs between sqlglot versions
        ctes = {c.alias_or_name: c.this for c in (with_.expressions if with_ else [])}
        subqs = {}
        f = _from(sel)
        items = ([f.this] if f else []) + [j.this for j in sel.args.get("joins") or []]
        for it in items:
            if isinstance(it, exp.Subquery) and isinstance(it.this, exp.Select):
                subqs[it.alias_or_name] = it.this
        self._defs = {**ctes, **subqs}
        self.distinct = bool(sel.args.get("distinct"))
        self._raw: list[tuple[str, exp.Expression]] = []
        for p in sel.expressions:
            if isinstance(p, exp.Star):
                continue
            name = p.alias_or_name
            raw = p.this if isinstance(p, exp.Alias) else p
            self._raw.append((name, raw))
            self.columns.append((name, self._resolve(raw)))
        if f:
            self.base = _src(f.this)
        for j in sel.args.get("joins") or []:
            table, alias, conds = _src(j.this)
            side = (j.args.get("side") or "").upper()
            kind = (j.args.get("kind") or "").upper()
            jt = side or ("INNER" if kind in ("", "INNER") else kind)
            on = j.args.get("on")
            pairs = []
            if on is not None:
                for eq in [on] if isinstance(on, exp.EQ) else list(on.find_all(exp.EQ)):
                    lhs, rhs = eq.this, eq.expression
                    if isinstance(lhs, exp.Column) and isinstance(rhs, exp.Column):
                        pairs.append((lhs.table, lhs.name, rhs.table, rhs.name))
            self.joins.append({"type": jt, "table": table, "alias": alias, "conds": conds, "pairs": pairs, "sql": j.sql("duckdb")})
        w = sel.args.get("where")
        self.where = w.this.sql("duckdb") if w is not None else None
        g = sel.args.get("group")
        if g is not None:
            if g.args.get("all") or any(isinstance(x, exp.Var) and x.name.upper() == "ALL" for x in g.expressions) or not g.expressions:
                self.group = [n for n, e in self.columns if not list(e.find_all(exp.AggFunc))]
            else:
                names = []
                for x in g.expressions:
                    # GROUP BY names the projection's source expression (c.ID) or its output name (CUSTOMER_ID)
                    hit = next(
                        (
                            n
                            for n, raw in self._raw
                            if raw.sql() == x.sql() or (isinstance(x, exp.Column) and not x.table and n.upper() == x.name.upper())
                        ),
                        None,
                    )
                    if hit:
                        names.append(hit)
                self.group = names
            self._group_sql = g.sql("duckdb")

    def _resolve(self, e: exp.Expression, depth: int = 0) -> exp.Expression:
        """A bare column taken from a CTE / derived table resolves to that table's expression for it."""
        if depth < 4 and isinstance(e, exp.Column) and e.table in self._defs:
            inner = self._defs[e.table]
            if isinstance(inner, exp.Select):
                for p in inner.expressions:
                    if p.alias_or_name.upper() == e.name.upper() and not isinstance(p, exp.Star):
                        return self._resolve(p.this if isinstance(p, exp.Alias) else p, depth + 1)
        return e

    def output_of(self, alias: str, column: str) -> str | None:
        """The output column that is a plain copy of alias.column (to relate source keys to target keys)."""
        for n, e in self.columns:
            if isinstance(e, exp.Column) and e.name.upper() == column.upper() and (not e.table or e.table == alias):
                return n
        return None


# ------------------------------------------------------------------ test generation
PROP_TEXT = {
    "UPPER": ("POSITIVE", "{c} is upper-case in the uploaded target", "No value contains lower-case letters"),
    "LOWER": ("POSITIVE", "{c} is lower-case in the uploaded target", "No value contains upper-case letters"),
    "TRIMMED": ("POSITIVE", "{c} has no leading or trailing spaces in the uploaded target", "No value with surrounding spaces"),
    "MAX_DECIMALS": ("EDGE", "{c} has at most {a} decimal place(s) in the uploaded target", "No value with more than {a} decimals"),
    "NOT_NULL": ("NEGATIVE", "{c} is never null in the uploaded target, even when inputs are null", "0 nulls (fallback {fb})"),
    "NON_NEGATIVE_INTEGER": ("EDGE", "{c} is a whole number of 0 or more in the uploaded target", "No negative, fractional or null value"),
    "NON_NEGATIVE": ("EDGE", "{c} is never negative in the uploaded target", "No negative value"),
    "MAX_LENGTH": ("EDGE", "{c} is at most {a} characters long in the uploaded target", "No value longer than {a}"),
    "ALLOWED_VALUES": ("POSITIVE", "{c} only contains the values the CASE expression can return", "Only {a}"),
}
ORDER = ["ALLOWED_VALUES", "UPPER", "LOWER", "TRIMMED", "MAX_LENGTH", "NOT_NULL", "NON_NEGATIVE_INTEGER", "NON_NEGATIVE", "MAX_DECIMALS"]


def note(version) -> str | None:
    """Why no transformation-logic tests can be derived (None when they can)."""
    if version is None:
        return "No transformation yet"
    if version.mode == "PYTHON":
        return PY_NOTE
    return Logic(version.code or "").error


def generate(b, ctx, version) -> None:
    """Add the transformation-logic tests (group TL) to the builder."""
    G = "TL"
    if version is None:
        return
    if note(version):  # Python, or SQL the analyser cannot read: the test-pack status explains why there are none
        return
    lg = Logic(version.code or "")
    tcols = {c["name"].upper(): c["name"] for c in (ctx.target.columns if ctx.target else [])}
    covered = {
        (t.validation.get("column") or "").upper()
        for t in b.out
        if t.track == "ACTUAL" and t.validation.get("type") == "COLUMN_MATCH" and t.state == "READY"
    }
    have = {
        (t.validation.get("type"), str(t.validation.get("columns") or t.validation.get("column") or t.validation.get("base")))
        for t in b.out
        if t.track == "ACTUAL"
    }
    tag = f"v{version.version}"
    sc = (ctx.run.settings or {}).get("string_compare", {})

    for name, e in lg.columns:
        C = tcols.get(name.upper())
        frag = e.sql("duckdb")
        short = frag if len(frag) <= 140 else frag[:137] + "…"
        if not C:
            continue  # the schema test reports columns the target lacks
        if C.upper() not in covered and C.upper() not in {k.upper() for k in ctx.keys}:
            pm = re.search(r"ROUND\(.*,\s*(\d+)\)", frag, re.I)
            tol = 0.5 / (10 ** int(pm.group(1))) if pm else float((ctx.run.settings or {}).get("default_numeric_tolerance", 0.0))
            b.add(
                G,
                "POSITIVE",
                "ACTUAL",
                "TRANSFORMATION_LOGIC",
                f"{C} in the uploaded target matches the transformation logic",
                {
                    "type": "COLUMN_MATCH",
                    "column": C,
                    "keys": ctx.keys,
                    "tolerance": tol,
                    "trim": bool(sc.get("trim")),
                    "case_insensitive": bool(sc.get("case_insensitive")),
                },
                expected=f"Every {C} equals the value of: {short}",
                rationale=f"Derived from the SQL ({tag}): {short}. No acceptance criterion covers this column.",
                basis="TRANSFORMATION_LOGIC",
                blocked=None if ctx.keys else "No business key; record-level comparison cannot be performed",
            )
        pr = props(e)
        for k in ORDER:
            if k not in pr:
                continue
            if k == "NON_NEGATIVE" and "NON_NEGATIVE_INTEGER" in pr:
                continue
            if k == "MAX_DECIMALS" and (pr.get("NON_NEGATIVE_INTEGER") or "ALLOWED_VALUES" in pr):
                continue
            if k in ("NOT_NULL",) and "_FALLBACK" not in pr and not isinstance(e, exp.Count):
                continue  # only test "never null" where the logic explicitly guarantees it
            if k in ("UPPER", "LOWER", "TRIMMED") and "ALLOWED_VALUES" in pr:
                continue
            if ("VALUE_PROPERTY", f"{C}:{k}") in have:
                continue
            ttype, title, expected = PROP_TEXT[k]
            a = pr[k]
            shown = ", ".join(a) if isinstance(a, list) else a
            v = {
                "type": "VALUE_PROPERTY",
                "table": "actual",
                "column": C,
                "property": k,
                "arg": a if k in ("MAX_DECIMALS", "MAX_LENGTH", "ALLOWED_VALUES") else None,
                "allow_null": bool(pr.get("_ALLOW_NULL")) if k == "ALLOWED_VALUES" else None,
                "columns": f"{C}:{k}",
            }
            b.add(
                G,
                ttype,
                "ACTUAL",
                "TRANSFORMATION_LOGIC",
                title.format(c=C, a=shown),
                v,
                expected=expected.format(a=shown, fb=pr.get("_FALLBACK", "")),
                rationale=f"Derived from the SQL ({tag}): {C} = {short}",
                basis="TRANSFORMATION_LOGIC",
                priority="P2",
            )

    base_table, base_alias, base_conds = lg.base
    for j in lg.joins:
        if not j["table"] or not j["pairs"]:
            continue
        # the base side of the ON clause and the target column that copies it
        p = next(((lt, lc, rt, rc) for lt, lc, rt, rc in j["pairs"] if lt == base_alias and rt == j["alias"]), None) or next(
            ((rt, rc, lt, lc) for lt, lc, rt, rc in j["pairs"] if rt == base_alias and lt == j["alias"]), None
        )
        if not p or not base_table:
            continue
        _, bk, _, rk = p
        out = lg.output_of(base_alias, bk)
        if not out or out.upper() not in tcols:
            continue
        T = tcols[out.upper()]
        if j["type"] == "LEFT" and not base_conds and not lg.where:
            b.add(
                G,
                "POSITIVE",
                "ACTUAL",
                "TRANSFORMATION_LOGIC",
                f"The LEFT JOIN keeps every {base_table} record in the uploaded target",
                {"type": "KEY_COVERAGE", "base": base_table, "base_key": bk, "key": T},
                expected=f"Every {base_table}.{bk} appears as {T}",
                rationale=f"Derived from the SQL ({tag}): {j['sql'][:200]}",
                basis="TRANSFORMATION_LOGIC",
                priority="P1",
            )
        elif j["type"] == "INNER":
            src = (
                f"SELECT DISTINCT b.{_q(bk)} FROM {_q('src_' + base_table)} AS b JOIN {_q('src_' + j['table'])} AS r "
                f"ON b.{_q(bk)} = r.{_q(rk)}"
            )
            b.add(
                G,
                "NEGATIVE",
                "ACTUAL",
                "TRANSFORMATION_LOGIC",
                f"Records without a matching {j['table']} row are not in the uploaded target (INNER JOIN)",
                {
                    "type": "KEY_SUBSET",
                    "table": "actual",
                    "key": T,
                    "source_sql": src,
                    "source_desc": f"{base_table} records that have a match in {j['table']}",
                },
                expected=f"Every {T} has a matching {j['table']}.{rk}",
                rationale=f"Derived from the SQL ({tag}): {j['sql'][:200]}",
                basis="TRANSFORMATION_LOGIC",
            )

    # filter on the base table (in a derived table or in WHERE when it only references the base)
    if base_table:
        conds = list(base_conds)
        if lg.where and not lg.joins:
            conds.append(lg.where)
        kcol = next((c for c in ctx.keys if lg.output_of(base_alias, c)), None)
        if conds and kcol:
            T = tcols.get(lg.output_of(base_alias, kcol).upper())
            src = f"SELECT DISTINCT {_q(base_alias)}.{_q(kcol)} FROM {_q('src_' + base_table)} AS {_q(base_alias)} WHERE " + " AND ".join(
                f"({c})" for c in conds
            )
            if T:
                b.add(
                    G,
                    "NEGATIVE",
                    "ACTUAL",
                    "TRANSFORMATION_LOGIC",
                    f"{base_table} records that fail the filter are not in the uploaded target",
                    {
                        "type": "KEY_SUBSET",
                        "table": "actual",
                        "key": T,
                        "source_sql": src,
                        "source_desc": f"{base_table} records where {' AND '.join(conds)}",
                    },
                    expected="0 records that fail the filter",
                    rationale=f"Derived from the SQL ({tag}): WHERE {' AND '.join(conds)}",
                    basis="TRANSFORMATION_LOGIC",
                )

    if lg.group:
        gcols = [tcols[c.upper()] for c in lg.group if c.upper() in tcols]
        keyed = [k for k in ctx.keys if k.upper() in {c.upper() for c in gcols}] or gcols
        if keyed:
            b.add(
                G,
                "NEGATIVE",
                "ACTUAL",
                "TRANSFORMATION_LOGIC",
                f"The uploaded target has one row per {', '.join(keyed)} (GROUP BY)",
                {"type": "KEY_UNIQUE", "columns": keyed, "table": "actual"},
                expected="0 duplicate groups",
                rationale=f"Derived from the SQL ({tag}): {getattr(lg, '_group_sql', 'GROUP BY')}",
                basis="TRANSFORMATION_LOGIC",
            )


def _q(n: str) -> str:
    return '"' + n.replace('"', '""') + '"'
