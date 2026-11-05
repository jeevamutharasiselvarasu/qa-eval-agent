import React, { useState } from 'react'
import { RefreshCw, Check, X, Pencil, Plus, KeyRound } from 'lucide-react'
import { api } from '../../api.js'
import { useRun } from '../RunWorkspace.jsx'
import { Badge, Btn, Card, Empty, Ids, Modal, useAction, useLoad } from '../../components/ui.jsx'

const TYPES = ['DIRECT', 'RENAMED', 'DERIVED', 'AGGREGATED', 'CONDITIONAL', 'CONCATENATION', 'LOOKUP', 'CONSTANT', 'JOINED', 'FILTERED', 'UNMAPPED_SOURCE', 'UNMAPPED_TARGET']

function Editor({ m, onClose, onSaved }) {
  const { runId } = useRun()
  const isNew = !m.id
  const [f, setF] = useState({ target_column: m.target_column || '', mapping_type: m.mapping_type || 'DERIVED', expression: m.expression || '',
    source_columns: (m.source_columns || []).join(', '), notes: m.notes || '', is_key: !!m.is_key })
  const [busy, act] = useAction()
  const save = () => act('s', async () => {
    const body = { ...f, source_columns: f.source_columns.split(',').map(s => s.trim()).filter(Boolean) }
    if (isNew) await api.post(`/runs/${runId}/mappings`, body)
    else await api.patch(`/runs/${runId}/mappings/${m.id}`, { ...body, status: 'ACCEPTED' })
    onSaved()
  }, isNew ? 'Mapping added' : 'Mapping updated (marked user-confirmed)')
  return (
    <Modal title={isNew ? 'Add mapping' : `Edit ${m.id}`} onClose={onClose} footer={<><button className="btn" onClick={onClose}>Cancel</button><Btn className="primary" busy={busy} onClick={save}>Save</Btn></>}>
      <div className="grid g2">
        <label className="field">Target column<input value={f.target_column} onChange={e => setF({ ...f, target_column: e.target.value })} /></label>
        <label className="field">Mapping type<select value={f.mapping_type} onChange={e => setF({ ...f, mapping_type: e.target.value })}>{TYPES.map(t => <option key={t}>{t}</option>)}</select></label>
      </div>
      <label className="field mt">Expression (DuckDB SQL, qualify columns with dataset alias)
        <textarea className="mono" rows={3} value={f.expression} onChange={e => setF({ ...f, expression: e.target.value })} placeholder="ROUND(SUM(transactions.AMOUNT - transactions.DISCOUNT), 2)" /></label>
      <label className="field mt">Source columns (comma separated alias.COLUMN)<input value={f.source_columns} onChange={e => setF({ ...f, source_columns: e.target.value })} /></label>
      <label className="field mt">Notes / unresolved questions<textarea rows={2} value={f.notes} onChange={e => setF({ ...f, notes: e.target.value })} /></label>
      <label className="row gap-s mt small"><input type="checkbox" checked={f.is_key} onChange={e => setF({ ...f, is_key: e.target.checked })} />Business key column</label>
      {m.evidence && <div className="mt"><h4>Evidence</h4><div className="pre small">{m.evidence}</div></div>}
    </Modal>
  )
}

export default function MappingStudio() {
  const { runId, reload: reloadRun } = useRun()
  const [maps, reload] = useLoad(() => api.get(`/runs/${runId}/mappings`), [runId])
  const [busy, act] = useAction()
  const [edit, setEdit] = useState(null)
  const [hideUnmapped, setHide] = useState(true)
  const refresh = () => { reload(); reloadRun() }
  const status = (m, s) => act(m.id, async () => { await api.patch(`/runs/${runId}/mappings/${m.id}`, { status: s }); refresh() })
  const shown = (maps || []).filter(m => !hideUnmapped || m.mapping_type !== 'UNMAPPED_SOURCE')
  const pending = (maps || []).filter(m => m.status === 'PROPOSED' && m.mapping_type !== 'UNMAPPED_SOURCE').length

  return (
    <div className="col">
      <Card title="Source-to-target mappings" sub="Mappings come from explicit Jira rules and attachments. Name similarity alone is only ever an AI-inferred proposal."
        actions={<>
          <label className="row gap-s small"><input type="checkbox" checked={hideUnmapped} onChange={e => setHide(e.target.checked)} />Hide unmapped source columns</label>
          <Btn icon={Plus} onClick={() => setEdit({})}>Add mapping</Btn>
          <Btn icon={RefreshCw} busy={busy === 'gen'} onClick={() => window.confirm('Regenerate proposals? Manual edits will be replaced.') && act('gen', async () => { await api.post(`/runs/${runId}/mappings/generate`); refresh() }, 'Mappings regenerated')}>Regenerate</Btn>
        </>} flush>
        {pending > 0 && <div className="card-b"><div className="alert warn">{pending} proposed mapping(s) need an accept or reject decision before mappings can be approved.</div></div>}
        {!maps ? <Empty>Loading…</Empty> : !maps.length ? <Empty>No mappings yet — generate them after requirements and datasets are ready.</Empty> : (
          <div className="tw"><table className="t">
            <thead><tr><th>ID</th><th>Source</th><th>Target</th><th>Type</th><th>Rule / expression</th><th>Requirements</th><th>Provenance</th><th>Status</th><th>Review</th></tr></thead>
            <tbody>{shown.map(m => (
              <tr key={m.id}>
                <td className="mono small">{m.id} <span className="tiny muted">v{m.version}</span></td>
                <td className="tiny mono">{m.source_columns.map(s => <div key={s}>{s}</div>)}</td>
                <td className="mono">{m.target_column || <span className="muted">—</span>} {m.is_key && <KeyRound size={12} color="#9a5b05" title="Business key" />}</td>
                <td><Badge className="outline">{m.mapping_type}</Badge></td>
                <td style={{ maxWidth: 360 }}><code className="small">{m.expression || '—'}</code>{m.notes && <div className="tiny" style={{ color: 'var(--blocked)' }}>{m.notes}</div>}</td>
                <td><Ids ids={m.requirement_ids} /></td>
                <td><Badge s={m.provenance} /><div className="tiny muted">{m.confidence}</div></td>
                <td><Badge s={m.status} /></td>
                <td className="nowrap">
                  <button className="btn sm ghost" title="Accept" onClick={() => status(m, 'ACCEPTED')}><Check size={14} /></button>
                  <button className="btn sm ghost" title="Reject" onClick={() => status(m, 'REJECTED')}><X size={14} /></button>
                  <button className="btn sm ghost" title="Edit" onClick={() => setEdit(m)}><Pencil size={14} /></button>
                </td>
              </tr>))}</tbody>
          </table></div>
        )}
      </Card>
      {edit && <Editor m={edit} onClose={() => setEdit(null)} onSaved={() => { setEdit(null); refresh() }} />}
    </div>
  )
}
