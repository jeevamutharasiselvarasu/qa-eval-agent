import React from 'react'
import { FileText, FileSpreadsheet, FileJson, BarChart3, ExternalLink } from 'lucide-react'
import { api } from '../../api.js'
import { useRun } from '../RunWorkspace.jsx'
import { Badge, Btn, Card, DataTable, Empty, Ids, Stat, STATUS_COLORS, fmtDate, useAction, useLoad } from '../../components/ui.jsx'

export default function QAReport() {
  const { runId, reload: reloadRun } = useRun()
  const [d, reloadData] = useLoad(() => api.get(`/runs/${runId}/report-data`), [runId])
  const [reports, reload] = useLoad(() => api.get(`/runs/${runId}/reports`), [runId])
  const [busy, act] = useAction()
  const gen = () => act('gen', async () => { await api.post(`/runs/${runId}/reports`, {}); reload(); reloadData(); reloadRun() }, 'Report generated (Excel, HTML, JSON, Power BI)')
  const dl = (r, fmt) => act(fmt, () => api.download(`/reports/${r.id}/download/${fmt}`, `${runId}_${fmt === 'powerbi' ? 'powerbi_tables.zip' : 'report.' + fmt}`))
  if (!d) return <Empty>Loading…</Empty>
  const s = d.summary
  const c = s.counts
  return (
    <div className="col">
      <Card title={<div className="row gap-s"><h3>QA report · {s.jira_ticket}</h3><Badge s={s.overall === 'PASS' ? 'PASS' : s.overall === 'NO_EXECUTION' ? '' : s.overall.startsWith('PASS') ? 'BLOCKED' : 'FAIL'}>{s.overall.replaceAll('_', ' ')}</Badge></div>}
        sub={`Latest finished execution per track: ${s.executions.map(e => `${e.execution_id} (${e.track.toLowerCase()})`).join(', ') || 'none'}`}
        actions={<Btn className="primary" icon={FileText} busy={busy === 'gen'} disabled={!s.executions.length} onClick={gen}>Generate report files</Btn>}>
        <div className="grid g6">
          <Stat label="Total tests" value={c.total} />
          {['PASS', 'FAIL', 'BLOCKED', 'ERROR', 'SKIPPED'].map(k => <Stat key={k} label={k.toLowerCase()} value={c[k] || 0} color={STATUS_COLORS[k]} />)}
        </div>
        {s.open_gaps.length > 0 && <div className="small mt" style={{ color: 'var(--blocked)' }}>Open gaps at report time: {s.open_gaps.map(g => g.id).join(', ')}</div>}
      </Card>
      <Card title="Generated reports" flush>
        {!reports?.length ? <Empty>No report files yet.</Empty> : <table className="t"><tbody>{reports.map(r => (
          <tr key={r.id}><td className="mono small">{r.id}</td><td><Badge s={r.overall.startsWith('PASS') ? 'PASS' : 'FAIL'}>{r.overall}</Badge></td><td className="small">{fmtDate(r.created_at)} · {r.created_by}</td><td className="tiny"><Ids ids={r.execution_ids} /></td>
            <td className="nowrap">
              <button className="btn sm" onClick={() => dl(r, 'xlsx')}><FileSpreadsheet size={13} />Excel</button>{' '}
              <button className="btn sm" onClick={() => dl(r, 'html')}><FileText size={13} />HTML</button>{' '}
              <button className="btn sm" onClick={() => dl(r, 'json')}><FileJson size={13} />JSON</button>{' '}
              <button className="btn sm" onClick={() => dl(r, 'powerbi')}><BarChart3 size={13} />Power BI</button>{' '}
              <a className="btn sm ghost" href={`/api/reports/${r.id}/download/html?inline=true`} target="_blank" rel="noreferrer"><ExternalLink size={13} />Open</a>
            </td></tr>))}</tbody></table>}
      </Card>
      <Card title="Requirement traceability" sub="Jira acceptance criterion → requirement → mapping → test case → execution → evidence" flush>
        {!d.traceability.length ? <Empty>No acceptance criteria.</Empty> : <div className="tw"><table className="t">
          <thead><tr><th>AC</th><th>Criterion</th><th>Requirements</th><th>Mappings</th><th>Test cases</th><th>Executions</th><th>Evidence</th><th>Coverage</th></tr></thead>
          <tbody>{d.traceability.map(t => (
            <tr key={t.acceptance_criterion}><td className="mono">{t.acceptance_criterion}</td><td className="small" style={{ maxWidth: 320 }}>{t.criterion_text}</td>
              <td><Ids ids={t.requirement_ids} /></td><td><Ids ids={t.mapping_ids} /></td><td><Ids ids={t.test_case_ids} /></td><td><Ids ids={t.execution_ids} /></td><td className="small">{t.evidence_ids.length ? `${t.evidence_ids.length} item(s)` : '—'}</td>
              <td><Badge s={t.coverage === 'PARTIAL' ? 'BLOCKED' : t.coverage === 'NOT_COVERED' ? 'FAIL' : t.coverage}>{t.coverage.replaceAll('_', ' ')}</Badge></td></tr>))}</tbody>
        </table></div>}
      </Card>
      {d.reconciliation && Object.keys(d.reconciliation).length > 0 && <Card title="Actual-data reconciliation">
        <div className="grid g6">{Object.entries(d.reconciliation).map(([k, v]) => <Stat key={k} label={k.replaceAll('_', ' ')} value={v ?? '—'} />)}</div>
      </Card>}
      <Card title="Column-level differences" flush>{d.column_differences.length ? <DataTable rows={d.column_differences} columns={['test_case_id', 'business_key', 'column', 'expected', 'actual', 'difference']} /> : <Empty>No column differences.</Empty>}</Card>
      <Card title="AI failure analysis" sub="Advisory; separated into confirmed facts, likely explanations and unverified hypotheses." flush>
        {d.failure_analysis.length ? <div className="tw"><table className="t">
          <thead><tr><th>Test</th><th>Category</th><th>Suspected layer</th><th>Facts</th><th>Likely</th><th>Hypotheses</th><th>Recommended</th></tr></thead>
          <tbody>{d.failure_analysis.map(f => (
            <tr key={f.test_case_id + f.execution_id}><td className="mono small">{f.test_case_id}</td><td className="small">{f.category}</td><td className="small">{f.suspected_layer}</td>
              {['confirmed_facts', 'likely_explanations', 'unverified_hypotheses', 'recommended_checks'].map(k => <td key={k} className="tiny" style={{ maxWidth: 240 }}>{(f[k] || []).map((x, i) => <div key={i}>• {x}</div>)}</td>)}</tr>))}</tbody>
        </table></div> : <Empty>No failures analysed.</Empty>}
      </Card>
      <Card title="Synthetic testing" flush>{d.synthetic.length ? <div className="tw"><table className="t">
        <thead><tr><th>Scenario</th><th>Status</th><th>Seed</th><th>Explanation</th><th>Results</th></tr></thead>
        <tbody>{d.synthetic.map(s => <tr key={s.scenario_id}><td><span className="mono small">{s.scenario_id}</span><div className="small">{s.scenario}</div></td><td><Badge s={s.status} />{s.blocked_reason && <div className="tiny" style={{ color: 'var(--blocked)' }}>{s.blocked_reason}</div>}</td>
          <td className="mono small">{s.seed}</td><td className="tiny" style={{ maxWidth: 360 }}>{s.explanation}</td>
          <td>{Object.entries(s.results).map(([k, v]) => <div key={k} className="row gap-s tiny"><span className="mono">{k}</span><Badge s={v} /></div>)}</td></tr>)}</tbody>
      </table></div> : <Empty>No synthetic scenarios.</Empty>}</Card>
    </div>
  )
}
