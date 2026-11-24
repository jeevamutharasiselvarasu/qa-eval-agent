import React, { useEffect, useMemo, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { RefreshCw, Database, SlidersHorizontal, ListChecks, Play } from 'lucide-react'
import { api } from '../../api.js'
import { useRun } from '../RunWorkspace.jsx'
import { Alert, Badge, Btn, Card, DataTable, Empty, Modal, useAction, useLoad } from '../../components/ui.jsx'
import { ResultModal } from './ExecutionMonitor.jsx'

export const TYPE_STYLE = {
  POSITIVE: { bg: '#e6f5ec', fg: '#137a3e', label: 'Positive' },
  NEGATIVE: { bg: '#fdecea', fg: '#b42318', label: 'Negative' },
  EDGE: { bg: '#fff3dd', fg: '#9a5b05', label: 'Edge' },
  GENERAL: { bg: '#eef0f4', fg: '#4b5468', label: 'General' },
}
export function TypeBadge({ t }) {
  const s = TYPE_STYLE[t] || TYPE_STYLE.GENERAL
  return <span className="badge" style={{ background: s.bg, color: s.fg }}>{s.label}</span>
}

/** Latest finished result per test case (across the actual and synthetic tracks). */
export function useLatestResults(runId, refreshKey = 0) {
  const [map, setMap] = useState({})
  const [execIds, setExecIds] = useState({})
  useEffect(() => {
    let alive = true
    api.get(`/runs/${runId}/executions`).then(async (execs) => {
      const latest = {}
      for (const e of execs) if (!latest[e.track] && !['QUEUED', 'RUNNING'].includes(e.status)) latest[e.track] = e
      const out = {}, ids = {}
      for (const e of Object.values(latest)) {
        const d = await api.get(`/executions/${e.id}`)
        for (const r of d.results) { out[r.test_case_id] = r; ids[r.test_case_id] = e.id }
      }
      if (alive) { setMap(out); setExecIds(ids) }
    }).catch(() => {})
    return () => { alive = false }
  }, [runId, refreshKey])
  return [map, execIds]
}

function ScenarioModal({ s, runId, onClose }) {
  const [tab, setTab] = useState(0)
  const items = [...s.sources.map(d => ({ label: `Source: ${d.alias}`, d })), ...(s.expected ? [{ label: 'Expected output', d: s.expected }] : [])]
  const [rows] = useLoad(() => items[tab] ? api.get(`/runs/${runId}/datasets/${items[tab].d.id}/preview?limit=100`) : Promise.resolve(null), [tab])
  return (
    <Modal title={`${s.id} · ${s.name}`} onClose={onClose}>
      <ul className="small" style={{ marginTop: 0, paddingLeft: 18 }}>{s.explanation.map((e, i) => <li key={i}>{e}</li>)}</ul>
      {s.assumptions.map((a, i) => <div key={i} className="tiny" style={{ color: 'var(--blocked)' }}>Assumption: {a}</div>)}
      {s.blocked_reason && <Alert kind="warn">{s.blocked_reason}</Alert>}
      <div className="tabs mt">{items.map((it, i) => <button key={i} className={tab === i ? 'active' : ''} onClick={() => setTab(i)}>{it.label} ({it.d.row_count})</button>)}</div>
      {rows ? <DataTable columns={rows.columns} rows={rows.rows} /> : <Empty>Loading…</Empty>}
      <div className="tiny muted mt">Synthetic data only; never mixed with uploaded files. The expected output is produced by running the transformation on this data.</div>
    </Modal>
  )
}

export const BASIS = {
  ACCEPTANCE_CRITERIA: 'Acceptance criteria', TRANSFORMATION_LOGIC: 'Transformation logic', GENERAL: 'General checks',
}

function TriBox({ ids, sel, onChange }) {
  const n = ids.filter(i => sel.has(i)).length
  return <input type="checkbox" checked={n > 0 && n === ids.length} ref={el => { if (el) el.indeterminate = n > 0 && n < ids.length }}
    onChange={e => onChange(ids, e.target.checked)} title={n === ids.length ? 'Deselect all in this group' : 'Select all in this group'} />
}

export default function TestCases() {
  const { runId, reload: reloadRun } = useRun()
  const navigate = useNavigate()
  const [data, reload] = useLoad(() => api.get(`/runs/${runId}/test-cases`), [runId])
  const [pack, reloadPack] = useLoad(() => api.get(`/runs/${runId}/test-pack`), [runId])
  const [scen, reloadScen] = useLoad(() => api.get(`/runs/${runId}/synthetic-data`), [runId])
  const [results, execIds] = useLatestResults(runId)
  const [type, setType] = useState('ALL')
  const [track, setTrack] = useState('ALL')
  const [basis, setBasis] = useState('ALL')
  const [sel, setSel] = useState(null)
  const [showScen, setShowScen] = useState(null)
  const [openRes, setOpenRes] = useState(null)
  const [tune, setTune] = useState(null)
  const [busy, act] = useAction()
  const refresh = () => { reload(); reloadPack(); reloadScen(); reloadRun() }
  const scenById = useMemo(() => Object.fromEntries((scen || []).map(s => [s.id, s])), [scen])
  useEffect(() => { if (data) setSel(new Set(data.test_cases.filter(t => t.selected !== false).map(t => t.id))) }, [data])

  const all = data?.test_cases || []
  const tcs = all.filter(t => (type === 'ALL' || t.test_type === type) && (track === 'ALL' || t.track === track) && (basis === 'ALL' || t.basis === basis))
    .sort((x, y) => x.id.localeCompare(y.id, undefined, { numeric: true }))
  const acs = data?.coverage?.acceptance_criteria || []
  const groups = [
    ...acs.map(a => ({ key: a.ac_id, title: a.ac_id, text: a.text, tests: tcs.filter(t => t.ac_ids.includes(a.ac_id)) })),
    { key: 'TL', title: 'Transformation logic', text: 'Derived from the SQL itself: every function, CASE, COALESCE, ROUND, join, filter and GROUP BY it uses.', tests: tcs.filter(t => t.basis === 'TRANSFORMATION_LOGIC') },
    { key: 'GEN', title: 'General', text: 'Reconciliation and data-quality checks that apply to every target.', tests: tcs.filter(t => !t.ac_ids.length && t.basis !== 'TRANSFORMATION_LOGIC') },
  ]
  const patch = (t, body, msg) => act(t.id, async () => { await api.patch(`/runs/${runId}/test-cases/${t.id}`, body); refresh() }, msg)
  const choose = (ids, on) => {
    if (!sel || !ids.length) return
    const next = new Set(sel); ids.forEach(i => on ? next.add(i) : next.delete(i)); setSel(next)
    api.put(`/runs/${runId}/test-cases/selection`, on ? { select: ids } : { deselect: ids }).then(() => reloadPack()).catch(() => reload())
  }
  const selected = all.filter(t => sel?.has(t.id))
  const runnable = selected.filter(t => t.state !== 'BLOCKED').length
  const approved = pack?.pack_approved && pack?.transformation === 'APPROVED'
  const runSelected = () => act('run', async () => {
    await api.post(`/runs/${runId}/run-validation`, { force: false })
    navigate(`/runs/${runId}/results`)
  }, `Running ${selected.length} selected test case(s); the report is created when they finish`)

  return (
    <div className="col">
      <Card title="Test pack" sub="Built from the acceptance criteria and from the transformation logic: positive, negative and edge cases, on the uploaded data and on generated synthetic data."
        actions={<Btn icon={RefreshCw} busy={busy === 'build'} onClick={() => act('build', async () => { await api.post(`/runs/${runId}/test-pack`); refresh() }, 'Test pack rebuilt (your selection is kept)')}>Rebuild test pack</Btn>}>
        {!pack ? <Empty>Loading…</Empty> : <>
          {pack.stale && <div className="mb"><Alert kind="warn">Requirements, data or the transformation changed after this pack was built. Rebuild it before approving.</Alert></div>}
          <div className="row wrap gap-l">
            {['POSITIVE', 'NEGATIVE', 'EDGE', 'GENERAL'].map(k => <div key={k} className="row gap-s"><TypeBadge t={k} /><b>{pack.by_type[k]}</b></div>)}
            <span className="small muted">· {pack.test_cases} test cases · {pack.blocked_tests} blocked · {pack.scenarios.generated} synthetic scenarios</span>
          </div>
          <div className="row wrap gap-l small mt-s">
            {Object.entries(BASIS).map(([k, l]) => <span key={k}>{l}: <b>{pack.by_basis?.[k] ?? 0}</b></span>)}
          </div>
          {pack.logic_note && <div className="tiny muted mt-s">No transformation-logic tests: {pack.logic_note}</div>}
        </>}
        <div className="row wrap gap-s mt">
          <SlidersHorizontal size={14} className="muted" />
          <div className="seg">{['ALL', 'POSITIVE', 'NEGATIVE', 'EDGE', 'GENERAL'].map(k => <button key={k} className={type === k ? 'on' : ''} onClick={() => setType(k)}>{k === 'ALL' ? 'All types' : TYPE_STYLE[k].label}</button>)}</div>
          <div className="seg">{[['ALL', 'All sources'], ...Object.entries(BASIS)].map(([k, l]) => <button key={k} className={basis === k ? 'on' : ''} onClick={() => setBasis(k)}>{l}</button>)}</div>
          <div className="seg">{[['ALL', 'All data'], ['ACTUAL', 'Uploaded data'], ['SYNTHETIC', 'Synthetic data']].map(([k, l]) => <button key={k} className={track === k ? 'on' : ''} onClick={() => setTrack(k)}>{l}</button>)}</div>
        </div>
      </Card>

      {sel && all.length > 0 && <div className="card card-b selbar">
        <div className="row wrap gap-s">
          <ListChecks size={16} />
          <b>{selected.length} of {all.length} test cases selected</b>
          {selected.length > runnable && <span className="small muted">({selected.length - runnable} blocked: reported as BLOCKED)</span>}
          <span className="grow" />
          <button className="btn sm" onClick={() => choose(all.map(t => t.id), true)}>Select all</button>
          <button className="btn sm" onClick={() => choose(all.map(t => t.id), false)}>Select none</button>
          {tcs.length !== all.length && <>
            <button className="btn sm" onClick={() => choose(tcs.map(t => t.id), true)}>Select shown ({tcs.length})</button>
            <button className="btn sm" onClick={() => choose(tcs.map(t => t.id), false)}>Deselect shown</button></>}
          <Btn className="primary" icon={Play} busy={busy === 'run'} disabled={!approved || !selected.length} onClick={runSelected}
            title={!approved ? 'Both approvals are needed first' : ''}>Run selected and create report</Btn>
        </div>
        {!approved && <div className="tiny muted mt-s">Approve the transformation and the test pack on <Link to={`/runs/${runId}/review`}>Review &amp; approve</Link> first. Choosing tests does not need a new approval.</div>}
      </div>}

      {!data ? <Empty>Loading…</Empty> : !data.test_cases.length ? <Card><Empty>No test cases yet. Complete the <Link to={`/runs/${runId}/requirements`}>Requirements</Link>; the test pack is built automatically.</Empty></Card> :
        groups.filter(g => g.tests.length).map(g => (
          <Card key={g.key} title={<div className="row top gap-s">{sel && <TriBox ids={g.tests.map(t => t.id)} sel={sel} onChange={choose} />}
            <span className="badge outline">{g.title}</span><span className="small" style={{ fontWeight: 500 }}>{g.text}</span></div>} flush>
            <div className="tw"><table className="t">
              <thead><tr><th style={{ width: 28 }} /><th>ID</th><th>Type</th><th>Test case</th><th>Data</th><th>Expected result</th><th>Last result</th><th /></tr></thead>
              <tbody>{g.tests.map(t => {
                const r = results[t.id]
                const sc = t.scenario_id && scenById[t.scenario_id]
                const on = sel?.has(t.id)
                return (
                  <tr key={t.id} style={on ? {} : { opacity: 0.55 }}>
                    <td><input type="checkbox" checked={!!on} onChange={e => choose([t.id], e.target.checked)} aria-label={`Select ${t.id}`} /></td>
                    <td className="mono small nowrap">{t.id}</td>
                    <td><TypeBadge t={t.test_type} /></td>
                    <td style={{ maxWidth: 380 }}>{t.title}{t.rationale && <div className="tiny muted">{t.rationale}</div>}</td>
                    <td className="small nowrap">{t.track === 'ACTUAL' ? <span className="muted">Uploaded files</span> :
                      sc ? <button className="btn sm ghost" onClick={() => setShowScen(sc)} title="View the synthetic data"><Database size={13} />{sc.name}</button> : t.scenario_id}</td>
                    <td className="small" style={{ maxWidth: 240 }}>{t.expected_result}</td>
                    <td className="nowrap">
                      {t.state === 'BLOCKED' ? <span title={t.blocked_reason}><Badge s="BLOCKED" /></span>
                        : r ? <button className="btn sm ghost" style={{ padding: 0 }} onClick={() => setOpenRes({ r, ex: execIds[t.id] })}><Badge s={r.status} /></button>
                          : <span className="tiny muted">not run</span>}
                      {t.state === 'BLOCKED' && <div className="tiny" style={{ color: 'var(--blocked)', maxWidth: 220, whiteSpace: 'normal' }}>{t.blocked_reason}</div>}
                    </td>
                    <td className="nowrap">{t.validation.type === 'COLUMN_MATCH' && <button className="btn sm" onClick={() => setTune(t)}>Tune</button>}</td>
                  </tr>)
              })}</tbody>
            </table></div>
          </Card>
        ))}
      {showScen && <ScenarioModal s={showScen} runId={runId} onClose={() => setShowScen(null)} />}
      {openRes && <ResultModal r={openRes.r} exId={openRes.ex} onClose={() => setOpenRes(null)} />}
      {tune && <TuneModal t={tune} onClose={() => setTune(null)} onSave={(b) => { patch(tune, b, 'Test updated: the test pack must be approved again'); setTune(null) }} />}
    </div>
  )
}

function TuneModal({ t, onClose, onSave }) {
  const [tol, setTol] = useState(t.validation.tolerance ?? 0)
  const [trim, setTrim] = useState(!!t.validation.trim)
  const [ci, setCi] = useState(!!t.validation.case_insensitive)
  return (
    <Modal title={`Comparison options · ${t.id}`} onClose={onClose} footer={<><button className="btn" onClick={onClose}>Cancel</button>
      <button className="btn primary" onClick={() => onSave({ tolerance: Number(tol), trim, case_insensitive: ci })}>Save</button></>}>
      <p className="small">{t.title}</p>
      <label className="field">Numeric tolerance (absolute)<input type="number" step="0.0001" min="0" value={tol} onChange={e => setTol(e.target.value)} /></label>
      <div className="row gap-l mt small">
        <label className="row gap-s"><input type="checkbox" checked={trim} onChange={e => setTrim(e.target.checked)} />Ignore leading/trailing whitespace</label>
        <label className="row gap-s"><input type="checkbox" checked={ci} onChange={e => setCi(e.target.checked)} />Case-insensitive</label>
      </div>
      <div className="tiny muted mt">Loosening a comparison can hide defects the requirement cares about (e.g. TRIM/UPPER rules). The change is audited.</div>
    </Modal>
  )
}
