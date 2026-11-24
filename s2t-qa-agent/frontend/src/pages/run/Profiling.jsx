import React, { useRef, useState } from 'react'
import { RefreshCw, Upload, Eye, Trash2 } from 'lucide-react'
import { api } from '../../api.js'
import { useRun } from '../RunWorkspace.jsx'
import { Badge, Btn, Card, DataTable, Empty, Modal, useAction, useLoad } from '../../components/ui.jsx'

export default function Profiling() {
  const { runId, reload: reloadRun } = useRun()
  const [profiles, reload] = useLoad(() => api.get(`/runs/${runId}/profiles`), [runId])
  const [preview, setPreview] = useState(null)
  const [busy, act] = useAction()
  const srcRef = useRef(); const tgtRef = useRef()
  const refresh = () => { reload(); reloadRun() }

  const upload = (role, f) => act('up', async () => {
    const fd = new FormData(); fd.append('file', f); fd.append('label', f.name)
    await api.upload(`/runs/${runId}/datasets/${role}`, fd); await api.post(`/runs/${runId}/profile`); refresh()
  }, 'File added; requirements re-checked')

  return (
    <div className="col">
      <Card title="Data files" sub="The uploaded source and target files. Originals are stored read-only with content fingerprints. Changing the files re-checks the requirements and, when they are complete, regenerates the SQL."
        actions={<>
          <Btn icon={Upload} onClick={() => srcRef.current.click()} busy={busy === 'up'}>Add source</Btn>
          <Btn icon={Upload} onClick={() => tgtRef.current.click()}>Replace target</Btn>
          <Btn icon={RefreshCw} busy={busy === 'prof'} onClick={() => act('prof', async () => { await api.post(`/runs/${runId}/profile`); refresh() }, 'Profiles refreshed')}>Re-check files</Btn>
          <input ref={srcRef} type="file" hidden onChange={e => e.target.files[0] && upload('sources', e.target.files[0])} />
          <input ref={tgtRef} type="file" hidden onChange={e => e.target.files[0] && upload('target', e.target.files[0])} />
        </>} flush>
        {!profiles ? <Empty>Loading…</Empty> : !profiles.length ? <Empty>No datasets uploaded.</Empty> : (
          <div className="tw"><table className="t">
            <thead><tr><th>Role</th><th>Alias</th><th>File</th><th className="num">Rows</th><th className="num">Cols</th><th>Candidate keys</th><th>Feeds target</th><th>Fingerprint</th><th /></tr></thead>
            <tbody>{profiles.map(({ dataset: d, profile: p }) => (
              <tr key={d.id}>
                <td><Badge s={d.role === 'TARGET' ? 'info' : ''}>{d.role}</Badge></td>
                <td className="mono">{d.alias}</td>
                <td className="small">{d.filename}{d.sheet_name && <span className="muted"> · {d.sheet_name}</span>}<div className="tiny muted">{d.file_format} · {(d.size_bytes / 1024).toFixed(1)} KB</div></td>
                <td className="num">{d.row_count}</td><td className="num">{d.columns.length}</td>
                <td>{p?.candidate_keys?.length ? p.candidate_keys.map(k => <span className="chip" key={k.join()}>{k.join(' + ')}</span>) : <span className="muted small">none</span>}</td>
                <td onClick={e => e.stopPropagation()}>{d.role === 'SOURCE' ? <input type="checkbox" checked={d.feeds_target}
                  onChange={e => act('f', async () => { await api.patch(`/runs/${runId}/datasets/${d.id}`, { feeds_target: e.target.checked }); refresh() })} /> : '—'}</td>
                <td className="tiny mono muted" title={d.fingerprint}>{d.fingerprint.slice(7, 19)}…</td>
                <td className="nowrap" onClick={e => e.stopPropagation()}>
                  <button className="btn sm ghost" title="Preview rows" onClick={() => act('pv', async () => setPreview({ d, data: await api.get(`/runs/${runId}/datasets/${d.id}/preview?limit=50`) }))}><Eye size={14} /></button>
                  <button className="btn sm ghost" title="Remove" onClick={() => window.confirm(`Remove ${d.filename} from this run?`) && act('rm', async () => { await api.del(`/runs/${runId}/datasets/${d.id}`); refresh() })}><Trash2 size={14} /></button>
                </td>
              </tr>))}</tbody>
          </table></div>
        )}
      </Card>

      {preview && <Modal title={`Preview · ${preview.d.alias}`} onClose={() => setPreview(null)}><DataTable columns={preview.data.columns} rows={preview.data.rows} /></Modal>}
    </div>
  )
}
