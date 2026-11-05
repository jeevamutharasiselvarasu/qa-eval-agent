"""VisualTransformationCompiler: JSON visual definition -> DuckDB SQL (and -> pandas code for AI drafts).

Visual spec:
{
  "sources":  [{"alias": "customers", "as": "c", "filters": [COND...]}, ...],      # first = base
  "joins":    [{"right": "t", "type": "LEFT|INNER|RIGHT|FULL", "on": [{"left": "c.CUSTOMER_ID", "right": "t.CUSTOMER_ID"}]}],
  "filters":  [COND...],              # post-join WHERE
  "columns":  [{"target": "NET_AMOUNT", "expr": EXPR, "mapping_id": "MAP-004"}],
  "dedupe":   {"keys": ["CUSTOMER_ID"], "order_by": [{"column": "X", "desc": true}]},   # optional
  "post_filters": [COND...],          # on output columns
  "order_by": ["CUSTOMER_ID"]
}
EXPR:  {"col": "c.X"} | {"lit": v} | {"fn": NAME, "args": [...]} | {"op": OP, "args": [...]}
       | {"case": [{"when": COND, "then": EXPR}], "else": EXPR} | {"cast": EXPR, "to": TYPE}
COND:  EXPR, or shorthand {"column": "t.STATUS", "op": "=", "value": "COMPLETED"}
Aggregates trigger GROUP BY ALL.
"""
from __future__ import annotations

import re
from typing import Any

import sqlglot
from sqlglot import exp

FUNCS = {"UPPER", "LOWER", "TRIM", "LTRIM", "RTRIM", "CONCAT", "REPLACE", "SUBSTRING", "SPLIT_PART", "LENGTH",
         "COALESCE", "NULLIF", "ROUND", "FLOOR", "CEIL", "ABS", "SUM", "COUNT", "AVG", "MIN", "MAX",
         "COUNT_DISTINCT", "STRPTIME", "STRFTIME", "DATE_TRUNC", "DATE_DIFF", "LEFT", "RIGHT", "LPAD", "RPAD",
         "GREATEST", "LEAST", "CURRENT_DATE", "MD5", "REGEXP_REPLACE"}
AGG = {"SUM", "COUNT", "AVG", "MIN", "MAX", "COUNT_DISTINCT"}
BINOPS = {"+", "-", "*", "/", "=", "!=", "<>", "<", "<=", ">", ">=", "AND", "OR", "LIKE", "||", "%"}
UNOPS = {"NOT", "IS NULL", "IS NOT NULL", "NEG"}
TYPES = re.compile(r"^(VARCHAR|INTEGER|BIGINT|DOUBLE|BOOLEAN|DATE|TIMESTAMP|DECIMAL\(\d{1,2},\s*\d{1,2}\))$", re.I)
JOIN_TYPES = {"LEFT", "INNER", "RIGHT", "FULL"}
IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class VisualSpecError(ValueError):
    pass


def _qi(name: str) -> str:
    if not IDENT.match(name):
        raise VisualSpecError(f"Invalid identifier '{name}'")
    return f'"{name}"'


def _col(ref: str) -> str:
    parts = ref.split(".")
    if len(parts) > 2:
        raise VisualSpecError(f"Invalid column reference '{ref}'")
    return ".".join(_qi(p) for p in parts)


def _lit(v: Any) -> str:
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, (int, float)):
        return repr(v)
    return "'" + str(v).replace("'", "''") + "'"


def norm_cond(c: dict) -> dict:
    if "column" in c:
        op = c.get("op", "=").upper()
        if op in ("IS NULL", "IS NOT NULL"):
            return {"op": op, "args": [{"col": c["column"]}]}
        if op in ("IN", "NOT IN"):
            vals = c.get("value") if isinstance(c.get("value"), list) else [v.strip() for v in str(c.get("value", "")).split(",")]
            return {"op": op, "args": [{"col": c["column"]}] + [{"lit": v} for v in vals]}
        if op == "BETWEEN":
            lo, hi = c.get("value", [None, None])
            return {"op": "BETWEEN", "args": [{"col": c["column"]}, {"lit": lo}, {"lit": hi}]}
        return {"op": op, "args": [{"col": c["column"]}, {"lit": c.get("value")}]}
    return c


def compile_expr(e: Any, aggs: list | None = None) -> str:
    if not isinstance(e, dict):
        raise VisualSpecError(f"Invalid expression node: {e!r}")
    if "col" in e:
        return _col(e["col"])
    if "lit" in e:
        return _lit(e["lit"])
    if "fn" in e:
        fn = str(e["fn"]).upper()
        if fn not in FUNCS:
            raise VisualSpecError(f"Function '{fn}' is not supported in visual mode")
        args = [compile_expr(a, aggs) for a in e.get("args", [])]
        if fn in AGG and aggs is not None:
            aggs.append(fn)
        if fn == "COUNT_DISTINCT":
            return f"COUNT(DISTINCT {args[0]})"
        if fn == "COUNT" and not args:
            return "COUNT(*)"
        return f"{fn}({', '.join(args)})"
    if "op" in e:
        op = str(e["op"]).upper()
        args = [compile_expr(a, aggs) for a in e.get("args", [])]
        if op in BINOPS:
            if len(args) < 2:
                raise VisualSpecError(f"Operator {op} needs two operands")
            return "(" + f" {op} ".join(args) + ")"
        if op == "NOT":
            return f"(NOT {args[0]})"
        if op == "NEG":
            return f"(-{args[0]})"
        if op in ("IS NULL", "IS NOT NULL"):
            return f"({args[0]} {op})"
        if op in ("IN", "NOT IN"):
            return f"({args[0]} {op} ({', '.join(args[1:])}))"
        if op == "BETWEEN":
            return f"({args[0]} BETWEEN {args[1]} AND {args[2]})"
        raise VisualSpecError(f"Operator '{op}' is not supported")
    if "case" in e:
        parts = ["CASE"]
        for w in e["case"]:
            parts.append(f"WHEN {compile_expr(norm_cond(w['when']), aggs)} THEN {compile_expr(w['then'], aggs)}")
        if "else" in e:
            parts.append(f"ELSE {compile_expr(e['else'], aggs)}")
        parts.append("END")
        return " ".join(parts)
    if "cast" in e:
        to = str(e.get("to", ""))
        if not TYPES.match(to):
            raise VisualSpecError(f"Cast type '{to}' is not supported")
        return f"CAST({compile_expr(e['cast'], aggs)} AS {to.upper()})"
    if "sql" in e:  # validated free-form scalar expression (no subqueries / tables)
        try:
            node = sqlglot.parse_one(str(e["sql"]), read="duckdb", into=exp.Condition) if False else sqlglot.parse_one(f"SELECT {e['sql']}", read="duckdb")
        except Exception as ex:  # noqa: BLE001
            raise VisualSpecError(f"Invalid expression '{e['sql']}': {ex}") from ex
        if any(isinstance(n, (exp.Table, exp.Subquery, exp.Select)) for n in node.expressions[0].walk()):
            raise VisualSpecError("Subqueries are not allowed in visual expressions")
        if aggs is not None and any(isinstance(n, exp.AggFunc) for n in node.walk()):
            aggs.append("SQL_AGG")
        return node.expressions[0].sql("duckdb")
    raise VisualSpecError(f"Unknown expression node keys: {list(e)}")


def compile_visual(spec: dict, available: dict[str, list[str]] | None = None) -> str:
    srcs = spec.get("sources") or []
    if not srcs:
        raise VisualSpecError("At least one source dataset is required")
    cols = spec.get("columns") or []
    if not cols:
        raise VisualSpecError("At least one target column is required")
    by_as = {}
    for s in srcs:
        if available is not None and s["alias"] not in available:
            raise VisualSpecError(f"Unknown dataset '{s['alias']}'")
        by_as[s.get("as") or s["alias"]] = s

    def src_sql(s: dict) -> str:
        a = s.get("as") or s["alias"]
        if s.get("filters"):
            conds = " AND ".join(compile_expr(norm_cond(f)) for f in s["filters"])
            return f"(SELECT * FROM {_qi(s['alias'])} AS {_qi(a)} WHERE {conds}) AS {_qi(a)}"
        return f"{_qi(s['alias'])} AS {_qi(a)}"

    base = srcs[0]
    lines = []
    aggs: list[str] = []
    select_parts = [f"    {compile_expr(c['expr'], aggs)} AS {_qi(c['target'])}" for c in cols]
    lines.append("SELECT\n" + ",\n".join(select_parts))
    lines.append(f"FROM {src_sql(base)}")
    joined = {base.get("as") or base["alias"]}
    for j in spec.get("joins") or []:
        r = j["right"]
        if r not in by_as:
            raise VisualSpecError(f"Join references unknown source '{r}'")
        jt = str(j.get("type", "INNER")).upper()
        if jt not in JOIN_TYPES:
            raise VisualSpecError(f"Unsupported join type '{jt}'")
        if not j.get("on"):
            raise VisualSpecError(f"Join to '{r}' has no join keys")
        on = " AND ".join(f"{_col(k['left'])} = {_col(k['right'])}" for k in j["on"])
        lines.append(f"{jt if jt != 'FULL' else 'FULL OUTER'} JOIN {src_sql(by_as[r])} ON {on}")
        joined.add(r)
    missing = set(by_as) - joined
    if missing:
        raise VisualSpecError(f"Sources not joined: {', '.join(sorted(missing))}")
    if spec.get("filters"):
        lines.append("WHERE " + " AND ".join(compile_expr(norm_cond(f)) for f in spec["filters"]))
    if aggs:
        lines.append("GROUP BY ALL")
    sql = "\n".join(lines)
    dd = spec.get("dedupe")
    post = spec.get("post_filters") or []
    if dd or post:
        outer = f"SELECT * FROM (\n{sql}\n) AS __v"
        if post:
            outer += "\nWHERE " + " AND ".join(compile_expr(norm_cond(f)) for f in post)
        if dd:
            keys = ", ".join(_qi(k) for k in dd["keys"])
            order = ", ".join(f"{_qi(o['column'])} {'DESC' if o.get('desc') else 'ASC'}" for o in dd.get("order_by", [])) or keys
            outer += f"\nQUALIFY ROW_NUMBER() OVER (PARTITION BY {keys} ORDER BY {order}) = 1"
        sql = outer
    if spec.get("order_by"):
        sql += "\nORDER BY " + ", ".join(_qi(c) for c in spec["order_by"])
    return sql


# ------------------------------------------------------------------ sqlglot -> visual expression
def sql_to_expr(sql_expr: str, qualify: dict[str, str] | None = None) -> dict:
    """Convert a simple SQL scalar/aggregate expression to a visual EXPR tree. Unknown shapes fall back to
    a validated {"sql": ...} node. `qualify` maps bare column name -> source 'as' alias."""
    qualify = qualify or {}
    try:
        node = sqlglot.parse_one(sql_expr, read="duckdb")
    except Exception:  # noqa: BLE001
        return {"sql": sql_expr}

    def conv(n: exp.Expression) -> dict:
        if isinstance(n, exp.Paren):
            return conv(n.this)
        if isinstance(n, exp.Column):
            name = n.name
            tbl = n.table
            if not tbl:
                tbl = qualify.get(name.upper(), "")
            elif tbl not in qualify.values():
                tbl = qualify.get(name.upper(), tbl)
            return {"col": f"{tbl}.{name}" if tbl else name}
        if isinstance(n, exp.Literal):
            return {"lit": n.this if n.is_string else (float(n.this) if "." in n.this else int(n.this))}
        if isinstance(n, exp.Null):
            return {"lit": None}
        ops = {exp.Add: "+", exp.Sub: "-", exp.Mul: "*", exp.Div: "/", exp.EQ: "=", exp.NEQ: "!=", exp.GT: ">",
               exp.GTE: ">=", exp.LT: "<", exp.LTE: "<=", exp.And: "AND", exp.Or: "OR", exp.DPipe: "||", exp.Mod: "%"}
        for cls, op in ops.items():
            if type(n) is cls:
                return {"op": op, "args": [conv(n.left), conv(n.right)]}
        if isinstance(n, exp.Not):
            return {"op": "NOT", "args": [conv(n.this)]}
        if isinstance(n, exp.Neg):
            return {"op": "NEG", "args": [conv(n.this)]}
        if isinstance(n, exp.Count):
            inner = n.this
            if isinstance(inner, exp.Distinct):
                return {"fn": "COUNT_DISTINCT", "args": [conv(inner.expressions[0])]}
            if isinstance(inner, exp.Star) or inner is None:
                return {"fn": "COUNT", "args": []}
            return {"fn": "COUNT", "args": [conv(inner)]}
        if isinstance(n, exp.Cast):
            return {"cast": conv(n.this), "to": n.to.sql("duckdb")}
        if isinstance(n, exp.Case):
            out = {"case": [{"when": conv(i.this), "then": conv(i.args["true"])} for i in n.args.get("ifs", [])]}
            if n.args.get("default") is not None:
                out["else"] = conv(n.args["default"])
            return out
        if isinstance(n, exp.Func):
            fn = (n.name if isinstance(n, exp.Anonymous) else n.sql_name()).upper()
            if fn in FUNCS:
                args = [a for a in n.args.values() if isinstance(a, exp.Expression)]
                flat = []
                for a in n.iter_expressions():
                    flat.append(conv(a))
                return {"fn": fn, "args": flat or [conv(a) for a in args]}
        raise ValueError("unsupported")

    try:
        return conv(node)
    except Exception:  # noqa: BLE001
        sql = node.sql("duckdb")
        return {"sql": sql}


def expr_columns(e: Any) -> list[str]:
    out: list[str] = []
    if isinstance(e, dict):
        if "col" in e:
            out.append(e["col"])
        for v in e.values():
            if isinstance(v, (dict, list)):
                out.extend(expr_columns(v))
    elif isinstance(e, list):
        for v in e:
            out.extend(expr_columns(v))
    return out


def has_agg(e: Any) -> bool:
    if isinstance(e, dict):
        if e.get("fn", "").upper() in AGG:
            return True
        if "sql" in e:
            try:
                return any(isinstance(n, exp.AggFunc) for n in sqlglot.parse_one(f"SELECT {e['sql']}", read="duckdb").walk())
            except Exception:  # noqa: BLE001
                return False
        return any(has_agg(v) for v in e.values() if isinstance(v, (dict, list)))
    if isinstance(e, list):
        return any(has_agg(v) for v in e)
    return False


# ------------------------------------------------------------------ visual -> pandas (AI draft generator)
PD_FN = {"UPPER": "{0}.str.upper()", "LOWER": "{0}.str.lower()", "TRIM": "{0}.str.strip()",
         "LTRIM": "{0}.str.lstrip()", "RTRIM": "{0}.str.rstrip()", "ABS": "{0}.abs()", "LENGTH": "{0}.str.len()",
         "FLOOR": "np.floor({0})", "CEIL": "np.ceil({0})"}


class PandasUnsupported(Exception):
    pass


def _pd_expr(e: dict, frame: str, colmap: dict[str, str]) -> str:
    if "col" in e:
        ref = e["col"]
        key = colmap.get(ref) or colmap.get(ref.split(".")[-1])
        if not key:
            raise PandasUnsupported(f"column {ref}")
        return f'{frame}["{key}"]'
    if "lit" in e:
        return repr(e["lit"])
    if "op" in e:
        op = e["op"].upper()
        a = [_pd_expr(x, frame, colmap) for x in e["args"]]
        m = {"+": "+", "-": "-", "*": "*", "/": "/", "=": "==", "!=": "!=", "<>": "!=", "<": "<", "<=": "<=",
             ">": ">", ">=": ">=", "AND": "&", "OR": "|"}
        if op in m:
            return f"({a[0]} {m[op]} {a[1]})"
        if op == "IS NULL":
            return f"{a[0]}.isna()"
        if op == "IS NOT NULL":
            return f"{a[0]}.notna()"
        if op == "NOT":
            return f"(~{a[0]})"
        if op == "IN":
            return f"{a[0]}.isin([{', '.join(a[1:])}])"
        raise PandasUnsupported(op)
    if "fn" in e:
        fn = e["fn"].upper()
        a = [_pd_expr(x, frame, colmap) for x in e.get("args", [])]
        if fn in PD_FN:
            return PD_FN[fn].format(*a)
        if fn == "COALESCE":
            out = a[0]
            for x in a[1:]:
                out = f"{out}.fillna({x})" if not x.startswith(frame) else f"{out}.fillna({x})"
            return out
        if fn == "ROUND":
            return f"_round_half_up({a[0]}, {a[1] if len(a) > 1 else 0})"
        if fn == "CONCAT":
            return " + ".join(f"{x}.astype(str)" if x.startswith(frame) else x for x in a)
        raise PandasUnsupported(fn)
    raise PandasUnsupported(str(e)[:40])


def compile_visual_to_pandas(spec: dict, columns_by_alias: dict[str, list[str]]) -> str:
    """Generate an equivalent pandas transform() for common specs (filters, joins, scalar + aggregate columns)."""
    srcs = spec["sources"]
    L = ["import numpy as np", "import pandas as pd", "", "",
         "def _round_half_up(s, n):",
         "    # Half-up rounding (away from zero), matching SQL ROUND semantics.",
         "    f = 10 ** n",
         "    return np.sign(s) * np.floor(np.abs(s) * f + 0.5 + 1e-9) / f",
         "", "",
         "def transform(sources):"]
    colmap: dict[str, str] = {}
    first = True
    for s in srcs:
        a = s.get("as") or s["alias"]
        v = f"df_{a}"
        L.append(f'    {v} = sources["{s["alias"]}"].copy()')
        for f in s.get("filters", []):
            cm = {c: c for c in columns_by_alias.get(s["alias"], [])}
            cm.update({f"{a}.{c}": c for c in columns_by_alias.get(s["alias"], [])})
            L.append(f"    {v} = {v}[{_pd_expr(norm_cond(f), v, cm)}]")
        if first:
            L.append(f'    df = {v}.add_prefix("{a}.")')
            first = False
        for c in columns_by_alias.get(s["alias"], []):
            colmap[f"{a}.{c}"] = f"{a}.{c}"
    for j in spec.get("joins", []):
        r = j["right"]
        rs = next(s for s in srcs if (s.get("as") or s["alias"]) == r)
        how = {"LEFT": "left", "INNER": "inner", "RIGHT": "right", "FULL": "outer"}[j.get("type", "INNER").upper()]
        lk = [colmap[k["left"]] if k["left"] in colmap else k["left"] for k in j["on"]]
        rk = [f"{r}.{k['right'].split('.')[-1]}" for k in j["on"]]
        L.append(f'    df = df.merge(df_{r}.add_prefix("{r}."), how="{how}", left_on={lk!r}, right_on={rk!r})')
        _ = rs
    for f in spec.get("filters", []):
        L.append(f"    df = df[{_pd_expr(norm_cond(f), 'df', colmap)}]")
    cols = spec["columns"]
    agg_cols = [c for c in cols if has_agg(c["expr"])]
    if not agg_cols:
        L.append("    out = pd.DataFrame(index=df.index)")
        for c in cols:
            L.append(f'    out["{c["target"]}"] = {_pd_expr(c["expr"], "df", colmap)}')
    else:
        keys = [c for c in cols if not has_agg(c["expr"])]
        for c in keys:
            L.append(f'    df["_k_{c["target"]}"] = {_pd_expr(c["expr"], "df", colmap)}')
        aggdefs = []
        post: dict[str, str] = {}
        for i, c in enumerate(agg_cols):
            inner_holder: list = []
            post[c["target"]] = _pd_post(c["expr"], colmap, aggdefs, inner_holder, L, i)
        gb = [f"_k_{c['target']}" for c in keys]
        L.append(f"    g = df.groupby({gb!r}, dropna=False, sort=False)")
        L.append("    out = g.agg(**{")
        for name, col, how in aggdefs:
            L.append(f'        "{name}": ("{col}", {how}),')
        L.append("    }).reset_index()")
        L.append(f'    out = out.rename(columns={{{", ".join(repr(k) + ": " + repr(k[3:]) for k in gb)}}})')
        for t, e in post.items():
            L.append(f'    out["{t}"] = {e}')
        L.append(f"    out = out[{[c['target'] for c in cols]!r}]")
    L.append("    return out.reset_index(drop=True)")
    return "\n".join(L) + "\n"


def _pd_post(e: dict, colmap, aggdefs, holder, L, idx) -> str:
    """Translate an aggregate-bearing expression: inner aggregate inputs become pre-agg helper columns."""
    if "fn" in e and e["fn"].upper() in AGG:
        fn = e["fn"].upper()
        n = len(aggdefs)
        name = f"_agg{n}"
        if fn == "COUNT" and not e.get("args"):
            L.append(f'    df["_in{n}"] = 1')
            aggdefs.append((name, f"_in{n}", '"sum"'))
        else:
            L.append(f'    df["_in{n}"] = {_pd_expr(e["args"][0], "df", colmap)}')
            how = {"SUM": 'lambda s: s.sum(min_count=1)', "COUNT": '"count"', "AVG": '"mean"', "MIN": '"min"',
                   "MAX": '"max"', "COUNT_DISTINCT": '"nunique"'}[fn]
            aggdefs.append((name, f"_in{n}", how))
        return f'out["{name}"]'
    if "fn" in e:
        fn = e["fn"].upper()
        a = [_pd_post(x, colmap, aggdefs, holder, L, idx) if has_agg(x) else _pd_expr(x, "out", {}) for x in e.get("args", [])]
        if fn == "COALESCE":
            return f"{a[0]}.fillna({a[1]})"
        if fn == "ROUND":
            return f"_round_half_up({a[0]}, {a[1] if len(a) > 1 else 0})"
        if fn in PD_FN:
            return PD_FN[fn].format(*a)
        raise PandasUnsupported(fn)
    if "op" in e and e["op"] in ("+", "-", "*", "/"):
        a = [_pd_post(x, colmap, aggdefs, holder, L, idx) if has_agg(x) else _pd_expr(x, "out", {}) for x in e["args"]]
        return f"({a[0]} {e['op']} {a[1]})"
    if "lit" in e:
        return repr(e["lit"])
    raise PandasUnsupported("post-aggregate expression")
