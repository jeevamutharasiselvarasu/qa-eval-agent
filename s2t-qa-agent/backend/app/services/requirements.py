"""RequirementIntelligenceService.

Deterministic, evidence-quoting extraction of typed rules from Jira content (provenance JIRA_EXPLICIT /
ATTACHMENT_DERIVED), deterministic gap detection, and an optional AI pass whose output is stored only as
AI_INFERRED proposals. Nothing is invented: every rule keeps its exact source sentence.
"""
from __future__ import annotations

import csv
import hashlib
import io
import re
from dataclasses import dataclass, field

import sqlglot
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core.common import audit
from ..models import LLMCall, Requirement, RequirementGap
from .llm import get_llm, timed

IDENT = r"[A-Z][A-Z0-9_]+"
INJECTION_PATTERNS = [
    r"ignore (all |any )?(previous|prior|above) instructions",
    r"^\s*(system|assistant)\s*:",
    r"mark (every|all) tests? as (pass|passed)",
    r"you are now",
    r"disregard (the )?(rules|instructions)",
]


@dataclass
class TextUnit:
    ref: str
    text: str
    ac_id: str | None = None


@dataclass
class Extracted:
    kind: str
    description: str
    spec: dict
    provenance: str
    ref: str
    evidence: str
    ac_id: str | None = None
    confidence: str = "HIGH"
    status: str = "ACCEPTED"


@dataclass
class AnalysisResult:
    requirements: list[Extracted] = field(default_factory=list)
    gaps: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


# ------------------------------------------------------------------ helpers

def _sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[.?!])\s+(?=[A-Z])", text.strip())
    return [p.strip() for p in parts if p.strip()]


def _take_expression(s: str) -> str:
    """Read a SQL-ish expression from the start of s, stopping at depth-0 prose (lowercase words, commas)."""
    out, depth, i = [], 0, 0
    tokens = re.findall(r"'[^']*'|[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)?|\d+(?:\.\d+)?|[()+\-*/,]|\S", s)
    for tok in tokens:
        if depth == 0:
            if tok == "," or tok == ".":
                break
            if re.fullmatch(r"[a-z][a-z0-9_]*", tok):  # prose word at top level
                break
        if tok == "(":
            depth += 1
        elif tok == ")":
            depth -= 1
            if depth < 0:
                break
        out.append(tok)
        i += 1
    expr = " ".join(out)
    expr = re.sub(r"\s*\(\s*", "(", expr)
    expr = re.sub(r"\s*\)", ")", expr)
    expr = re.sub(r"\s*,\s*", ", ", expr)
    return expr.strip()


def normalize_expr(expr: str) -> str:
    """Canonical SQL with table qualifiers removed (for conflict detection)."""
    try:
        tree = sqlglot.parse_one(expr, read="duckdb")
        for col in tree.find_all(sqlglot.exp.Column):
            col.set("table", None)
        return tree.sql("duckdb").upper()
    except Exception:  # noqa: BLE001
        return re.sub(r"\s+", "", re.sub(r"\b\w+\.", "", expr)).upper()


def columns_in(expr: str) -> list[str]:
    try:
        tree = sqlglot.parse_one(expr, read="duckdb")
        return sorted({c.name for c in tree.find_all(sqlglot.exp.Column)})
    except Exception:  # noqa: BLE001
        return sorted(set(re.findall(IDENT, expr)) - {"SUM", "COUNT", "AVG", "MIN", "MAX", "ROUND", "UPPER", "LOWER", "TRIM"})


def _gap_id(*parts: str) -> str:
    return "GAP-" + hashlib.sha1("|".join(parts).encode()).hexdigest()[:8].upper()


def detect_injection(text: str) -> bool:
    return any(re.search(p, text, re.I | re.M) for p in INJECTION_PATTERNS)


# ------------------------------------------------------------------ extraction

def collect_units(doc: dict) -> tuple[list[TextUnit], list[TextUnit], list[dict]]:
    """Return (acceptance-criterion units, other description/custom-field units, comments)."""
    acs: list[TextUnit] = []
    other: list[TextUnit] = []
    desc = doc.get("description", "") or ""
    in_ac = False
    n_auto = 0
    for raw in desc.splitlines():
        line = raw.strip()
        if not line:
            continue
        if re.match(r"^(h\d\.\s*)?acceptance criteria\b", line, re.I):
            in_ac = True
            continue
        m = re.match(r"^(AC[-\s]?(\d+))\s*[:.)-]\s*(.+)$", line, re.I)
        if m:
            acs.append(TextUnit(f"description:AC{m.group(2)}", m.group(3).strip(), f"AC{m.group(2)}"))
            continue
        if in_ac and re.match(r"^([-*•]|\d+[.)])\s+", line):
            n_auto += 1
            acs.append(TextUnit(f"description:AC-item-{n_auto}", re.sub(r"^([-*•]|\d+[.)])\s+", "", line), f"AC{len(acs)+1}"))
            continue
        if in_ac and re.match(r"^[A-Z][A-Za-z ]{2,40}$", line):  # next heading
            in_ac = False
        other.append(TextUnit("description", line))
    for name, val in (doc.get("custom_fields") or {}).items():
        for i, line in enumerate([l.strip(" -*\t") for l in str(val).splitlines() if l.strip()], 1):
            if "acceptance" in name.lower():
                acs.append(TextUnit(f"custom_field:{name}#{i}", line, f"AC-CF{i}"))
            else:
                other.append(TextUnit(f"custom_field:{name}", line))
    return acs, other, doc.get("comments") or []


def extract_rules(unit: TextUnit, provenance: str = "JIRA_EXPLICIT") -> list[Extracted]:
    out: list[Extracted] = []
    for sent in _sentences(unit.text):
        s = sent.rstrip(".")

        def add(kind, desc, spec, conf="HIGH"):
            out.append(Extracted(kind, desc, spec, provenance, unit.ref, sent, unit.ac_id, conf))

        KEYLIST = rf"{IDENT}(?:\s*(?:,|\+|\band\b)\s*{IDENT})*"
        m = (re.search(rf"\b({KEYLIST})\s+(?:together\s+)?(?:is|are|form|forms|make up|makes up)\s+the\s+(?:composite\s+)?(?:business|primary|natural)\s+key", s)
             or re.search(rf"\b(?:business|primary|natural|composite)\s+key\b(?:\s+of\s+\w+)?\s+(?:is|are|=)\s+({KEYLIST})", s))
        if m:
            cols = re.findall(IDENT, m.group(1))
            add("BUSINESS_KEY", f"{' + '.join(cols)} {'is' if len(cols) == 1 else 'form'} the business key", {"columns": cols})
        subj = re.match(rf"^({IDENT})\b", s)
        if subj and re.search(r"\b(must|should) be unique\b", s):
            add("UNIQUE_KEY", f"{subj.group(1)} must be unique", {"columns": [subj.group(1)]})
        if subj and re.search(r"\b(cannot|can't|must not|should not|may not) be null\b", s, re.I):
            add("NOT_NULL", f"{subj.group(1)} cannot be null", {"columns": [subj.group(1)]})

        m = re.search(rf"^({IDENT}) is copied (?:as-is|directly) from (\w+)\.({IDENT})", s)
        if m:
            add("DIRECT_MAPPING", f"{m.group(1)} copied as-is from {m.group(2)}.{m.group(3)}",
                {"target": m.group(1), "source_dataset": m.group(2), "source_column": m.group(3)})

        m = re.search(rf"Only ([A-Za-z_][\w ]*?) with ({IDENT})\s*(=|!=|<>|>=|<=|>|<)\s*('[^']*'|[\w.]+) (?:are|is) included", s)
        if m:
            val = m.group(4).strip("'") if m.group(4).startswith("'") else m.group(4)
            add("FILTER", f"Only {m.group(1)} with {m.group(2)} {m.group(3)} {m.group(4)} are included",
                {"dataset_hint": m.group(1), "column": m.group(2), "op": m.group(3), "value": val})

        m = re.search(rf"(LEFT|RIGHT|INNER|FULL(?: OUTER)?) JOIN (\w+) to (\w+) on ({IDENT}(?:\s*(?:,|and)\s*{IDENT})*)", s, re.I)
        if m:
            keys = re.findall(IDENT, m.group(4))
            add("JOIN", f"{m.group(1).upper()} JOIN {m.group(2)} to {m.group(3)} on {', '.join(keys)}",
                {"type": m.group(1).upper().replace(" OUTER", ""), "left": m.group(2), "right": m.group(3), "keys": keys})

        m = re.search(r"without .*? get (.+)$", s)
        if m:
            defaults = {k: _num(v) for k, v in re.findall(rf"({IDENT})\s*=\s*('[^']*'|-?\d+(?:\.\d+)?)", m.group(1))}
            if defaults:
                add("DEFAULT_WHEN_UNMATCHED", "Defaults for records without matches: " +
                    ", ".join(f"{k} = {v}" for k, v in defaults.items()), {"defaults": defaults})
            continue  # don't also read "X = 0" as a derived column

        m = re.search(rf"\b({IDENT})\s*=\s*(.+)$", s)
        if m and not re.search(r"\b(with|only)\b", s[: m.start()], re.I):
            target, rest = m.group(1), m.group(2)
            expr = _take_expression(rest)
            if expr and not re.fullmatch(r"'[^']*'|-?\d+(\.\d+)?", expr):
                spec: dict = {"target": target, "expression": expr, "columns": columns_in(expr)}
                g = re.search(rf"\bper ({IDENT}(?:\s*(?:,|and)\s*{IDENT})*)", rest)
                if g:
                    spec["group_by"] = re.findall(IDENT, g.group(1))
                p = re.search(r"rounded to (\d+) decimal", rest)
                if p:
                    spec["precision"] = int(p.group(1))
                try:
                    tree = sqlglot.parse_one(expr, read="duckdb")
                    spec["aggregate"] = bool(list(tree.find_all(sqlglot.exp.AggFunc)))
                    spec["parsed"] = True
                except Exception:  # noqa: BLE001
                    spec["parsed"] = False
                add("DERIVED_COLUMN", f"{target} = {expr}" + (f" per {', '.join(spec['group_by'])}" if "group_by" in spec else "")
                    + (f", rounded to {spec['precision']} decimals" if "precision" in spec else ""), spec)

        m = re.search(r"Rounding is (half[- ]up|half[- ]even|banker'?s|truncat\w*)", s, re.I)
        if m:
            add("ROUNDING_MODE", f"Rounding mode: {m.group(1)}", {"mode": m.group(1).upper().replace(" ", "_").replace("-", "_")})
    return out


def _num(v: str):
    if v.startswith("'"):
        return v.strip("'")
    return float(v) if "." in v else int(v)


def extract_attachment_mappings(att: dict) -> list[Extracted]:
    out: list[Extracted] = []
    content = att.get("content")
    if not content or not att["filename"].lower().endswith((".csv", ".tsv")):
        return out
    dialect = "excel-tab" if att["filename"].lower().endswith(".tsv") else "excel"
    rows = list(csv.DictReader(io.StringIO(content), dialect=dialect))
    if not rows:
        return out
    cols = {c.lower().strip(): c for c in rows[0].keys() if c}
    need = {"source_dataset", "source_column", "target_column"}
    if not need.issubset(cols):
        return out
    for i, r in enumerate(rows, 2):
        tgt = (r[cols["target_column"]] or "").strip()
        src_cols = [c.strip() for c in (r[cols["source_column"]] or "").split("|") if c.strip()]
        rule = (r.get(cols.get("rule", ""), "") or "direct").strip()
        ds = (r[cols["source_dataset"]] or "").strip()
        spec = {"target": tgt, "source_dataset": ds, "source_columns": src_cols, "rule": rule}
        if rule.lower() not in ("direct", "rename", ""):
            spec["expression"] = rule
            spec["columns"] = columns_in(rule)
        out.append(Extracted("MAPPING_SPEC", f"{ds}.{'|'.join(src_cols)} -> {tgt}: {rule}", spec, "ATTACHMENT_DERIVED",
                             f"attachment:{att['filename']}#row{i}", ",".join(r.get(c, "") or "" for c in rows[0].keys())))
    return out


def analyze(doc: dict) -> AnalysisResult:
    res = AnalysisResult()
    acs, other, comments = collect_units(doc)

    for unit in acs:
        if detect_injection(unit.text):
            res.warnings.append(f"Possible prompt-injection text ignored in {unit.ref}")
            continue
        res.requirements.append(Extracted("ACCEPTANCE_CRITERION", unit.text, {}, "JIRA_EXPLICIT", unit.ref, unit.text, unit.ac_id))
        rules = extract_rules(unit)
        res.requirements.extend(rules)
        if not rules:
            cols = sorted(set(re.findall(rf"\b{IDENT}\b", unit.text)))
            res.gaps.append({"id": _gap_id("UNPARSED", unit.ref), "gap_type": "UNPARSED_RULE",
                             "description": f"{unit.ac_id} could not be converted into a testable rule: \"{unit.text[:200]}\"",
                             "detail": "Add a structured requirement, reword the criterion in Jira, or accept it as a limitation. "
                                       "Column checks that depend on these columns are blocked until then.",
                             "affected": {"columns": cols, "topic": "UNPARSED_RULE"}, "evidence": unit.ref})
    for unit in other:
        if not detect_injection(unit.text):
            res.requirements.extend(extract_rules(unit))

    for c in comments:
        body = c.get("body", "") or ""
        ref = f"comment:{c.get('id')}"
        if detect_injection(body):
            res.warnings.append(f"Possible prompt-injection text ignored in {ref} (author: {c.get('author', '?')})")
            continue
        statements = [s for s in _sentences(body) if not s.endswith("?")]
        if statements:
            res.requirements.extend(extract_rules(TextUnit(ref, " ".join(statements))))
        for q in [s for s in re.split(r"(?<=\?)\s*", body) if s.strip().endswith("?")]:
            q = q.strip()
            cols = sorted(set(re.findall(rf"\b{IDENT}\b", q)))
            topic = ("NULL_HANDLING" if re.search(r"\bnull", q, re.I) else
                     "NEGATIVE_VALUES" if re.search(r"negative", q, re.I) else
                     "JOIN_TYPE" if re.search(r"\bjoin", q, re.I) else
                     "PRECISION" if re.search(r"round|precision|decimal", q, re.I) else
                     "DUPLICATES" if re.search(r"duplicate", q, re.I) else "AMBIGUOUS")
            res.gaps.append({"id": _gap_id("QUESTION", ref, q), "gap_type": topic,
                             "description": f"Unanswered question in Jira: {q}",
                             "detail": "Behaviour is not specified in the description or acceptance criteria.",
                             "affected": {"columns": cols, "topic": topic}, "evidence": f"{ref}: {q}"})

    for att in doc.get("attachments") or []:
        if att.get("content") and detect_injection(att["content"]):
            res.warnings.append(f"Possible prompt-injection text in attachment {att['filename']}; review manually")
        res.requirements.extend(extract_attachment_mappings(att))
        if att.get("skipped_reason"):
            res.warnings.append(f"Attachment {att['filename']} not analysed: {att['skipped_reason']}")

    _structural_gaps(res)
    return res


def _structural_gaps(res: AnalysisResult) -> None:
    kinds = {r.kind for r in res.requirements}
    if not ({"BUSINESS_KEY", "UNIQUE_KEY"} & kinds):
        res.gaps.append({"id": _gap_id("NO_KEY"), "gap_type": "NO_BUSINESS_KEY",
                         "description": "No business key identified in Jira.",
                         "detail": "Record-level reconciliation needs an approved key; it is BLOCKED until one is confirmed.",
                         "affected": {"columns": [], "topic": "NO_BUSINESS_KEY"}, "evidence": "No 'business key'/'unique' rule found"})
    derived = [r for r in res.requirements if r.kind == "DERIVED_COLUMN"]
    for d in derived:
        expr = d.spec.get("expression", "")
        if re.search(r"[-+*/]", expr) and "precision" not in d.spec and "ROUND" not in expr.upper():
            res.gaps.append({"id": _gap_id("PRECISION", d.spec["target"]), "gap_type": "PRECISION",
                             "description": f"Rounding precision for {d.spec['target']} is not defined.",
                             "detail": "Comparison tolerance cannot be derived from requirements.",
                             "affected": {"columns": [d.spec["target"]], "topic": "PRECISION"}, "evidence": d.evidence})
    # conflict detection between AC expressions and attachment mapping rules
    ac_expr = {}
    for d in derived:
        e = d.spec["expression"]
        if "precision" in d.spec:
            e = f"ROUND({e}, {d.spec['precision']})"
        ac_expr[d.spec["target"]] = (normalize_expr(e), d)
    for m in [r for r in res.requirements if r.kind == "MAPPING_SPEC" and "expression" in r.spec]:
        t = m.spec["target"]
        if t in ac_expr and normalize_expr(m.spec["expression"]) != ac_expr[t][0]:
            res.gaps.append({"id": _gap_id("CONFLICT", t), "gap_type": "CONFLICT",
                             "description": f"Conflicting rules for {t}: '{ac_expr[t][1].spec['expression']}' (Jira) vs "
                                            f"'{m.spec['expression']}' ({m.ref}).",
                             "detail": "Confirm which rule is authoritative.", "affected": {"columns": [t], "topic": "CONFLICT"},
                             "evidence": f"{ac_expr[t][1].ref} vs {m.ref}"})
    datasets = {r.spec.get("source_dataset") for r in res.requirements if r.kind == "MAPPING_SPEC"} - {None, ""}
    if len(datasets) > 1 and "JOIN" not in kinds:
        res.gaps.append({"id": _gap_id("JOIN_TYPE"), "gap_type": "JOIN_TYPE",
                         "description": f"Multiple sources ({', '.join(sorted(datasets))}) but no join rule is stated.",
                         "detail": "Join type and keys must be confirmed.", "affected": {"columns": [], "topic": "JOIN_TYPE"},
                         "evidence": "No JOIN statement found"})


# ------------------------------------------------------------------ persistence

def persist_analysis(db: Session, run_id: str, doc: dict, actor: str, use_ai: bool = True) -> AnalysisResult:
    res = analyze(doc)
    # preserve user-confirmed requirements and gap clarifications across re-analysis
    old_gaps = {g.id: g for g in db.scalars(select(RequirementGap).where(RequirementGap.run_id == run_id))}
    for r in db.scalars(select(Requirement).where(Requirement.run_id == run_id, Requirement.provenance != "USER_CONFIRMED")):
        db.delete(r)
    db.flush()
    existing_ids = set(db.scalars(select(Requirement.id).where(Requirement.run_id == run_id)))
    n = len(existing_ids)
    for e in res.requirements:
        n += 1
        rid = f"REQ-{n:03d}"
        while rid in existing_ids:
            n += 1
            rid = f"REQ-{n:03d}"
        db.add(Requirement(id=rid, run_id=run_id, ac_id=e.ac_id, kind=e.kind, description=e.description, spec=e.spec,
                           provenance=e.provenance, confidence=e.confidence, source_ref=e.ref, evidence=e.evidence,
                           status=e.status))

    if use_ai:
        llm = get_llm()
        text = "\n".join([doc.get("summary", ""), doc.get("description", "")] +
                         [c.get("body", "") for c in doc.get("comments", []) if not detect_injection(c.get("body", ""))])
        out, ms, err = timed(llm.propose_rules, text)
        db.add(LLMCall(run_id=run_id, purpose="REQUIREMENT_PROPOSALS", provider=llm.name, sent_data_samples=False,
                       ok=err is None, latency_ms=ms, error=err))
        if out:
            for pr in out.rules:
                n += 1
                db.add(Requirement(id=f"REQ-{n:03d}", run_id=run_id, kind=pr.kind, description=pr.description,
                                   spec={"target": pr.target_column} if pr.target_column else {},
                                   provenance="AI_INFERRED", confidence=pr.confidence, source_ref=f"ai:{llm.name}",
                                   evidence=pr.evidence_quote, status="PROPOSED"))
            for g in out.gaps:
                res.gaps.append({"id": _gap_id("AI", g), "gap_type": "AMBIGUOUS", "description": g,
                                 "detail": "Raised by AI review; confirm or dismiss.", "affected": {"columns": re.findall(IDENT, g)},
                                 "evidence": f"ai:{llm.name}", "provenance": "AI_INFERRED"})
        elif err:
            res.warnings.append(f"AI requirement review unavailable: {err[:200]}")

    for g in res.gaps:
        prev = old_gaps.pop(g["id"], None)
        if prev:
            prev.description, prev.detail, prev.affected, prev.evidence = g["description"], g["detail"], g["affected"], g["evidence"]
        else:
            db.add(RequirementGap(id=g["id"], run_id=run_id, description=g["description"], detail=g["detail"],
                                  gap_type=g["gap_type"], affected=g["affected"], evidence=g["evidence"],
                                  provenance=g.get("provenance", "DETERMINISTIC_CHECK")))
    for stale in old_gaps.values():  # gap no longer detected
        if stale.provenance != "PROFILE_CHECK" and stale.status == "OPEN":
            db.delete(stale)
    audit(db, actor, "REQUIREMENTS_ANALYZED", run_id, "QA_RUN", run_id, requirements=len(res.requirements),
          gaps=len(res.gaps), warnings=res.warnings)
    return res


def profile_gaps(db: Session, run_id: str, profiles: dict[str, dict], alias_map: dict[str, str]) -> list[str]:
    """After profiling: nulls in columns referenced by derived rules with no null-handling rule -> gap
    (or evidence attached to an existing NULL_HANDLING question)."""
    reqs = list(db.scalars(select(Requirement).where(Requirement.run_id == run_id)))
    gaps = list(db.scalars(select(RequirementGap).where(RequirementGap.run_id == run_id)))
    created = []
    for r in reqs:
        if r.kind != "DERIVED_COLUMN" or r.status == "REJECTED":
            continue
        for col in r.spec.get("columns", []):
            for alias, prof in profiles.items():
                c = next((c for c in prof.get("columns", []) if c["name"].upper() == col.upper()), None)
                if not c or not c.get("null_count"):
                    continue
                ev = f"profile:{alias}.{c['name']} has {c['null_count']} null(s)"
                existing = next((g for g in gaps if g.affected.get("topic") == "NULL_HANDLING"
                                 and col in g.affected.get("columns", [])), None)
                if existing:
                    if ev not in existing.evidence:
                        existing.evidence = existing.evidence + " | " + ev
                    aff = dict(existing.affected)
                    aff["actual_data_nulls"] = sorted(set(aff.get("actual_data_nulls", []) + [f"{alias}.{c['name']}"]))
                    existing.affected = aff
                    continue
                gid = _gap_id("PROFILE_NULL", alias, col)
                if any(g.id == gid for g in gaps):
                    continue
                g = RequirementGap(id=gid, run_id=run_id, gap_type="NULL_HANDLING", provenance="PROFILE_CHECK",
                                   description=f"Null handling for {col} is not specified, and the uploaded data contains nulls.",
                                   detail=f"Affects {r.spec.get('target')} ({r.id}).",
                                   affected={"columns": [col, r.spec.get("target")], "topic": "NULL_HANDLING",
                                             "requirements": [r.id], "actual_data_nulls": [f"{alias}.{c['name']}"]},
                                   evidence=ev)
                db.add(g)
                gaps.append(g)
                created.append(gid)
    return created
