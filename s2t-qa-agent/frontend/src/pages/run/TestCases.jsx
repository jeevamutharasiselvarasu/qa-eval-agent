import { useEffect, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { RefreshCw, Filter, SlidersHorizontal, ListChecks, Play, CheckCircle2, XCircle } from 'lucide-react'
import StepNav from './StepNav.jsx'
import { api } from '../../api.js'
import { useRun } from '../RunWorkspace.jsx'
import { Alert, Badge, Btn, Card, Empty, Loading, Modal, useAction, useLoad } from '../../components/ui.jsx'
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

/** Latest finished result per test case. */
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

export const BASIS = {
  ACCEPTANCE_CRITERIA: 'Acceptance criteria', TRANSFORMATION_LOGIC: 'Transformation logic', GENERAL: 'General checks',
}

function TriBox({ ids, sel, onChange }) {
  const n = ids.filter(i => sel.has(i)).length
  return <input type="checkbox" checked={n > 0 && n === ids.length} ref={el => { if (el) el.indeterminate = n > 0 && n < ids.length }}
    onChange={e => onChange(ids, e.target.checked)} title={n === ids.length ? 'Deselect all in this group' : 'Select all in this group'} />
}

export default function TestCases() {
  const { runId, reload: reloadRun, meta } = useRun()
  const canApprove = ['approver', 'admin'].includes(meta?.role)
  const navigate = useNavigate()
  const [data, reload] = useLoad(() => api.get(`/runs/${runId}/test-cases`), [runId])
  const [pack, reloadPack] = useLoad(() => api.get(`/runs/${runId}/test-pack`), [runId])
  const [results, execIds] = useLatestResults(runId)
  const [type, setType] = useState('ALL')
  const [scopeF, setScopeF] = useState('ALL')
  const [basis, setBasis] = useState('ALL')
  const [sel, setSel] = useState(null)
  const [openRes, setOpenRes] = useState(null)
  const [tune, setTune] = useState(null)
  const [busy, act] = useAction()
  const refresh = () => { reload(); reloadPack(); reloadRun() }
  useEffect(() => { if (data) setSel(new Set(data.test_cases.filter(t => t.selected !== false).map(t => t.id))) }, [data])

  const all = data?.test_cases || []
  const tcs = all.filter(t => (type === 'ALL' || t.test_type === type) && (scopeF === 'ALL' || (scopeF === 'EDGE' ? !!t.validation?.scope : !t.validation?.scope)) && (basis === 'ALL' || t.basis === basis))
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
  const sqlOk = pack?.transformation === 'APPROVED'
  const approved = pack?.pack_approved && sqlOk
  const needsBuild = pack && (!pack.built || pack.stale)
  const runSelected = (approveFirst) => act('run', async () => {
    if (approveFirst) await api.post(`/runs/${runId}/test-pack/approve`, { decision: 'APPROVED', reason: '' })
    await api.post(`/runs/${runId}/run-validation`, { force: false })
    reloadRun()
    navigate(`/runs/${runId}/results`)
  }, `${approveFirst ? 'Test pack approved. ' : ''}Running ${selected.length} selected test case(s); the report is created when they finish`)
  const rejectPack = () => {
    const reason = window.prompt('Why is the test pack rejected?')
    if (reason) act('rej', async () => { await api.post(`/runs/${runId}/test-pack/approve`, { decision: 'REJECTED', reason }); refresh() }, 'Test pack rejected')
  }
  const rebuild = () => act('build', async () => { await api.post(`/runs/${runId}/test-pack`); refresh() }, 'Test pack rebuilt (your selection is kept)')
  const LABELS = { REQUIREMENTS: 'Requirements & answers', DATASETS: 'Source and target files', MAPPINGS: 'Target column rules',
    TEST_CASES: 'Test cases', EXECUTION_SETTINGS: 'Execution settings' }

  return (
    <div className="col">
      <StepNav step="tests" />
      <Card title="Test pack" sub="Built from the acceptance criteria and from the transformation logic. Every test checks the uploaded target against the expected target the approved SQL builds from the uploaded sources. Negative and edge cases check only the records in the uploaded data that hit that situation, and are skipped if there are none."
        actions={<><Badge s={approved ? 'APPROVED' : needsBuild ? 'STALE' : 'PENDING'}>{approved ? 'Test pack approved' : needsBuild ? 'Needs rebuild' : 'Test pack not approved yet'}</Badge>
          <Btn icon={RefreshCw} busy={busy === 'build'} onClick={rebuild}>Rebuild test pack</Btn></>}>
        {!pack ? <Loading /> : <>
          {pack.stale && <div className="mb"><Alert kind="warn">Requirements, data or the transformation changed after this pack was built. Rebuild it before approving.</Alert></div>}
          <div className="row wrap gap-l">
            {['POSITIVE', 'NEGATIVE', 'EDGE', 'GENERAL'].map(k => <div key={k} className="row gap-s"><TypeBadge t={k} /><b>{pack.by_type[k]}</b></div>)}
            <span className="small muted">· {pack.test_cases} test cases · {pack.blocked_tests} blocked</span>
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
          <div className="seg">{[['ALL', 'All records'], ['WHOLE', 'Whole target'], ['EDGE', 'Edge-case records']].map(([k, l]) => <button key={k} className={scopeF === k ? 'on' : ''} onClick={() => setScopeF(k)}>{l}</button>)}</div>
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
          {!sqlOk ? <span className="btn primary" aria-disabled="true" style={{ opacity: 0.45, cursor: 'not-allowed' }}><Play size={14} />Run selected</span>
            : needsBuild ? <Btn className="primary" icon={RefreshCw} busy={busy === 'build'} onClick={rebuild}>Rebuild test pack</Btn>
              : approved ? <Btn className="primary" icon={Play} busy={busy === 'run'} disabled={!selected.length} onClick={() => runSelected(false)}>Run selected ({selected.length}) and create report</Btn>
                : canApprove ? <>
                  <Btn className="danger" icon={XCircle} busy={busy === 'rej'} onClick={rejectPack}>Reject</Btn>
                  <Btn className="primary" icon={CheckCircle2} busy={busy === 'run'} disabled={!selected.length || !pack?.can_approve} onClick={() => runSelected(true)}>Approve test pack and run selected ({selected.length})</Btn></>
                  : <span className="btn primary" aria-disabled="true" style={{ opacity: 0.45, cursor: 'not-allowed' }}>Waiting for an approver</span>}
        </div>
        {!sqlOk && <div className="small mt-s"><Alert kind="warn">The SQL is not approved yet. <Link to={`/runs/${runId}/studio`}>Go back to step 2</Link> and approve it, then come back here.</Alert></div>}
        {sqlOk && !approved && !needsBuild && <div className="tiny muted mt-s">
          {canApprove ? 'Approving covers the whole test pack below; you can still change which tests run afterwards without approving again.'
            : `Your role (${meta?.role}) can choose tests but cannot approve. An approver or admin must approve the test pack.`}
          {pack?.blockers?.filter(b => !b.startsWith('Approve the transformation')).map(b => <div key={b} style={{ color: 'var(--blocked)' }}>{b}</div>)}</div>}
        {pack && <details className="mt-s"><summary className="tiny">What approving the test pack covers</summary>
          <table className="t mt-s"><tbody>{Object.entries(pack.artifacts).filter(([, v]) => v.status !== 'NOT_READY').map(([k, v]) => (
            <tr key={k}><td className="small">{LABELS[k]}</td><td className="small muted">{v.artifact_version}</td><td><Badge s={v.status} /></td></tr>))}</tbody></table>
          {pack.ignored_requirements.length + pack.ignored_mappings.length > 0 && <div className="tiny muted mt-s">
            Not used: {pack.ignored_requirements.length} AI note(s) and {pack.ignored_mappings.length} unconfirmed column suggestion(s).</div>}
        </details>}
      </div>}

      {!data ? <Loading /> : !data.test_cases.length ? <Card><Empty>No test cases yet. Complete the <Link to={`/runs/${runId}/requirements`}>Requirements</Link>; the test pack is built automatically.</Empty></Card> :
        groups.filter(g => g.tests.length).map(g => (
          <Card key={g.key} title={<div className="row top gap-s">{sel && <TriBox ids={g.tests.map(t => t.id)} sel={sel} onChange={choose} />}
            <span className="badge outline">{g.title}</span><span className="small" style={{ fontWeight: 500 }}>{g.text}</span></div>} flush>
            <div className="tw"><table className="t">
              <thead><tr><th style={{ width: 28 }} /><th>ID</th><th>Type</th><th>Test case</th><th>Records checked</th><th>Expected result</th><th>Last result</th><th /></tr></thead>
              <tbody>{g.tests.map(t => {
                const r = results[t.id]
                const on = sel?.has(t.id)
                return (
                  <tr key={t.id} style={on ? {} : { opacity: 0.55 }}>
                    <td><input type="checkbox" checked={!!on} onChange={e => choose([t.id], e.target.checked)} aria-label={`Select ${t.id}`} /></td>
                    <td className="mono small nowrap">{t.id}</td>
                    <td><TypeBadge t={t.test_type} /></td>
                    <td style={{ maxWidth: 380 }}>{t.title}{t.rationale && <div className="tiny muted">{t.rationale}</div>}</td>
                    <td className="small" style={{ maxWidth: 220 }}>{t.validation?.scope
                      ? <span title={t.validation.scope.sql}><Filter size={12} /> Only records with {t.validation.scope.desc}</span>
                      : <span className="muted">Whole uploaded target</span>}</td>
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
