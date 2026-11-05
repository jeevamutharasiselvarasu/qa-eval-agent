"""SyntheticDataGenerator. Seeded, rule-driven synthetic source data per scenario. Expected targets are produced
ONLY by executing the approved transformation version through the common engine (never guessed).
Synthetic artifacts are stored under runs/<id>/synthetic/ with track=SYNTHETIC and never mixed with uploads."""
from __future__ import annotations

import datetime as dt
import random
import re
from dataclasses import dataclass, field
from pathlib import Path

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core.common import audit, is_sensitive, run_dir, set_state
from ..models import ColumnMapping, Dataset, QARun, Requirement, RequirementGap, SyntheticScenario, TestCase
from .approvals import approval_state, current_version
from .datasets import duck_columns, schema_fingerprint
from ..core.common import sha256_file
from .studio import actual_inputs, run_version

SCENARIOS = ["POSITIVE", "FILTER_EXCLUSION", "NO_MATCH_JOIN", "MULTI_MATCH_JOIN", "DUPLICATE_KEYS", "NULL_KEY",
             "BOUNDARY_ZERO", "DECIMAL_PRECISION", "SPECIAL_CHARACTERS", "EMPTY_RIGHT_SOURCE", "NULL_VALUES", "NEGATIVE_RESULT"]
RESOLVED = ("CLARIFIED", "ACCEPTED_LIMITATION")


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
    categories: dict[tuple[str, str], list] = field(default_factory=dict)
    ranges: dict[tuple[str, str], tuple] = field(default_factory=dict)
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
    reqs = {r.id: r for r in db.scalars(select(Requirement).where(Requirement.run_id == run_id)) if r.status != "REJECTED"}
    cols = {a: d.columns for a, d in srcs.items()}
    m = Model(aliases=list(srcs), base=next(iter(srcs)), columns=cols)
    for j in [x for x in maps if x.mapping_type == "JOINED"]:
        mm = re.match(r"(\w+) JOIN (\w+) ON (.+)$", j.expression)
        if mm:
            pairs = [p.strip().split(" = ") for p in mm.group(3).split(" AND ")]
            left = pairs[0][0].split(".")[0]
            m.join = {"type": mm.group(1), "left": left, "right": mm.group(2), "keys": [p[0].split(".")[1] for p in pairs],
                      "req": j.requirement_ids}
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
            m.numeric_rules.append({"target": mp.target_column, "cols": num, "minus": [((a, b), (c, d)) for a, b, c, d in minus],
                                    "req": mp.requirement_ids, "expr": mp.expression})
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
                n, d = c.execute(f"SELECT COUNT(*), COUNT(DISTINCT ({kl})) FROM read_parquet(?)", [srcs[m.join["right"]].parquet_path]).fetchone()
                m.right_unique = n > 0 and n == d
    # domain values and ranges from actual data (values only, never whole records)
    for a, d in srcs.items():
        with duckdb.connect(":memory:") as c:
            c.execute(f"CREATE VIEW t AS SELECT * FROM read_parquet('{d.parquet_path}')")
            for col in d.columns:
                fam = _fam(col["type"])
                q = '"' + col["name"].replace('"', '""') + '"'
                if fam == "str" and not is_sensitive(col["name"]):
                    nd = c.execute(f"SELECT COUNT(DISTINCT {q}) FROM t").fetchone()[0]
                    total = c.execute("SELECT COUNT(*) FROM t").fetchone()[0]
                    if 0 < nd <= 12 and nd < max(2, total * 0.6):
                        m.categories[(a, col["name"])] = [r[0] for r in c.execute(f"SELECT DISTINCT {q} FROM t WHERE {q} IS NOT NULL ORDER BY 1").fetchall()]
                elif fam in ("num", "int", "date"):
                    mn, mx = c.execute(f"SELECT MIN({q}), MAX({q}) FROM t").fetchone()
                    m.ranges[(a, col["name"])] = (mn, mx)
    return m


class Gen:
    def __init__(self, model: Model, seed: int, scenario_idx: int):
        self.m = model
        self.rnd = random.Random(seed * 1000 + scenario_idx)
        self.seq = 0
        self.si = scenario_idx

    def _id(self, col: str, typ: str) -> object:
        self.seq += 1
        fam = _fam(typ)
        if fam == "int":
            return 900000 + self.si * 1000 + self.seq
        if fam == "num":
            return float(900000 + self.si * 1000 + self.seq)
        if fam == "date":
            return dt.date(2030, 1, 1) + dt.timedelta(days=self.si * 400 + self.seq)
        prefix = re.sub(r"[^A-Z]", "", col.upper())[:2] or "K"
        return f"SYN{self.si:02d}{prefix}{self.seq:04d}"

    def value(self, alias: str, col: dict, i: int) -> object:
        name, typ = col["name"], col["type"]
        fam = _fam(typ)
        cats = self.m.categories.get((alias, name))
        if cats:
            return self.rnd.choice(cats)
        if fam == "str":
            if is_sensitive(name):
                return f"synthetic{self.si}_{i}@example.test" if "MAIL" in name.upper() else f"SYN-{self.si}-{i}"
            return f"Synthetic {name.title().replace('_', ' ')} {self.si}-{i}"
        if fam in ("num", "int"):
            lo, hi = self.m.ranges.get((alias, name), (0, 1000))
            lo = max(float(lo or 0), 0.0)
            hi = float(hi or 1000) or 1000.0
            v = self.rnd.uniform(lo, hi)
            return int(v) if fam == "int" else round(v, 2)
        if fam == "date":
            lo, _ = self.m.ranges.get((alias, name), (dt.date(2026, 1, 1), None))
            base = lo if isinstance(lo, (dt.date, dt.datetime)) else dt.date(2026, 1, 1)
            return base + dt.timedelta(days=self.rnd.randint(0, 60))
        if fam == "bool":
            return self.rnd.random() < 0.5
        return None

    def record(self, alias: str, i: int, fixed: dict | None = None) -> dict:
        rec = {}
        for col in self.m.columns[alias]:
            rec[col["name"]] = self.value(alias, col, i)
        # unique ids for single-column identifiers that look like keys (e.g. TXN_ID)
        for col in self.m.columns[alias]:
            if re.search(r"(^|_)ID$", col["name"].upper()) and not (self.m.join and col["name"] in self.m.join["keys"]) \
                    and col["name"] not in self.m.key:
                rec[col["name"]] = self._id(col["name"], col["type"])
        # keep subtraction pairs non-negative by default
        for rule in self.m.numeric_rules:
            for (a1, c1), (a2, c2) in rule["minus"]:
                if a1 == alias == a2 and isinstance(rec.get(c1), (int, float)):
                    rec[c2] = round(rec[c1] * self.rnd.uniform(0, 0.2), 2)
        f = self.m.filter
        if f and f["alias"] == alias:
            rec[f["column"]] = passing_value(f, rec.get(f["column"]))
        rec.update(fixed or {})
        return rec

    def key_value(self, col: str | None = None) -> object:
        kcol = col or (self.m.key or (self.m.join["keys"] if self.m.join else ["ID"]))[0]
        typ = next((c["type"] for c in self.m.columns[self.m.base] if c["name"].upper() == kcol.upper()), "VARCHAR")
        return self._id(kcol, typ)


def passing_value(f: dict, current):
    """A value that satisfies the filter (current value kept when it already does)."""
    op, v = f["op"], f["value"]
    if op == "=":
        return v
    if isinstance(v, (int, float)):
        ok = {">": lambda x: x > v, ">=": lambda x: x >= v, "<": lambda x: x < v, "<=": lambda x: x <= v,
              "!=": lambda x: x != v, "<>": lambda x: x != v}.get(op)
        if ok and isinstance(current, (int, float)) and ok(current):
            return current
        return {">": v + 1, ">=": v, "<": v - 1, "<=": v, "!=": v + 1, "<>": v + 1}.get(op, current)
    return current


def failing_values(f: dict, categories: list) -> list:
    """Values that fail the filter, boundary values first."""
    op, v = f["op"], f["value"]
    if isinstance(v, (int, float)):
        return {">": [v, v - 1], ">=": [v - 1], "<": [v, v + 1], "<=": [v + 1], "=": [v + 1], "!=": [v], "<>": [v]}.get(op, [])
    if op == "=":
        return [c for c in categories if c != v] or ["__OTHER__"]
    if op in ("!=", "<>"):
        return [v]
    return []


def _base_key(m: Model) -> str | None:
    cand = m.key or (m.join["keys"] if m.join else [])
    for k in cand:
        for c in m.columns[m.base]:
            if c["name"].upper() == k.upper():
                return c["name"]
    return None


def _right_key(m: Model) -> str | None:
    if not m.join:
        return None
    k = m.join["keys"][0]
    return next((c["name"] for c in m.columns[m.join["right"]] if c["name"].upper() == k.upper()), None)


def design(m: Model, kind: str, n: int, gen: Gen, gaps: list[RequirementGap], reqs: list[Requirement]) -> dict:
    """Return {data: {alias: [rows]}, explanation, assumptions, blocked, gap_ids, req_ids, applicable}."""
    bk, rk = _base_key(m), _right_key(m)
    right = m.join["right"] if m.join else None
    jl = None
    if m.join:
        jl = next((c["name"] for c in m.columns[m.base] if c["name"].upper() == m.join["keys"][0].upper()), None)
    per = 1 if m.right_unique else 2
    data = {a: [] for a in m.aliases}
    expl, assum, req_ids, gap_ids = [], [], [], []
    blocked = None

    def base_rec(i, **fx):
        fixed = {bk: gen.key_value(bk)} if bk else {}
        if jl and jl != bk:
            fixed[jl] = gen.key_value(jl)
        r = gen.record(m.base, i, fixed)
        r.update(fx)
        data[m.base].append(r)
        return r

    def right_recs(key, count, **fx):
        for j in range(count):
            data[right].append(gen.record(right, j, {rk: key, **fx}))

    def place(i, values: dict, n_right: int = 1):
        """Base record (+ matching right records) with column values placed on whichever source owns them."""
        r = base_rec(i, **{c: v for (a, c), v in values.items() if a == m.base})
        if right:
            right_recs(r[jl], n_right, **{c: v for (a, c), v in values.items() if a == right})
        return r

    nb = max(2, n // 3)
    if kind == "POSITIVE":
        for i in range(nb):
            r = base_rec(i)
            if right:
                right_recs(r[jl], per)
        expl.append(f"{nb} valid {m.base} records" + (f", each with {per} matching {right} record(s)" if right else "") + ", all satisfying the filters.")
    elif kind == "FILTER_EXCLUSION" and m.filter and m.filter["alias"] == m.base:
        f = m.filter
        bad = failing_values(f, m.categories.get((m.base, f["column"]), []))
        if not bad:
            return {"skip": "Cannot derive failing values for the filter"}
        for i in range(nb):
            r = base_rec(i, **{f["column"]: bad[i % len(bad)]})
            if right:
                right_recs(r[jl], 1)
        r = base_rec(nb)
        if right:
            right_recs(r[jl], 1)
        expl.append(f"{nb} {m.base} records fail {f['column']} {f['op']} {f['value']!r} (values used, boundary first: "
                    f"{', '.join(map(str, bad))}); 1 passing control record.")
        req_ids += f["req"]
    elif kind == "FILTER_EXCLUSION":
        if not (m.filter and right and m.filter["alias"] == right):
            return {"skip": "No filter rule on a joined source"}
        others = [v for v in m.categories.get((right, m.filter["column"]), []) if v != m.filter["value"]] or ["__OTHER__"]
        for i in range(nb):
            r = base_rec(i)
            right_recs(r[jl], 2, **{m.filter["column"]: gen.rnd.choice(others)})
        r = base_rec(nb)
        right_recs(r[jl], 1)
        right_recs(r[jl], 1, **{m.filter["column"]: others[0]})
        expl.append(f"{nb} {m.base} records whose {right} rows all fail {m.filter['column']} {m.filter['op']} '{m.filter['value']}' "
                    f"(values used: {', '.join(map(str, others))}); 1 record with one included and one excluded row.")
        req_ids += m.filter["req"]
    elif kind == "NO_MATCH_JOIN":
        if not right:
            return {"skip": "No join rule"}
        for i in range(nb):
            base_rec(i)
        r = base_rec(nb)
        right_recs(r[jl], 1)
        expl.append(f"{nb} {m.base} records with no {right} rows at all (unmatched {m.join['type']} join), plus 1 matched control record.")
        req_ids += m.join["req"]
    elif kind == "MULTI_MATCH_JOIN":
        if not right:
            return {"skip": "No join rule"}
        for i in range(nb):
            r = base_rec(i)
            right_recs(r[jl], gen.rnd.randint(3, 6))
        expl.append(f"{nb} {m.base} records each matching 3–6 {right} rows (one-to-many aggregation).")
        req_ids += m.join["req"]
    elif kind == "DUPLICATE_KEYS":
        if not (m.key and bk):
            return {"skip": "No business key rule"}
        for i in range(nb):
            r = base_rec(i)
            if right:
                right_recs(r[jl], 1)
        dup = dict(data[m.base][0])
        data[m.base].append(dup)
        expl.append(f"Record with {bk}={dup[bk]} appears twice in {m.base} (exact duplicate).")
        assum.append("Jira states the target key must be unique but does not define source de-duplication; the expected "
                     "values below are whatever the approved transformation produces — review them.")
        req_ids += m.req_ids.get("key", [])
    elif kind == "NULL_KEY":
        if not (bk and m.req_ids.get("not_null")):
            return {"skip": "No not-null key rule"}
        for i in range(nb):
            r = base_rec(i)
            if right:
                right_recs(r[jl], 1)
        base_rec(nb, **{bk: None})
        expl.append(f"One {m.base} record has a NULL {bk}; the target must not contain null keys.")
        req_ids += m.req_ids["not_null"]
    elif kind == "BOUNDARY_ZERO":
        if not m.numeric_rules:
            return {"skip": "No numeric derivation"}
        zero_cols = {c for rule in m.numeric_rules for c in rule["cols"]}
        for i in range(nb):
            r = base_rec(i, **{c: 0 for a, c in zero_cols if a == m.base})
            if right:
                right_recs(r[jl], 2, **{c: 0 for a, c in zero_cols if a == right})
        expl.append("All numeric inputs to derived columns set to 0: " + ", ".join(f"{a}.{c}" for a, c in sorted(zero_cols)))
        req_ids += [x for rule in m.numeric_rules for x in rule["req"]]
    elif kind == "DECIMAL_PRECISION":
        if m.precision is None or not m.numeric_rules:
            return {"skip": "No rounding precision rule"}
        p = m.precision
        rule = next((r for r in m.numeric_rules if f", {p})" in r["expr"]), m.numeric_rules[0])
        main = rule["minus"][0][0] if rule["minus"] else rule["cols"][-1]
        others = {}
        for col in rule["cols"]:
            if col == main:
                continue
            others[col] = 0 if any(col == b for _, b in rule["minus"]) else 1  # neutral element for - / *
        vals = [10 + 5 / 10 ** (p + 1), 2.5 / 10 ** p + 1, 99.994999, 0.1 + 0.2]
        for i, v in enumerate(vals):
            place(i, {main: round(v, p + 4), **others})
        expl.append(f"{main[0]}.{main[1]} values with more than {p} decimals ({', '.join(str(round(v, p + 4)) for v in vals)}) to exercise "
                    f"ROUND(…, {p}) and the rounding mode" + (f"; other inputs held neutral ({', '.join(f'{c}={x}' for (_, c), x in others.items())})" if others else "")
                    + ". Check expected values by hand: half-up of 10.005 is 10.01.")
        req_ids += rule["req"]
    elif kind == "SPECIAL_CHARACTERS":
        if not m.string_rules:
            return {"skip": "No string transformation rule"}
        specials = ["  O'Brien  ", "José Ñúñez", "Anne-Marie\tLee", "ZOË 李 ", "  "]
        cols = [(a, c) for rule in m.string_rules for a, c in rule["cols"] if a == m.base]
        for i, s in enumerate(specials):
            r = base_rec(i, **{c: s for a, c in cols})
            if right:
                right_recs(r[jl], 1)
        expl.append("String inputs with apostrophes, accents, tabs, CJK characters and whitespace-only values for " +
                    ", ".join(c for _, c in cols))
        req_ids += [x for rule in m.string_rules for x in rule["req"]]
    elif kind == "EMPTY_RIGHT_SOURCE":
        if not right:
            return {"skip": "No join rule"}
        for i in range(nb):
            base_rec(i)
        expl.append(f"{right} is empty; every {m.base} record is unmatched.")
        req_ids += m.join["req"]
    elif kind.startswith("NULL_VALUES:"):
        alias, col = kind.split(":", 1)[1].split(".")
        resolved = [g for g in gaps if g.affected.get("topic") == "NULL_HANDLING" and col in g.affected.get("columns", []) and g.status in RESOLVED]
        open_ = [g for g in gaps if g.affected.get("topic") == "NULL_HANDLING" and col in g.affected.get("columns", []) and g.status not in RESOLVED]
        explicit = bool(re.search(rf"(COALESCE|IFNULL)\(\s*\"?(\w+\"?\.\"?)?{col}\"?\s*,", " ".join(r["expr"] for r in m.numeric_rules), re.I))
        gap_ids = [g.id for g in open_ + resolved]
        if not resolved and not explicit:
            blocked = (f"Null handling for {alias}.{col} is not specified" + (f" (open gap {', '.join(g.id for g in open_)})" if open_ else
                                                                            " in Jira") + "; expected output cannot be determined.")
        for i in range(nb):
            r = base_rec(i)
            if right:
                right_recs(r[jl], 2)
        target = data[alias]
        if target:
            target[0][col] = None
        expl.append(f"{alias}.{col} is NULL in one record.")
        if resolved:
            assum.append("Clarification applied: " + "; ".join(f"{g.id}: {g.clarification}" for g in resolved))
    elif kind == "NEGATIVE_RESULT":
        pair = next(((rule, rule["minus"][0]) for rule in m.numeric_rules if rule["minus"]), None)
        if not pair:
            return {"skip": "No subtraction rule"}
        rule, ((a1, c1), (a2, c2)) = pair
        g_all = [g for g in gaps if g.affected.get("topic") == "NEGATIVE_VALUES" and
                 ({rule["target"], c1, c2} & set(g.affected.get("columns", [])))]
        gap_ids = [g.id for g in g_all]
        if not any(g.status in RESOLVED for g in g_all):
            blocked = f"Behaviour when {c2} exceeds {c1} (negative {rule['target']}) is not specified" + \
                      (f" (open gap {', '.join(gap_ids)})" if gap_ids else "")
        for i in range(nb):
            place(i, {(a1, c1): 50.0, (a2, c2): 80.0})
        expl.append(f"{c2} (80.00) exceeds {c1} (50.00) so {rule['target']} would be negative.")
    return {"data": data, "explanation": expl, "assumptions": assum, "blocked": blocked, "gap_ids": gap_ids,
            "req_ids": sorted(set(req_ids))}


def _write(alias_rows: list[dict], schema: pa.Schema, path: Path) -> None:
    cols = {}
    for f in schema:
        vals = [r.get(f.name) for r in alias_rows]
        try:
            cols[f.name] = pa.array(vals, type=f.type)
        except (pa.ArrowInvalid, pa.ArrowTypeError):
            cols[f.name] = pa.array([None if v is None else str(v) for v in vals], type=pa.string()).cast(f.type, safe=False)
    pq.write_table(pa.table(cols, schema=schema), path)


def generate(db: Session, run: QARun, actor: str, scenarios: list[str] | None = None, records: int | None = None,
             seed: int | None = None) -> list[SyntheticScenario]:
    v = current_version(db, run.id)
    st = approval_state(db, run.id)
    if not v or st["TRANSFORMATION"]["status"] != "APPROVED":
        raise ValueError("Approve the current transformation version first: synthetic expected targets are produced by "
                         "executing the approved transformation.")
    s = run.settings or {}
    records = int(records or s.get("synthetic_records_per_scenario", 12))
    seed = int(seed if seed is not None else s.get("synthetic_seed", 42))
    m = build_model(db, run.id)
    gaps = list(db.scalars(select(RequirementGap).where(RequirementGap.run_id == run.id)))
    reqs = list(db.scalars(select(Requirement).where(Requirement.run_id == run.id)))
    null_cols = sorted({f"{a}.{c}" for rule in m.numeric_rules for a, c in rule["cols"]})
    full = [k for k in SCENARIOS if k != "NULL_VALUES"] + [f"NULL_VALUES:{x}" for x in null_cols]
    selected = [k for k in full if not scenarios or k in scenarios or k.split(":")[0] in scenarios]
    ids = {k: f"SCN-{i:02d}" for i, k in enumerate(full, 1)}
    # regenerate only the selected scenarios; their previous artifacts leave the active set (files stay for audit)
    for old in db.scalars(select(SyntheticScenario).where(SyntheticScenario.run_id == run.id)):
        if old.scenario_type in selected or old.id not in ids.values():
            db.delete(old)
    keep_ids = {ids[k] for k in full if k not in selected}
    for d in db.scalars(select(Dataset).where(Dataset.run_id == run.id, Dataset.track == "SYNTHETIC")):
        if d.scenario_id not in keep_ids:
            db.delete(d)
    db.flush()
    schemas = {a: pq.read_schema(Path(d.parquet_path)) for a, d in actual_inputs(db, run.id).items()}
    out = []
    for kind in selected:
        idx = full.index(kind) + 1
        gen = Gen(m, seed, idx)
        d = design(m, kind, records, gen, gaps, reqs)
        if "skip" in d:
            continue
        sc = SyntheticScenario(id=ids[kind], run_id=run.id, scenario_type=kind,
                               name=kind.replace("_", " ").title().replace(":", " — "), description=" ".join(d["explanation"]),
                               seed=seed, records=records, gap_ids=d["gap_ids"], requirement_ids=d["req_ids"],
                               explanation=d["explanation"], assumptions=d["assumptions"],
                               generation_method=f"SEEDED_RULE_BASED(seed={seed}, scenario_index={idx})")
        db.add(sc)
        folder = run_dir(run.id, "synthetic", sc.id)
        ds_map: dict[str, Dataset] = {}
        for alias, rows in d["data"].items():
            p = folder / f"{alias}.parquet"
            _write(rows, schemas[alias], p)
            cols = duck_columns(p)
            ds = Dataset(run_id=run.id, role="SYNTHETIC_SOURCE", track="SYNTHETIC", alias=alias, label=f"{sc.name}: {alias}",
                         filename=p.name, file_format="parquet", size_bytes=p.stat().st_size, parquet_path=str(p),
                         fingerprint=sha256_file(p), schema_fingerprint=schema_fingerprint(cols), row_count=len(rows),
                         columns=cols, scenario_id=sc.id)
            db.add(ds)
            ds_map[alias] = ds
        db.flush()
        sc.source_dataset_ids = {a: x.id for a, x in ds_map.items()}
        if d["blocked"]:
            sc.status, sc.blocked_reason = "BLOCKED", d["blocked"]
        else:
            res, exp_ds = run_version(db, run, v, ds_map, purpose="SYNTHETIC_EXPECTED", role="SYNTHETIC_EXPECTED",
                                      track="SYNTHETIC", actor=actor, scenario_id=sc.id, out_dir=folder)
            if res.status == "SUCCESS":
                sc.status, sc.expected_dataset_id, sc.transformation_version_id = "GENERATED", exp_ds.id, v.id
            else:
                sc.status, sc.blocked_reason = "ERROR", "; ".join(e.message for e in res.errors)
        out.append(sc)
    set_state(db, run, "SYNTHETIC_DATA_GENERATED", actor)
    audit(db, actor, "SYNTHETIC_GENERATED", run.id, "QA_RUN", run.id, scenarios=len(out), seed=seed, records=records,
          blocked=[s.id for s in out if s.status == "BLOCKED"])
    from .testgen import generate_synthetic_tests
    generate_synthetic_tests(db, run, actor)
    return out
