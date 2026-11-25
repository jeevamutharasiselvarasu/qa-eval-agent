"""Rule model: what the accepted requirements and mappings say about the source data (base table, join, filter,
numeric and string rules). Used to derive the test cases."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import duckdb
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import ColumnMapping, Requirement
from .studio import actual_inputs


@dataclass
class Model:
    """What the rules tell us about the data."""

    aliases: list[str]
    base: str
    columns: dict[str, list[dict]]
    key: list[str] = field(default_factory=list)  # business key columns on base
    join: dict | None = None  # {left, right, type, keys}
    filter: dict | None = None  # {alias, column, op, value, req}
    numeric_rules: list[dict] = field(default_factory=list)  # {target, cols:[(alias,col)], minus:[(a,b)], req}
    string_rules: list[dict] = field(default_factory=list)  # {target, cols:[(alias,col)], req}
    precision: int | None = None
    req_ids: dict[str, list[str]] = field(default_factory=dict)
    right_unique: bool = False  # join key unique on the right side (dimension/lookup)


def _fam(t: str) -> str:
    t = t.lower()
    if any(x in t for x in ("int", "double", "decimal", "float")):
        return "int" if "int" in t else "num"
    if "date" in t or "timestamp" in t:
        return "date"
    if "bool" in t:
        return "bool"
    return "str"


def build_model(db: Session, run_id: str) -> Model:
    srcs = actual_inputs(db, run_id)
    maps = [m for m in db.scalars(select(ColumnMapping).where(ColumnMapping.run_id == run_id)) if m.status == "ACCEPTED"]
    reqs = {r.id: r for r in db.scalars(select(Requirement).where(Requirement.run_id == run_id)) if r.status == "ACCEPTED"}
    cols = {a: d.columns for a, d in srcs.items()}
    m = Model(aliases=list(srcs), base=next(iter(srcs)), columns=cols)
    for j in [x for x in maps if x.mapping_type == "JOINED"]:
        mm = re.match(r"(\w+) JOIN (\w+) ON (.+)$", j.expression)
        if mm:
            pairs = [p.strip().split(" = ") for p in mm.group(3).split(" AND ")]
            left = pairs[0][0].split(".")[0]
            m.join = {
                "type": mm.group(1),
                "left": left,
                "right": mm.group(2),
                "keys": [p[0].split(".")[1] for p in pairs],
                "req": j.requirement_ids,
            }
            m.base = left
    for f in [x for x in maps if x.mapping_type == "FILTERED"]:
        mm = re.match(r"(\w+)\.(\w+)\s*(=|!=|<>|>=|<=|>|<)\s*(.+)$", f.expression)
        if mm:
            v = mm.group(4).strip("'") if mm.group(4).startswith("'") else float(mm.group(4))
            m.filter = {"alias": mm.group(1), "column": mm.group(2), "op": mm.group(3), "value": v, "req": f.requirement_ids}
    keyreq = [r for r in reqs.values() if r.kind == "BUSINESS_KEY"] or [r for r in reqs.values() if r.kind == "UNIQUE_KEY"]
    if keyreq:
        m.key = keyreq[0].spec.get("columns", [])
        m.req_ids["key"] = [r.id for r in keyreq]
    m.req_ids["not_null"] = [r.id for r in reqs.values() if r.kind == "NOT_NULL"]
    for mp in [x for x in maps if x.target_column and x.mapping_type not in ("UNMAPPED_TARGET",)]:
        refs = [(s.split(".")[0], s.split(".")[1]) for s in mp.source_columns if "." in s]
        num = [r for r in refs if _fam(next((c["type"] for c in cols.get(r[0], []) if c["name"] == r[1]), "")) in ("num", "int")]
        minus = re.findall(r"(\w+)\.(\w+)\s*-\s*(\w+)\.(\w+)", mp.expression.replace('"', ""))
        if num and re.search(r"[-+*/]|SUM|AVG", mp.expression, re.I):
            m.numeric_rules.append(
                {
                    "target": mp.target_column,
                    "cols": num,
                    "minus": [((a, b), (c, d)) for a, b, c, d in minus],
                    "req": mp.requirement_ids,
                    "expr": mp.expression,
                }
            )
        if re.search(r"UPPER|LOWER|TRIM|CONCAT|REPLACE", mp.expression, re.I):
            m.string_rules.append({"target": mp.target_column, "cols": refs, "req": mp.requirement_ids})
        p = re.search(r"ROUND\(.*,\s*(\d+)\)", mp.expression)
        if p:
            m.precision = int(p.group(1))
    if m.join and m.join["right"] in srcs:
        rk = [c["name"] for c in cols[m.join["right"]] if c["name"].upper() in {k.upper() for k in m.join["keys"]}]
        if rk:
            with duckdb.connect(":memory:") as c:
                kl = ", ".join('"' + k + '"' for k in rk)
                n, d = c.execute(
                    f"SELECT COUNT(*), COUNT(DISTINCT ({kl})) FROM read_parquet(?)", [srcs[m.join["right"]].parquet_path]
                ).fetchone()
                m.right_unique = n > 0 and n == d
    return m


def base_key(m: Model) -> str | None:
    cand = m.key or (m.join["keys"] if m.join else [])
    for k in cand:
        for c in m.columns[m.base]:
            if c["name"].upper() == k.upper():
                return c["name"]
    return None


def right_key(m: Model) -> str | None:
    if not m.join:
        return None
    k = m.join["keys"][0]
    return next((c["name"] for c in m.columns[m.join["right"]] if c["name"].upper() == k.upper()), None)
