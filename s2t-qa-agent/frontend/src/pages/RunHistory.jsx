import { useState } from 'react'
import { Link } from 'react-router-dom'
import { GitCompare } from 'lucide-react'
import { api } from '../api.js'
import { Badge, Btn, Card, Empty, Loading, Modal, PageHeader, ResultBar, StatePill, fmtDate, useAction, useLoad } from '../components/ui.jsx'

export default function RunHistory() {
  const [q, setQ] = useState('')
  const [runs] = useLoad(() => api.get(`/runs?q=${encodeURIComponent(q)}`), [q])
  const [sel, setSel] = useState([])
  const [cmp, setCmp] = useState(null)
  const [busy, act] = useAction()
  const toggle = (id) => setSel(s => s.includes(id) ? s.filter(x => x !== id) : [...s.slice(-1), id])

  return (
    <div>
      <PageHeader title="Run history" description="Search runs, open their results and compare two executions."
        actions={<>
          <input placeholder="Search by Jira key, name or run ID" value={q} onChange={e => setQ(e.target.value)} style={{ width: 280 }} />
          <Btn icon={GitCompare} disabled={sel.length !== 2} busy={busy === 'cmp'}
            onClick={() => act('cmp', async () => setCmp(await api.get(`/compare?a=${sel[0]}&b=${sel[1]}`)))}>Compare selected ({sel.length}/2)</Btn>
        </>} />
      <Card flush>
        {!runs ? <Loading /> : !runs.length ? <Empty>No runs match.</Empty> : (
          <div className="tw"><table className="t">
            <thead><tr><th>Run</th><th>Jira</th><th>State</th><th>Executions (select two to compare)</th><th>Created</th></tr></thead>
            <tbody>{runs.map(r => (
              <tr key={r.id}>
                <td><Link to={`/runs/${r.id}`}>{r.name}</Link><div className="tiny muted mono">{r.id} · {r.created_by}</div></td>
                <td className="mono">{r.jira_key}</td>
                <td><div className="row wrap gap-s"><StatePill state={r.status} />{r.exception_state && <StatePill state={r.exception_state} />}</div></td>
                <td>{r.executions.length ? <div className="col gap-s">{r.executions.map(e => (
                  <label key={e.id} className="row gap-s small">
                    <input type="checkbox" checked={sel.includes(e.id)} onChange={() => toggle(e.id)} />
                    <span className="mono tiny">{e.id}</span><Badge s={e.status} />
                    <ResultBar s={e.summary} />
                  </label>))}</div> : <span className="muted small">Not executed</span>}</td>
                <td className="small nowrap">{fmtDate(r.created_at)}</td>
              </tr>))}</tbody>
          </table></div>
        )}
      </Card>
      {cmp && <Modal title={`Compare ${cmp.a.id} → ${cmp.b.id}`} onClose={() => setCmp(null)}>
        <p className="small">{cmp.changed} test result(s) changed between the two executions.</p>
        <div className="tw"><table className="t">
          <thead><tr><th>Test case</th><th>{cmp.a.id}</th><th>{cmp.b.id}</th></tr></thead>
          <tbody>{cmp.rows.map(r => <tr key={r.test_case_id} className={r.changed ? 'sel' : ''}><td className="mono">{r.test_case_id}</td><td>{r.a ? <Badge s={r.a} /> : '—'}</td><td>{r.b ? <Badge s={r.b} /> : '—'}</td></tr>)}</tbody>
        </table></div>
      </Modal>}
    </div>
  )
}
