import React, { useMemo, useState } from 'react'
import { Plus, Trash2, Braces } from 'lucide-react'

const AGG = ['SUM', 'COUNT', 'AVG', 'MIN', 'MAX', 'COUNT_DISTINCT']
const OPS = ['=', '!=', '>', '>=', '<', '<=', 'IN', 'IS NULL', 'IS NOT NULL']
const ARITH = ['+', '-', '*', '/']
const CASTS = ['VARCHAR', 'INTEGER', 'BIGINT', 'DOUBLE', 'DECIMAL(18,2)', 'DATE', 'TIMESTAMP', 'BOOLEAN']
const KINDS = [['direct', 'Direct / rename'], ['string', 'String transform'], ['arith', 'Arithmetic'], ['aggregate', 'Aggregate'],
  ['const', 'Constant'], ['case', 'Conditional (CASE)'], ['cast', 'Type conversion'], ['advanced', 'Advanced (JSON)']]

// ---------- expression <-> form
const isCol = (e) => e && typeof e === 'object' && 'col' in e
const isLit = (e) => e && typeof e === 'object' && 'lit' in e
const operand = (e) => isCol(e) ? { type: 'col', col: e.col } : isLit(e) ? { type: 'num', value: e.lit } : null
const opExpr = (o) => o.type === 'col' ? { col: o.col } : { lit: Number(o.value) }

function stringChain(e) {
  const f = { trim: false, upper: false, lower: false }
  let x = e
  while (x && x.fn && ['UPPER', 'LOWER', 'TRIM'].includes(x.fn.toUpperCase()) && x.args?.length === 1) { f[x.fn.toLowerCase()] = true; x = x.args[0] }
  return isCol(x) && (f.trim || f.upper || f.lower) ? { ...f, col: x.col } : null
}

export function decompose(expr) {
  let e = expr
  let def = null, rnd = null
  if (e?.fn === 'COALESCE' && e.args?.length === 2 && isLit(e.args[1])) { def = e.args[1].lit; e = e.args[0] }
  if (e?.fn === 'ROUND' && e.args?.length === 2 && isLit(e.args[1])) { rnd = e.args[1].lit; e = e.args[0] }
  const base = { def, rnd }
  if (isCol(e)) return { ...base, kind: 'direct', col: e.col }
  const s = stringChain(e)
  if (s) return { ...base, kind: 'string', ...s }
  if (e?.fn && AGG.includes(e.fn.toUpperCase())) {
    const a = e.args?.[0]
    if (!a) return { ...base, kind: 'aggregate', fn: e.fn.toUpperCase(), input: { kind: 'star' } }
    if (isCol(a)) return { ...base, kind: 'aggregate', fn: e.fn.toUpperCase(), input: { kind: 'col', col: a.col } }
    if (a.op && ARITH.includes(a.op) && operand(a.args[0]) && operand(a.args[1]))
      return { ...base, kind: 'aggregate', fn: e.fn.toUpperCase(), input: { kind: 'arith', left: operand(a.args[0]), op: a.op, right: operand(a.args[1]) } }
  }
  if (e?.op && ARITH.includes(e.op) && operand(e.args[0]) && operand(e.args[1])) return { ...base, kind: 'arith', left: operand(e.args[0]), op: e.op, right: operand(e.args[1]) }
  if (isLit(e)) return { ...base, kind: 'const', value: e.lit }
  if (e?.case?.length === 1 && e.case[0].when?.column && isLit(e.case[0].then) && (!e.else || isLit(e.else)))
    return { ...base, kind: 'case', when: e.case[0].when, then: e.case[0].then.lit, else: e.else ? e.else.lit : null }
  if (e?.cast && isCol(e.cast)) return { ...base, kind: 'cast', col: e.cast.col, to: e.to }
  return { def: null, rnd: null, kind: 'advanced', json: JSON.stringify(expr, null, 1) }
}

export function compose(f) {
  let e
  switch (f.kind) {
    case 'direct': e = { col: f.col }; break
    case 'string': e = { col: f.col }; if (f.trim) e = { fn: 'TRIM', args: [e] }; if (f.upper) e = { fn: 'UPPER', args: [e] }; if (f.lower) e = { fn: 'LOWER', args: [e] }; break
    case 'arith': e = { op: f.op, args: [opExpr(f.left), opExpr(f.right)] }; break
    case 'aggregate': {
      const i = f.input
      e = { fn: f.fn, args: i.kind === 'star' ? [] : i.kind === 'col' ? [{ col: i.col }] : [{ op: i.op, args: [opExpr(i.left), opExpr(i.right)] }] }
      break
    }
    case 'const': e = { lit: f.value === '' || isNaN(Number(f.value)) ? f.value : Number(f.value) }; break
    case 'case': e = { case: [{ when: f.when, then: { lit: f.then } }], else: { lit: f.else } }; break
    case 'cast': e = { cast: { col: f.col }, to: f.to }; break
    default: try { return JSON.parse(f.json) } catch { return { lit: null } }
  }
  if (f.rnd !== null && f.rnd !== '' && f.rnd !== undefined && f.kind !== 'advanced') e = { fn: 'ROUND', args: [e, { lit: Number(f.rnd) }] }
  if (f.def !== null && f.def !== '' && f.def !== undefined && f.kind !== 'advanced') e = { fn: 'COALESCE', args: [e, { lit: isNaN(Number(f.def)) ? f.def : Number(f.def) }] }
  return e
}

// ---------- small inputs
function ColSel({ value, cols, onChange, style }) {
  return <select value={value || ''} onChange={e => onChange(e.target.value)} style={style}>
    <option value="">— column —</option>{cols.map(c => <option key={c} value={c}>{c}</option>)}</select>
}
function Operand({ v, cols, onChange }) {
  return <span className="row gap-s">
    <select value={v.type} onChange={e => onChange(e.target.value === 'col' ? { type: 'col', col: '' } : { type: 'num', value: 0 })} style={{ width: 100 }}>
      <option value="col">column</option><option value="num">number</option></select>
    {v.type === 'col' ? <ColSel value={v.col} cols={cols} onChange={c => onChange({ type: 'col', col: c })} /> :
      <input type="number" value={v.value} onChange={e => onChange({ type: 'num', value: e.target.value })} style={{ width: 90 }} />}
  </span>
}
function Cond({ c, cols, onChange, onRemove }) {
  return <div className="row wrap gap-s mt-s">
    <ColSel value={c.column} cols={cols} onChange={v => onChange({ ...c, column: v })} />
    <select value={c.op} onChange={e => onChange({ ...c, op: e.target.value })}>{OPS.map(o => <option key={o}>{o}</option>)}</select>
    {!c.op?.includes('NULL') && <input value={Array.isArray(c.value) ? c.value.join(',') : c.value ?? ''} placeholder={c.op === 'IN' ? 'a,b,c' : 'value'}
      onChange={e => onChange({ ...c, value: e.target.value })} style={{ width: 140 }} />}
    <button className="btn sm ghost" onClick={onRemove} aria-label="Remove condition"><Trash2 size={13} /></button>
  </div>
}

function ColumnEditor({ col, cols, onChange, onRemove }) {
  const f = useMemo(() => decompose(col.expr), [col.expr])
  const set = (patch) => onChange({ ...col, expr: compose({ ...f, ...patch }) })
  const switchKind = (k) => {
    const first = cols[0] || ''
    const fresh = { direct: { col: f.col || first }, string: { col: f.col || first, trim: true, upper: false, lower: false },
      arith: { left: { type: 'col', col: first }, op: '-', right: { type: 'col', col: first } },
      aggregate: { fn: 'SUM', input: { kind: 'col', col: f.col || first } }, const: { value: '' },
      case: { when: { column: first, op: '=', value: '' }, then: '', else: '' }, cast: { col: f.col || first, to: 'VARCHAR' },
      advanced: { json: JSON.stringify(col.expr, null, 1) } }[k]
    onChange({ ...col, expr: compose({ ...f, kind: k, ...fresh }) })
  }
  return (
    <div className="vis-row">
      <input value={col.target} onChange={e => onChange({ ...col, target: e.target.value })} placeholder="TARGET_COLUMN" className="mono" />
      <select value={f.kind} onChange={e => switchKind(e.target.value)}>{KINDS.map(([k, l]) => <option key={k} value={k}>{l}</option>)}</select>
      <div className="col gap-s">
        <div className="row wrap gap-s">
          {f.kind === 'direct' && <ColSel value={f.col} cols={cols} onChange={c => set({ col: c })} />}
          {f.kind === 'string' && <>
            <ColSel value={f.col} cols={cols} onChange={c => set({ col: c })} />
            {['trim', 'upper', 'lower'].map(k => <label key={k} className="row gap-s small"><input type="checkbox" checked={!!f[k]} onChange={e => set({ [k]: e.target.checked })} />{k.toUpperCase()}</label>)}
          </>}
          {f.kind === 'arith' && <><Operand v={f.left} cols={cols} onChange={v => set({ left: v })} />
            <select value={f.op} onChange={e => set({ op: e.target.value })}>{ARITH.map(o => <option key={o}>{o}</option>)}</select>
            <Operand v={f.right} cols={cols} onChange={v => set({ right: v })} /></>}
          {f.kind === 'aggregate' && <>
            <select value={f.fn} onChange={e => set({ fn: e.target.value })}>{AGG.map(o => <option key={o}>{o}</option>)}</select>
            <select value={f.input.kind} onChange={e => set({ input: e.target.value === 'arith' ? { kind: 'arith', left: { type: 'col', col: cols[0] }, op: '-', right: { type: 'col', col: cols[0] } } : e.target.value === 'star' ? { kind: 'star' } : { kind: 'col', col: cols[0] } })}>
              <option value="col">of column</option><option value="arith">of expression</option><option value="star">of rows (*)</option></select>
            {f.input.kind === 'col' && <ColSel value={f.input.col} cols={cols} onChange={c => set({ input: { kind: 'col', col: c } })} />}
            {f.input.kind === 'arith' && <><Operand v={f.input.left} cols={cols} onChange={v => set({ input: { ...f.input, left: v } })} />
              <select value={f.input.op} onChange={e => set({ input: { ...f.input, op: e.target.value } })}>{ARITH.map(o => <option key={o}>{o}</option>)}</select>
              <Operand v={f.input.right} cols={cols} onChange={v => set({ input: { ...f.input, right: v } })} /></>}
          </>}
          {f.kind === 'const' && <input value={f.value ?? ''} onChange={e => set({ value: e.target.value })} placeholder="constant value" />}
          {f.kind === 'case' && <>
            <span className="small muted">WHEN</span>
            <ColSel value={f.when.column} cols={cols} onChange={c => set({ when: { ...f.when, column: c } })} />
            <select value={f.when.op} onChange={e => set({ when: { ...f.when, op: e.target.value } })}>{OPS.slice(0, 6).map(o => <option key={o}>{o}</option>)}</select>
            <input value={f.when.value ?? ''} onChange={e => set({ when: { ...f.when, value: e.target.value } })} style={{ width: 110 }} placeholder="value" />
            <span className="small muted">THEN</span><input value={f.then ?? ''} onChange={e => set({ then: e.target.value })} style={{ width: 110 }} />
            <span className="small muted">ELSE</span><input value={f.else ?? ''} onChange={e => set({ else: e.target.value })} style={{ width: 110 }} />
          </>}
          {f.kind === 'cast' && <><ColSel value={f.col} cols={cols} onChange={c => set({ col: c })} />
            <select value={f.to} onChange={e => set({ to: e.target.value })}>{CASTS.map(o => <option key={o}>{o}</option>)}</select></>}
          {f.kind === 'advanced' && <textarea className="mono" rows={4} defaultValue={f.json} onBlur={e => { try { onChange({ ...col, expr: JSON.parse(e.target.value) }) } catch { /* keep */ } }} />}
        </div>
        {f.kind !== 'advanced' && <div className="row wrap gap-s tiny muted">
          <label className="row gap-s">Round to <input type="number" min="0" max="10" value={f.rnd ?? ''} onChange={e => set({ rnd: e.target.value === '' ? null : e.target.value })} style={{ width: 60 }} /> decimals</label>
          <label className="row gap-s">Default when null <input value={f.def ?? ''} onChange={e => set({ def: e.target.value === '' ? null : e.target.value })} style={{ width: 80 }} /></label>
          {col.mapping_id && <span className="chip">{col.mapping_id}</span>}
        </div>}
      </div>
      <button className="btn sm ghost" onClick={onRemove} aria-label="Remove column"><Trash2 size={14} /></button>
    </div>
  )
}

export default function VisualBuilder({ spec, schema, onChange }) {
  const [raw, setRaw] = useState(false)
  const s = spec || { sources: [], joins: [], filters: [], columns: [] }
  const srcAliases = Object.keys(schema?.sources || {})
  const colsOf = (src) => (schema?.sources?.[src.alias] || []).map(c => `${src.as || src.alias}.${c.name}`)
  const allCols = s.sources.flatMap(colsOf)
  const up = (patch) => onChange({ ...s, ...patch })
  const setSrc = (i, v) => up({ sources: s.sources.map((x, j) => j === i ? v : x) })
  const asNames = s.sources.map(x => x.as || x.alias)

  if (raw) return (
    <div>
      <div className="row between mb"><span className="small muted">Raw visual definition (validated server-side before compile)</span><button className="btn sm" onClick={() => setRaw(false)}>Back to builder</button></div>
      <textarea className="mono" rows={22} defaultValue={JSON.stringify(s, null, 2)} onBlur={e => { try { onChange(JSON.parse(e.target.value)) } catch { alert('Invalid JSON') } }} />
    </div>
  )
  return (
    <div className="col">
      <div className="row between"><h4>Sources & pre-join filters</h4>
        <div className="row gap-s"><button className="btn sm" onClick={() => setRaw(true)}><Braces size={13} />JSON</button>
          <button className="btn sm" onClick={() => up({ sources: [...s.sources, { alias: srcAliases[0], as: srcAliases[0] + (s.sources.length ? s.sources.length : ''), filters: [] }] })}><Plus size={13} />Source</button></div></div>
      {s.sources.map((src, i) => (
        <div key={i} className="card card-b" style={{ padding: 10 }}>
          <div className="row wrap gap-s">
            <span className="small muted">{i === 0 ? 'Base' : 'Joined'}</span>
            <select value={src.alias} onChange={e => setSrc(i, { ...src, alias: e.target.value })}>{srcAliases.map(a => <option key={a}>{a}</option>)}</select>
            <span className="small muted">as</span><input value={src.as || ''} onChange={e => setSrc(i, { ...src, as: e.target.value })} style={{ width: 120 }} />
            <button className="btn sm ghost" onClick={() => setSrc(i, { ...src, filters: [...(src.filters || []), { column: colsOf(src)[0], op: '=', value: '' }] })}><Plus size={13} />Filter</button>
            {i > 0 && <button className="btn sm ghost" onClick={() => up({ sources: s.sources.filter((_, j) => j !== i), joins: (s.joins || []).filter(j => j.right !== (src.as || src.alias)) })}><Trash2 size={13} /></button>}
          </div>
          {(src.filters || []).map((f, k) => <Cond key={k} c={f} cols={colsOf(src)} onChange={v => setSrc(i, { ...src, filters: src.filters.map((x, j) => j === k ? v : x) })}
            onRemove={() => setSrc(i, { ...src, filters: src.filters.filter((_, j) => j !== k) })} />)}
        </div>
      ))}
      {s.sources.length > 1 && <>
        <h4>Joins</h4>
        {s.sources.slice(1).map((src) => {
          const a = src.as || src.alias
          const j = (s.joins || []).find(x => x.right === a) || { right: a, type: 'LEFT', on: [] }
          const setJ = (nj) => up({ joins: [...(s.joins || []).filter(x => x.right !== a), nj] })
          const leftCols = s.sources.filter(x => (x.as || x.alias) !== a).flatMap(colsOf)
          return (
            <div key={a} className="card card-b" style={{ padding: 10 }}>
              <div className="row wrap gap-s"><select value={j.type} onChange={e => setJ({ ...j, type: e.target.value })}>{['LEFT', 'INNER', 'RIGHT', 'FULL'].map(t => <option key={t}>{t}</option>)}</select>
                <span className="small">JOIN <b>{a}</b> ON</span>
                <button className="btn sm ghost" onClick={() => setJ({ ...j, on: [...j.on, { left: leftCols[0], right: colsOf(src)[0] }] })}><Plus size={13} />Key</button></div>
              {j.on.map((k, n) => <div key={n} className="row gap-s mt-s">
                <ColSel value={k.left} cols={leftCols} onChange={v => setJ({ ...j, on: j.on.map((x, m) => m === n ? { ...x, left: v } : x) })} /><span>=</span>
                <ColSel value={k.right} cols={colsOf(src)} onChange={v => setJ({ ...j, on: j.on.map((x, m) => m === n ? { ...x, right: v } : x) })} />
                <button className="btn sm ghost" onClick={() => setJ({ ...j, on: j.on.filter((_, m) => m !== n) })}><Trash2 size={13} /></button></div>)}
              {!j.on.length && <div className="tiny" style={{ color: 'var(--fail)' }}>A join needs at least one key.</div>}
            </div>)
        })}
      </>}
      <div className="row between"><h4>Post-join filters</h4><button className="btn sm" onClick={() => up({ filters: [...(s.filters || []), { column: allCols[0], op: '=', value: '' }] })}><Plus size={13} />Filter</button></div>
      {(s.filters || []).map((f, k) => <Cond key={k} c={f} cols={allCols} onChange={v => up({ filters: s.filters.map((x, j) => j === k ? v : x) })} onRemove={() => up({ filters: s.filters.filter((_, j) => j !== k) })} />)}
      <div className="row between"><h4>Target columns</h4><button className="btn sm" onClick={() => up({ columns: [...s.columns, { target: 'NEW_COLUMN', expr: { col: allCols[0] } }] })}><Plus size={13} />Column</button></div>
      <div className="tiny muted">Aggregates automatically group by all non-aggregated columns.</div>
      <div>{s.columns.map((c, i) => <ColumnEditor key={i} col={c} cols={allCols} onChange={v => up({ columns: s.columns.map((x, j) => j === i ? v : x) })} onRemove={() => up({ columns: s.columns.filter((_, j) => j !== i) })} />)}</div>
      <div className="row wrap gap-s small"><span className="muted">De-duplicate on output keys</span>
        <input placeholder="e.g. CUSTOMER_ID" value={(s.dedupe?.keys || []).join(',')} onChange={e => { const k = e.target.value.split(',').map(x => x.trim()).filter(Boolean); up({ dedupe: k.length ? { keys: k, order_by: s.dedupe?.order_by || [] } : undefined }) }} />
      </div>
      <div className="tiny muted">{asNames.length} source(s) · {s.columns.length} target column(s)</div>
    </div>
  )
}
