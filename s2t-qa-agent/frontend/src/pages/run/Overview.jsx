import React from 'react'
import { Link } from 'react-router-dom'
import { CheckCircle2, Circle, AlertTriangle } from 'lucide-react'
import { useRun } from '../RunWorkspace.jsx'
import { Badge, Card, Stat } from '../../components/ui.jsx'

export default function Overview() {
  const { run, runId } = useRun()
  const c = run.counts
  const a = run.approvals
  const ok = (k) => a[k]?.status === 'APPROVED'
  const checklist = [
    ['Jira requirements retrieved and analysed', c.requirements > 0, 'requirements'],
    ['Open requirement gaps reviewed', c.open_gaps === 0, 'requirements', c.open_gaps ? `${c.open_gaps} open — dependent tests will be BLOCKED, others still run` : ''],
    ['Sources and target uploaded and profiled', c.sources > 0 && c.targets === 1, 'profiling'],
    ['Mappings reviewed', c.mappings > 0, 'mapping'],
    ['Transformation authored (SQL / Python / Visual)', !!run.transformation_version, 'studio'],
    ['Transformation approved', ok('TRANSFORMATION'), 'studio'],
    ['Test cases generated', c.test_cases > 0, 'tests'],
    ['Synthetic scenarios generated (approved transformation required)', c.scenarios > 0, 'synthetic'],
    ['All required artifacts approved', ['REQUIREMENTS', 'DATASETS', 'MAPPINGS', 'TRANSFORMATION', 'TEST_CASES', 'EXECUTION_SETTINGS'].every(ok), 'approvals'],
    ['Executions run', c.executions > 0, 'execution'],
    ['Report generated', c.reports > 0, 'report'],
  ]
  return (
    <div className="grid g2" style={{ gridTemplateColumns: 'minmax(0,1.3fr) minmax(0,1fr)' }}>
      <Card title="Next steps">
        <div className="col gap-s">
          {checklist.map(([l, done, tab, note]) => (
            <div key={l} className="row top gap-s">
              {done ? <CheckCircle2 size={17} color="#1f9d55" /> : note ? <AlertTriangle size={17} color="#d99a1a" /> : <Circle size={17} color="#c9ced9" />}
              <div className="grow"><Link to={`/runs/${runId}/${tab}`}>{l}</Link>{note && <div className="tiny muted">{note}</div>}</div>
            </div>
          ))}
        </div>
      </Card>
      <div className="col">
        <div className="grid g3">
          <Stat label="Requirements" value={c.requirements} />
          <Stat label="Open gaps" value={c.open_gaps} color={c.open_gaps ? '#9a5b05' : undefined} />
          <Stat label="Mappings" value={c.mappings} />
          <Stat label="Test cases" value={c.test_cases} />
          <Stat label="Scenarios" value={c.scenarios} />
          <Stat label="Executions" value={c.executions} />
        </div>
        <Card title="Approval status">
          <div className="col gap-s">
            {Object.entries(a).map(([k, v]) => (
              <div key={k} className="row between small"><span>{k.replaceAll('_', ' ').toLowerCase()}</span>
                <span className="row gap-s">{v.artifact_version && <span className="tiny muted">{v.artifact_version}</span>}<Badge s={v.status} /></span></div>
            ))}
          </div>
          {run.transformation_version && <div className="tiny muted mt">Current transformation: v{run.transformation_version.version} · {run.transformation_version.mode} · {run.transformation_version.origin.replaceAll('_', ' ').toLowerCase()}</div>}
        </Card>
      </div>
    </div>
  )
}
