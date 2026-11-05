import React from 'react'
import { Link } from 'react-router-dom'
import { CheckCircle2, XCircle } from 'lucide-react'
import { api } from '../../api.js'
import { useRun } from '../RunWorkspace.jsx'
import { Badge, Btn, Card, Empty, fmtDate, useAction, useLoad } from '../../components/ui.jsx'

const INFO = {
  REQUIREMENTS: ['Jira requirements, gaps & clarifications', 'requirements'],
  DATASETS: ['Source & target datasets and profiles', 'profiling'],
  MAPPINGS: ['Source-to-target mappings', 'mapping'],
  TRANSFORMATION: ['Current transformation version (SQL / Python / Visual)', 'studio'],
  TEST_CASES: ['Test cases & expected results', 'tests'],
  SYNTHETIC_DATA: ['Synthetic data & synthetic expected targets', 'synthetic'],
  EXECUTION_SETTINGS: ['Execution environment & settings', 'overview'],
}

export default function Approvals() {
  const { runId, run, reload: reloadRun, meta } = useRun()
  const [data, reload] = useLoad(() => api.get(`/runs/${runId}/approvals`), [runId])
  const [busy, act] = useAction()
  const decide = (k, decision, hash) => {
    const reason = decision === 'REJECTED' ? window.prompt('Reason for rejection?') : ''
    if (decision === 'REJECTED' && !reason) return
    act(k, async () => { await api.post(`/runs/${runId}/approvals`, { artifact_type: k, decision, reason, expected_hash: hash }); reload(); reloadRun() }, `${k.replaceAll('_', ' ').toLowerCase()} ${decision.toLowerCase()}`)
  }
  if (!data) return <Empty>Loading…</Empty>
  const canApprove = ['approver', 'admin'].includes(meta?.role)
  return (
    <div className="col">
      <Card title="Approval center" sub="Approvals bind to a content hash. Any material change makes an approval STALE and requires re-approval before execution." flush>
        <div className="tw"><table className="t">
          <thead><tr><th>Artifact</th><th>Required for</th><th>Version</th><th>Status</th><th>Last decision</th><th>Current hash</th><th>Decide</th></tr></thead>
          <tbody>{Object.entries(data.state).map(([k, v]) => (
            <tr key={k}>
              <td><Link to={`/runs/${runId}/${INFO[k][1]}`}><b>{k.replaceAll('_', ' ').toLowerCase()}</b></Link><div className="tiny muted">{INFO[k][0]}</div></td>
              <td className="small">{['ACTUAL', 'SYNTHETIC'].filter(t => data.required[t].includes(k)).map(t => <Badge key={t} className="outline">{t.toLowerCase()}</Badge>)}</td>
              <td className="small">{v.artifact_version || '—'}</td>
              <td><Badge s={v.status} /></td>
              <td className="small">{v.last ? <>{v.last.decision.toLowerCase()} by <b>{v.last.reviewer}</b><div className="tiny muted">{fmtDate(v.last.at)} · {v.last.artifact_version}</div>{v.last.reason && <div className="tiny">“{v.last.reason}”</div>}</> : <span className="muted">—</span>}</td>
              <td className="tiny mono muted">{v.current_hash ? v.current_hash.slice(7, 19) : '—'}</td>
              <td className="nowrap">{v.status === 'NOT_READY' ? <span className="tiny muted">not ready</span> : <>
                <Btn className="sm" icon={CheckCircle2} busy={busy === k} disabled={!canApprove || v.status === 'APPROVED'} onClick={() => decide(k, 'APPROVED', v.current_hash)}>Approve</Btn>{' '}
                <Btn className="sm danger" icon={XCircle} disabled={!canApprove} onClick={() => decide(k, 'REJECTED', v.current_hash)}>Reject</Btn></>}</td>
            </tr>))}</tbody>
        </table></div>
        {!canApprove && <div className="card-b"><span className="small muted">Your role ({meta?.role}) cannot approve. Approver or admin role required.</span></div>}
      </Card>
      <Card title="Decision history" flush>
        {!data.history.length ? <Empty>No decisions yet.</Empty> : (
          <div className="tw"><table className="t">
            <thead><tr><th>When</th><th>Artifact</th><th>Version</th><th>Decision</th><th>Reviewer</th><th>Reason</th><th>Hash</th></tr></thead>
            <tbody>{data.history.map(a => (
              <tr key={a.id}><td className="small nowrap">{fmtDate(a.created_at)}</td><td className="small">{a.artifact_type}</td><td className="small">{a.artifact_version}</td>
                <td><Badge s={a.decision} /></td><td className="small">{a.reviewer}</td><td className="small">{a.reason}</td><td className="tiny mono muted">{a.content_hash.slice(7, 19)}</td></tr>))}</tbody>
          </table></div>
        )}
      </Card>
    </div>
  )
}
