import React, { useState } from 'react'
import { Link } from 'react-router-dom'
import { FlaskConical, Download, Eye, RefreshCw } from 'lucide-react'
import { api } from '../../api.js'
import { useRun } from '../RunWorkspace.jsx'
import { Alert, Badge, Btn, Card, DataTable, Empty, Ids, Modal, useAction, useLoad } from '../../components/ui.jsx'

export default function SyntheticStudio() {
  const { runId, run, reload: reloadRun, meta } = useRun()
  const [scen, reload] = useLoad(() => api.get(`/runs/${runId}/synthetic-data`), [runId])
  const [records, setRecords] = useState(run.settings?.synthetic_records_per_scenario || 12)
  const [seed, setSeed] = useState(run.settings?.synthetic_seed || 42)
  const [pick, setPick] = useState([])
  const [pv, setPv] = useState(null)
  const [busy, act] = useAction()
  const trApproved = run.approvals.TRANSFORMATION?.status === 'APPROVED'
  const gen = (scenarios) => act('gen', async () => {
    await api.post(`/runs/${runId}/synthetic-data/generate`, { records: Number(records), seed: Number(seed), scenarios: scenarios?.length ? scenarios : null })
    reload(); reloadRun()
  }, 'Synthetic scenarios generated — synthetic test cases refreshed')
  const show = (d, title) => act('pv', async () => setPv({ title, d, data: await api.get(`/runs/${runId}/datasets/${d.id}/preview?limit=100`) }))
  const dl = (d) => act('dl', () => api.download(`/runs/${runId}/datasets/${d.id}/download`, `${d.scenario_id}_${d.role.toLowerCase()}_${d.alias}.csv`))
  const types = meta?.scenarios || []

  return (
    <div className="col">
      <Card title="Synthetic Data Studio" sub="Seeded, rule-driven synthetic sources. Expected targets come only from executing the approved transformation — never from AI guesses.">
        {!trApproved && <div className="mb"><Alert kind="warn">Approve the current transformation version in the <Link to={`/runs/${runId}/studio`}>Transformation Studio</Link> first. Synthetic expected targets are produced by running it.</Alert></div>}
        <div className="row wrap gap-l">
          <label className="field">Records per scenario<input type="number" min="3" max="500" value={records} onChange={e => setRecords(e.target.value)} style={{ width: 120 }} /></label>
          <label className="field">Random seed<input type="number" value={seed} onChange={e => setSeed(e.target.value)} style={{ width: 150 }} /></label>
          <label className="field">Scenarios (none selected = all applicable)
            <select multiple value={pick} onChange={e => setPick([...e.target.selectedOptions].map(o => o.value))} style={{ minWidth: 240, height: 92 }}>
              {types.map(t => <option key={t} value={t}>{t.replaceAll('_', ' ').toLowerCase()}</option>)}</select></label>
          <div className="col gap-s"><Btn className="primary" icon={FlaskConical} busy={busy === 'gen'} disabled={!trApproved} onClick={() => gen(pick)}>{pick.length ? 'Regenerate selected' : 'Generate all'}</Btn>
            <div className="tiny muted">Same seed ⇒ identical data.</div></div>
        </div>
      </Card>
      {!scen ? <Empty>Loading…</Empty> : !scen.length ? <Card><Empty>No scenarios generated yet.</Empty></Card> : (
        <div className="grid g2">
          {scen.map(s => (
            <Card key={s.id} title={<div className="row gap-s"><span className="mono small muted">{s.id}</span><h3>{s.name}</h3></div>}
              actions={<><Badge s={s.status} /><button className="btn sm ghost" title="Regenerate" disabled={!trApproved} onClick={() => gen([s.scenario_type])}><RefreshCw size={13} /></button></>}>
              {s.blocked_reason && <div className="mb"><Alert kind={s.status === 'ERROR' ? 'err' : 'warn'}>{s.blocked_reason}</Alert></div>}
              <ul className="small" style={{ margin: '0 0 8px', paddingLeft: 18 }}>{s.explanation.map((e, i) => <li key={i}>{e}</li>)}</ul>
              {s.assumptions.map((a, i) => <div key={i} className="tiny" style={{ color: 'var(--blocked)' }}>Assumption: {a}</div>)}
              <div className="kv mt">
                <dt>Sources</dt><dd className="row wrap gap-s">{s.sources.map(d => <span key={d.id} className="row gap-s"><span className="chip">{d.alias} · {d.row_count} rows</span>
                  <button className="btn sm ghost" onClick={() => show(d, `${s.name} · source ${d.alias}`)}><Eye size={13} /></button>
                  <button className="btn sm ghost" onClick={() => dl(d)}><Download size={13} /></button></span>)}</dd>
                <dt>Expected target</dt><dd>{s.expected ? <span className="row gap-s"><span className="chip">{s.expected.row_count} rows · {s.expected.fingerprint.slice(7, 15)}</span>
                  <button className="btn sm ghost" onClick={() => show(s.expected, `${s.name} · expected target`)}><Eye size={13} /></button>
                  <button className="btn sm ghost" onClick={() => dl(s.expected)}><Download size={13} /></button></span> : <span className="muted small">not produced</span>}</dd>
                <dt>Requirements</dt><dd><Ids ids={s.requirement_ids} /></dd>
                <dt>Gaps</dt><dd><Ids ids={s.gap_ids} /></dd>
                <dt>Test cases</dt><dd><Ids ids={s.test_case_ids} /></dd>
                <dt>Method</dt><dd className="tiny mono">{s.generation_method}</dd>
              </div>
            </Card>))}
        </div>
      )}
      {pv && <Modal title={pv.title} onClose={() => setPv(null)}>
        <div className="tiny muted mb">Dataset {pv.d.id} · track {pv.d.track} · fingerprint {pv.d.fingerprint}</div>
        <DataTable columns={pv.data.columns} rows={pv.data.rows} /></Modal>}
    </div>
  )
}
