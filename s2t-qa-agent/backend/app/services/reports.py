"""ReportService: Excel, HTML, JSON and Power BI-ready (CSV star schema) outputs plus traceability."""
from __future__ import annotations

import csv
import html
import io
import json
import zipfile
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core.common import audit, mask_records, run_dir, set_state
from ..models import (Approval, AuditLog, ColumnMapping, Dataset, Evidence, Execution, FailureAnalysis, JiraIssue, QARun,
                      Report, Requirement, RequirementGap, SyntheticScenario, TestCase, TestResult, TransformationVersion)
from .approvals import approval_state


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
        coverage = "NOT_COVERED" if not tc_ids else ("FAIL" if "FAIL" in statuses else "BLOCKED" if "BLOCKED" in statuses and "PASS" not in statuses
                                                     else "PASS" if statuses == ["PASS"] else "PARTIAL" if statuses else "NOT_EXECUTED")
        rows.append({"jira_ticket": run.jira_key, "acceptance_criterion": ac.ac_id, "criterion_text": ac.description,
                     "requirement_ids": sorted(rids), "mapping_ids": mids, "test_case_ids": tc_ids,
                     "execution_ids": sorted({x.execution_id for x in res}), "result_statuses": statuses,
                     "evidence_ids": sorted({x.evidence_id for x in res if x.evidence_id}), "coverage": coverage})
    return rows


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
    evidence = {e.id: e for e in db.scalars(select(Evidence).where(Evidence.execution_id.in_(ex_ids)))}
    fa = list(db.scalars(select(FailureAnalysis).where(FailureAnalysis.execution_id.in_(ex_ids))))
    jira = db.scalars(select(JiraIssue).where(JiraIssue.run_id == run.id)).first()
    datasets = list(db.scalars(select(Dataset).where(Dataset.run_id == run.id)))
    maps = list(db.scalars(select(ColumnMapping).where(ColumnMapping.run_id == run.id).order_by(ColumnMapping.id)))
    versions = {v.id: v for v in db.scalars(select(TransformationVersion))}
    scen = list(db.scalars(select(SyntheticScenario).where(SyntheticScenario.run_id == run.id).order_by(SyntheticScenario.id)))
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
    overall = "NO_EXECUTION" if not results else ("FAIL" if tot["FAIL"] else "ERROR" if tot["ERROR"] else
                                                  "PASS_WITH_BLOCKED" if tot["BLOCKED"] else "PASS")
    recon = {}
    for e in execs:
        if e.track == "ACTUAL":
            m = {tcs[r.test_case_id].validation.get("type"): r.metrics for r in results if r.execution_id == e.id}
            exp_ds = datasets and next((d for d in datasets if d.id == (e.expected_dataset_ids or {}).get("ACTUAL")), None)
            tgt = next((d for d in datasets if d.role == "TARGET"), None)
            missing = (m.get("RECON_MISSING") or {}).get("count")
            extra = (m.get("RECON_EXTRA") or {}).get("count")
            mism = sum((r.metrics or {}).get("mismatches", 0) for r in results if r.execution_id == e.id and tcs[r.test_case_id].validation.get("type") == "COLUMN_MATCH")
            recon = {"generated_expected_count": exp_ds.row_count if exp_ds else None, "uploaded_target_count": tgt.row_count if tgt else None,
                     "missing_records": missing, "extra_records": extra, "mismatched_values": mism,
                     "matching_records": (exp_ds.row_count - (missing or 0)) if exp_ds and missing is not None else None}
    col_diffs = []
    for r in results:
        tc = tcs[r.test_case_id]
        if tc.validation.get("type") == "COLUMN_MATCH" and r.status == "FAIL" and r.evidence_id in evidence:
            keys = tc.validation.get("keys", [])
            for row in evidence[r.evidence_id].sample:
                col_diffs.append({"test_case_id": tc.id, "execution_id": r.execution_id,
                                  "business_key": "|".join(str(row.get(k)) for k in keys), "column": row.get("column_name"),
                                  "expected": row.get("expected"), "actual": row.get("actual"), "difference": row.get("difference")})
    st = approval_state(db, run.id)
    approvals = [{"artifact_type": a.artifact_type, "artifact_id": a.artifact_id, "version": a.artifact_version, "decision": a.decision,
                  "reviewer": a.reviewer, "reason": a.reason, "at": _iso(a.created_at), "hash": a.content_hash}
                 for a in db.scalars(select(Approval).where(Approval.run_id == run.id).order_by(Approval.created_at))]
    tv = versions.get(execs[0].transformation_version_id) if execs else None
    return {
        "report_schema_version": "1.0",
        "summary": {"qa_run_id": run.id, "run_name": run.name, "jira_ticket": run.jira_key,
                    "jira_source": jira.source if jira else None, "generated_for_executions": ex_ids,
                    "executions": [{"execution_id": e.id, "track": e.track, "status": e.status, "started_at": _iso(e.started_at),
                                    "ended_at": _iso(e.ended_at), "attempt": e.attempt, "summary": e.summary} for e in execs],
                    "source_datasets": [{"id": d.id, "alias": d.alias, "file": d.filename, "rows": d.row_count, "fingerprint": d.fingerprint}
                                        for d in datasets if d.role == "SOURCE"],
                    "target_dataset": [{"id": d.id, "file": d.filename, "rows": d.row_count, "fingerprint": d.fingerprint}
                                       for d in datasets if d.role == "TARGET"],
                    "counts": tot, "counts_actual": counts("ACTUAL"), "counts_synthetic": counts("SYNTHETIC"), "overall": overall,
                    "open_gaps": [{"id": g.id, "status": g.status, "description": g.description} for g in gaps if g.status in ("OPEN", "BLOCKED")]},
        "traceability": build_traceability(db, run, ex_ids),
        "mappings": [{"mapping_id": m.id, "source_columns": ", ".join(m.source_columns), "target_column": m.target_column,
                      "type": m.mapping_type, "rule": m.expression, "provenance": m.provenance, "status": m.status,
                      "requirements": ", ".join(m.requirement_ids)} for m in maps],
        "transformation": None if not tv else {"version_id": tv.id, "version": tv.version, "mode": tv.mode, "origin": tv.origin,
                                               "content_hash": tv.content_hash, "code": tv.code, "approval": st["TRANSFORMATION"]["status"]},
        "reconciliation": recon,
        "test_results": [{"test_case_id": r.test_case_id, "execution_id": r.execution_id, "track": tcs[r.test_case_id].track,
                          "scenario_id": tcs[r.test_case_id].scenario_id, "category": tcs[r.test_case_id].category,
                          "title": tcs[r.test_case_id].title, "status": r.status, "expected": r.expected, "actual": r.actual,
                          "message": r.message, "acceptance_criteria": ", ".join(tcs[r.test_case_id].ac_ids),
                          "requirements": ", ".join(tcs[r.test_case_id].requirement_ids), "evidence_id": r.evidence_id,
                          "duration_ms": r.duration_ms} for r in sorted(results, key=lambda x: x.test_case_id)],
        "synthetic": [{"scenario_id": s.id, "scenario": s.name, "status": s.status, "blocked_reason": s.blocked_reason,
                       "seed": s.seed, "source_datasets": s.source_dataset_ids, "expected_dataset": s.expected_dataset_id,
                       "explanation": " ".join(s.explanation), "assumptions": " ".join(s.assumptions),
                       "results": {r.test_case_id: r.status for r in results if tcs[r.test_case_id].scenario_id == s.id}} for s in scen],
        "column_differences": mask_records(col_diffs),
        "failure_analysis": [{"test_case_id": f.test_case_id, "execution_id": f.execution_id, "provider": f.provider, **f.payload} for f in fa],
        "audit": {"approvals": approvals, "dataset_fingerprints": {d.id: d.fingerprint for d in datasets if d.role in ("SOURCE", "TARGET", "ACTUAL_EXPECTED")},
                  "transformation_versions": [{"id": v.id, "version": v.version, "mode": v.mode, "origin": v.origin, "hash": v.content_hash,
                                               "by": v.created_by, "at": _iso(v.created_at)} for v in versions.values()
                                              if v.transformation.run_id == run.id],
                  "events": [{"at": _iso(a.ts), "actor": a.actor, "action": a.action, "entity": f"{a.entity_type}:{a.entity_id}"}
                             for a in db.scalars(select(AuditLog).where(AuditLog.run_id == run.id).order_by(AuditLog.id))][-300:]},
    }


def _html(data: dict) -> str:
    s = data["summary"]
    c = s["counts"]
    e = html.escape

    def table(rows, cols):
        if not rows:
            return "<p class='muted'>None.</p>"
        h = "".join(f"<th>{e(x)}</th>" for x in cols)
        b = "".join("<tr>" + "".join(f"<td class='{e(str(r.get(x, '')))}'>{e(str(r.get(x, '') if r.get(x) is not None else ''))}</td>" for x in cols) + "</tr>" for r in rows)
        return f"<div class='tw'><table><thead><tr>{h}</tr></thead><tbody>{b}</tbody></table></div>"

    fa = "".join(
        f"<div class='card'><h4>{e(f['test_case_id'])} — {e(f.get('category', ''))} <span class='pill'>{e(f.get('suspected_layer', ''))}</span></h4>"
        f"<p><b>Confirmed facts</b></p><ul>{''.join('<li>' + e(x) + '</li>' for x in f.get('confirmed_facts', []))}</ul>"
        f"<p><b>Likely explanations</b></p><ul>{''.join('<li>' + e(x) + '</li>' for x in f.get('likely_explanations', [])) or '<li>None</li>'}</ul>"
        f"<p><b>Unverified hypotheses</b></p><ul>{''.join('<li>' + e(x) + '</li>' for x in f.get('unverified_hypotheses', [])) or '<li>None</li>'}</ul>"
        f"<p><b>Recommended checks</b></p><ul>{''.join('<li>' + e(x) + '</li>' for x in f.get('recommended_checks', []))}</ul>"
        f"<p class='muted'>Provider: {e(f.get('provider', ''))}. AI analysis cannot change the deterministic status ({e(f.get('deterministic_status', ''))}).</p></div>"
        for f in data["failure_analysis"])
    tiles = "".join(f"<div class='tile {k}'><div class='n'>{c.get(k, 0)}</div><div>{k}</div></div>"
                    for k in ("PASS", "FAIL", "BLOCKED", "ERROR", "SKIPPED", "NOT_EXECUTED"))
    rec = data["reconciliation"]
    return f"""<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>
<title>QA Report {e(s['qa_run_id'])}</title><style>
body{{font-family:Inter,system-ui,Segoe UI,Arial,sans-serif;background:#f7f8fb;color:#1d2330;margin:0;padding:24px}}
h1{{margin:0 0 4px}} h2{{margin-top:32px;border-bottom:1px solid #e3e6ee;padding-bottom:6px}} .muted{{color:#6b7385}}
.tiles{{display:flex;gap:12px;flex-wrap:wrap}} .tile{{background:#fff;border:1px solid #e3e6ee;border-radius:10px;padding:12px 16px;min-width:110px}}
.tile .n{{font-size:26px;font-weight:700}} .PASS .n,td.PASS{{color:#127a3b}} .FAIL .n,td.FAIL{{color:#b42318}} .BLOCKED .n,td.BLOCKED{{color:#a15c07}} .ERROR .n,td.ERROR{{color:#7a1fa2}}
.tw{{overflow-x:auto}} table{{border-collapse:collapse;width:100%;background:#fff;font-size:13px}} th,td{{border:1px solid #e3e6ee;padding:6px 8px;text-align:left;vertical-align:top}}
th{{background:#f0f2f7}} .card{{background:#fff;border:1px solid #e3e6ee;border-radius:10px;padding:12px 16px;margin:12px 0}}
.pill{{background:#eef2ff;color:#3346a8;border-radius:999px;padding:2px 8px;font-size:12px}} pre{{background:#fff;border:1px solid #e3e6ee;padding:12px;overflow:auto}}
</style></head><body>
<h1>QA Report — {e(s['jira_ticket'])}</h1><p class='muted'>Run {e(s['qa_run_id'])} · Jira source: {e(str(s['jira_source']))} ·
Executions: {e(', '.join(x['execution_id'] + ' (' + x['track'] + ', ' + x['status'] + ')' for x in s['executions']))}</p>
<h2>Execution summary — overall: {e(s['overall'])}</h2><div class='tiles'>{tiles}</div>
<p>Actual track: {e(json.dumps(s['counts_actual']))}<br>Synthetic track: {e(json.dumps(s['counts_synthetic']))}</p>
<h3>Open requirement gaps</h3>{table(s['open_gaps'], ['id', 'status', 'description'])}
<h2>Requirement traceability</h2>{table([{**r, 'requirement_ids': ', '.join(r['requirement_ids']), 'mapping_ids': ', '.join(r['mapping_ids']), 'test_case_ids': ', '.join(r['test_case_ids']), 'result_statuses': ', '.join(r['result_statuses'])} for r in data['traceability']], ['acceptance_criterion', 'criterion_text', 'requirement_ids', 'mapping_ids', 'test_case_ids', 'result_statuses', 'coverage'])}
<h2>Actual-data reconciliation</h2>{table([rec] if rec else [], list(rec.keys()))}
<h2>Test results</h2>{table(data['test_results'], ['test_case_id', 'track', 'scenario_id', 'category', 'title', 'status', 'expected', 'actual', 'message', 'acceptance_criteria'])}
<h2>Column-level differences</h2>{table(data['column_differences'], ['test_case_id', 'business_key', 'column', 'expected', 'actual', 'difference'])}
<h2>Synthetic testing</h2>{table(data['synthetic'], ['scenario_id', 'scenario', 'status', 'blocked_reason', 'seed', 'explanation', 'assumptions'])}
<h2>AI-assisted failure analysis</h2>{fa or "<p class='muted'>No failures.</p>"}
<h2>Mapping and transformation</h2>{table(data['mappings'], ['mapping_id', 'source_columns', 'target_column', 'type', 'rule', 'provenance', 'status', 'requirements'])}
{('<p>Transformation v' + str(data['transformation']['version']) + ' (' + e(data['transformation']['mode']) + ', ' + e(data['transformation']['origin']) + ', approval ' + e(data['transformation']['approval']) + ', ' + e(data['transformation']['content_hash']) + ')</p><pre>' + e(data['transformation']['code']) + '</pre>') if data['transformation'] else ''}
<h2>Audit</h2>{table(data['audit']['approvals'], ['artifact_type', 'version', 'decision', 'reviewer', 'reason', 'at'])}
</body></html>"""


def _xlsx(data: dict, path: Path) -> None:
    wb = Workbook()
    hdr = PatternFill("solid", fgColor="E8ECF7")
    fills = {"PASS": "E7F6EC", "FAIL": "FDECEA", "BLOCKED": "FFF4E0", "ERROR": "F3E8FA"}

    def sheet(title, rows, cols=None):
        ws = wb.create_sheet(title[:31])
        cols = cols or (list(rows[0].keys()) if rows else ["(none)"])
        ws.append(cols)
        for c in ws[1]:
            c.font, c.fill = Font(bold=True), hdr
        for r in rows:
            ws.append([json.dumps(r.get(c)) if isinstance(r.get(c), (dict, list)) else r.get(c) for c in cols])
        if "status" in cols:
            si = cols.index("status") + 1
            for row in ws.iter_rows(min_row=2):
                v = row[si - 1].value
                if v in fills:
                    row[si - 1].fill = PatternFill("solid", fgColor=fills[v])
        for i, c in enumerate(cols, 1):
            ws.column_dimensions[get_column_letter(i)].width = min(60, max(12, len(str(c)) + 4))
        ws.freeze_panes = "A2"
        return ws

    s = data["summary"]
    ws = wb.active
    ws.title = "Summary"
    for k in ("qa_run_id", "run_name", "jira_ticket", "jira_source", "overall"):
        ws.append([k, s[k]])
    for k, v in s["counts"].items():
        ws.append([f"tests_{k}", v])
    for e in s["executions"]:
        ws.append([f"execution {e['execution_id']}", f"{e['track']} {e['status']} {e['started_at']} → {e['ended_at']}"])
    for c in ws["A"]:
        c.font = Font(bold=True)
    ws.column_dimensions["A"].width, ws.column_dimensions["B"].width = 28, 80
    sheet("Traceability", data["traceability"])
    sheet("Test Results", data["test_results"])
    sheet("Reconciliation", [data["reconciliation"]] if data["reconciliation"] else [])
    sheet("Column Differences", data["column_differences"])
    sheet("Synthetic", data["synthetic"])
    sheet("Failure Analysis", data["failure_analysis"])
    sheet("Mappings", data["mappings"])
    sheet("Approvals", data["audit"]["approvals"])
    sheet("Audit Events", data["audit"]["events"])
    for wsx in wb.worksheets:
        for row in wsx.iter_rows():
            for c in row:
                c.alignment = Alignment(vertical="top", wrap_text=False)
    wb.save(path)


def _powerbi(data: dict, path: Path) -> None:
    """Star-schema CSVs: fact_test_result, dim_test_case, dim_requirement_trace, fact_column_difference, dim_run."""
    s = data["summary"]
    tables = {
        "dim_run.csv": [{"qa_run_id": s["qa_run_id"], "jira_ticket": s["jira_ticket"], "overall": s["overall"], **{f"n_{k}": v for k, v in s["counts"].items()}}],
        "fact_test_result.csv": [{"qa_run_id": s["qa_run_id"], **{k: r[k] for k in ("test_case_id", "execution_id", "track", "scenario_id", "category", "status", "duration_ms")}} for r in data["test_results"]],
        "dim_test_case.csv": [{"test_case_id": r["test_case_id"], "title": r["title"], "category": r["category"], "acceptance_criteria": r["acceptance_criteria"], "requirements": r["requirements"]} for r in data["test_results"]],
        "dim_requirement_trace.csv": [{**r, **{k: "|".join(r[k]) for k in ("requirement_ids", "mapping_ids", "test_case_ids", "execution_ids", "result_statuses", "evidence_ids")}} for r in data["traceability"]],
        "fact_column_difference.csv": [{"qa_run_id": s["qa_run_id"], **r} for r in data["column_differences"]],
        "fact_execution.csv": [{"qa_run_id": s["qa_run_id"], "execution_id": e["execution_id"], "track": e["track"], "status": e["status"], "started_at": e["started_at"], "ended_at": e["ended_at"], **(e["summary"] or {})} for e in s["executions"]],
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
        z.writestr("README.txt", "Power BI-ready tables. Relationships: fact_test_result.test_case_id -> dim_test_case.test_case_id; "
                                 "fact_*.qa_run_id -> dim_run.qa_run_id. Import with Get Data > Folder or Text/CSV.\n")


def generate(db: Session, run: QARun, actor: str, execution_ids: list[str] | None = None) -> Report:
    data = collect(db, run, execution_ids)
    if not data["summary"]["executions"]:
        raise ValueError("No finished execution to report on")
    rpt = Report(run_id=run.id, execution_ids=data["summary"]["generated_for_executions"], overall=data["summary"]["overall"], created_by=actor)
    db.add(rpt)
    db.flush()
    d = run_dir(run.id, "reports", rpt.id)
    files = {}
    try:
        (d / "report.json").write_text(json.dumps(data, indent=2, default=str))
        files["json"] = str(d / "report.json")
        (d / "report.html").write_text(_html(data))
        files["html"] = str(d / "report.html")
        _xlsx(data, d / "report.xlsx")
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
