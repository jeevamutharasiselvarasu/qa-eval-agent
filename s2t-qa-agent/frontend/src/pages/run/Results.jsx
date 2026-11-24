import React, { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { Play, Loader2 } from 'lucide-react'
import { api } from '../../api.js'
import { useRun } from '../RunWorkspace.jsx'
import { Alert, Badge, Btn, Card, Empty, ResultBar, useAction, useLoad } from '../../components/ui.jsx'
import ExecutionMonitor, { ResultModal } from './ExecutionMonitor.jsx'
import QAReport from './QAReport.jsx'
import { TypeBadge, useLatestResults } from './TestCases.jsx'

export default function Results() {
  const { runId, reload: reloadRun } = useRun()
  const [pack, reloadPack] = useLoad(() => api.get(`/runs/${runId}/test-pack`), [runId])
  const [execs, reloadExecs] = useLoad(() => api.get(`/runs/${runId}/executions`), [runId], 2500)
  const [tick, setTick] = useState(0)
  const [results, execIds] = useLatestResults(runId, tick)
  const [tcData] = useLoad(() => api.get(`/runs/${runId}/test-cases`), [runId, tick])
  const [force, setForce] = useState(false)
  const [open, setOpen] = useState(null)
  const [busy, act] = useAction()
  const running = (execs || []).some(e => ['QUEUED', 'RUNNING'].includes(e.status))
  const [wasRunning, setWasRunning] = useState(false)
  useEffect(() => {
    if (wasRunning && !running) { setTimeout(() => setTick(t => t + 1), 1500); reloadRun(); reloadPack() }
    setWasRunning(running)
  }, [running]) // eslint-disable-line react-hooks/exhaustive-deps

  const runAll = () => act('run', async () => {
    const ex = await api.post(`/runs/${runId}/run-validation`, { force })
    reloadExecs()
    if (ex.every(e => !['QUEUED', 'RUNNING'].includes(e.status))) setTick(t => t + 1)
  }, 'Validation started for the selected test cases')
  const latest = {}
  for (const e of execs || []) if (!latest[e.track]) latest[e.track] = e
  const allTcs = [...(tcData?.test_cases || [])].sort((x, y) => x.id.localeCompare(y.id, undefined, { numeric: true }))
  const tcs = allTcs.filter(t => t.selected !== false)
  const acs = tcData?.coverage?.acceptance_criteria || []
  const rowsFor = (id) => tcs.filter(t => id === 'TL' ? t.basis === 'TRANSFORMATION_LOGIC' : id === 'GEN' ? (!t.ac_ids.length && t.basis !== 'TRANSFORMATION_LOGIC') : t.ac_ids.includes(id))
  const tally = (list) => {
    const c = { PASS: 0, FAIL: 0, BLOCKED: 0, ERROR: 0, SKIPPED: 0 }
    list.forEach(t => { const s = t.state === 'BLOCKED' ? 'BLOCKED' : results[t.id]?.status; if (s && c[s] !== undefined) c[s]++ })
    return { ...c, total: list.length }
  }

  return (
    <div className="col">
      <Card title="Run validation" sub="Runs the selected test cases: Track A on the uploaded files and Track B on the synthetic edge-case data. A report covering exactly those tests is created at the end."
        actions={<Btn className="primary" icon={running ? Loader2 : Play} busy={busy === 'run'} disabled={running || !pack?.pack_approved || pack?.transformation !== 'APPROVED' || !pack?.selected} onClick={runAll}>
          {running ? 'Running…' : `Run selected (${pack?.selected ?? 0})`}</Btn>}>
        {pack && <div className="small mb">{pack.selected} of {pack.test_cases} test cases selected. <Link to={`/runs/${runId}/tests`}>Change the selection</Link></div>}
        {pack && (!pack.pack_approved || pack.transformation !== 'APPROVED') &&
          <div className="mb"><Alert kind="warn">Both approvals are needed first: transformation <Badge s={pack.transformation} /> and test pack <Badge s={pack.pack_approved ? 'APPROVED' : 'PENDING'} />. Go to <Link to={`/runs/${runId}/review`}>Review &amp; approve</Link>.</Alert></div>}
        <div className="grid g2">
          {['ACTUAL', 'SYNTHETIC'].map(t => (
            <div key={t} className="card card-b" style={{ padding: 12 }}>
              <div className="row between"><b className="small">{t === 'ACTUAL' ? 'Track A · uploaded data' : 'Track B · synthetic edge cases'}</b>{latest[t] && <Badge s={latest[t].status} />}</div>
              <div className="mt-s">{latest[t] ? (['QUEUED', 'RUNNING'].includes(latest[t].status)
                ? <span className="small row gap-s"><Loader2 size={13} className="spin" />{latest[t].progress.done}/{latest[t].progress.total} tests</span>
                : <ResultBar s={latest[t].summary} />) : <span className="small muted">Not run yet</span>}</div>
            </div>))}
        </div>
        <label className="row gap-s small mt"><input type="checkbox" checked={force} onChange={e => setForce(e.target.checked)} />Run again even if nothing changed (otherwise the previous identical results are reused)</label>
      </Card>

      <Card title="Results of the selected test cases" sub="Grouped by acceptance criterion, transformation logic and general checks. Click a status to see the evidence and, for failures, the AI analysis." flush>
        {!tcs.length ? <Empty>No test cases selected. <Link to={`/runs/${runId}/tests`}>Choose test cases</Link>.</Empty> : <div className="tw"><table className="t">
          <thead><tr><th>Group</th><th>Summary</th><th>Tests</th></tr></thead>
          <tbody>{[...acs.map(a => ({ id: a.ac_id, label: a.ac_id, text: a.text })), { id: 'TL', label: 'Transformation logic', text: 'Checks derived from the SQL itself' },
            { id: 'GEN', label: 'General', text: 'General reconciliation and data quality' }].map(a => {
            const list = rowsFor(a.id)
            if (!list.length) return null
            return (
              <tr key={a.id}>
                <td style={{ maxWidth: 320 }}><span className="badge outline">{a.label}</span><div className="small mt-s">{a.text}</div></td>
                <td style={{ minWidth: 140 }}><ResultBar s={tally(list)} /></td>
                <td>{list.map(t => {
                  const r = results[t.id]
                  const st = t.state === 'BLOCKED' ? 'BLOCKED' : r?.status
                  return (
                    <div key={t.id} className="row gap-s small" style={{ marginBottom: 4 }}>
                      <TypeBadge t={t.test_type} />
                      {r ? <button className="btn sm ghost" style={{ padding: 0 }} onClick={() => setOpen({ r, ex: execIds[t.id] })}><Badge s={st} /></button>
                        : st ? <span title={t.blocked_reason}><Badge s={st} /></span> : <span className="tiny muted">not run</span>}
                      <span>{t.title}</span>
                      <span className="tiny muted">{t.track === 'ACTUAL' ? 'uploaded data' : 'synthetic'}</span>
                    </div>)
                })}</td>
              </tr>)
          })}</tbody>
        </table></div>}
      </Card>

      <QAReport refreshKey={tick} />
      <details className="card card-b"><summary className="small"><b>Execution details</b> (executions, logs, retry and cancel)</summary>
        <div className="mt"><ExecutionMonitor compact onFinished={() => setTick(t => t + 1)} /></div></details>
      {open && <ResultModal r={open.r} exId={open.ex} onClose={() => setOpen(null)} />}
    </div>
  )
}
