import { useEffect, useState, useCallback, createContext, useContext } from 'react'
import { Loader2, X } from 'lucide-react'

export const STATUS_COLORS = { PASS: '#1f9d55', FAIL: '#d64545', BLOCKED: '#d99a1a', ERROR: '#8e44ad', SKIPPED: '#9aa1b2', NOT_EXECUTED: '#c9ced9' }

const RESULT_CODES = new Set(['PASS', 'FAIL', 'BLOCKED', 'ERROR', 'SKIPPED'])

/** Status badge. Result codes stay upper-case; other codes are shown as readable words. */
export function Badge({ s, children, title, className = '' }) {
  const text = children ?? (RESULT_CODES.has(s) ? s : humanize(s).replace(/\bAi\b/g, 'AI').replace(/\bai\b/g, 'AI').replace(/\bsql\b/gi, 'SQL'))
  return <span className={`badge ${s || ''} ${className}`} title={title}>{text}</span>
}

export function Card({ title, actions, children, flush, className = '', sub }) {
  return (
    <div className={`card ${className}`}>
      {(title || actions) && (
        <div className="card-h">
          <div>
            {typeof title === 'string' ? <h3>{title}</h3> : title}
            {sub && <div className="muted small">{sub}</div>}
          </div>
          {actions && <div className="row wrap gap-s">{actions}</div>}
        </div>
      )}
      <div className={`card-b ${flush ? 'flush' : ''}`}>{children}</div>
    </div>
  )
}

export function Btn({ busy, icon: Icon, children, className = '', ...p }) {
  return (
    <button className={`btn ${className}`} disabled={busy || p.disabled} {...p}>
      {busy ? <Loader2 size={14} className="spin" /> : Icon ? <Icon size={14} /> : null}
      {children}
    </button>
  )
}

export function Empty({ children }) { return <div className="empty">{children}</div> }

export function Alert({ kind = 'info', children }) { return <div className={`alert ${kind}`}>{children}</div> }

export function Stat({ label, value, color, sub }) {
  return (
    <div className="card stat">
      <div className="v" style={{ color }}>{value}</div>
      <div className="l">{label}</div>
      {sub && <div className="tiny muted mt-s">{sub}</div>}
    </div>
  )
}

export function ResultBar({ s }) {
  if (!s || !s.total) return <span className="muted small">—</span>
  return (
    <div style={{ minWidth: 120 }}>
      <div className="bar">
        {['PASS', 'FAIL', 'BLOCKED', 'ERROR', 'SKIPPED', 'NOT_EXECUTED'].map(k => s[k] ? <span key={k} title={`${k}: ${s[k]}`} style={{ width: `${(100 * s[k]) / s.total}%`, background: STATUS_COLORS[k] }} /> : null)}
      </div>
      <div className="tiny muted mt-s">{s.PASS || 0} pass · {s.FAIL || 0} fail · {s.BLOCKED || 0} blocked{s.ERROR ? ` · ${s.ERROR} error` : ''}</div>
    </div>
  )
}

export function Legend() {
  return <div className="legend">{Object.entries(STATUS_COLORS).map(([k, c]) => <span key={k}><i style={{ background: c }} />{k.replace('_', ' ')}</span>)}</div>
}

export function DataTable({ columns, rows, max = 200 }) {
  if (!rows || !rows.length) return <Empty>No rows.</Empty>
  const cols = columns || Object.keys(rows[0])
  return (
    <div className="tw">
      <table className="t">
        <thead><tr>{cols.map(c => <th key={c}>{c}</th>)}</tr></thead>
        <tbody>
          {rows.slice(0, max).map((r, i) => (
            <tr key={i}>{cols.map(c => <td key={c} className={typeof r[c] === 'number' ? 'num mono' : ''}>{r[c] === null || r[c] === undefined ? <span className="muted">null</span> : typeof r[c] === 'object' ? JSON.stringify(r[c]) : String(r[c])}</td>)}</tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

export function Modal({ title, onClose, children, footer }) {
  useEffect(() => {
    const k = (e) => e.key === 'Escape' && onClose()
    window.addEventListener('keydown', k)
    return () => window.removeEventListener('keydown', k)
  }, [onClose])
  return (
    <div className="modal-bg" onMouseDown={onClose}>
      <div className="modal" onMouseDown={(e) => e.stopPropagation()}>
        <div className="card-h"><h3>{title}</h3><button className="btn ghost sm" onClick={onClose} aria-label="Close"><X size={16} /></button></div>
        <div className="card-b">{children}</div>
        {footer && <div className="card-h" style={{ borderTop: '1px solid var(--border)', borderBottom: 'none', justifyContent: 'flex-end' }}>{footer}</div>}
      </div>
    </div>
  )
}

const ToastCtx = createContext(() => {})
export function ToastProvider({ children }) {
  const [t, setT] = useState(null)
  const show = useCallback((msg, kind = 'ok') => { setT({ msg, kind }); setTimeout(() => setT(null), kind === 'err' ? 7000 : 3500) }, [])
  return (
    <ToastCtx.Provider value={show}>
      {children}
      {t && <div className="toast"><Alert kind={t.kind}>{t.msg}</Alert></div>}
    </ToastCtx.Provider>
  )
}
export const useToast = () => useContext(ToastCtx)

/** Async action helper: runs fn, shows errors as toasts, tracks busy state. */
export function useAction() {
  const toast = useToast()
  const [busy, setBusy] = useState(null)
  const run = useCallback(async (key, fn, okMsg) => {
    setBusy(key)
    try {
      const r = await fn()
      if (okMsg) toast(okMsg, 'ok')
      return r
    } catch (e) {
      toast(e.message || String(e), 'err')
      return undefined
    } finally { setBusy(null) }
  }, [toast])
  return [busy, run]
}

export function useLoad(fn, deps = [], intervalMs = 0) {
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)
  const [tick, setTick] = useState(0)
  const reload = useCallback(() => setTick(t => t + 1), [])
  useEffect(() => {
    let alive = true
    fn().then(d => { if (alive) { setData(d); setError(null) } }).catch(e => alive && setError(e.message))
    return () => { alive = false }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, tick])
  useEffect(() => {
    if (!intervalMs) return
    const id = setInterval(reload, intervalMs)
    return () => clearInterval(id)
  }, [intervalMs, reload])
  return [data, reload, error]
}

export const fmtDate = (d) => d ? new Date(d).toLocaleString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' }) : '—'

export function Ids({ ids, max = 8 }) {
  if (!ids || !ids.length) return <span className="muted">—</span>
  const shown = ids.slice(0, max)
  return <span title={ids.join(', ')}>{shown.map(i => <span className="chip" key={i}>{i}</span>)}{ids.length > max && <span className="tiny muted"> +{ids.length - max} more</span>}</span>
}

/* ------------------------------------------------------------------ human-readable labels */
export const RUN_STATE = {
  RUN_CREATED: ['Created', 'neutral'], JIRA_RETRIEVAL_IN_PROGRESS: ['Reading Jira', 'info'], JIRA_RETRIEVED: ['Jira read', 'neutral'],
  REQUIREMENTS_ANALYZED: ['Requirements read', 'neutral'], DATASETS_UPLOADED: ['Files uploaded', 'neutral'],
  DATASETS_PROFILED: ['Files checked', 'neutral'], MAPPING_GENERATED: ['Column rules ready', 'neutral'],
  TRANSFORMATION_DESIGNED: ['SQL ready', 'info'], TEST_CASES_GENERATED: ['Test cases ready', 'info'],
  SYNTHETIC_DATA_GENERATED: ['Test cases ready', 'info'], AWAITING_APPROVAL: ['Awaiting approval', 'warn'], APPROVED: ['Approved', 'info'],
  ACTUAL_TRANSFORMATION_IN_PROGRESS: ['Running', 'info'], SYNTHETIC_EXECUTION_IN_PROGRESS: ['Running', 'info'],
  VALIDATION_IN_PROGRESS: ['Running', 'info'], VALIDATION_COMPLETED: ['Validated', 'ok'], FAILURE_ANALYSIS_COMPLETED: ['Validated', 'ok'],
  REPORT_GENERATED: ['Report ready', 'ok'],
  // exception states
  JIRA_RETRIEVAL_FAILED: ['Jira error', 'bad'], REQUIREMENT_GAP: ['Needs input', 'warn'], DATASET_PARSE_FAILED: ['File error', 'bad'],
  PROFILE_FAILED: ['File check failed', 'bad'], MAPPING_REVIEW_REQUIRED: ['Needs input', 'warn'], APPROVAL_REJECTED: ['Rejected', 'bad'],
  TRANSFORMATION_FAILED: ['SQL error', 'bad'], EXECUTION_FAILED: ['Run failed', 'bad'], PARTIAL_SUCCESS: ['Partly completed', 'warn'],
  CANCELLED: ['Cancelled', 'neutral'],
}

/** A run's workflow state as a readable pill (raw code in the tooltip). */
export function StatePill({ state }) {
  if (!state) return null
  const [label, tone] = RUN_STATE[state] || [humanize(state), 'neutral']
  return <span className={`pill ${tone}`} title={state}>{label}</span>
}

/** CONSTANT_CASE -> "Constant case". */
export function humanize(code) {
  const s = String(code || '').replaceAll('_', ' ').toLowerCase()
  return s.charAt(0).toUpperCase() + s.slice(1)
}

/** Placeholder lines while data loads. */
export function Loading({ rows = 3, label = 'Loading' }) {
  return (
    <div className="skeleton" role="status" aria-label={label}>
      {Array.from({ length: rows }, (_, i) => <div key={i} className="sk-line" style={{ width: `${92 - i * 14}%` }} />)}
    </div>
  )
}

/** Page heading with optional breadcrumb, description and actions. Also sets the browser tab title. */
export function PageHeader({ title, description, crumbs, actions, meta, docTitle }) {
  useEffect(() => {
    const t = docTitle || (typeof title === 'string' ? title : '')
    document.title = t ? `${t} · S2T QA Agent` : 'S2T QA Agent'
  }, [docTitle, title])
  return (
    <div className="page-head">
      <div className="grow">
        {crumbs && <nav className="crumbs" aria-label="Breadcrumb">{crumbs}</nav>}
        <h1>{title}</h1>
        {description && <p>{description}</p>}
        {meta && <div className="page-meta">{meta}</div>}
      </div>
      {actions && <div className="row wrap gap-s">{actions}</div>}
    </div>
  )
}

/** Initials avatar. */
export function Avatar({ name = '' }) {
  const ini = name.split(/[.\s_@-]+/).filter(Boolean).slice(0, 2).map(x => x[0].toUpperCase()).join('') || '?'
  return <span className="avatar" aria-hidden="true">{ini}</span>
}
