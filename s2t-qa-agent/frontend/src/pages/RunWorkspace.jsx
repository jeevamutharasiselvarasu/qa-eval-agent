import React, { createContext, useContext } from 'react'
import { NavLink, Navigate, Route, Routes, useParams } from 'react-router-dom'
import { api } from '../api.js'
import { Badge, Empty, useLoad } from '../components/ui.jsx'
import Overview from './run/Overview.jsx'
import Requirements from './run/Requirements.jsx'
import Profiling from './run/Profiling.jsx'
import MappingStudio from './run/MappingStudio.jsx'
import TransformationStudio from './run/TransformationStudio.jsx'
import SyntheticStudio from './run/SyntheticStudio.jsx'
import TestCases from './run/TestCases.jsx'
import Approvals from './run/Approvals.jsx'
import ExecutionMonitor from './run/ExecutionMonitor.jsx'
import QAReport from './run/QAReport.jsx'
import AuditTrail from './run/AuditTrail.jsx'

const RunCtx = createContext(null)
export const useRun = () => useContext(RunCtx)

const STEPS = [
  ['JIRA_RETRIEVED', 'Jira'], ['REQUIREMENTS_ANALYZED', 'Requirements'], ['DATASETS_PROFILED', 'Profiled'],
  ['MAPPING_GENERATED', 'Mappings'], ['TRANSFORMATION_DESIGNED', 'Transformation'], ['TEST_CASES_GENERATED', 'Tests'],
  ['SYNTHETIC_DATA_GENERATED', 'Synthetic'], ['APPROVED', 'Approved'], ['VALIDATION_COMPLETED', 'Validated'],
  ['FAILURE_ANALYSIS_COMPLETED', 'Analysed'], ['REPORT_GENERATED', 'Reported'],
]

const TABS = [
  ['overview', 'Overview'], ['requirements', 'Jira requirements'], ['profiling', 'Data profiling'], ['mapping', 'Mapping studio'],
  ['studio', 'Transformation studio'], ['synthetic', 'Synthetic data'], ['tests', 'Test cases'], ['approvals', 'Approval center'],
  ['execution', 'Execution monitor'], ['report', 'QA report'], ['audit', 'Audit'],
]

export default function RunWorkspace({ meta }) {
  const { runId } = useParams()
  const [run, reload, err] = useLoad(() => api.get(`/runs/${runId}`), [runId])
  if (err) return <Empty>{err}</Empty>
  if (!run) return <div className="muted">Loading…</div>
  const states = meta?.workflow_states || []
  const cur = states.indexOf(run.status)
  const pending = Object.entries(run.approvals).filter(([, v]) => ['PENDING', 'STALE'].includes(v.status)).length
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
            {k === 'requirements' && run.counts.open_gaps > 0 && <Badge s="OPEN">{run.counts.open_gaps}</Badge>}
            {k === 'approvals' && pending > 0 && <Badge s="PENDING">{pending}</Badge>}
          </NavLink>
        ))}
      </div>
      <Routes>
        <Route index element={<Navigate to="overview" replace />} />
        <Route path="overview" element={<Overview />} />
        <Route path="requirements" element={<Requirements />} />
        <Route path="profiling" element={<Profiling />} />
        <Route path="mapping" element={<MappingStudio />} />
        <Route path="studio" element={<TransformationStudio />} />
        <Route path="synthetic" element={<SyntheticStudio />} />
        <Route path="tests" element={<TestCases />} />
        <Route path="approvals" element={<Approvals />} />
        <Route path="execution" element={<ExecutionMonitor />} />
        <Route path="report" element={<QAReport />} />
        <Route path="audit" element={<AuditTrail />} />
      </Routes>
    </RunCtx.Provider>
  )
}
