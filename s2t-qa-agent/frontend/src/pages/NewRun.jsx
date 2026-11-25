import { useRef, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { Upload, X, Play, CheckCircle2, Circle, Loader2, Search, Cloud } from 'lucide-react'
import { api } from '../api.js'
import { Alert, Badge, Btn, Card, PageHeader, useToast } from '../components/ui.jsx'
import BlobPath, { StoragePath, blobName, describeCheck } from '../components/BlobPath.jsx'

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

function SourceTabs({ mode, setMode }) {
  return (
    <div className="seg" role="tablist">
      <button role="tab" aria-selected={mode === 'file'} className={mode === 'file' ? 'on' : ''} onClick={() => setMode('file')}><Upload size={13} />Upload file</button>
      <button role="tab" aria-selected={mode === 'blob'} className={mode === 'blob' ? 'on' : ''} onClick={() => setMode('blob')}><Cloud size={13} />Azure storage path</button>
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
  const [settings, setSettings] = useState({ default_numeric_tolerance: 0, trim: false, ci: false })
  const [steps, setSteps] = useState([])
  const [err, setErr] = useState(null)
  const [ticket, setTicket] = useState(null)
  const [checking, setChecking] = useState(false)
  const [srcMode, setSrcMode] = useState('file')
  const [tgtMode, setTgtMode] = useState('file')
  const busy = steps.some(s => s.state === 'run')

  const addSources = (files) => setSources(s => [...s, ...files.map(f => ({ file: f, alias: alias(f.name), label: f.name }))])
  const addBlobSource = ({ path, check }) => setSources(s => [...s, { path, check, alias: alias(blobName(path)), label: blobName(path) }])
  const sendSource = (runId, s) => s.path
    ? api.post(`/runs/${runId}/datasets/sources/blob`, { path: s.path, alias: s.alias, label: s.label })
    : (() => { const fd = new FormData(); fd.append('file', s.file); fd.append('alias', s.alias); fd.append('label', s.label); return api.upload(`/runs/${runId}/datasets/sources`, fd) })()
  const sendTarget = (runId) => target.path
    ? api.post(`/runs/${runId}/datasets/target/blob`, { path: target.path, label: target.name })
    : (() => { const fd = new FormData(); fd.append('file', target); fd.append('label', target.name); return api.upload(`/runs/${runId}/datasets/target`, fd) })()
  const canGo = key.trim() && sources.length && target && !busy

  async function go() {
    setErr(null)
    const plan = ['Create QA run', 'Retrieve Jira issue', ...sources.map(s => `${s.path ? 'Read' : 'Upload'} source: ${s.alias}`), target?.path ? 'Read target from storage' : 'Upload target', 'Parse & profile datasets', 'Read how each target column is built',
      'Generate the SQL and test pack (when the requirements are complete)']
    const st = plan.map(l => ({ label: l, state: 'todo' }))
    setSteps(st)
    const mark = (i, state, note) => setSteps(prev => prev.map((p, j) => j === i ? { ...p, state, note } : p))
    let i = 0
    let runId = null
    try {
      mark(i, 'run')
      const run = await api.post('/runs', { jira_key: key.trim(), name, settings: {
        default_numeric_tolerance: Number(settings.default_numeric_tolerance), string_compare: { trim: settings.trim, case_insensitive: settings.ci } } })
      runId = run.id
      mark(i++, 'done', run.id)
      mark(i, 'run')
      const j = await api.post(`/runs/${runId}/jira/retrieve`)
      mark(i++, 'done', `${j.source === 'MOCK' ? 'Mock fixture' : 'Jira Cloud'} · ${j.payload.summary}`)
      for (const s of sources) {
        mark(i, 'run')
        const d = await sendSource(runId, s)
        mark(i++, 'done', `${d.row_count} rows · ${d.columns.length} columns`)
      }
      mark(i, 'run')
      const t = await sendTarget(runId)
      mark(i++, 'done', `${t.row_count} rows · ${t.columns.length} columns`)
      mark(i, 'run'); await api.post(`/runs/${runId}/profile`); mark(i++, 'done')
      mark(i, 'run'); await api.post(`/runs/${runId}/mappings/generate`)
      const { sync, spec } = await api.post(`/runs/${runId}/target-spec/sync`, {})
      mark(i++, 'done', `${spec.columns.length} target columns · ${spec.columns.filter(c => c.state === 'CLEAR').length} clear`)
      mark(i, 'run')
      if (!sync.ready) {
        mark(i++, 'done', `Waiting: ${sync.open_items.length} item(s) in the requirements need your input`)
        toast('Run created: complete the requirements to generate the SQL')
        nav(`/runs/${runId}/requirements`)
        return
      }
      const pk = await api.get(`/runs/${runId}/test-pack`)
      mark(i++, 'done', `SQL v${sync.version ?? ''} · ${pk.test_cases} test cases (${pk.by_type.POSITIVE} positive, ${pk.by_type.NEGATIVE} negative, ${pk.by_type.EDGE} edge)`)
      toast('Requirements are complete and the SQL is ready: check them, then continue to step 2')
      nav(`/runs/${runId}/requirements`)
    } catch (e) {
      mark(i, 'err', e.message)
      setErr({ msg: e.message, runId })
    }
  }

  return (
    <div>
      <PageHeader title="New QA run" description="Enter a Jira ticket, add one or more source files and the target file to validate." />
      <div className="grid g2" style={{ gridTemplateColumns: 'minmax(0,1.4fr) minmax(0,1fr)' }}>
        <div className="col">
          <Card title="1 · Jira ticket" actions={meta && <Badge s={meta.jira_mode === 'mock' ? 'MOCK' : 'APPROVED'} title={meta.jira_site}>{meta.jira_mode === 'mock' ? 'Sample tickets' : 'Jira Cloud connected'}</Badge>}>
            <div className="grid g2">
              <label className="field">Issue key
                <span className="row gap-s"><input value={key} onChange={e => { setKey(e.target.value.toUpperCase()); setTicket(null) }} placeholder="DATA-1234" className="grow" />
                  <Btn icon={Search} busy={checking} onClick={async () => {
                    setChecking(true)
                    try { setTicket({ ok: true, ...(await api.post('/jira/preview', { jira_key: key.trim() })) }) } catch (e) { setTicket({ ok: false, error: e.message }) }
                    setChecking(false)
                  }}>Check</Btn></span></label>
              <label className="field">Run name (optional)<input value={name} onChange={e => setName(e.target.value)} placeholder="Customer master validation" /></label>
            </div>
            {ticket && (ticket.ok ? (
              <div className="mt"><Alert kind="ok"><b>{ticket.key}</b> · {ticket.summary} <span className="tiny">({ticket.issue_type}, {ticket.status}{ticket.source === 'MOCK' ? ', sample ticket' : ''})</span>
                <div className="tiny mt-s">{ticket.acceptance_criteria.length} acceptance criteria found · {ticket.comments} comments · {ticket.attachments} attachments</div>
                {ticket.acceptance_criteria.length === 0 && <div className="tiny mt-s">No acceptance criteria were found. Tests will be limited to general reconciliation.</div>}
              </Alert></div>) : <div className="mt"><Alert kind="err">{ticket.error}</Alert></div>)}
            {meta?.jira_mode === 'mock' && <div className="tiny muted mt-s">Using built-in sample tickets (try DATA-1234 or SALES-77). Connect your Jira Cloud site in <Link to="/settings">Settings</Link>.</div>}
          </Card>
          <Card title="2 · Source datasets" sub="Each source becomes a SQL table / Python sources[...] key under its alias." actions={<SourceTabs mode={srcMode} setMode={setSrcMode} />}>
            {srcMode === 'file' ? <Drop multiple onFiles={addSources} label="Drop source files or click to browse" /> : <BlobPath onAdd={addBlobSource} addLabel="Add source" />}
            {sources.length > 0 && <div className="tw mt"><table className="t">
              <thead><tr><th>File or path</th><th>Alias (table name)</th><th>Label</th><th /></tr></thead>
              <tbody>{sources.map((s, i) => (
                <tr key={i}>
                  <td className="small" style={{ maxWidth: 260, overflowWrap: 'anywhere' }}>{s.path
                    ? <><StoragePath uri={s.path} /><div className="tiny muted">{describeCheck(s.check)}</div></>
                    : <>{s.file.name}<div className="tiny muted">{(s.file.size / 1024).toFixed(1)} KB</div></>}</td>
                  <td><input value={s.alias} onChange={e => setSources(x => x.map((y, j) => j === i ? { ...y, alias: e.target.value } : y))} style={{ width: 150 }} /></td>
                  <td><input value={s.label} onChange={e => setSources(x => x.map((y, j) => j === i ? { ...y, label: e.target.value } : y))} /></td>
                  <td><button className="btn ghost sm" onClick={() => setSources(x => x.filter((_, j) => j !== i))} aria-label="Remove"><X size={14} /></button></td>
                </tr>))}</tbody>
            </table></div>}
          </Card>
          <Card title="3 · Existing target dataset" sub="Exactly one target per run. It is never modified." actions={!target && <SourceTabs mode={tgtMode} setMode={setTgtMode} />}>
            {target ? (
              <div className="row between"><div style={{ overflowWrap: 'anywhere' }}>{target.path
                ? <><b>{target.name}</b><StoragePath uri={target.path} /><div className="tiny muted">{describeCheck(target.check)}</div></>
                : <><b>{target.name}</b> <span className="muted small">{(target.size / 1024).toFixed(1)} KB</span></>}</div>
                <button className="btn ghost sm" onClick={() => setTarget(null)}><X size={14} />Remove</button></div>
            ) : tgtMode === 'file' ? <Drop onFiles={(f) => setTarget(f[0])} label="Drop the target file or click to browse" />
              : <BlobPath addLabel="Use as target" onAdd={({ path, check }) => setTarget({ path, check, name: blobName(path) })} />}
          </Card>
        </div>
        <div className="col">
          <Card title="Execution settings">
            <div className="grid g2">
              <label className="field">Default numeric tolerance<input type="number" step="0.0001" min="0" value={settings.default_numeric_tolerance} onChange={e => setSettings({ ...settings, default_numeric_tolerance: e.target.value })} /></label>
            </div>
            <div className="row mt gap-l small">
              <label className="row gap-s"><input type="checkbox" checked={settings.trim} onChange={e => setSettings({ ...settings, trim: e.target.checked })} />Trim strings when comparing</label>
              <label className="row gap-s"><input type="checkbox" checked={settings.ci} onChange={e => setSettings({ ...settings, ci: e.target.checked })} />Case-insensitive</label>
            </div>
            <div className="tiny muted mt-s">Column tolerances derived from Jira precision rules override the default. Settings are part of the approval set.</div>
          </Card>
          <Card title="Start">
            <Btn className="primary" icon={Play} onClick={go} disabled={!canGo} busy={busy}>Create run</Btn>
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
