import React, { useEffect, useRef, useState } from 'react'
import { RefreshCw, Upload, Eye, Trash2 } from 'lucide-react'
import { api } from '../../api.js'
import { useRun } from '../RunWorkspace.jsx'
import { Badge, Btn, Card, DataTable, Empty, Modal, useAction, useLoad } from '../../components/ui.jsx'

export default function Profiling() {
  const { runId, reload: reloadRun } = useRun()
  const [profiles, reload] = useLoad(() => api.get(`/runs/${runId}/profiles`), [runId])
  const [sel, setSel] = useState(null)
  const [preview, setPreview] = useState(null)
  const [busy, act] = useAction()
  const srcRef = useRef(); const tgtRef = useRef()
  useEffect(() => { if (profiles?.length && !sel) setSel(profiles[0].dataset.id) }, [profiles, sel])
  const refresh = () => { reload(); reloadRun() }
  const cur = profiles?.find(p => p.dataset.id === sel)
  const target = profiles?.find(p => p.dataset.role === 'TARGET')
  const sources = profiles?.filter(p => p.dataset.role === 'SOURCE') || []

  const upload = (role, f) => act('up', async () => {
    const fd = new FormData(); fd.append('file', f); fd.append('label', f.name)
    await api.upload(`/runs/${runId}/datasets/${role}`, fd); await api.post(`/runs/${runId}/profile`); refresh()
  }, 'Uploaded and profiled')

  return (
    <div className="col">
      <Card title="Datasets" sub="Originals are stored read-only with content fingerprints. Profiling never modifies data."
        actions={<>
          <Btn icon={Upload} onClick={() => srcRef.current.click()} busy={busy === 'up'}>Add source</Btn>
          <Btn icon={Upload} onClick={() => tgtRef.current.click()}>Replace target</Btn>
          <Btn icon={RefreshCw} busy={busy === 'prof'} onClick={() => act('prof', async () => { await api.post(`/runs/${runId}/profile`); refresh() }, 'Profiles refreshed')}>Re-profile</Btn>
          <input ref={srcRef} type="file" hidden onChange={e => e.target.files[0] && upload('sources', e.target.files[0])} />
          <input ref={tgtRef} type="file" hidden onChange={e => e.target.files[0] && upload('target', e.target.files[0])} />
        </>} flush>
        {!profiles ? <Empty>Loading…</Empty> : !profiles.length ? <Empty>No datasets uploaded.</Empty> : (
          <div className="tw"><table className="t">
            <thead><tr><th>Role</th><th>Alias</th><th>File</th><th className="num">Rows</th><th className="num">Cols</th><th>Candidate keys</th><th>Feeds target</th><th>Fingerprint</th><th /></tr></thead>
            <tbody>{profiles.map(({ dataset: d, profile: p }) => (
              <tr key={d.id} className={`click ${sel === d.id ? 'sel' : ''}`} onClick={() => setSel(d.id)}>
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
                  <button className="btn sm ghost" title="Remove" onClick={() => window.confirm(`Remove ${d.filename} from this run?`) && act('rm', async () => { await api.del(`/runs/${runId}/datasets/${d.id}`); setSel(null); refresh() })}><Trash2 size={14} /></button>
                </td>
              </tr>))}</tbody>
          </table></div>
        )}
      </Card>

      {cur && cur.profile && (
        <Card title={`Column profile · ${cur.dataset.alias}`} sub={`${cur.profile.row_count} rows · ${cur.profile.duplicate_row_count} fully duplicated rows · schema ${cur.profile.schema_fingerprint.slice(7, 19)}`} flush>
          <div className="tw"><table className="t">
            <thead><tr><th>Column</th><th>Type</th><th className="num">Nulls</th><th className="num">Blank</th><th className="num">Distinct</th><th className="num">Dup values</th><th>Min</th><th>Max</th><th>Samples</th><th>Issues</th></tr></thead>
            <tbody>{cur.profile.columns.map(c => (
              <tr key={c.name}>
                <td className="mono">{c.name} {c.sensitive && <Badge s="BLOCKED" title="Masked in samples, logs and reports">masked</Badge>}</td>
                <td className="tiny mono">{c.type}</td>
                <td className="num" style={{ color: c.null_count ? 'var(--blocked)' : undefined }}>{c.null_count}<div className="tiny muted">{c.null_pct}%</div></td>
                <td className="num">{c.blank_count}</td><td className="num">{c.distinct_count}</td><td className="num">{c.duplicate_value_count}</td>
                <td className="small mono">{c.min ?? '—'}</td><td className="small mono">{c.max ?? '—'}</td>
                <td className="tiny">{c.sample_values.map(s => <span key={s} className="chip">{s}</span>)}</td>
                <td className="tiny">{Object.entries(c.format_issues || {}).map(([k, v]) => <div key={k}>{k.replaceAll('_', ' ')}: {v}</div>)}
                  {c.negative_count ? <div>negative: {c.negative_count}</div> : null}{c.zero_count ? <div>zero: {c.zero_count}</div> : null}</td>
              </tr>))}</tbody>
          </table></div>
        </Card>
      )}

      {target && sources.length > 0 && (
        <Card title="Profile comparison" sub="Where each target column name appears among the sources (name match only; mappings decide the real relationship)." flush>
          <div className="tw"><table className="t">
            <thead><tr><th>Target column</th><th>Target type</th>{sources.map(s => <th key={s.dataset.id}>{s.dataset.alias}</th>)}</tr></thead>
            <tbody>{target.dataset.columns.map(tc => (
              <tr key={tc.name}><td className="mono">{tc.name}</td><td className="tiny mono">{tc.type}</td>
                {sources.map(s => { const m = s.dataset.columns.find(c => c.name.toUpperCase() === tc.name.toUpperCase()); return <td key={s.dataset.id} className="tiny mono">{m ? m.type : <span className="muted">—</span>}</td> })}
              </tr>))}</tbody>
          </table></div>
        </Card>
      )}
      {preview && <Modal title={`Preview · ${preview.d.alias}`} onClose={() => setPreview(null)}><DataTable columns={preview.data.columns} rows={preview.data.rows} /></Modal>}
    </div>
  )
}
