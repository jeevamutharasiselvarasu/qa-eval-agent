import { useState } from 'react'
import { Link } from 'react-router-dom'
import { RefreshCw, Pencil, Check, MessageSquareText, Paperclip, ShieldAlert, KeyRound, Link2, Filter, CircleSlash, CheckCircle2, AlertTriangle, Sparkles, Plus, Trash2 } from 'lucide-react'
import { api } from '../../api.js'
import { useRun } from '../RunWorkspace.jsx'
import StepNav from './StepNav.jsx'
import { Alert, Badge, Btn, Card, Empty, Loading, Modal, useAction, useLoad } from '../../components/ui.jsx'

const STATE_LABEL = { CLEAR: 'Clear', MISSING: 'Missing', SUGGESTED: 'Confirm', QUESTION: 'Question', INVALID: 'Fix rule' }
const ORIGIN_LABEL = { JIRA: 'Jira', EDITED: 'Edited', SUGGESTED: 'Suggested', NONE: '—' }
const OPS = ['=', '!=', '>', '>=', '<', '<=']
const JOIN_TYPES = ['LEFT', 'INNER', 'RIGHT', 'FULL']

function syncMessage(sync) {
  if (!sync) return 'Saved'
  if (sync.generated) return `All requirements are clear: SQL v${sync.version} generated${sync.test_pack_built ? ' and the test pack built' : ''}`
  if (!sync.ready) return `Saved. ${sync.open_items.length} item(s) still need your input`
  return `Saved. ${sync.reason || ''}`
}

/* ------------------------------------------------------------------ edit one target column */
function ColumnEditor({ col, spec, onClose, onSaved }) {
  const { runId } = useRun()
  const [expr, setExpr] = useState(col.rule || '')
  const [note, setNote] = useState('')
  const [res, setRes] = useState(Object.fromEntries(col.open_questions.map(q => [q.id, 'CLARIFIED'])))
  const [err, setErr] = useState(null)
  const [busy, act] = useAction()
  const insert = (t) => setExpr(e => (e && !e.endsWith(' ') && !e.endsWith('(') ? e + ' ' : e) + t)
  const save = () => act('s', async () => {
    setErr(null)
    try {
      const resolve = Object.fromEntries(Object.entries(res).filter(([, v]) => v !== 'OPEN').map(([k, v]) => [k, { status: v, note: '' }]))
      const out = await api.put(`/runs/${runId}/target-spec/columns/${encodeURIComponent(col.target)}`, { expression: expr, note, resolve })
      onSaved(out)
    } catch (e) { setErr(e.message); throw e }
  }, null)
  return (
    <Modal title={`How is ${col.target} built?`} onClose={onClose} footer={<>
      <button className="btn" onClick={onClose}>Cancel</button>
      <Btn className="primary" busy={busy === 's'} onClick={save}>Save rule</Btn></>}>
      <div className="small muted mb">Write the rule as a SQL expression over the source columns. Click a column to insert it.
        {col.group_by?.length > 0 && <> Aggregates (SUM, COUNT, …) are grouped per <b>{col.group_by.join(', ')}</b>.</>}
        {col.default_when_unmatched != null && <> Records with no match get <b>{String(col.default_when_unmatched)}</b> (table rule).</>}</div>
      <div className="col gap-s mb">{spec.sources.map(s => (
        <div key={s.alias} className="row wrap gap-s"><span className="tiny muted mono" style={{ minWidth: 90 }}>{s.alias}</span>
          {s.columns.map(c => <button key={c} type="button" className="chip click" onClick={() => insert(`${s.alias}.${c}`)}>{c}</button>)}</div>))}</div>
      <label className="field">Rule for {col.target}
        <textarea rows={3} className="mono" value={expr} onChange={e => setExpr(e.target.value)} placeholder="e.g. UPPER(TRIM(customers.CUSTOMER_NAME))   or   ROUND(SUM(transactions.AMOUNT - COALESCE(transactions.DISCOUNT, 0)), 2)" /></label>
      {col.jira_rule && <div className="tiny muted mt-s">Jira rule (replaced by your edit): {col.jira_rule}</div>}
      {col.evidence && <div className="tiny muted mt-s">Evidence: {col.evidence.slice(0, 300)}</div>}
      {col.open_questions.length > 0 && <div className="mt">
        <h4 className="mb">Open questions for this column</h4>
        {col.open_questions.map(q => (
          <div key={q.id} className="row top gap-s small" style={{ marginBottom: 8 }}>
            <div className="grow"><span className="mono tiny">{q.id}</span> {q.description}</div>
            <select value={res[q.id]} onChange={e => setRes(r => ({ ...r, [q.id]: e.target.value }))}>
              <option value="CLARIFIED">My rule handles this</option>
              <option value="ACCEPTED_LIMITATION">Unknown: accept as limitation</option>
              <option value="OPEN">Leave open</option>
            </select>
          </div>))}
        <div className="tiny muted">A limitation lets the SQL be generated, but tests that depend on it stay BLOCKED.</div>
      </div>}
      <label className="field mt">Note (optional: who decided, where)<input value={note} onChange={e => setNote(e.target.value)} placeholder="e.g. Agreed with Priya (BA) on 24 Nov" /></label>
      {err && <div className="mt"><Alert kind="err">{err}</Alert></div>}
    </Modal>
  )
}

/* ------------------------------------------------------------------ edit table rules */
function TableEditor({ spec, onClose, onSaved }) {
  const { runId } = useRun()
  const t = spec.table
  const targets = spec.columns.map(c => c.target)
  const aliases = spec.sources.map(s => s.alias)
  const colsOf = (a) => spec.sources.find(s => s.alias === a)?.columns || []
  const [key, setKey] = useState(t.key)
  const [joins, setJoins] = useState(t.join_specs.map(j => ({ ...j, keys: j.keys.join(', ') })))
  const [filters, setFilters] = useState(t.filter_specs.map(f => ({ dataset: f.dataset_hint, column: f.column, op: f.op, value: String(f.value) })))
  const [defaults, setDefaults] = useState(Object.fromEntries(Object.entries(t.defaults).map(([k, v]) => [k, String(v)])))
  const [dirty, setDirty] = useState({})
  const [err, setErr] = useState(null)
  const [busy, act] = useAction()
  const touch = (k) => setDirty(d => ({ ...d, [k]: true }))
  const save = () => act('s', async () => {
    setErr(null)
    const body = {}
    if (dirty.key) body.key = key
    if (dirty.joins) body.joins = joins.map(j => ({ ...j, keys: j.keys.split(',').map(s => s.trim()).filter(Boolean) }))
    if (dirty.filters) body.filters = filters
    if (dirty.defaults) body.defaults = defaults
    if (!Object.keys(body).length) { onClose(); return }
    try { onSaved(await api.put(`/runs/${runId}/target-spec/table-rules`, body)) } catch (e) { setErr(e.message); throw e }
  }, null)
  const setJ = (i, k, v) => { touch('joins'); setJoins(js => js.map((j, n) => n === i ? { ...j, [k]: v } : j)) }
  const setF = (i, k, v) => { touch('filters'); setFilters(fs => fs.map((f, n) => n === i ? { ...f, [k]: v } : f)) }
  return (
    <Modal title="Table rules" onClose={onClose} footer={<>
      <button className="btn" onClick={onClose}>Cancel</button>
      <Btn className="primary" busy={busy === 's'} onClick={save}>Save rules</Btn></>}>
      <h4 className="row gap-s mb"><KeyRound size={14} />Business key <span className="tiny muted">(the column(s) that identify one target record)</span></h4>
      <div className="row wrap gap-l mb">{targets.map(c => (
        <label key={c} className="row gap-s small"><input type="checkbox" checked={key.includes(c)}
          onChange={e => { touch('key'); setKey(k => e.target.checked ? [...k, c] : k.filter(x => x !== c)) }} /><span className="mono">{c}</span></label>))}</div>

      <h4 className="row gap-s mb mt"><Link2 size={14} />Joins between the sources</h4>
      {joins.map((j, i) => (
        <div key={i} className="row wrap gap-s mb">
          <select value={j.left} onChange={e => setJ(i, 'left', e.target.value)}>{aliases.map(a => <option key={a}>{a}</option>)}</select>
          <select value={j.type} onChange={e => setJ(i, 'type', e.target.value)}>{JOIN_TYPES.map(x => <option key={x}>{x}</option>)}</select>
          <span className="small">JOIN</span>
          <select value={j.right} onChange={e => setJ(i, 'right', e.target.value)}>{aliases.map(a => <option key={a}>{a}</option>)}</select>
          <span className="small">on</span>
          <input value={j.keys} onChange={e => setJ(i, 'keys', e.target.value)} placeholder="KEY_COLUMN, …" style={{ width: 180 }} />
          <button className="btn sm ghost" title="Remove" onClick={() => { touch('joins'); setJoins(js => js.filter((_, n) => n !== i)) }}><Trash2 size={13} /></button>
        </div>))}
      {aliases.length > 1 && <button className="btn sm" onClick={() => { touch('joins'); setJoins(js => [...js, { type: 'LEFT', left: aliases[0], right: aliases[1], keys: '' }]) }}><Plus size={13} />Add join</button>}

      <h4 className="row gap-s mb mt"><Filter size={14} />Row filters <span className="tiny muted">(only source rows that match are used)</span></h4>
      {filters.map((f, i) => (
        <div key={i} className="row wrap gap-s mb">
          <select value={f.dataset} onChange={e => setF(i, 'dataset', e.target.value)}>{aliases.map(a => <option key={a}>{a}</option>)}</select>
          <select value={f.column} onChange={e => setF(i, 'column', e.target.value)}><option value="">column…</option>{colsOf(f.dataset).map(c => <option key={c}>{c}</option>)}</select>
          <select value={f.op} onChange={e => setF(i, 'op', e.target.value)}>{OPS.map(o => <option key={o}>{o}</option>)}</select>
          <input value={f.value} onChange={e => setF(i, 'value', e.target.value)} placeholder="value" style={{ width: 140 }} />
          <button className="btn sm ghost" title="Remove" onClick={() => { touch('filters'); setFilters(fs => fs.filter((_, n) => n !== i)) }}><Trash2 size={13} /></button>
        </div>))}
      <button className="btn sm" onClick={() => { touch('filters'); setFilters(fs => [...fs, { dataset: aliases[0], column: '', op: '=', value: '' }]) }}><Plus size={13} />Add filter</button>

      <h4 className="row gap-s mb mt"><CircleSlash size={14} />Value when a record has no match <span className="tiny muted">(after a LEFT join; blank = NULL)</span></h4>
      <div className="grid g3">{targets.filter(c => !key.includes(c)).map(c => (
        <label key={c} className="field"><span className="mono">{c}</span>
          <input value={defaults[c] ?? ''} onChange={e => { touch('defaults'); setDefaults(d => ({ ...d, [c]: e.target.value })) }} placeholder="NULL" /></label>))}</div>
      {err && <div className="mt"><Alert kind="err">{err}</Alert></div>}
    </Modal>
  )
}

/* ------------------------------------------------------------------ answer a question */
function Answer({ q, onClose, onSaved }) {
  const { runId } = useRun()
  const [status, setStatus] = useState(q.status === 'OPEN' ? 'CLARIFIED' : q.status)
  const [note, setNote] = useState(q.clarification || '')
  const [err, setErr] = useState(null)
  const [busy, act] = useAction()
  return (
    <Modal title={`Answer ${q.id}`} onClose={onClose} footer={<>
      <button className="btn" onClick={onClose}>Cancel</button>
      <Btn className="primary" busy={busy === 's'} onClick={() => act('s', async () => {
        setErr(null)
        try { onSaved(await api.post(`/runs/${runId}/target-spec/questions/${q.id}`, { status, note })) } catch (e) { setErr(e.message); throw e }
      }, null)}>Save answer</Btn></>}>
      <p>{q.description}</p>
      {q.detail && <p className="small muted">{q.detail}</p>}
      {q.evidence && <div className="pre small">{q.evidence}</div>}
      <label className="field mt">Answer<select value={status} onChange={e => setStatus(e.target.value)}>
        <option value="CLARIFIED">Answered (the rules handle it)</option>
        <option value="ACCEPTED_LIMITATION">Unknown: accept as a limitation (dependent tests stay BLOCKED)</option>
        <option value="OPEN">Still open</option></select></label>
      <label className="field mt">What was decided, and by whom
        <textarea rows={3} value={note} onChange={e => setNote(e.target.value)} placeholder="e.g. Per Priya (BA): negative NET_AMOUNT is allowed as-is." /></label>
      {q.columns?.length > 0 && <div className="tiny muted mt-s">If the answer changes how a column is built, edit that column's rule as well ({q.columns.join(', ')}).</div>}
      {err && <div className="mt"><Alert kind="err">{err}</Alert></div>}
    </Modal>
  )
}

/* ------------------------------------------------------------------ page */
export default function Requirements() {
  const { runId, reload: reloadRun } = useRun()
  const [jira, reloadJira] = useLoad(() => api.get(`/runs/${runId}/jira`), [runId])
  const [spec, reloadSpec, specErr] = useLoad(() => api.get(`/runs/${runId}/target-spec`), [runId])
  const [warn] = useLoad(() => api.get(`/runs/${runId}/requirements`), [runId])
  const [busy, act] = useAction()
  const [editCol, setEditCol] = useState(null)
  const [editTable, setEditTable] = useState(false)
  const [answer, setAnswer] = useState(null)
  const refresh = () => { reloadSpec(); reloadJira(); reloadRun() }
  const saved = (out) => { setEditCol(null); setEditTable(false); setAnswer(null); refresh(); return syncMessage(out?.sync) }
  const savedToast = (out) => act('toast', async () => saved(out), syncMessage(out?.sync))
  const confirm = (c) => act(c.target, async () => {
    const out = await api.put(`/runs/${runId}/target-spec/columns/${encodeURIComponent(c.target)}`, { expression: c.rule })
    refresh(); return out
  }, `${c.target} confirmed`)
  const regenerate = () => act('regen', async () => { await api.post(`/runs/${runId}/target-spec/sync`, { force: true }); refresh() }, 'SQL regenerated from the requirements')
  const p = jira?.payload
  const openQ = (spec?.questions || []).filter(q => q.status === 'OPEN' || q.status === 'BLOCKED')
  const doneQ = (spec?.questions || []).filter(q => !(q.status === 'OPEN' || q.status === 'BLOCKED'))
  const actOn = (i) => {
    if (i.action === 'edit_column') setEditCol(spec.columns.find(c => c.target === i.ref))
    else if (i.action === 'edit_table') setEditTable(true)
    else if (i.action === 'answer_gap') setAnswer(spec.questions.find(q => q.id === i.ref))
  }

  return (
    <div className="col">
      <StepNav step="requirements" />
      {specErr ? <Alert kind="err">{specErr}</Alert> : !spec ? <Loading /> : <>
        {/* readiness */}
        {spec.ready ? (
          <Card>
            <div className="row top gap-s">
              <CheckCircle2 size={20} color="#1f9d55" />
              <div className="grow">
                <b>All requirements are clear.</b>{' '}
                {spec.sql.state === 'CURRENT' && <>SQL v{spec.sql.version} was generated from them. Continue with <b>Next: SQL</b> to review and approve it.</>}
                {spec.sql.state === 'READY_TO_GENERATE' && <>The SQL has not been generated yet.</>}
                {spec.sql.state === 'MANUAL_OUTDATED' && <>The SQL (v{spec.sql.version}) was edited by hand and the requirements changed since, so it was not replaced automatically.</>}
                {spec.sql.state === 'WILL_REGENERATE' && <>The SQL will be regenerated.</>}
              </div>
              {['READY_TO_GENERATE', 'MANUAL_OUTDATED', 'WILL_REGENERATE'].includes(spec.sql.state) &&
                <Btn className="primary" icon={Sparkles} busy={busy === 'regen'} onClick={regenerate}>Generate SQL from requirements</Btn>}
            </div>
          </Card>
        ) : (
          <Card>
            <div className="row top gap-s">
              <AlertTriangle size={20} color="#d99a1a" />
              <div className="grow">
                <b>{spec.open_items.length} item(s) need your input before the SQL can be generated.</b>
                <div className="small muted">Edit the requirements below. The SQL query and the test pack are generated automatically once everything is clear.
                  {spec.sql.version && <> The current SQL (v{spec.sql.version}) is kept until then.</>}</div>
                <div className="col gap-s mt">{spec.open_items.map((i, n) => (
                  <div key={n} className="row gap-s small">
                    <Badge s={i.state}>{STATE_LABEL[i.state] || i.state}</Badge>
                    <b className="mono">{i.ref}</b><span className="grow">{i.message}</span>
                    {i.action === 'data' ? <Link className="btn sm" to={`/runs/${runId}/profiling`}>Go to Data</Link> :
                      <button className="btn sm" onClick={() => actOn(i)}>{i.action === 'answer_gap' ? 'Answer' : 'Edit'}</button>}
                  </div>))}</div>
              </div>
            </div>
          </Card>
        )}

        {/* target columns */}
        <Card title="Target columns" sub="How each column of the target is built from the source columns. Edit a rule if it is missing or wrong." flush>
          {!spec.columns.length ? <Empty>Upload the target file to list its columns.</Empty> : (
            <div className="tw"><table className="t">
              <thead><tr><th>Target column</th><th>Source column(s)</th><th>How it is built</th><th>From</th><th>Status</th><th /></tr></thead>
              <tbody>{spec.columns.map(c => (
                <tr key={c.target}>
                  <td className="nowrap"><span className="mono"><b>{c.target}</b></span>
                    <div className="row gap-s mt-s">{c.is_key && <Badge s="info">key</Badge>}{c.not_null && <span className="tiny muted">not null</span>}
                      <span className="tiny muted mono">{(c.type || '').toLowerCase()}</span>{!c.in_target_file && <Badge s="PENDING" title="Named in the requirements but not in the uploaded target file">not in file</Badge>}</div></td>
                  <td>{c.source_columns.length ? c.source_columns.map(s => <span key={s} className="chip mono">{s}</span>) : <span className="muted small">—</span>}</td>
                  <td style={{ maxWidth: 420 }}>
                    {c.rule ? <div className="mono small">{c.rule}</div> : <span className="small" style={{ color: 'var(--fail)' }}>No rule</span>}
                    {c.summary && <div className="tiny muted">{c.summary}</div>}
                    {c.default_when_unmatched != null && <div className="tiny muted">No match → {String(c.default_when_unmatched)}</div>}
                    {c.ai_notes.map(n => <div key={n} className="tiny" style={{ color: '#7c3aed' }}>AI note: {n}</div>)}
                  </td>
                  <td className="nowrap"><Badge s={c.origin}>{ORIGIN_LABEL[c.origin]}</Badge>
                    {c.ac_id && <div className="tiny muted mono mt-s">{c.ac_id}</div>}</td>
                  <td style={{ maxWidth: 260 }}><Badge s={c.state}>{STATE_LABEL[c.state]}</Badge>
                    {c.message && <div className="tiny muted mt-s">{c.message}</div>}
                    {c.open_questions.map(q => <div key={q.id} className="tiny mt-s" style={{ color: 'var(--blocked)' }}>{q.id}: {q.description}</div>)}</td>
                  <td className="nowrap">
                    {c.state === 'SUGGESTED' && <Btn className="sm" icon={Check} busy={busy === c.target} onClick={() => confirm(c)}>Confirm</Btn>}
                    <button className="btn sm" onClick={() => setEditCol(c)}><Pencil size={13} />Edit</button>
                  </td>
                </tr>))}</tbody>
            </table></div>
          )}
        </Card>

        {/* table rules */}
        <Card title="Table rules" sub="Rules that apply to the whole target, not one column." actions={<button className="btn" onClick={() => setEditTable(true)} disabled={!spec.sources.length}><Pencil size={13} />Edit</button>}>
          <div className="kv">
            <dt><KeyRound size={13} /> Business key</dt>
            <dd>{spec.table.key.length ? <>{spec.table.key.map(k => <span key={k} className="chip mono">{k}</span>)} <Badge s={spec.table.key_origin}>{ORIGIN_LABEL[spec.table.key_origin]}</Badge></> : <span style={{ color: 'var(--fail)' }}>Not set</span>}</dd>
            <dt><Link2 size={13} /> Join</dt>
            <dd>{spec.table.joins.length ? spec.table.joins.map(j => <div key={j.id} className="mono small">{j.expression} {j.status !== 'ACCEPTED' && <Badge s="PROPOSED">check</Badge>}</div>) : <span className="muted">None</span>}</dd>
            <dt><Filter size={13} /> Row filter</dt>
            <dd>{spec.table.filters.length ? spec.table.filters.map(f => <div key={f.id} className="mono small">{f.expression} {f.status !== 'ACCEPTED' && <Badge s="PROPOSED">check</Badge>}</div>) : <span className="muted">None: all source rows</span>}</dd>
            <dt><CircleSlash size={13} /> No match</dt>
            <dd>{Object.keys(spec.table.defaults).length ? Object.entries(spec.table.defaults).map(([k, v]) => <span key={k} className="chip mono">{k} = {String(v)}</span>) : <span className="muted">NULL</span>}</dd>
            {spec.table.rounding && <><dt>Rounding</dt><dd className="small">{spec.table.rounding.replaceAll('_', ' ').toLowerCase()}</dd></>}
          </div>
        </Card>

        {/* questions */}
        <Card title="Questions" sub="Missing, unclear or conflicting points found in the Jira ticket and the data.">
          {!spec.questions.length ? <Empty>No questions.</Empty> : <div className="col gap-s">
            {[...openQ, ...doneQ].map(q => (
              <div key={q.id} className="row top gap-s small" style={{ borderBottom: '1px solid var(--border)', paddingBottom: 8 }}>
                <Badge s={q.status} />
                <div className="grow"><span className="mono tiny">{q.id}</span> {q.description}
                  {q.clarification && <div className="tiny" style={{ color: q.status === 'CLARIFIED' ? 'var(--pass)' : 'var(--blocked)' }}>↳ {q.clarification}</div>}
                  {q.columns?.length > 0 && <div className="tiny muted">Columns: {q.columns.join(', ')}</div>}</div>
                <button className="btn sm" onClick={() => setAnswer(q)}>{q.status === 'OPEN' ? 'Answer' : 'Change'}</button>
              </div>))}
          </div>}
        </Card>
      </>}

      {/* Jira source */}
      <Card title={p ? `${p.key} · ${p.summary}` : 'Jira issue'} sub={p && `${p.issue_type} · ${p.status} · ${p.project?.name || ''}`}
        actions={<>
          {jira && <Badge s={jira.source === 'MOCK' ? 'MOCK' : 'APPROVED'}>{jira.source === 'MOCK' ? 'Sample ticket' : 'Live Jira Cloud'}</Badge>}
          <Btn icon={RefreshCw} busy={busy === 'jira'} onClick={() => act('jira', async () => { await api.post(`/runs/${runId}/jira/retrieve`); refresh() }, 'Jira re-retrieved; your edits were kept')}>{jira ? 'Re-retrieve' : 'Retrieve'}</Btn>
        </>}>
        {!p ? <Empty>Not retrieved yet.</Empty> : (
          <details>
            <summary className="small">Show the ticket (description, comments, attachments)</summary>
            <div className="grid g2 mt">
              <div><h4 className="mb">Description</h4><div className="pre">{p.description}</div></div>
              <div className="col">
                <div><h4 className="mb"><MessageSquareText size={13} /> Comments ({p.comments.length})</h4>
                  {p.comments.map(c => <div key={c.id} className="small" style={{ borderLeft: '3px solid var(--border)', paddingLeft: 8, marginBottom: 8 }}>
                    <b>{c.author}</b> <span className="tiny muted">{c.created}</span><div>{c.body}</div></div>)}</div>
                <div><h4 className="mb"><Paperclip size={13} /> Attachments ({p.attachments.length})</h4>
                  {p.attachments.map(a => <div key={a.id} className="small">{a.filename} {a.skipped_reason ? <span className="muted">: {a.skipped_reason}</span> : <Badge s="ACCEPTED">analysed</Badge>}</div>)}</div>
              </div>
            </div>
          </details>
        )}
        {warn?.warnings?.length > 0 && <div className="col gap-s mt">{warn.warnings.map(w => <Alert key={w} kind="warn"><ShieldAlert size={13} /> {w}</Alert>)}</div>}
        {spec?.other_notes?.length > 0 && <div className="tiny muted mt">AI notes (informational): {spec.other_notes.map(n => n.description).join(' · ')}</div>}
      </Card>

      {editCol && <ColumnEditor col={editCol} spec={spec} onClose={() => setEditCol(null)} onSaved={savedToast} />}
      {editTable && <TableEditor spec={spec} onClose={() => setEditTable(false)} onSaved={savedToast} />}
      {answer && <Answer q={answer} onClose={() => setAnswer(null)} onSaved={savedToast} />}
    </div>
  )
}
