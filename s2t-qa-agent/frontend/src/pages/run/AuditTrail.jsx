import { useState } from 'react'
import { api } from '../../api.js'
import { useRun } from '../RunWorkspace.jsx'
import { Badge, Card, Empty, Loading, fmtDate, useLoad } from '../../components/ui.jsx'

const LABELS = { REQUIREMENTS: 'Requirements & answers', DATASETS: 'Source and target files', MAPPINGS: 'Target column rules',
  TEST_CASES: 'Test cases', EXECUTION_SETTINGS: 'Execution settings', TRANSFORMATION: 'SQL transformation' }

export default function AuditTrail() {
  const { runId } = useRun()
  const [rows] = useLoad(() => api.get(`/runs/${runId}/audit`), [runId])
  const [hist] = useLoad(() => api.get(`/runs/${runId}/approvals`), [runId])
  const [q, setQ] = useState('')
  const shown = (rows || []).filter(r => !q || JSON.stringify(r).toLowerCase().includes(q.toLowerCase()))
  return (
    <div className="col">
      <Card title="Approval decisions" sub="Every approval is tied to the exact content it covered; any later change makes it out of date." flush>
        {!hist?.history?.length ? <Empty>No decisions yet.</Empty> : (
          <div className="tw"><table className="t">
            <thead><tr><th>When</th><th>Item</th><th>Version</th><th>Decision</th><th>Reviewer</th><th>Reason</th></tr></thead>
            <tbody>{hist.history.map(a => (
              <tr key={a.id}><td className="small nowrap">{fmtDate(a.created_at)}</td><td className="small">{LABELS[a.artifact_type] || a.artifact_type}</td>
                <td className="small">{a.artifact_version}</td><td><Badge s={a.decision} /></td><td className="small">{a.reviewer}</td><td className="small">{a.reason}</td></tr>))}</tbody>
          </table></div>
        )}
      </Card>
      <Card title="Audit trail" sub="Every state change, upload, approval, code version, execution and AI call is recorded with actor and timestamp."
        actions={<input placeholder="Filter…" value={q} onChange={e => setQ(e.target.value)} />} flush>
        {!rows ? <Loading /> : !shown.length ? <Empty>No events.</Empty> : (
          <div className="tw"><table className="t">
            <thead><tr><th>When</th><th>Actor</th><th>Action</th><th>Entity</th><th>Details</th></tr></thead>
            <tbody>{shown.map(r => (
              <tr key={r.id}><td className="small nowrap">{fmtDate(r.ts)}</td><td className="small">{r.actor}</td><td className="small mono">{r.action}</td>
                <td className="tiny mono">{r.entity_type}:{r.entity_id}</td><td className="tiny mono" style={{ maxWidth: 520, overflowWrap: 'anywhere' }}>{JSON.stringify(r.details)}</td></tr>))}</tbody>
          </table></div>
        )}
      </Card>
    </div>
  )
}
