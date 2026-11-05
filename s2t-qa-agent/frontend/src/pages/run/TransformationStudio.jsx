import React, { useEffect, useMemo, useState } from 'react'
import CodeMirror from '@uiw/react-codemirror'
import { sql as sqlLang, StandardSQL } from '@codemirror/lang-sql'
import { python } from '@codemirror/lang-python'
import { Sparkles, BookOpen, Wrench, ShieldCheck, Save, Play, CheckCircle2, XCircle, History, Database, RotateCcw, Eye } from 'lucide-react'
import { api } from '../../api.js'
import { useRun } from '../RunWorkspace.jsx'
import { Alert, Badge, Btn, Card, DataTable, Empty, Modal, fmtDate, useAction, useLoad } from '../../components/ui.jsx'
import VisualBuilder from './VisualBuilder.jsx'

const pyTemplate = (aliases) => `import pandas as pd


def transform(sources):
    """sources: dict of dataset alias -> pandas.DataFrame (copies of the run's inputs).
    Return one pandas.DataFrame: the expected target."""
${aliases.map(a => `    ${a} = sources["${a}"]`).join('\n')}
    result = ${aliases[0] || 'pd.DataFrame()'}
    return result
`
const sqlTemplate = (aliases) => `-- DuckDB SQL. Registered datasets: ${aliases.join(', ')}\nSELECT *\nFROM ${aliases[0] || 'source'}\n`

function Diff({ a, b }) {
  const al = (a || '').split('\n'), bl = (b || '').split('\n')
  const sa = new Set(al), sb = new Set(bl)
  return (
    <div className="grid g2">
      <div className="pre">{al.map((l, i) => <div key={i} style={{ background: sb.has(l) ? undefined : '#fdecea' }}>{l || ' '}</div>)}</div>
      <div className="pre">{bl.map((l, i) => <div key={i} style={{ background: sa.has(l) ? undefined : '#e6f5ec' }}>{l || ' '}</div>)}</div>
    </div>
  )
}

export default function TransformationStudio() {
  const { runId, run, reload: reloadRun, meta } = useRun()
  const [tr, reload] = useLoad(() => api.get(`/runs/${runId}/transformations`), [runId])
  const [schema] = useLoad(() => api.get(`/runs/${runId}/transformations/schema`), [runId])
  const [mode, setMode] = useState('SQL')
  const [drafts, setDrafts] = useState({ SQL: '', PYTHON: '', VISUAL: null })
  const [loadedFrom, setLoadedFrom] = useState(null)
  const [out, setOut] = useState(null)
  const [explain, setExplain] = useState(null)
  const [fix, setFix] = useState(null)
  const [instruction, setInstruction] = useState('')
  const [limit, setLimit] = useState(100)
  const [view, setView] = useState(null)
  const [busy, act] = useAction()
  const aliases = Object.keys(schema?.sources || {})
  const cur = tr?.current

  useEffect(() => {
    if (!tr || !schema) return
    if (cur && loadedFrom !== cur.id) {
      setMode(cur.mode)
      setDrafts(d => ({ ...d, [cur.mode]: cur.mode === 'VISUAL' ? cur.visual_spec : cur.code }))
      setLoadedFrom(cur.id)
    } else if (!cur && loadedFrom === null) {
      setDrafts({ SQL: sqlTemplate(aliases), PYTHON: pyTemplate(aliases), VISUAL: { sources: aliases.slice(0, 1).map(a => ({ alias: a, as: a, filters: [] })), joins: [], filters: [], columns: [] } })
      setLoadedFrom('template')
    }
  }, [tr, schema]) // eslint-disable-line react-hooks/exhaustive-deps

  const switchMode = (m) => {
    setMode(m); setOut(null)
    if (drafts[m] === '' || drafts[m] === null) {
      const lastOfMode = [...(tr?.versions || [])].reverse().find(v => v.mode === m)
      setDrafts(d => ({ ...d, [m]: lastOfMode ? (m === 'VISUAL' ? lastOfMode.visual_spec : lastOfMode.code) : m === 'SQL' ? sqlTemplate(aliases) : m === 'PYTHON' ? pyTemplate(aliases) : { sources: aliases.slice(0, 1).map(a => ({ alias: a, as: a, filters: [] })), joins: [], filters: [], columns: [] } }))
    }
  }
  const body = () => ({ mode, code: mode === 'VISUAL' ? '' : drafts[mode], visual_spec: mode === 'VISUAL' ? drafts.VISUAL : null })
  const draftEqualsCurrent = cur && cur.mode === mode && (mode === 'VISUAL' ? JSON.stringify(cur.visual_spec) === JSON.stringify(drafts.VISUAL) : cur.code === drafts[mode])
  const lastErr = out?.errors?.[0]
  const ext = useMemo(() => mode === 'PYTHON' ? [python()] : [sqlLang({ dialect: StandardSQL, upperCaseKeywords: true,
    schema: Object.fromEntries(Object.entries(schema?.sources || {}).map(([a, cs]) => [a, cs.map(c => c.name)])) })], [mode, schema])

  const refresh = async () => { reload(); reloadRun() }
  const loadVersion = (v) => { setMode(v.mode); setDrafts(d => ({ ...d, [v.mode]: v.mode === 'VISUAL' ? v.visual_spec : v.code })); setLoadedFrom(v.id) }

  const generate = () => act('gen', async () => {
    const v = await api.post(`/runs/${runId}/transformations/generate`, { mode, instruction })
    loadVersion(v); setOut({ kind: 'validation', ...v.validation }); setInstruction(''); refresh()
  }, `AI draft saved as a new version — review it before approving`)
  const validate = () => act('val', async () => setOut({ kind: 'validation', ...(await api.post(`/runs/${runId}/transformations/validate`, body())) }))
  const save = async () => {
    const v = await api.post(`/runs/${runId}/transformations`, { ...body(), comment: '' })
    setLoadedFrom(v.id); await refresh(); return v
  }
  const preview = () => act('prev', async () => {
    const v = draftEqualsCurrent ? cur : await save()
    if (!v.validation.valid) { setOut({ kind: 'validation', ...v.validation }); return }
    setOut({ kind: 'preview', ...(await api.post(`/runs/${runId}/transformations/${v.id}/preview`, { limit: Number(limit) })) })
  })
  const doExplain = () => act('exp', async () => setExplain((await api.post(`/runs/${runId}/transformations/explain`, body())).explanation))
  const doFix = () => act('fix', async () => setFix(await api.post(`/runs/${runId}/transformations/fix`, { ...body(), error: lastErr ? `${lastErr.message}` : '' })))
  const decide = (decision) => {
    const reason = decision === 'REJECTED' ? window.prompt('Reason for rejection?') : ''
    if (decision === 'REJECTED' && !reason) return
    act('apr', async () => { await api.post(`/runs/${runId}/transformations/${cur.id}/approve`, { decision, reason }); refresh() }, `Version ${decision.toLowerCase()}`)
  }
  const insert = (text) => mode !== 'VISUAL' && setDrafts(d => ({ ...d, [mode]: d[mode] + text }))

  if (!tr || !schema) return <div className="muted">Loading…</div>
  const apr = tr.approval
  return (
    <div className="col">
      <Card title={<div className="row wrap gap-s"><h3>Transformation Studio</h3>
        {cur && <><Badge className="outline">v{cur.version} · {cur.mode}</Badge><Badge s={cur.origin}>{cur.origin.replaceAll('_', ' ')}</Badge><Badge s={apr.status}>approval: {apr.status}</Badge></>}
        {!draftEqualsCurrent && <Badge s="PENDING">unsaved draft</Badge>}</div>}
        sub="SQL, Python and Visual definitions all execute through the same deterministic engine. AI output is a proposal until approved."
        actions={<div className="seg">{['SQL', 'PYTHON', 'VISUAL'].map(m => <button key={m} className={mode === m ? 'on' : ''} onClick={() => switchMode(m)}>{m === 'PYTHON' ? 'Python' : m === 'VISUAL' ? 'Visual builder' : 'SQL'}</button>)}</div>}>
        <div className="split">
          <div>
            <h4 className="row gap-s mb"><Database size={14} />Datasets</h4>
            <div className="schema-tree">
              {Object.entries(schema.sources).map(([a, cs]) => (
                <div key={a}>
                  <div className="ds" onClick={() => insert(a)} style={{ cursor: 'pointer' }}>{a}</div>
                  {cs.map(c => <div key={c.name} className="c" onClick={() => insert(mode === 'PYTHON' ? `["${c.name}"]` : `${a}.${c.name}`)}><span className="mono">{c.name}</span><span className="tiny muted">{c.type.toLowerCase()}</span></div>)}
                </div>))}
              <div className="ds mt">target (uploaded)</div>
              {schema.target.map(c => <div key={c.name} className="c"><span className="mono">{c.name}</span><span className="tiny muted">{c.type.toLowerCase()}</span></div>)}
            </div>
            <div className="tiny muted mt">{mode === 'PYTHON' ? schema.python_contract : mode === 'SQL' ? `Dialect: ${meta?.sql_dialect || 'DuckDB'}. One read-only SELECT.` : 'Compiles to DuckDB SQL.'}</div>
          </div>
          <div className="col" style={{ minWidth: 0 }}>
            {mode === 'VISUAL' ? <VisualBuilder spec={drafts.VISUAL} schema={schema} onChange={v => setDrafts(d => ({ ...d, VISUAL: v }))} /> :
              <CodeMirror value={drafts[mode] || ''} height="380px" extensions={ext} basicSetup={{ lineNumbers: true, foldGutter: true, highlightActiveLine: true, autocompletion: true }}
                onChange={v => setDrafts(d => ({ ...d, [mode]: v }))} />}
            <div className="row wrap gap-s">
              <input value={instruction} onChange={e => setInstruction(e.target.value)} placeholder={mode === 'VISUAL' ? 'AI draft from accepted mappings' : 'Optional instruction, e.g. “aggregate NET_AMOUNT by CUSTOMER_ID”'} className="grow" style={{ minWidth: 220 }} />
              <Btn icon={Sparkles} busy={busy === 'gen'} onClick={generate}>AI generate</Btn>
              <Btn icon={BookOpen} busy={busy === 'exp'} onClick={doExplain}>Explain</Btn>
              <Btn icon={Wrench} busy={busy === 'fix'} onClick={doFix} disabled={mode === 'VISUAL'} title={lastErr ? 'Suggest a fix for the last error' : 'Run or validate first'}>Fix</Btn>
            </div>
            <div className="row wrap gap-s">
              <Btn icon={ShieldCheck} busy={busy === 'val'} onClick={validate}>Validate</Btn>
              <Btn icon={Save} busy={busy === 'save'} disabled={draftEqualsCurrent} onClick={() => act('save', save, 'Saved as a new version')}>Save version</Btn>
              <span className="row gap-s small"><Btn className="primary" icon={Play} busy={busy === 'prev'} onClick={preview}>Run preview</Btn>
                <select value={limit} onChange={e => setLimit(e.target.value)}>{[20, 100, 500, 5000].map(n => <option key={n} value={n}>{n} rows</option>)}</select></span>
              <span className="grow" />
              {cur && <>
                <Btn icon={CheckCircle2} busy={busy === 'apr'} disabled={!draftEqualsCurrent || apr.status === 'APPROVED'} onClick={() => decide('APPROVED')}>Approve v{cur.version}</Btn>
                <Btn icon={XCircle} className="danger" disabled={!draftEqualsCurrent} onClick={() => decide('REJECTED')}>Reject</Btn></>}
            </div>
            {!draftEqualsCurrent && cur && <div className="tiny muted">Approval applies to a saved version. Saving a change creates v{cur.version + 1} and invalidates the existing approval.</div>}
          </div>
        </div>
      </Card>

      {explain && <Alert kind="info"><b>Explanation{meta?.llm_mode === 'mock' ? ' (mock provider)' : ''}:</b> {explain} <button className="btn sm ghost" onClick={() => setExplain(null)}>dismiss</button></Alert>}

      {out && <Card title={out.kind === 'preview' ? `Preview · ${out.status}` : `Validation · ${out.valid ? 'passed' : 'failed'}`}
        sub={out.kind === 'preview' ? `${out.row_count} rows · ${out.duration_ms} ms · ${out.code_version}` : 'Static safety and syntax checks; nothing was executed.'}>
        {out.errors?.length > 0 && <div className="col gap-s mb">{out.errors.map((e, i) => <Alert key={i} kind="err"><b>{e.kind}{e.line ? ` · line ${e.line}` : ''}:</b> {e.message}</Alert>)}</div>}
        {out.warnings?.length > 0 && <div className="col gap-s mb">{out.warnings.map((w, i) => <Alert key={i} kind="warn">{w}</Alert>)}</div>}
        {out.compiled_sql && <details className="mb"><summary className="small">Compiled SQL</summary><div className="pre mt-s">{out.compiled_sql}</div></details>}
        {out.kind === 'preview' && out.status === 'SUCCESS' && <>
          <div className="row wrap gap-s mb">{out.output_schema.map(c => <span key={c.name} className="chip">{c.name}: {c.type}</span>)}</div>
          <DataTable columns={out.preview.columns} rows={out.preview.rows} />
        </>}
        {out.logs?.length > 0 && <details className="mt"><summary className="small">Logs</summary><div className="pre mt-s">{out.logs.join('\n')}</div></details>}
        {out.kind === 'validation' && out.valid && !out.errors?.length && <Alert kind="ok">No safety or syntax problems found{out.referenced_datasets ? ` · reads ${out.referenced_datasets.join(', ')}` : ''}.</Alert>}
      </Card>}

      <Card title={<span className="row gap-s"><History size={15} /><h3>Version history</h3></span>} flush>
        {!tr.versions.length ? <Empty>No saved versions yet.</Empty> : (
          <div className="tw"><table className="t">
            <thead><tr><th>Version</th><th>Mode</th><th>Origin</th><th>Validation</th><th>Hash</th><th>By</th><th>Saved</th><th>Note</th><th /></tr></thead>
            <tbody>{[...tr.versions].reverse().map(v => (
              <tr key={v.id} className={cur?.id === v.id ? 'sel' : ''}>
                <td className="mono">v{v.version}{cur?.id === v.id && <Badge s="info" className="mt-s">current</Badge>}</td>
                <td>{v.mode}</td><td><Badge s={v.origin}>{v.origin.replaceAll('_', ' ')}</Badge></td>
                <td><Badge s={v.validation.valid ? 'PASS' : 'FAIL'}>{v.validation.valid ? 'valid' : 'invalid'}</Badge></td>
                <td className="tiny mono muted">{v.content_hash.slice(7, 17)}</td><td className="small">{v.created_by}</td><td className="small nowrap">{fmtDate(v.created_at)}</td>
                <td className="tiny muted" style={{ maxWidth: 260 }}>{v.comment}</td>
                <td className="nowrap">
                  <button className="btn sm ghost" title="View / diff" onClick={() => setView(v)}><Eye size={14} /></button>
                  {cur?.id !== v.id && <button className="btn sm ghost" title="Restore as new version" onClick={() => act('rs', async () => { const nv = await api.post(`/runs/${runId}/transformations/restore`, { version: v.version }); loadVersion(nv); refresh() }, `Restored v${v.version}`)}><RotateCcw size={14} /></button>}
                </td>
              </tr>))}</tbody>
          </table></div>
        )}
      </Card>

      {fix && <Modal title="Suggested fix" onClose={() => setFix(null)} footer={<><button className="btn" onClick={() => setFix(null)}>Discard</button>
        <button className="btn primary" onClick={() => { setDrafts(d => ({ ...d, [mode]: fix.code })); setFix(null) }}>Apply to editor (not saved)</button></>}>
        <p className="small">{fix.explanation} <Badge s="AI_INFERRED">{fix.provider}</Badge></p>
        <Diff a={drafts[mode]} b={fix.code} />
      </Modal>}
      {view && <Modal title={`v${view.version} · ${view.mode} · ${view.origin}`} onClose={() => setView(null)}
        footer={<button className="btn" onClick={() => { loadVersion(view); setView(null) }}>Load into editor</button>}>
        {cur && cur.id !== view.id ? <><div className="row between small muted mb"><span>v{view.version}</span><span>current v{cur.version}</span></div><Diff a={view.code} b={cur.code} /></> : <div className="pre">{view.code}</div>}
        {view.visual_spec && <details className="mt"><summary className="small">Visual definition</summary><div className="pre">{JSON.stringify(view.visual_spec, null, 2)}</div></details>}
      </Modal>}
    </div>
  )
}
