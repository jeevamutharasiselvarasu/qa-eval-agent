import React, { useState } from 'react'
import { RefreshCw, Check, X, MessageSquareText, Paperclip, ShieldAlert } from 'lucide-react'
import { api } from '../../api.js'
import { useRun } from '../RunWorkspace.jsx'
import { Alert, Badge, Btn, Card, Empty, Modal, useAction, useLoad } from '../../components/ui.jsx'

function GapEditor({ gap, onClose, onSaved }) {
  const { runId } = useRun()
  const [status, setStatus] = useState(gap.status === 'OPEN' ? 'CLARIFIED' : gap.status)
  const [text, setText] = useState(gap.clarification || '')
  const [busy, act] = useAction()
  return (
    <Modal title={`Clarify ${gap.id}`} onClose={onClose} footer={<>
      <button className="btn" onClick={onClose}>Cancel</button>
      <Btn className="primary" busy={busy} onClick={() => act('s', async () => {
        await api.post(`/runs/${runId}/requirements/clarifications`, { gap_id: gap.id, status, clarification: text }); onSaved()
      }, 'Gap updated — requirement approval must be renewed')}>Save</Btn></>}>
      <p>{gap.description}</p>
      <p className="small muted">{gap.detail}</p>
      <div className="pre small">{gap.evidence}</div>
      <div className="grid g2 mt">
        <label className="field">Status<select value={status} onChange={e => setStatus(e.target.value)}>
          <option value="OPEN">Open</option><option value="CLARIFIED">Clarified</option>
          <option value="ACCEPTED_LIMITATION">Accepted as limitation</option><option value="BLOCKED">Blocked</option></select></label>
      </div>
      <label className="field mt">Clarification (record the decision and who made it)
        <textarea rows={4} value={text} onChange={e => setText(e.target.value)} placeholder="e.g. Per Priya (BA) on 2026-10-25: treat null DISCOUNT as 0." /></label>
      <div className="tiny muted mt-s">Clarifying a gap does not change transformation code. Update the code if the clarified behaviour requires it.</div>
    </Modal>
  )
}

export default function Requirements() {
  const { runId, reload: reloadRun } = useRun()
  const [jira, reloadJira] = useLoad(() => api.get(`/runs/${runId}/jira`), [runId])
  const [data, reload] = useLoad(() => api.get(`/runs/${runId}/requirements`), [runId])
  const [busy, act] = useAction()
  const [gap, setGap] = useState(null)
  const [showAc, setShowAc] = useState(true)
  const refresh = () => { reload(); reloadJira(); reloadRun() }
  const setStatus = (r, status) => act(r.id, async () => { await api.patch(`/runs/${runId}/requirements/${r.id}`, { status }); refresh() })
  const p = jira?.payload

  return (
    <div className="col">
      <Card title={p ? `${p.key} · ${p.summary}` : 'Jira issue'} sub={p && `${p.issue_type} · ${p.status} · ${p.project?.name || ''}`}
        actions={<>
          {jira && <Badge s={jira.source === 'MOCK' ? 'MOCK' : 'APPROVED'}>{jira.source === 'MOCK' ? 'Mock fixture' : 'Live Jira Cloud'}</Badge>}
          <Btn icon={RefreshCw} busy={busy === 'jira'} onClick={() => act('jira', async () => { await api.post(`/runs/${runId}/jira/retrieve`); refresh() }, 'Jira re-retrieved and re-analysed')}>{jira ? 'Re-retrieve' : 'Retrieve'}</Btn>
        </>}>
        {!p ? <Empty>Not retrieved yet.</Empty> : (
          <div className="grid g2">
            <div>
              <h4 className="mb">Description</h4>
              <div className="pre">{p.description}</div>
              <div className="row wrap gap-s mt">{p.labels.map(l => <Badge key={l} className="outline">{l}</Badge>)}{p.components.map(l => <Badge key={l}>{l}</Badge>)}</div>
            </div>
            <div className="col">
              <div><h4 className="mb"><MessageSquareText size={13} /> Comments ({p.comments.length})</h4>
                {p.comments.map(c => <div key={c.id} className="small" style={{ borderLeft: '3px solid var(--border)', paddingLeft: 8, marginBottom: 8 }}>
                  <b>{c.author}</b> <span className="tiny muted">{c.created} · comment:{c.id}</span><div>{c.body}</div></div>)}</div>
              <div><h4 className="mb"><Paperclip size={13} /> Attachments ({p.attachments.length})</h4>
                {p.attachments.map(a => <div key={a.id} className="small">{a.filename} {a.skipped_reason ? <span className="muted">— {a.skipped_reason}</span> : <Badge s="ACCEPTED">analysed</Badge>}</div>)}</div>
              {p.links?.length > 0 && <div className="small"><h4 className="mb">Linked issues</h4>{p.links.map(l => <div key={l.key}>{l.type} <span className="mono">{l.key}</span> {l.summary}</div>)}</div>}
            </div>
          </div>
        )}
        {data?.warnings?.length > 0 && <div className="col gap-s mt">{data.warnings.map(w => <Alert key={w} kind="warn"><ShieldAlert size={13} /> {w}</Alert>)}</div>}
      </Card>

      <Card title="Requirement gaps" sub="Missing, ambiguous or conflicting rules. Only dependent mappings, scenarios and tests are blocked.">
        {!data ? <Empty>Loading…</Empty> : !data.gaps.length ? <Empty>No gaps detected.</Empty> : (
          <div className="tw"><table className="t">
            <thead><tr><th>Gap</th><th>Type</th><th>Description</th><th>Affected columns</th><th>Evidence</th><th>Status</th><th /></tr></thead>
            <tbody>{data.gaps.map(g => (
              <tr key={g.id}>
                <td className="mono small">{g.id}</td><td><Badge>{g.gap_type}</Badge></td>
                <td>{g.description}{g.clarification && <div className="tiny" style={{ color: 'var(--pass)' }}>↳ {g.clarification}</div>}</td>
                <td>{(g.affected.columns || []).map(c => <span className="chip" key={c}>{c}</span>)}</td>
                <td className="tiny muted" style={{ maxWidth: 260 }}>{g.evidence}</td>
                <td><Badge s={g.status} /></td>
                <td><button className="btn sm" onClick={() => setGap(g)}>Clarify</button></td>
              </tr>))}</tbody>
          </table></div>
        )}
      </Card>

      <Card title="Extracted requirements" sub="Each rule keeps its exact Jira evidence. AI-inferred proposals must be accepted or rejected explicitly."
        actions={<label className="row gap-s small"><input type="checkbox" checked={showAc} onChange={e => setShowAc(e.target.checked)} />Show acceptance criteria rows</label>} flush>
        {!data ? <Empty>Loading…</Empty> : !data.requirements.length ? <Empty>No requirements extracted.</Empty> : (
          <div className="tw"><table className="t">
            <thead><tr><th>ID</th><th>AC</th><th>Kind</th><th>Requirement</th><th>Provenance</th><th>Source</th><th>Status</th><th>Review</th></tr></thead>
            <tbody>{data.requirements.filter(r => showAc || r.kind !== 'ACCEPTANCE_CRITERION').map(r => (
              <tr key={r.id} style={r.kind === 'ACCEPTANCE_CRITERION' ? { background: 'var(--surface-2)' } : {}}>
                <td className="mono small">{r.id}</td><td className="mono small">{r.ac_id || '—'}</td>
                <td className="small">{r.kind.replaceAll('_', ' ').toLowerCase()}</td>
                <td style={{ maxWidth: 420 }}>{r.kind === 'ACCEPTANCE_CRITERION' ? <b>{r.description}</b> : r.description}
                  {r.evidence && r.kind !== 'ACCEPTANCE_CRITERION' && <div className="tiny muted">“{r.evidence.slice(0, 160)}”</div>}</td>
                <td><Badge s={r.provenance} />{r.provenance === 'AI_INFERRED' && <div className="tiny muted mt-s">confidence {r.confidence}</div>}</td>
                <td className="tiny mono muted">{r.source_ref}</td>
                <td><Badge s={r.status} /></td>
                <td className="nowrap">
                  <button className="btn sm ghost" title="Accept" disabled={busy === r.id} onClick={() => setStatus(r, 'ACCEPTED')}><Check size={14} /></button>
                  <button className="btn sm ghost" title="Reject" disabled={busy === r.id} onClick={() => setStatus(r, 'REJECTED')}><X size={14} /></button>
                </td>
              </tr>))}</tbody>
          </table></div>
        )}
      </Card>
      {gap && <GapEditor gap={gap} onClose={() => setGap(null)} onSaved={() => { setGap(null); refresh() }} />}
    </div>
  )
}
