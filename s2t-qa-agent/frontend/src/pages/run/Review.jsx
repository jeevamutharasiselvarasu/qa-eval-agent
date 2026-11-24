import React from 'react'
import { Link } from 'react-router-dom'
import { CheckCircle2, XCircle, RefreshCw, Code2, PackageCheck } from 'lucide-react'
import { api } from '../../api.js'
import { useRun } from '../RunWorkspace.jsx'
import { Alert, Badge, Btn, Card, Empty, fmtDate, useAction, useLoad } from '../../components/ui.jsx'
import { TypeBadge } from './TestCases.jsx'

const LABELS = { REQUIREMENTS: 'Requirements & gap decisions', DATASETS: 'Source and target datasets', MAPPINGS: 'Target column rules',
  TEST_CASES: 'Test cases', SYNTHETIC_DATA: 'Synthetic data & expected outputs', EXECUTION_SETTINGS: 'Execution settings' }

export default function Review() {
  const { runId, run, reload: reloadRun, meta } = useRun()
  const [pack, reloadPack] = useLoad(() => api.get(`/runs/${runId}/test-pack`), [runId])
  const [tr, reloadTr] = useLoad(() => api.get(`/runs/${runId}/transformations`), [runId])
  const [hist, reloadHist] = useLoad(() => api.get(`/runs/${runId}/approvals`), [runId])
  const [busy, act] = useAction()
  const canApprove = ['approver', 'admin'].includes(meta?.role)
  const refresh = () => { reloadPack(); reloadTr(); reloadHist(); reloadRun() }
  const reason = (d) => d === 'REJECTED' ? window.prompt('Reason for rejection?') : ''

  const decideTr = (d) => { const r = reason(d); if (d === 'REJECTED' && !r) return
    act('tr', async () => { await api.post(`/runs/${runId}/transformations/${tr.current.id}/approve`, { decision: d, reason: r }); refresh() },
      d === 'APPROVED' ? 'Transformation approved' : 'Transformation rejected') }
  const decidePack = (d) => { const r = reason(d); if (d === 'REJECTED' && !r) return
    act('pack', async () => { await api.post(`/runs/${runId}/test-pack/approve`, { decision: d, reason: r }); refresh() },
      d === 'APPROVED' ? 'Test pack approved: you can run the validation' : 'Test pack rejected') }

  if (!pack || !tr) return <Empty>Loading…</Empty>
  const cur = tr.current
  return (
    <div className="col">
      {!canApprove && <Alert kind="info">Your role ({meta?.role}) can review but not approve. An approver or admin must make these two decisions.</Alert>}
      <div className="grid g2">
        <Card title={<span className="row gap-s"><Code2 size={16} /><h3>Approval 1 · Transformation</h3></span>}
          sub="The logic that turns the sources into the expected target. Every test depends on it."
          actions={<Badge s={tr.approval.status} />}>
          {!cur ? <Empty>No SQL yet: it is generated once the requirements are complete. <Link to={`/runs/${runId}/requirements`}>Open Requirements</Link>.</Empty> : <>
            <div className="kv">
              <dt>Version</dt><dd>v{cur.version} · {cur.mode} · <Badge s={cur.origin}>{cur.origin.replaceAll('_', ' ')}</Badge></dd>
              <dt>Validation</dt><dd><Badge s={cur.validation.valid ? 'PASS' : 'FAIL'}>{cur.validation.valid ? 'safe & valid' : 'fails validation'}</Badge></dd>
              <dt>Saved</dt><dd>{cur.created_by} · {fmtDate(cur.created_at)}</dd>
              {tr.approval.last && <><dt>Last decision</dt><dd>{tr.approval.last.decision.toLowerCase()} by {tr.approval.last.reviewer} · {fmtDate(tr.approval.last.at)}</dd></>}
            </div>
            <details className="mt"><summary className="small">Show code</summary><div className="pre mt-s">{cur.code}</div></details>
            <div className="row gap-s mt">
              <Btn className="primary" icon={CheckCircle2} busy={busy === 'tr'} disabled={!canApprove || tr.approval.status === 'APPROVED' || !cur.validation.valid} onClick={() => decideTr('APPROVED')}>Approve v{cur.version}</Btn>
              <Btn className="danger" icon={XCircle} disabled={!canApprove} onClick={() => decideTr('REJECTED')}>Reject</Btn>
              <Link className="btn ghost" to={`/runs/${runId}/studio`}>Edit in Transformation tab</Link>
            </div>
          </>}
        </Card>

        <Card title={<span className="row gap-s"><PackageCheck size={16} /><h3>Approval 2 · Test pack</h3></span>}
          sub="Everything else in one decision: requirements, data, mappings, test cases, synthetic data and settings."
          actions={<Badge s={pack.pack_approved ? 'APPROVED' : pack.stale ? 'STALE' : 'PENDING'}>{pack.pack_approved ? 'APPROVED' : pack.stale ? 'OUT OF DATE' : 'PENDING'}</Badge>}>
          <div className="row wrap gap-l mb">
            {['POSITIVE', 'NEGATIVE', 'EDGE', 'GENERAL'].map(k => <span key={k} className="row gap-s"><TypeBadge t={k} /><b>{pack.by_type[k]}</b></span>)}
          </div>
          {pack.by_basis && <div className="small muted mb">From acceptance criteria: {pack.by_basis.ACCEPTANCE_CRITERIA} · from the transformation logic: {pack.by_basis.TRANSFORMATION_LOGIC} · general: {pack.by_basis.GENERAL}.
            Approving covers the whole pack; you choose which tests to run afterwards.</div>}
          <div className="small">{pack.test_cases} test cases ({pack.blocked_tests} blocked by open questions) · {pack.scenarios.generated} synthetic scenarios
            {pack.scenarios.blocked ? ` (${pack.scenarios.blocked} blocked)` : ''}. <Link to={`/runs/${runId}/tests`}>Review test cases</Link></div>
          <table className="t mt"><tbody>{Object.entries(pack.artifacts).filter(([, v]) => v.status !== 'NOT_READY').map(([k, v]) => (
            <tr key={k}><td className="small">{LABELS[k]}</td><td className="small muted">{v.artifact_version}</td><td><Badge s={v.status} /></td></tr>))}</tbody></table>
          {pack.ignored_requirements.length + pack.ignored_mappings.length > 0 && <div className="mt"><Alert kind="info">
            Not used: {pack.ignored_requirements.length} AI note(s) and {pack.ignored_mappings.length} unconfirmed column suggestion(s).
            Confirm or edit them on the <Link to={`/runs/${runId}/requirements`}>Requirements</Link> tab if they are needed.
          </Alert></div>}
          {pack.blockers.length > 0 && <div className="mt"><Alert kind="warn">{pack.blockers.map(b => <div key={b}>• {b}</div>)}</Alert></div>}
          <div className="row gap-s mt">
            <Btn className="primary" icon={CheckCircle2} busy={busy === 'pack'} disabled={!canApprove || !pack.can_approve || pack.pack_approved} onClick={() => decidePack('APPROVED')}>Approve test pack</Btn>
            <Btn className="danger" icon={XCircle} disabled={!canApprove || !pack.built} onClick={() => decidePack('REJECTED')}>Reject</Btn>
            <Btn icon={RefreshCw} busy={busy === 'build'} onClick={() => act('build', async () => { await api.post(`/runs/${runId}/test-pack`); refresh() }, 'Test pack rebuilt')}>Rebuild</Btn>
          </div>
          {pack.pack_approved && <div className="small mt">Next: <Link to={`/runs/${runId}/results`}>run the validation</Link>.</div>}
        </Card>
      </div>

      <Card title="Decision history" sub="Every approval is tied to the exact content it covered; any later change makes it out of date." flush>
        {!hist?.history.length ? <Empty>No decisions yet.</Empty> : (
          <div className="tw"><table className="t">
            <thead><tr><th>When</th><th>Item</th><th>Version</th><th>Decision</th><th>Reviewer</th><th>Reason</th></tr></thead>
            <tbody>{hist.history.map(a => (
              <tr key={a.id}><td className="small nowrap">{fmtDate(a.created_at)}</td><td className="small">{LABELS[a.artifact_type] || 'Transformation'}</td>
                <td className="small">{a.artifact_version}</td><td><Badge s={a.decision} /></td><td className="small">{a.reviewer}</td><td className="small">{a.reason}</td></tr>))}</tbody>
          </table></div>
        )}
      </Card>
    </div>
  )
}
