import React, { useEffect, useState } from 'react'
import { Play, RotateCcw, Square, Download, Loader2 } from 'lucide-react'
import { api } from '../../api.js'
import { useRun } from '../RunWorkspace.jsx'
import { Alert, Badge, Btn, Card, DataTable, Empty, Ids, Modal, ResultBar, fmtDate, useAction, useLoad } from '../../components/ui.jsx'

function Analysis({ a }) {
  if (!a) return <div className="small muted">No AI analysis for this result.</div>
  const sec = (t, xs, kind) => <div className="mt-s"><h4>{t}</h4>{xs?.length ? <ul className="small" style={{ margin: '4px 0', paddingLeft: 18 }}>{xs.map((x, i) => <li key={i}>{x}</li>)}</ul> : <div className="tiny muted">None.</div>}</div>
  return (
    <div className="card card-b" style={{ background: 'var(--surface-2)' }}>
      <div className="row wrap gap-s"><b>AI failure analysis</b><Badge s="AI_INFERRED">{a.provider}</Badge><Badge className="outline">{a.category}</Badge><Badge className="outline">suspected: {a.suspected_layer?.replaceAll('_', ' ').toLowerCase()}</Badge></div>
      {sec('Confirmed facts (from deterministic evidence)', a.confirmed_facts)}
      {sec('Likely explanations', a.likely_explanations)}
      {sec('Unverified hypotheses', a.unverified_hypotheses)}
      {sec('Recommended follow-up checks', a.recommended_checks)}
      <div className="tiny muted mt-s">AI analysis is advisory and cannot change the deterministic status ({a.deterministic_status}).</div>
    </div>
  )
}

function ResultModal({ r, exId, onClose }) {
  const [, act] = useAction()
  return (
    <Modal title={`${r.test_case_id} · ${r.title}`} onClose={onClose}>
      <div className="row wrap gap-s mb"><Badge s={r.status} /><Badge className="outline">{r.category}</Badge>{r.scenario_id && <Badge className="outline">{r.scenario_id}</Badge>}<Ids ids={r.ac_ids} /><Ids ids={r.requirement_ids} /></div>
      <div className="kv"><dt>Message</dt><dd>{r.message}</dd><dt>Expected</dt><dd>{r.expected || '—'}</dd><dt>Actual</dt><dd>{r.actual || '—'}</dd>
        <dt>Metrics</dt><dd className="mono tiny">{JSON.stringify(r.metrics)}</dd><dt>Duration</dt><dd>{r.duration_ms} ms</dd></div>
      {r.evidence && <div className="mt"><div className="row between mb"><h4>Evidence {r.evidence.id}</h4>
        {r.evidence.has_file && <button className="btn sm" onClick={() => act('d', () => api.download(`/executions/${exId}/evidence/${r.evidence.id}`, `${r.test_case_id}_evidence.csv`))}><Download size={13} />Full evidence CSV</button>}</div>
        {r.evidence.sample?.length ? <DataTable rows={r.evidence.sample} /> : <div className="small muted">No record-level evidence.</div>}</div>}
      {r.status === 'FAIL' && <div className="mt"><Analysis a={r.analysis} /></div>}
    </Modal>
  )
}

export default function ExecutionMonitor() {
  const { runId, run, reload: reloadRun } = useRun()
  const [execs, reload] = useLoad(() => api.get(`/runs/${runId}/executions`), [runId], 2500)
  const [sel, setSel] = useState(null)
  const [detail, setDetail] = useState(null)
  const [filter, setFilter] = useState('ALL')
  const [open, setOpen] = useState(null)
  const [force, setForce] = useState(false)
  const [busy, act] = useAction()
  const active = execs?.find(e => e.id === sel)
  const anyRunning = !!execs?.some(e => ['QUEUED', 'RUNNING'].includes(e.status))
  const wasRunning = React.useRef(false)
  useEffect(() => { if (wasRunning.current && !anyRunning) reloadRun(); wasRunning.current = anyRunning }, [anyRunning]) // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => { if (execs?.length && !sel) setSel(execs[0].id) }, [execs, sel])
  useEffect(() => {
    if (!sel) return
    let alive = true
    const load = () => api.get(`/executions/${sel}`).then(d => alive && setDetail(d)).catch(() => {})
    load()
    const id = setInterval(() => { if (['QUEUED', 'RUNNING'].includes(active?.status) || !detail || detail.id !== sel) load() }, 1500)
    return () => { alive = false; clearInterval(id) }
  }, [sel, active?.status]) // eslint-disable-line react-hooks/exhaustive-deps

  const start = (track) => act(track, async () => {
    const e = await api.post(`/runs/${runId}/execute/${track.toLowerCase()}`, { force })
    setSel(e.id); reload(); reloadRun()
    return e
  }, force ? 'Execution started' : undefined)
  const ap = run.approvals
  const ready = (t) => ['REQUIREMENTS', 'DATASETS', 'MAPPINGS', 'TRANSFORMATION', 'TEST_CASES', 'EXECUTION_SETTINGS', ...(t === 'SYNTHETIC' ? ['SYNTHETIC_DATA'] : [])].filter(k => ap[k]?.status !== 'APPROVED')
  const results = (detail?.results || []).filter(r => filter === 'ALL' || r.status === filter)

  return (
    <div className="col">
      <div className="grid g2">
        {['ACTUAL', 'SYNTHETIC'].map(t => {
          const miss = ready(t)
          const last = execs?.find(e => e.track === t)
          return (
            <Card key={t} title={t === 'ACTUAL' ? 'Track A · actual-data validation' : 'Track B · synthetic testing'}
              sub={t === 'ACTUAL' ? 'Uploaded sources → approved transformation → expected target → compare with uploaded target.' : 'Scenario sources → same approved transformation → compare with approved synthetic expected targets + rule checks.'}
              actions={<Btn className="primary" icon={Play} busy={busy === t} disabled={miss.length > 0} onClick={() => start(t)}>Run</Btn>}>
              {miss.length > 0 ? <Alert kind="warn">Needs current approval: {miss.join(', ').toLowerCase().replaceAll('_', ' ')}</Alert> :
                last ? <div className="row between"><span className="small">Latest <span className="mono">{last.id}</span> <Badge s={last.status} /></span><ResultBar s={last.summary} /></div> : <span className="small muted">Ready to run.</span>}
            </Card>)
        })}
      </div>
      <label className="row gap-s small"><input type="checkbox" checked={force} onChange={e => setForce(e.target.checked)} />Force a new execution even if an identical completed execution exists (otherwise it is reused to prevent duplicates)</label>
      <div className="grid" style={{ gridTemplateColumns: 'minmax(260px, 340px) minmax(0, 1fr)' }}>
        <Card title="Executions" flush>
          {!execs ? <Empty>Loading…</Empty> : !execs.length ? <Empty>No executions yet.</Empty> : (
            <table className="t"><tbody>{execs.map(e => (
              <tr key={e.id} className={`click ${sel === e.id ? 'sel' : ''}`} onClick={() => setSel(e.id)}>
                <td><div className="row between"><span className="mono small">{e.id}</span><Badge s={e.status} /></div>
                  <div className="row between mt-s"><Badge className="outline">{e.track.toLowerCase()}</Badge><span className="tiny muted">{fmtDate(e.created_at)} · attempt {e.attempt}</span></div>
                  {['QUEUED', 'RUNNING'].includes(e.status) ? <div className="tiny mt-s row gap-s"><Loader2 size={12} className="spin" />{e.progress.done}/{e.progress.total} tests{e.summary?.phase ? ' · AI analysis' : ''}</div> : <div className="mt-s"><ResultBar s={e.summary} /></div>}
                </td></tr>))}</tbody></table>
          )}
        </Card>
        <Card title={detail ? `Execution ${detail.id}` : 'Execution detail'} sub={detail && `${detail.track} · started by ${detail.started_by} · ${fmtDate(detail.started_at)} → ${fmtDate(detail.ended_at)}`}
          actions={detail && <>
            {['PARTIAL_SUCCESS', 'FAILED', 'INTERRUPTED', 'CANCELLED'].includes(detail.status) && <Btn icon={RotateCcw} busy={busy === 'retry'} onClick={() => act('retry', async () => { await api.post(`/executions/${detail.id}/retry`); reload() }, 'Retrying — completed results are kept')}>Retry</Btn>}
            {['QUEUED', 'RUNNING'].includes(detail.status) && <Btn icon={Square} className="danger" onClick={() => act('c', async () => { await api.post(`/executions/${detail.id}/cancel`); reload() })}>Cancel</Btn>}
          </>}>
          {!detail ? <Empty>Select an execution.</Empty> : <>
            {detail.error && <div className="mb"><Alert kind="err">{detail.error}</Alert></div>}
            <div className="row wrap between mb">
              <div className="seg">{['ALL', 'FAIL', 'PASS', 'BLOCKED', 'ERROR', 'SKIPPED', 'NOT_EXECUTED'].map(s => <button key={s} className={filter === s ? 'on' : ''} onClick={() => setFilter(s)}>{s.replace('_', ' ').toLowerCase()} {s !== 'ALL' && `(${(detail.results || []).filter(r => r.status === s).length})`}</button>)}</div>
              <span className="small muted">{detail.progress.done}/{detail.progress.total} results</span>
            </div>
            {!results.length ? <Empty>No results{filter !== 'ALL' ? ` with status ${filter}` : ''}.</Empty> : (
              <div className="tw"><table className="t">
                <thead><tr><th>Test</th><th>Status</th><th>Expected</th><th>Actual</th><th>Message</th></tr></thead>
                <tbody>{results.map(r => (
                  <tr key={r.id} className="click" onClick={() => setOpen(r)}>
                    <td><span className="mono small">{r.test_case_id}</span><div className="small">{r.title}</div></td>
                    <td><Badge s={r.status} /></td><td className="small cell-wrap">{r.expected}</td><td className="small cell-wrap">{r.actual}</td>
                    <td className="small" style={{ minWidth: 220, maxWidth: 320 }}>{r.message}{r.analysis && <div className="tiny" style={{ color: '#7c3aed' }}>AI: {r.analysis.category}</div>}</td>
                  </tr>))}</tbody>
              </table></div>
            )}
            {detail.transformation_executions?.length > 0 && <details className="mt"><summary className="small">Transformation executions & logs ({detail.transformation_executions.length})</summary>
              {detail.transformation_executions.map(t => <div key={t.id} className="pre mt-s">{`${t.id} ${t.purpose} ${t.scenario_id || ''} ${t.status} rows=${t.row_count} ${t.duration_ms}ms\n${t.logs.join('\n')}${t.errors.length ? '\nERRORS: ' + JSON.stringify(t.errors) : ''}`}</div>)}</details>}
            {detail.errors?.length > 0 && <details className="mt"><summary className="small">Technical errors ({detail.errors.length})</summary>{detail.errors.map(e => <div key={e.id} className="pre mt-s">{e.stage}: {e.message}</div>)}</details>}
          </>}
        </Card>
      </div>
      {open && <ResultModal r={open} exId={detail.id} onClose={() => setOpen(null)} />}
    </div>
  )
}
