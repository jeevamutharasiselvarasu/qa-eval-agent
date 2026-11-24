import React from 'react'
import { Link } from 'react-router-dom'
import { CheckCircle2, Circle, AlertTriangle, ArrowRight } from 'lucide-react'
import { api } from '../../api.js'
import { useRun } from '../RunWorkspace.jsx'
import { Badge, Card, Stat, useLoad } from '../../components/ui.jsx'
import { TypeBadge } from './TestCases.jsx'

export default function Overview() {
  const { run, runId } = useRun()
  const [pack] = useLoad(() => api.get(`/runs/${runId}/test-pack`), [runId, run.updated_at])
  const c = run.counts
  const trOk = run.approvals.TRANSFORMATION?.status === 'APPROVED'
  const steps = [
    { label: 'Complete the requirements: how each target column is built', tab: 'requirements', done: run.requirements_ready,
      warn: c.open_items > 0 ? `${c.open_items} item(s) need your input. The SQL is generated automatically once they are done.` : '' },
    { label: 'Check the data files', tab: 'profiling', done: c.sources > 0 && c.targets === 1 },
    { label: 'Review the generated SQL and approve it (approval 1)', tab: 'review', done: trOk },
    { label: 'Review the test cases and approve the test pack (approval 2)', tab: 'review', done: !!pack?.pack_approved,
      warn: pack?.stale ? 'The test pack is out of date: rebuild it' : '' },
    { label: 'Run the validation', tab: 'results', done: c.executions > 0 },
    { label: 'Download the report', tab: 'results', done: c.reports > 0 },
  ]
  const next = steps.find(s => !s.done && !s.warn) || steps.find(s => !s.done)
  return (
    <div className="grid g2" style={{ gridTemplateColumns: 'minmax(0,1.3fr) minmax(0,1fr)' }}>
      <Card title="Steps" actions={next && <Link className="btn primary" to={`/runs/${runId}/${next.tab}`}>Next: {next.label.split(' (')[0]} <ArrowRight size={14} /></Link>}>
        <div className="col gap-s">
          {steps.map((s, i) => (
            <div key={s.label} className="row top gap-s">
              {s.done ? <CheckCircle2 size={18} color="#1f9d55" /> : s.warn ? <AlertTriangle size={18} color="#d99a1a" /> : <Circle size={18} color="#c9ced9" />}
              <div className="grow"><span className="muted small">{i + 1}.</span> <Link to={`/runs/${runId}/${s.tab}`}>{s.label}</Link>{s.warn && <div className="tiny muted">{s.warn}</div>}</div>
            </div>
          ))}
        </div>
        <div className="tiny muted mt">The requirements are read from Jira automatically. When they are complete, the SQL, the synthetic data and the test cases are generated for you.</div>
      </Card>
      <div className="col">
        <div className="grid g3">
          <Stat label="Need your input" value={c.open_items} color={c.open_items ? '#9a5b05' : undefined} />
          <Stat label="Open questions" value={c.open_gaps} color={c.open_gaps ? '#9a5b05' : undefined} />
          <Stat label="Target columns" value={c.target_columns} />
        </div>
        {pack && <Card title="Test pack">
          <div className="row wrap gap-l">{['POSITIVE', 'NEGATIVE', 'EDGE', 'GENERAL'].map(k => <span key={k} className="row gap-s"><TypeBadge t={k} /><b>{pack.by_type[k]}</b></span>)}</div>
          <div className="small mt">{pack.test_cases} test cases · {pack.scenarios.generated} synthetic scenarios</div>
          <div className="row gap-s mt small">Transformation <Badge s={pack.transformation} /> · Test pack <Badge s={pack.pack_approved ? 'APPROVED' : pack.stale ? 'STALE' : 'PENDING'} /></div>
        </Card>}
      </div>
    </div>
  )
}
