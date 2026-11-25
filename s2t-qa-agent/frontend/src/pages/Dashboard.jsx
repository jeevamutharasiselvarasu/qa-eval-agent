import { Link } from 'react-router-dom'
import { PlusCircle } from 'lucide-react'
import { api } from '../api.js'
import { Card, Empty, Legend, Loading, PageHeader, ResultBar, STATUS_COLORS, Stat, StatePill, fmtDate, useLoad } from '../components/ui.jsx'

function Trend({ data }) {
  if (!data.length) return <Empty>No executions yet.</Empty>
  const W = 640, H = 170, pad = 24
  const max = Math.max(...data.map(d => d.pass + d.fail + d.blocked + d.error), 1)
  const bw = Math.min(28, (W - pad * 2) / data.length - 6)
  return (
    <div>
      <svg viewBox={`0 0 ${W} ${H + 24}`} width="100%" role="img" aria-label="Test results per execution">
        {[0, 0.5, 1].map(f => <line key={f} x1={pad} x2={W - pad} y1={H - f * (H - 10)} y2={H - f * (H - 10)} stroke="#e4e7ef" />)}
        {data.map((d, i) => {
          const x = pad + i * ((W - pad * 2) / data.length) + 3
          let y = H
          return (
            <g key={d.execution_id}>
              <title>{`${d.execution_id}\npass ${d.pass} · fail ${d.fail} · blocked ${d.blocked} · error ${d.error}`}</title>
              {[['pass', 'PASS'], ['fail', 'FAIL'], ['blocked', 'BLOCKED'], ['error', 'ERROR']].map(([k, s]) => {
                const h = (d[k] / max) * (H - 10)
                y -= h
                return h > 0 ? <rect key={k} x={x} y={y} width={bw} height={Math.max(h - 1, 0)} rx="2" fill={STATUS_COLORS[s]} /> : null
              })}
              <text x={x + bw / 2} y={H + 14} fontSize="9" textAnchor="middle" fill="#7a8296">{''}</text>
            </g>
          )
        })}
      </svg>
      <div className="row between"><Legend /><span /></div>
    </div>
  )
}

export default function Dashboard() {
  const [d] = useLoad(() => api.get('/dashboard'), [], 8000)
  if (!d) return <Loading rows={6} />
  const t = d.totals
  return (
    <div>
      <PageHeader title="Dashboard" description="Source-to-target QA runs driven by Jira: results and trends."
        actions={<Link className="btn primary" to="/runs/new"><PlusCircle size={15} />New QA run</Link>} />
      <div className="grid g6 mb">
        <Stat label="QA runs" value={d.run_count} />
        <Stat label="Executions" value={d.execution_count} />
        <Stat label="Tests passed" value={t.PASS} color={STATUS_COLORS.PASS} />
        <Stat label="Tests failed" value={t.FAIL} color={STATUS_COLORS.FAIL} />
        <Stat label="Tests blocked" value={t.BLOCKED} color={STATUS_COLORS.BLOCKED} />
        <Stat label="Technical errors" value={t.ERROR} color={STATUS_COLORS.ERROR} />
      </div>
      <div className="grid g2 mb" style={{ gridTemplateColumns: '2fr 1fr' }}>
        <Card title="Results per execution" sub="Last 30 finished executions"><Trend data={d.trend} /></Card>
        <Card title="Runs by workflow state">
          {Object.keys(d.runs_by_status).length ? (
            <div className="col gap-s">
              {Object.entries(d.runs_by_status).map(([k, v]) => (
                <div className="row between" key={k}><StatePill state={k} /><b>{v}</b></div>
              ))}
            </div>
          ) : <Empty>No runs.</Empty>}
        </Card>
      </div>
      <Card title="Recent runs" actions={<Link to="/runs" className="small">All runs →</Link>} flush>
        {d.recent_runs.length ? (
          <div className="tw"><table className="t">
            <thead><tr><th>Run</th><th>Jira</th><th>State</th><th>Latest results</th><th>Created</th></tr></thead>
            <tbody>{d.recent_runs.map(r => (
              <tr key={r.id}>
                <td><Link to={`/runs/${r.id}`}>{r.name}</Link><div className="tiny muted mono">{r.id}</div></td>
                <td className="mono">{r.jira_key}</td>
                <td><div className="row wrap gap-s"><StatePill state={r.status} />{r.exception_state && <StatePill state={r.exception_state} />}</div></td>
                <td><ResultBar s={r.latest.ACTUAL?.summary} /></td>
                <td className="small nowrap">{fmtDate(r.created_at)}</td>
              </tr>))}</tbody>
          </table></div>
        ) : <Empty>No runs yet. <Link to="/runs/new">Create the first QA run</Link>.</Empty>}
      </Card>
    </div>
  )
}
