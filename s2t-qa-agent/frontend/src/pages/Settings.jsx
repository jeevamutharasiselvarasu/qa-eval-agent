import { useEffect, useState } from 'react'
import { PlugZap, Save, CheckCircle2, XCircle, KeyRound } from 'lucide-react'
import { api } from '../api.js'
import { Alert, Badge, Btn, Card, Loading, PageHeader, fmtDate, useAction, useLoad } from '../components/ui.jsx'

export default function Settings({ meta, onSaved }) {
  const [cfg, reload] = useLoad(() => api.get('/settings/jira'), [])
  const [f, setF] = useState(null)
  const [test, setTest] = useState(null)
  const [busy, act] = useAction()
  const isAdmin = meta?.role === 'admin'

  useEffect(() => {
    if (cfg && !f) setF({ mode: cfg.mode, base_url: cfg.base_url || '', email: cfg.email || '', api_token: '',
      ac_custom_fields: (cfg.ac_custom_fields || []).join(', ') })
  }, [cfg]) // eslint-disable-line react-hooks/exhaustive-deps

  if (!cfg || !f) return <Loading rows={5} />
  const set = (k) => (e) => { setF({ ...f, [k]: e.target.value }); setTest(null) }
  const body = (extra = {}) => ({ ...f, api_token: f.api_token || null, ...extra })

  const doTest = () => act('test', async () => setTest(await api.post('/settings/jira/test', body({ mode: 'cloud' }))))
  const doSave = () => act('save', async () => {
    await api.put('/settings/jira', body())
    setF({ ...f, api_token: '' }); reload(); onSaved && onSaved()
  }, 'Jira settings saved')
  const clearToken = () => window.confirm('Remove the stored Jira API token?') && act('clr', async () => {
    await api.put('/settings/jira', { mode: 'mock', clear_token: true }); setF({ ...f, mode: 'mock', api_token: '' }); reload(); onSaved && onSaved()
  }, 'Token removed; switched to sample tickets')

  return (
    <div>
      <PageHeader title="Settings" description="Connect the app to your Jira Cloud site. Changes apply to the next Jira retrieval." />
      <div className="grid g2" style={{ gridTemplateColumns: 'minmax(0,1.4fr) minmax(0,1fr)' }}>
        <Card title="Jira connection" actions={<Badge s={cfg.mode === 'cloud' ? 'APPROVED' : 'MOCK'}>{cfg.mode === 'cloud' ? 'Connected to Jira Cloud' : 'Using sample tickets'}</Badge>}
          sub={cfg.source === 'settings' ? `Saved by ${cfg.updated_by} · ${fmtDate(cfg.updated_at)}` : 'Currently from the .env file / defaults'}>
          {!isAdmin && <div className="mb"><Alert kind="info">Only an admin can change these settings. Your role: {meta?.role}.</Alert></div>}
          <div className="seg mb">
            <button className={f.mode === 'mock' ? 'on' : ''} onClick={() => setF({ ...f, mode: 'mock' })} disabled={!isAdmin}>Sample tickets (offline)</button>
            <button className={f.mode === 'cloud' ? 'on' : ''} onClick={() => setF({ ...f, mode: 'cloud' })} disabled={!isAdmin}>Jira Cloud (REST API)</button>
          </div>
          {f.mode === 'mock' ? (
            <p className="small">Sample tickets <span className="mono">DATA-1234</span> and <span className="mono">SALES-77</span> are served from built-in files.
              No connection is made. Switch to <b>Jira Cloud</b> to read your own tickets.</p>
          ) : (
            <div className="col">
              <label className="field">Jira site URL<input value={f.base_url} onChange={set('base_url')} placeholder="https://your-site.atlassian.net" disabled={!isAdmin} /></label>
              <label className="field">Atlassian account email<input value={f.email} onChange={set('email')} placeholder="you@company.com" autoComplete="off" disabled={!isAdmin} /></label>
              <label className="field">API token
                <input type="password" value={f.api_token} onChange={set('api_token')} autoComplete="new-password" disabled={!isAdmin}
                  placeholder={cfg.token_set ? '•••••••••• saved — leave blank to keep it' : 'Paste your API token'} />
              </label>
              {cfg.token_unreadable && <Alert kind="warn">The stored token can't be decrypted (the key file changed). Enter it again.</Alert>}
              <label className="field">Acceptance-criteria custom fields (optional)
                <input value={f.ac_custom_fields} onChange={set('ac_custom_fields')} placeholder="e.g. customfield_10040" disabled={!isAdmin} /></label>
              <div className="tiny muted">Fields named “Acceptance Criteria” are found automatically. Criteria in the description are always read.</div>
            </div>
          )}
          <div className="row wrap gap-s mt">
            {f.mode === 'cloud' && <Btn icon={PlugZap} busy={busy === 'test'} disabled={!isAdmin} onClick={doTest}>Test connection</Btn>}
            <Btn className="primary" icon={Save} busy={busy === 'save'} disabled={!isAdmin} onClick={doSave}>Save</Btn>
            {cfg.token_set && <Btn className="danger" icon={KeyRound} disabled={!isAdmin} onClick={clearToken}>Remove token</Btn>}
          </div>
          {test && <div className="mt">{test.ok
            ? <Alert kind="ok"><CheckCircle2 size={13} /> Connected as <b>{test.display_name}</b>{test.deployment ? ` · ${test.deployment}` : ''} · {test.site}. Click <b>Save</b> to use this connection.</Alert>
            : <Alert kind="err"><XCircle size={13} /> {test.code}: {test.message}{test.hint && <div className="mt-s">{test.hint}</div>}</Alert>}</div>}
        </Card>
        <div className="col">
          <Card title="How to get a Jira API token">
            <ol className="small" style={{ margin: 0, paddingLeft: 18 }}>
              <li>Sign in at <span className="mono">id.atlassian.com</span> with the account that can see your tickets.</li>
              <li>Open <b>Security → Create and manage API tokens</b> and create a token.</li>
              <li>Paste it above with your site URL and email, then <b>Test connection</b> and <b>Save</b>.</li>
            </ol>
            <p className="tiny muted mt">The token is encrypted before it is stored, is never sent back to the browser, and is used only to read issues,
              comments and text attachments. The app needs read access only.</p>
          </Card>
          <Card title="AI provider">
            <div className="row gap-s"><Badge s={meta?.llm_mode === 'mock' ? 'MOCK' : 'APPROVED'}>{meta?.llm_mode === 'mock' ? 'Offline helper (mock)' : 'OpenAI-compatible API'}</Badge></div>
            <p className="tiny muted mt">Set <span className="mono">LLM_MODE</span>, <span className="mono">LLM_API_KEY</span> and <span className="mono">LLM_MODEL</span> in the
              <span className="mono"> .env</span> file and restart the backend to change it.</p>
          </Card>
        </div>
      </div>
    </div>
  )
}
