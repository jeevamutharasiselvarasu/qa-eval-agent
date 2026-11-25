import { createContext, useContext } from 'react'
import { Link, NavLink, Navigate, Route, Routes, useParams } from 'react-router-dom'
import { CheckCircle2, ChevronRight } from 'lucide-react'
import { api } from '../api.js'
import { Alert, Badge, Loading, PageHeader, StatePill, fmtDate, useLoad } from '../components/ui.jsx'
import { stepState } from './run/flow.js'
import Overview from './run/Overview.jsx'
import Requirements from './run/Requirements.jsx'
import Profiling from './run/Profiling.jsx'
import TransformationStudio from './run/TransformationStudio.jsx'
import TestCases from './run/TestCases.jsx'
import Results from './run/Results.jsx'
import AuditTrail from './run/AuditTrail.jsx'

export const RunCtx = createContext(null)
export const useRun = () => useContext(RunCtx)

// the straight flow (numbered) and two reference tabs
const TABS = [
  ['overview', 'Overview'], ['requirements', '1 · Requirements'], ['studio', '2 · SQL'], ['tests', '3 · Test cases'],
  ['results', '4 · Results & report'], ['|'], ['profiling', 'Data files'], ['audit', 'Audit'],
]

export default function RunWorkspace({ meta }) {
  const { runId } = useParams()
  const [run, reload, err] = useLoad(() => api.get(`/runs/${runId}`), [runId])
  if (err) return <Alert kind="err">{err}</Alert>
  if (!run) return <Loading rows={4} />
  const done = stepState(run)
  const sqlPending = ['PENDING', 'STALE'].includes(run.approvals.TRANSFORMATION?.status) && !!run.transformation_version
  return (
    <RunCtx.Provider value={{ run, reload, runId, meta }}>
      <PageHeader title={run.name} docTitle={`${run.jira_key} · ${run.name}`}
        crumbs={<><Link to="/runs">Run history</Link><ChevronRight size={13} /><span>{run.jira_key}</span></>}
        meta={<>
          <StatePill state={run.status} />
          {run.exception_state && <StatePill state={run.exception_state} />}
          <span>Jira <b className="mono" style={{ color: 'var(--text-2)' }}>{run.jira_key}</b></span><span className="sep" />
          <span>Created by {run.created_by} · {fmtDate(run.created_at)}</span><span className="sep" />
          <span className="mono">{run.id}</span>
          {run.project !== 'default' && <><span className="sep" /><span>Project {run.project}</span></>}
        </>} />
      <div className="tabs">
        {TABS.map(([k, l]) => k === '|' ? <span key="sep" className="grow" /> : (
          <NavLink key={k} to={`/runs/${runId}/${k}`} className={['profiling', 'audit'].includes(k) ? 'secondary' : ''}>
            {done[k]?.done && <CheckCircle2 size={14} className="tick" aria-label="done" />}{l}
            {k === 'requirements' && run.counts.open_items > 0 && <Badge s="OPEN" title="Items that need your input">{run.counts.open_items}</Badge>}
            {k === 'studio' && sqlPending && <Badge s="PENDING" title="Waiting for approval">To approve</Badge>}
          </NavLink>
        ))}
      </div>
      <Routes>
        <Route index element={<Navigate to="overview" replace />} />
        <Route path="overview" element={<Overview />} />
        <Route path="requirements" element={<Requirements />} />
        <Route path="profiling" element={<Profiling />} />
        <Route path="mapping" element={<Navigate to="../requirements" replace />} />
        <Route path="studio" element={<TransformationStudio />} />
        <Route path="tests" element={<TestCases />} />
        <Route path="review" element={<Navigate to="../tests" replace />} />
        <Route path="results" element={<Results />} />
        <Route path="synthetic" element={<Navigate to="../tests" replace />} />
        <Route path="approvals" element={<Navigate to="../tests" replace />} />
        <Route path="execution" element={<Navigate to="../results" replace />} />
        <Route path="report" element={<Navigate to="../results" replace />} />
        <Route path="audit" element={<AuditTrail />} />
      </Routes>
    </RunCtx.Provider>
  )
}
