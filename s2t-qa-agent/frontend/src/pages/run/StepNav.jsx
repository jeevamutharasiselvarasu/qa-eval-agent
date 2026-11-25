import { Link } from 'react-router-dom'
import { ArrowLeft, ArrowRight, CheckCircle2 } from 'lucide-react'
import { useRun } from '../RunWorkspace.jsx'
import { FLOW, stepState } from './flow.js'

export { FLOW, stepState }

/** Top-of-page bar for a step: where you are, Back (optional) and Next (enabled once the step is done). */
export default function StepNav({ step, nextLabel, children }) {
  const { run, runId } = useRun()
  const i = FLOW.findIndex(s => s.key === step)
  const cur = FLOW[i], prev = FLOW[i - 1], next = FLOW[i + 1]
  const st = stepState(run)[step]
  return (
    <div className="stepnav">
      <div className="row gap-s grow">
        <span className="stepno">{cur.n}</span>
        <div><div className="tiny muted">Step {cur.n} of {FLOW.length}</div><b>{cur.title}</b></div>
        {st.done && <span className="row gap-s small" style={{ color: 'var(--pass)' }}><CheckCircle2 size={15} />Done</span>}
      </div>
      {children}
      {prev && <Link className="btn ghost" to={`/runs/${runId}/${prev.key}`} title="Only needed if you want to change something"><ArrowLeft size={14} />{prev.label}</Link>}
      {next && (st.done
        ? <Link className="btn primary" to={`/runs/${runId}/${next.key}`}>{nextLabel || `Next: ${next.label}`}<ArrowRight size={14} /></Link>
        : <span className="btn primary" aria-disabled="true" style={{ opacity: 0.45, cursor: 'not-allowed' }} title={st.todo}>{nextLabel || `Next: ${next.label}`}<ArrowRight size={14} /></span>)}
      {next && !st.done && st.todo && <div className="tiny muted stepnav-hint">{st.todo}</div>}
    </div>
  )
}
