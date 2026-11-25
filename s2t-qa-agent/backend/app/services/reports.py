"""ReportService: Excel, HTML, JSON and Power BI-ready (CSV star schema) outputs plus traceability."""

from __future__ import annotations

import csv
import io
import json
import zipfile
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core.common import audit, mask_records, run_dir, set_state
from ..models import (
    Approval,
    AuditLog,
    ColumnMapping,
    Dataset,
    Execution,
    FailureAnalysis,
    JiraIssue,
    QARun,
    Report,
    Requirement,
    RequirementGap,
    TestCase,
    TestResult,
    TransformationVersion,
)
from .approvals import approval_state
from .report_render import render_html, render_xlsx
from .validation import CheckError, compare_targets


def _iso(d):
    return d.isoformat() if d else None


def build_traceability(db: Session, run: QARun, execution_ids: list[str]) -> list[dict]:
    reqs = list(db.scalars(select(Requirement).where(Requirement.run_id == run.id).order_by(Requirement.id)))
    tcs = list(db.scalars(select(TestCase).where(TestCase.run_id == run.id)))
    maps = list(db.scalars(select(ColumnMapping).where(ColumnMapping.run_id == run.id)))
    results = {}
    for r in db.scalars(select(TestResult).where(TestResult.execution_id.in_(execution_ids))):
        results.setdefault(r.test_case_id, []).append(r)
    rows = []
    acs = [r for r in reqs if r.kind == "ACCEPTANCE_CRITERION"]
    for ac in acs:
        rel = [r for r in reqs if r.ac_id == ac.ac_id and r.kind != "ACCEPTANCE_CRITERION"]
        rids = {r.id for r in rel}
        mids = sorted({m.id for m in maps if rids & set(m.requirement_ids)})
        tc_ids = sorted({t.id for t in tcs if ac.ac_id in t.ac_ids or rids & set(t.requirement_ids)})
        res = [x for t in tc_ids for x in results.get(t, [])]
        statuses = sorted({x.status for x in res})
        eff = [s for s in statuses if s != "SKIPPED"]  # edge cases absent from the uploaded data do not lower coverage
        coverage = (
            "NOT_COVERED"
            if not tc_ids
            else (
                "FAIL"
                if "FAIL" in eff
                else "BLOCKED"
                if "BLOCKED" in eff and "PASS" not in eff
                else "PASS"
                if eff == ["PASS"]
                else "PARTIAL"
                if eff
                else "NOT_IN_DATA"
                if statuses
                else "NOT_EXECUTED"
            )
        )
        rows.append(
            {
                "jira_ticket": run.jira_key,
                "acceptance_criterion": ac.ac_id,
                "criterion_text": ac.description,
                "requirement_ids": sorted(rids),
                "mapping_ids": mids,
                "test_case_ids": tc_ids,
                "execution_ids": sorted({x.execution_id for x in res}),
                "result_statuses": statuses,
                "evidence_ids": sorted({x.evidence_id for x in res if x.evidence_id}),
                "coverage": coverage,
            }
        )
    return rows


def _business_key(db: Session, run: QARun) -> list[str]:
    reqs = [r for r in db.scalars(select(Requirement).where(Requirement.run_id == run.id)) if r.status == "ACCEPTED"]
    keyreq = [r for r in reqs if r.kind == "BUSINESS_KEY"] or [r for r in reqs if r.kind == "UNIQUE_KEY"]
    if keyreq:
        return list(keyreq[0].spec.get("columns", []))
    maps = db.scalars(select(ColumnMapping).where(ColumnMapping.run_id == run.id))
    return [m.target_column for m in maps if m.status == "ACCEPTED" and m.is_key and m.target_column]


def _compare(db: Session, run: QARun, expected: Dataset | None, target: Dataset | None) -> dict:
    """Column differences between the expected and the uploaded target: computed once from the two tables,
    not from the test results, so each record and column appears at most once."""
    if not expected or not target or not Path(expected.parquet_path).exists() or not Path(target.parquet_path).exists():
        return {"status": "NOT_RUN", "message": "The expected or the uploaded target is not available.", "columns": [], "differences": []}
    keys = _business_key(db, run)
    try:
        out = compare_targets(Path(expected.parquet_path), Path(target.parquet_path), keys)
    except CheckError as ex:
        return {"status": "ERROR", "message": str(ex), "columns": [], "differences": []}
    return {"status": "OK", "message": "", **out}


def collect(db: Session, run: QARun, execution_ids: list[str] | None = None) -> dict:
    execs = list(db.scalars(select(Execution).where(Execution.run_id == run.id).order_by(Execution.created_at.desc())))
    if execution_ids:
        execs = [e for e in execs if e.id in execution_ids]
    else:
        latest = {}
        for e in execs:
            if e.track not in latest and e.status not in ("QUEUED", "RUNNING"):
                latest[e.track] = e
        execs = list(latest.values())
    ex_ids = [e.id for e in execs]
    tcs = {t.id: t for t in db.scalars(select(TestCase).where(TestCase.run_id == run.id))}
    results = list(db.scalars(select(TestResult).where(TestResult.execution_id.in_(ex_ids))))
    fa = list(db.scalars(select(FailureAnalysis).where(FailureAnalysis.execution_id.in_(ex_ids))))
    jira = db.scalars(select(JiraIssue).where(JiraIssue.run_id == run.id)).first()
    datasets = list(db.scalars(select(Dataset).where(Dataset.run_id == run.id)))
    maps = list(db.scalars(select(ColumnMapping).where(ColumnMapping.run_id == run.id).order_by(ColumnMapping.id)))
    versions = {v.id: v for v in db.scalars(select(TransformationVersion))}
    gaps = list(db.scalars(select(RequirementGap).where(RequirementGap.run_id == run.id)))

    def counts(track):
        c = {"total": 0, "PASS": 0, "FAIL": 0, "BLOCKED": 0, "ERROR": 0, "SKIPPED": 0, "NOT_EXECUTED": 0}
        for r in results:
            ex = next(e for e in execs if e.id == r.execution_id)
            if track and ex.track != track:
                continue
            c["total"] += 1
            c[r.status] = c.get(r.status, 0) + 1
        return c

    tot = counts(None)
    overall = (
        "NO_EXECUTION"
        if not results
        else ("FAIL" if tot["FAIL"] else "ERROR" if tot["ERROR"] else "PASS_WITH_BLOCKED" if tot["BLOCKED"] else "PASS")
    )
    recon = {}
    comparison = {"status": "NOT_RUN", "message": "The expected target has not been built yet.", "columns": [], "differences": []}
    for e in execs:
        if e.track == "ACTUAL":
            m = {tcs[r.test_case_id].validation.get("type"): r.metrics for r in results if r.execution_id == e.id}
            exp_ds = datasets and next((d for d in datasets if d.id == (e.expected_dataset_ids or {}).get("ACTUAL")), None)
            tgt = next((d for d in datasets if d.role == "TARGET"), None)
            missing = (m.get("RECON_MISSING") or {}).get("count")
            extra = (m.get("RECON_EXTRA") or {}).get("count")
            comparison = _compare(db, run, exp_ds, tgt)
            mism = comparison["total_differences"] if comparison.get("status") == "OK" else None
            recon = {
                "generated_expected_count": exp_ds.row_count if exp_ds else None,
                "uploaded_target_count": tgt.row_count if tgt else None,
                "missing_records": missing,
                "extra_records": extra,
                "mismatched_values": mism,
                "matching_records": (exp_ds.row_count - (missing or 0)) if exp_ds and missing is not None else None,
            }
    st = approval_state(db, run.id)
    approvals = [
        {
            "artifact_type": a.artifact_type,
            "artifact_id": a.artifact_id,
            "version": a.artifact_version,
            "decision": a.decision,
            "reviewer": a.reviewer,
            "reason": a.reason,
            "at": _iso(a.created_at),
            "hash": a.content_hash,
        }
        for a in db.scalars(select(Approval).where(Approval.run_id == run.id).order_by(Approval.created_at))
    ]
    tv = versions.get(execs[0].transformation_version_id) if execs else None
    return {
        "report_schema_version": "1.0",
        "summary": {
            "qa_run_id": run.id,
            "run_name": run.name,
            "jira_ticket": run.jira_key,
            "jira_source": jira.source if jira else None,
            "generated_for_executions": ex_ids,
            "executions": [
                {
                    "execution_id": e.id,
                    "track": e.track,
                    "status": e.status,
                    "started_at": _iso(e.started_at),
                    "ended_at": _iso(e.ended_at),
                    "attempt": e.attempt,
                    "summary": e.summary,
                }
                for e in execs
            ],
            "source_datasets": [
                {"id": d.id, "alias": d.alias, "file": d.filename, "rows": d.row_count, "fingerprint": d.fingerprint}
                for d in datasets
                if d.role == "SOURCE"
            ],
            "target_dataset": [
                {"id": d.id, "file": d.filename, "rows": d.row_count, "fingerprint": d.fingerprint} for d in datasets if d.role == "TARGET"
            ],
            "selection": {
                "selected": len(results),
                "in_pack": len(tcs),
                "not_selected": sorted(set(tcs) - {r.test_case_id for r in results}),
            },
            "counts": tot,
            "overall": overall,
            "open_gaps": [{"id": g.id, "status": g.status, "description": g.description} for g in gaps if g.status in ("OPEN", "BLOCKED")],
        },
        "traceability": build_traceability(db, run, ex_ids),
        "mappings": [
            {
                "mapping_id": m.id,
                "source_columns": ", ".join(m.source_columns),
                "target_column": m.target_column,
                "type": m.mapping_type,
                "rule": m.expression,
                "provenance": m.provenance,
                "status": m.status,
                "requirements": ", ".join(m.requirement_ids),
            }
            for m in maps
        ],
        "transformation": None
        if not tv
        else {
            "version_id": tv.id,
            "version": tv.version,
            "mode": tv.mode,
            "origin": tv.origin,
            "content_hash": tv.content_hash,
            "code": tv.code,
            "approval": st["TRANSFORMATION"]["status"],
        },
        "reconciliation": recon,
        "test_results": [
            {
                "test_case_id": r.test_case_id,
                "execution_id": r.execution_id,
                "category": tcs[r.test_case_id].category,
                "scope": (tcs[r.test_case_id].validation.get("scope") or {}).get("desc", ""),
                "title": tcs[r.test_case_id].title,
                "basis": tcs[r.test_case_id].basis,
                "test_type": tcs[r.test_case_id].test_type,
                "status": r.status,
                "expected": r.expected,
                "actual": r.actual,
                "message": r.message,
                "acceptance_criteria": ", ".join(tcs[r.test_case_id].ac_ids),
                "requirements": ", ".join(tcs[r.test_case_id].requirement_ids),
                "evidence_id": r.evidence_id,
                "duration_ms": r.duration_ms,
            }
            for r in sorted(results, key=lambda x: x.test_case_id)
        ],
        "column_comparison": {k: v for k, v in comparison.items() if k != "differences"},
        "column_differences": mask_records(comparison.get("differences") or []),
        "failure_analysis": [
            {"test_case_id": f.test_case_id, "execution_id": f.execution_id, "provider": f.provider, **f.payload} for f in fa
        ],
        "audit": {
            "approvals": approvals,
            "dataset_fingerprints": {d.id: d.fingerprint for d in datasets if d.role in ("SOURCE", "TARGET", "ACTUAL_EXPECTED")},
            "transformation_versions": [
                {
                    "id": v.id,
                    "version": v.version,
                    "mode": v.mode,
                    "origin": v.origin,
                    "hash": v.content_hash,
                    "by": v.created_by,
                    "at": _iso(v.created_at),
                }
                for v in versions.values()
                if v.transformation.run_id == run.id
            ],
            "events": [
                {"at": _iso(a.ts), "actor": a.actor, "action": a.action, "entity": f"{a.entity_type}:{a.entity_id}"}
                for a in db.scalars(select(AuditLog).where(AuditLog.run_id == run.id).order_by(AuditLog.id))
            ][-300:],
        },
    }


def _powerbi(data: dict, path: Path) -> None:
    """Star-schema CSVs: fact_test_result, dim_test_case, dim_requirement_trace, fact_column_difference, dim_run."""
    s = data["summary"]
    tables = {
        "dim_run.csv": [
            {
                "qa_run_id": s["qa_run_id"],
                "jira_ticket": s["jira_ticket"],
                "overall": s["overall"],
                **{f"n_{k}": v for k, v in s["counts"].items()},
            }
        ],
        "fact_test_result.csv": [
            {"qa_run_id": s["qa_run_id"], **{k: r[k] for k in ("test_case_id", "execution_id", "category", "status", "duration_ms")}}
            for r in data["test_results"]
        ],
        "dim_test_case.csv": [
            {
                "test_case_id": r["test_case_id"],
                "title": r["title"],
                "category": r["category"],
                "acceptance_criteria": r["acceptance_criteria"],
                "requirements": r["requirements"],
            }
            for r in data["test_results"]
        ],
        "dim_requirement_trace.csv": [
            {
                **r,
                **{
                    k: "|".join(r[k])
                    for k in ("requirement_ids", "mapping_ids", "test_case_ids", "execution_ids", "result_statuses", "evidence_ids")
                },
            }
            for r in data["traceability"]
        ],
        "fact_column_difference.csv": [{"qa_run_id": s["qa_run_id"], **r} for r in data["column_differences"]],
        "fact_execution.csv": [
            {
                "qa_run_id": s["qa_run_id"],
                "execution_id": e["execution_id"],
                "track": e["track"],
                "status": e["status"],
                "started_at": e["started_at"],
                "ended_at": e["ended_at"],
                **(e["summary"] or {}),
            }
            for e in s["executions"]
        ],
    }
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for name, rows in tables.items():
            buf = io.StringIO()
            cols = sorted({k for r in rows for k in r}) if rows else ["empty"]
            w = csv.DictWriter(buf, fieldnames=cols)
            w.writeheader()
            for r in rows:
                w.writerow(r)
            z.writestr(name, buf.getvalue())
        z.writestr(
            "README.txt",
            "Power BI-ready tables. Relationships: fact_test_result.test_case_id -> dim_test_case.test_case_id; "
            "fact_*.qa_run_id -> dim_run.qa_run_id. Import with Get Data > Folder or Text/CSV.\n",
        )


def generate(db: Session, run: QARun, actor: str, execution_ids: list[str] | None = None) -> Report:
    data = collect(db, run, execution_ids)
    if not data["summary"]["executions"]:
        raise ValueError("No finished execution to report on")
    rpt = Report(
        run_id=run.id, execution_ids=data["summary"]["generated_for_executions"], overall=data["summary"]["overall"], created_by=actor
    )
    db.add(rpt)
    db.flush()
    d = run_dir(run.id, "reports", rpt.id)
    files = {}
    try:
        (d / "report.json").write_text(json.dumps(data, indent=2, default=str))
        files["json"] = str(d / "report.json")
        (d / "report.html").write_text(render_html(data), encoding="utf-8")
        files["html"] = str(d / "report.html")
        render_xlsx(data, d / "report.xlsx")
        files["xlsx"] = str(d / "report.xlsx")
        _powerbi(data, d / "powerbi_tables.zip")
        files["powerbi"] = str(d / "powerbi_tables.zip")
    except Exception as e:  # noqa: BLE001 - report failure is recorded, results are untouched
        rpt.files = files
        rpt.overall = "REPORT_ERROR"
        audit(db, actor, "REPORT_FAILED", run.id, "REPORT", rpt.id, error=str(e)[:500])
        raise
    rpt.files = files
    set_state(db, run, "REPORT_GENERATED", actor)
    audit(db, actor, "REPORT_GENERATED", run.id, "REPORT", rpt.id, executions=rpt.execution_ids, overall=rpt.overall)
    return rpt
