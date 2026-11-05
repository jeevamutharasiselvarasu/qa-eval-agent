import React, { useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { Upload, X, Play, CheckCircle2, Circle, Loader2 } from 'lucide-react'
import { api } from '../api.js'
import { Alert, Badge, Btn, Card, useToast } from '../components/ui.jsx'

function Drop({ multiple, onFiles, label }) {
  const ref = useRef()
  const [over, setOver] = useState(false)
  return (
    <div className={`drop ${over ? 'over' : ''}`} onClick={() => ref.current.click()}
      onDragOver={(e) => { e.preventDefault(); setOver(true) }} onDragLeave={() => setOver(false)}
      onDrop={(e) => { e.preventDefault(); setOver(false); onFiles([...e.dataTransfer.files]) }}>
      <Upload size={18} /><div className="mt-s">{label}</div>
      <div className="tiny">CSV, TXT/TSV, XLSX, JSON, Parquet</div>
      <input ref={ref} type="file" hidden multiple={multiple} accept=".csv,.txt,.tsv,.xlsx,.json,.ndjson,.parquet"
        onChange={(e) => { onFiles([...e.target.files]); e.target.value = '' }} />
    </div>
  )
}

const alias = (n) => n.replace(/\.[^.]+$/, '').replace(/[^A-Za-z0-9_]+/g, '_').replace(/^_+|_+$/g, '').toLowerCase().replace(/^(\d)/, 'ds_$1')

export default function NewRun({ meta }) {
  const nav = useNavigate()
  const toast = useToast()
  const [key, setKey] = useState('DATA-1234')
  const [name, setName] = useState('')
  const [sources, setSources] = useState([])
  const [target, setTarget] = useState(null)
  const [settings, setSettings] = useState({ synthetic_records_per_scenario: 12, synthetic_seed: 20261103, default_numeric_tolerance: 0, trim: false, ci: false })
  const [steps, setSteps] = useState([])
  const [err, setErr] = useState(null)
  const busy = steps.some(s => s.state === 'run')

  const addSources = (files) => setSources(s => [...s, ...files.map(f => ({ file: f, alias: alias(f.name), label: f.name }))])
  const canGo = key.trim() && sources.length && target && !busy

  async function go() {
    setErr(null)
    const plan = ['Create QA run', 'Retrieve Jira issue', ...sources.map(s => `Upload source: ${s.alias}`), 'Upload target', 'Parse & profile datasets', 'Propose mappings']
    const st = plan.map(l => ({ label: l, state: 'todo' }))
    setSteps(st)
    const mark = (i, state, note) => setSteps(prev => prev.map((p, j) => j === i ? { ...p, state, note } : p))
    let i = 0
    let runId = null
    try {
      mark(i, 'run')
      const run = await api.post('/runs', { jira_key: key.trim(), name, settings: {
        synthetic_records_per_scenario: Number(settings.synthetic_records_per_scenario), synthetic_seed: Number(settings.synthetic_seed),
        default_numeric_tolerance: Number(settings.default_numeric_tolerance), string_compare: { trim: settings.trim, case_insensitive: settings.ci } } })
      runId = run.id
      mark(i++, 'done', run.id)
      mark(i, 'run')
      const j = await api.post(`/runs/${runId}/jira/retrieve`)
      mark(i++, 'done', `${j.source === 'MOCK' ? 'Mock fixture' : 'Jira Cloud'} · ${j.payload.summary}`)
      for (const s of sources) {
        mark(i, 'run')
        const fd = new FormData(); fd.append('file', s.file); fd.append('alias', s.alias); fd.append('label', s.label)
        const d = await api.upload(`/runs/${runId}/datasets/sources`, fd)
        mark(i++, 'done', `${d.row_count} rows · ${d.columns.length} columns`)
      }
      mark(i, 'run')
      const fd = new FormData(); fd.append('file', target); fd.append('label', target.name)
      const t = await api.upload(`/runs/${runId}/datasets/target`, fd)
      mark(i++, 'done', `${t.row_count} rows · ${t.columns.length} columns`)
      mark(i, 'run'); await api.post(`/runs/${runId}/profile`); mark(i++, 'done')
      mark(i, 'run'); const m = await api.post(`/runs/${runId}/mappings/generate`); mark(i++, 'done', `${m.length} mapping records`)
      toast('QA run ready for review')
      nav(`/runs/${runId}/requirements`)
    } catch (e) {
      mark(i, 'err', e.message)
      setErr({ msg: e.message, runId })
    }
  }

  return (
    <div>
      <div className="page-head"><div><h1>New QA run</h1><p>Enter a Jira ticket, upload one or more source datasets and exactly one existing target dataset.</p></div></div>
      <div className="grid g2" style={{ gridTemplateColumns: 'minmax(0,1.4fr) minmax(0,1fr)' }}>
        <div className="col">
          <Card title="1 · Jira ticket" actions={meta && <Badge s={meta.jira_mode === 'mock' ? 'MOCK' : 'APPROVED'}>{meta.jira_mode === 'mock' ? 'Mock Jira fixtures' : 'Jira Cloud connected'}</Badge>}>
            <div className="grid g2">
              <label className="field">Issue key<input value={key} onChange={e => setKey(e.target.value.toUpperCase())} placeholder="DATA-1234" /></label>
              <label className="field">Run name (optional)<input value={name} onChange={e => setName(e.target.value)} placeholder="Customer master validation" /></label>
            </div>
            {meta?.jira_mode === 'mock' && <div className="tiny muted mt-s">Mock mode serves clearly labelled fixtures (try DATA-1234). Set JIRA_MODE=cloud with credentials in .env for live Jira.</div>}
          </Card>
          <Card title="2 · Source datasets" sub="Each source becomes a SQL table / Python sources[...] key under its alias.">
            <Drop multiple onFiles={addSources} label="Drop source files or click to browse" />
            {sources.length > 0 && <div className="tw mt"><table className="t">
              <thead><tr><th>File</th><th>Alias (table name)</th><th>Label</th><th /></tr></thead>
              <tbody>{sources.map((s, i) => (
                <tr key={i}>
                  <td className="small">{s.file.name}<div className="tiny muted">{(s.file.size / 1024).toFixed(1)} KB</div></td>
                  <td><input value={s.alias} onChange={e => setSources(x => x.map((y, j) => j === i ? { ...y, alias: e.target.value } : y))} style={{ width: 150 }} /></td>
                  <td><input value={s.label} onChange={e => setSources(x => x.map((y, j) => j === i ? { ...y, label: e.target.value } : y))} /></td>
                  <td><button className="btn ghost sm" onClick={() => setSources(x => x.filter((_, j) => j !== i))} aria-label="Remove"><X size={14} /></button></td>
                </tr>))}</tbody>
            </table></div>}
          </Card>
          <Card title="3 · Existing target dataset" sub="Exactly one target per run. It is never modified.">
            {target ? (
              <div className="row between"><div><b>{target.name}</b> <span className="muted small">{(target.size / 1024).toFixed(1)} KB</span></div>
                <button className="btn ghost sm" onClick={() => setTarget(null)}><X size={14} />Remove</button></div>
            ) : <Drop onFiles={(f) => setTarget(f[0])} label="Drop the target file or click to browse" />}
          </Card>
        </div>
        <div className="col">
          <Card title="Execution settings">
            <div className="grid g2">
              <label className="field">Synthetic records / scenario<input type="number" min="3" max="500" value={settings.synthetic_records_per_scenario} onChange={e => setSettings({ ...settings, synthetic_records_per_scenario: e.target.value })} /></label>
              <label className="field">Random seed<input type="number" value={settings.synthetic_seed} onChange={e => setSettings({ ...settings, synthetic_seed: e.target.value })} /></label>
              <label className="field">Default numeric tolerance<input type="number" step="0.0001" min="0" value={settings.default_numeric_tolerance} onChange={e => setSettings({ ...settings, default_numeric_tolerance: e.target.value })} /></label>
            </div>
            <div className="row mt gap-l small">
              <label className="row gap-s"><input type="checkbox" checked={settings.trim} onChange={e => setSettings({ ...settings, trim: e.target.checked })} />Trim strings when comparing</label>
              <label className="row gap-s"><input type="checkbox" checked={settings.ci} onChange={e => setSettings({ ...settings, ci: e.target.checked })} />Case-insensitive</label>
            </div>
            <div className="tiny muted mt-s">Column tolerances derived from Jira precision rules override the default. Settings are part of the approval set.</div>
          </Card>
          <Card title="Start">
            <Btn className="primary" icon={Play} onClick={go} disabled={!canGo} busy={busy}>Create run, retrieve Jira and profile data</Btn>
            {!canGo && !busy && <div className="tiny muted mt-s">Requires a Jira key, at least one source and a target.</div>}
            {steps.length > 0 && <div className="col gap-s mt">
              {steps.map((s, i) => (
                <div key={i} className="row top gap-s small">
                  {s.state === 'done' ? <CheckCircle2 size={16} color="#1f9d55" /> : s.state === 'run' ? <Loader2 size={16} className="spin" /> : s.state === 'err' ? <X size={16} color="#d64545" /> : <Circle size={16} color="#c9ced9" />}
                  <div><div>{s.label}</div>{s.note && <div className="tiny muted">{s.note}</div>}</div>
                </div>))}
            </div>}
            {err && <div className="mt"><Alert kind="err">{err.msg}{err.runId && <> — <a href={`/runs/${err.runId}`}>open the run</a> to continue.</>}</Alert></div>}
          </Card>
        </div>
      </div>
    </div>
  )
}
