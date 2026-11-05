import React, { useState } from 'react'
import { ListChecks, Check, X } from 'lucide-react'
import { api } from '../../api.js'
import { useRun } from '../RunWorkspace.jsx'
import { Alert, Badge, Btn, Card, Empty, Ids, Modal, useAction, useLoad } from '../../components/ui.jsx'

function spec(v) {
  const t = v.type
  const bits = [t]
  if (v.column) bits.push(v.column)
  if (v.columns) bits.push(v.columns.join(','))
  if (v.keys?.length) bits.push(`key ${v.keys.join('+')}`)
  if (v.tolerance) bits.push(`±${v.tolerance}`)
  if (v.guards?.length) bits.push(`${v.guards.length} runtime guard(s)`)
  return bits.join(' · ')
}

export default function TestCases() {
  const { runId, reload: reloadRun } = useRun()
  const [data, reload] = useLoad(() => api.get(`/runs/${runId}/test-cases`), [runId])
  const [track, setTrack] = useState('ACTUAL')
  const [edit, setEdit] = useState(null)
  const [busy, act] = useAction()
  const refresh = () => { reload(); reloadRun() }
  const patch = (t, body, msg) => act(t.id, async () => { await api.patch(`/runs/${runId}/test-cases/${t.id}`, body); refresh() }, msg)
  const tcs = (data?.test_cases || []).filter(t => t.track === track)
  const cov = data?.coverage

  return (
    <div className="col">
      <div className="grid g2" style={{ gridTemplateColumns: 'minmax(0,1fr) minmax(0,1fr)' }}>
        <Card title="Acceptance-criteria coverage">
          {!cov ? <Empty>Loading…</Empty> : !cov.acceptance_criteria.length ? <Empty>No acceptance criteria found in Jira.</Empty> :
            <div className="col gap-s">{cov.acceptance_criteria.map(a => (
              <div key={a.ac_id} className="row top gap-s small"><Badge s={a.covered ? 'PASS' : 'FAIL'}>{a.ac_id}</Badge><span className="grow">{a.text}</span></div>))}</div>}
          {cov?.tests_without_requirement?.length > 0 && <div className="mt"><Alert kind="warn">Tests without a requirement or profiling rationale: {cov.tests_without_requirement.join(', ')}</Alert></div>}
        </Card>
        <Card title="Generation" actions={<Btn className="primary" icon={ListChecks} busy={busy === 'gen'} onClick={() => act('gen', async () => { await api.post(`/runs/${runId}/test-cases/generate`); refresh() }, 'Actual-track test cases regenerated')}>Generate actual-track tests</Btn>}>
          <p className="small">Test cases are generated deterministically from accepted requirements, mappings, gaps and profiles. Unknown behaviour produces <Badge s="BLOCKED" /> tests or runtime guards — never invented expectations.</p>
          <p className="small muted">Synthetic-track tests are created automatically for each scenario when synthetic data is generated.</p>
        </Card>
      </div>
      <Card title="Test cases" actions={<div className="seg">{['ACTUAL', 'SYNTHETIC'].map(t => <button key={t} className={track === t ? 'on' : ''} onClick={() => setTrack(t)}>{t === 'ACTUAL' ? 'Actual data' : 'Synthetic'} ({(data?.test_cases || []).filter(x => x.track === t).length})</button>)}</div>} flush>
        {!data ? <Empty>Loading…</Empty> : !tcs.length ? <Empty>No {track.toLowerCase()} test cases yet.</Empty> : (
          <div className="tw"><table className="t">
            <thead><tr><th>ID</th><th>Category</th><th>Test</th><th>ACs</th><th>Requirements</th><th>Check</th><th>Expected</th><th>Readiness</th><th>Review</th></tr></thead>
            <tbody>{tcs.map(t => (
              <tr key={t.id}>
                <td className="mono small nowrap">{t.id}<div className="tiny muted">{t.priority} · {t.severity.toLowerCase()}</div></td>
                <td className="small">{t.category.replaceAll('_', ' ').toLowerCase()}</td>
                <td style={{ maxWidth: 340 }}>{t.title}{t.rationale && <div className="tiny muted">{t.rationale}</div>}</td>
                <td><Ids ids={t.ac_ids} /></td><td><Ids ids={t.requirement_ids} /></td>
                <td className="tiny mono" style={{ maxWidth: 200 }}>{spec(t.validation)}</td>
                <td className="small" style={{ maxWidth: 200 }}>{t.expected_result}</td>
                <td>{t.state === 'READY' ? <Badge s="ACCEPTED">ready</Badge> : <Badge s={t.state === 'BLOCKED' ? 'BLOCKED' : ''}>{t.state.toLowerCase()}</Badge>}
                  {t.blocked_reason && <div className="tiny" style={{ color: 'var(--blocked)', maxWidth: 220 }}>{t.blocked_reason}</div>}</td>
                <td className="nowrap"><Badge s={t.status} />
                  <button className="btn sm ghost" title="Accept" onClick={() => patch(t, { status: 'ACCEPTED' })}><Check size={13} /></button>
                  <button className="btn sm ghost" title="Reject (recorded as SKIPPED at execution)" onClick={() => patch(t, { status: 'REJECTED' })}><X size={13} /></button>
                  {t.validation.type === 'COLUMN_MATCH' && <button className="btn sm" onClick={() => setEdit(t)}>Tune</button>}
                </td>
              </tr>))}</tbody>
          </table></div>
        )}
      </Card>
      {edit && <TuneModal t={edit} onClose={() => setEdit(null)} onSave={(b) => { patch(edit, b, 'Test updated — test-case approval must be renewed'); setEdit(null) }} />}
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
