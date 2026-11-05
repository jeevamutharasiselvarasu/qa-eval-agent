import React, { useState } from 'react'
import { api } from '../../api.js'
import { useRun } from '../RunWorkspace.jsx'
import { Card, Empty, fmtDate, useLoad } from '../../components/ui.jsx'

export default function AuditTrail() {
  const { runId } = useRun()
  const [rows] = useLoad(() => api.get(`/runs/${runId}/audit`), [runId])
  const [q, setQ] = useState('')
  const shown = (rows || []).filter(r => !q || JSON.stringify(r).toLowerCase().includes(q.toLowerCase()))
  return (
    <Card title="Audit trail" sub="Every state change, upload, approval, code version, execution and AI call is recorded with actor and timestamp."
      actions={<input placeholder="Filter…" value={q} onChange={e => setQ(e.target.value)} />} flush>
      {!rows ? <Empty>Loading…</Empty> : !shown.length ? <Empty>No events.</Empty> : (
        <div className="tw"><table className="t">
          <thead><tr><th>When</th><th>Actor</th><th>Action</th><th>Entity</th><th>Details</th></tr></thead>
          <tbody>{shown.map(r => (
            <tr key={r.id}><td className="small nowrap">{fmtDate(r.ts)}</td><td className="small">{r.actor}</td><td className="small mono">{r.action}</td>
              <td className="tiny mono">{r.entity_type}:{r.entity_id}</td><td className="tiny mono" style={{ maxWidth: 520, overflowWrap: 'anywhere' }}>{JSON.stringify(r.details)}</td></tr>))}</tbody>
        </table></div>
      )}
    </Card>
  )
}
