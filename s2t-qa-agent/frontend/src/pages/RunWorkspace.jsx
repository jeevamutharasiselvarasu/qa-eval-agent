import React, { createContext, useContext } from 'react'
import { NavLink, Navigate, Route, Routes, useParams } from 'react-router-dom'
import { api } from '../api.js'
import { Badge, Empty, useLoad } from '../components/ui.jsx'
import Overview from './run/Overview.jsx'
import Requirements from './run/Requirements.jsx'
import Profiling from './run/Profiling.jsx'
import TransformationStudio from './run/TransformationStudio.jsx'
import TestCases from './run/TestCases.jsx'
import Review from './run/Review.jsx'
import Results from './run/Results.jsx'
import AuditTrail from './run/AuditTrail.jsx'

const RunCtx = createContext(null)
export const useRun = () => useContext(RunCtx)

const STEPS = [
  ['REQUIREMENTS_ANALYZED', 'Requirements'], ['DATASETS_PROFILED', 'Data'], ['TRANSFORMATION_DESIGNED', 'SQL'],
  ['TEST_CASES_GENERATED', 'Test pack'], ['APPROVED', 'Approved'], ['VALIDATION_COMPLETED', 'Validated'], ['REPORT_GENERATED', 'Reported'],
]

const TABS = [
  ['overview', 'Overview'], ['requirements', 'Requirements'], ['profiling', 'Data'],
  ['studio', 'Transformation (SQL)'], ['tests', 'Test cases'], ['review', 'Review & approve'], ['results', 'Results'], ['audit', 'Audit'],
]

export default function RunWorkspace({ meta }) {
  const { runId } = useParams()
  const [run, reload, err] = useLoad(() => api.get(`/runs/${runId}`), [runId])
  if (err) return <Empty>{err}</Empty>
  if (!run) return <div className="muted">Loading…</div>
  const states = meta?.workflow_states || []
  const cur = states.indexOf(run.status)
  const pending = [run.approvals.TRANSFORMATION, run.approvals.TEST_CASES].filter(v => v && ['PENDING', 'STALE'].includes(v.status)).length
  return (
    <RunCtx.Provider value={{ run, reload, runId, meta }}>
      <div className="page-head">
        <div>
          <div className="row gap-s"><h1>{run.name}</h1></div>
          <p className="row wrap gap-s">
            <span className="mono">{run.id}</span>·<span className="mono">{run.jira_key}</span>·<span>project {run.project}</span>·
            <Badge>{run.status}</Badge>{run.exception_state && <Badge s="BLOCKED">{run.exception_state}</Badge>}
          </p>
        </div>
      </div>
      <div className="steps mb">
        {STEPS.map(([s, l]) => {
          const i = states.indexOf(s)
          const cls = cur > i ? 'done' : cur === i ? 'cur done' : ''
          return <div key={s} className={`step ${cls}`} title={s}><div className="ln" /><span>{l}</span></div>
        })}
      </div>
      <div className="tabs">
        {TABS.map(([k, l]) => (
          <NavLink key={k} to={`/runs/${runId}/${k}`}>{l}
            {k === 'requirements' && run.counts.open_items > 0 && <Badge s="OPEN" title="Items that need your input">{run.counts.open_items}</Badge>}
            {k === 'review' && pending > 0 && <Badge s="PENDING">{pending}</Badge>}
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
        <Route path="review" element={<Review />} />
        <Route path="results" element={<Results />} />
        <Route path="synthetic" element={<Navigate to="../tests" replace />} />
        <Route path="approvals" element={<Navigate to="../review" replace />} />
        <Route path="execution" element={<Navigate to="../results" replace />} />
        <Route path="report" element={<Navigate to="../results" replace />} />
        <Route path="audit" element={<AuditTrail />} />
      </Routes>
    </RunCtx.Provider>
  )
}
