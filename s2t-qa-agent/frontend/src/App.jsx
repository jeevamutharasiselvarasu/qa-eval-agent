import { NavLink, Route, Routes } from 'react-router-dom'
import { LayoutDashboard, PlusCircle, History, Settings as SettingsIcon } from 'lucide-react'
import { Avatar, ToastProvider, useLoad } from './components/ui.jsx'
import { api } from './api.js'
import Dashboard from './pages/Dashboard.jsx'
import NewRun from './pages/NewRun.jsx'
import RunHistory from './pages/RunHistory.jsx'
import RunWorkspace from './pages/RunWorkspace.jsx'
import Settings from './pages/Settings.jsx'

export default function App() {
  const [meta, reloadMeta] = useLoad(() => api.get('/meta'), [])
  return (
    <ToastProvider>
      <div className="shell">
        <aside className="sidebar">
          <div className="brand">
            <Logo />
            <div><div className="brand-name">S2T QA Agent</div><div className="brand-sub">Source-to-target validation</div></div>
          </div>
          <nav className="nav col gap-s" style={{ gap: 2 }}>
            <NavLink to="/" end><LayoutDashboard size={16} />Dashboard</NavLink>
            <NavLink to="/runs/new"><PlusCircle size={16} />New QA run</NavLink>
            <NavLink to="/runs" end><History size={16} />Run history</NavLink>
            <NavLink to="/settings"><SettingsIcon size={16} />Settings</NavLink>
          </nav>
          {meta && <div className="sidebar-foot">
            <dl className="env">
              <dt>Jira</dt>
              <dd title={meta.jira_site || ''}><span className="dot" style={{ background: meta.jira_mode === 'mock' ? '#9aa1b2' : '#1f9d55' }} />
                {meta.jira_mode === 'mock' ? 'Sample tickets' : 'Jira Cloud'}</dd>
              <dt>AI</dt>
              <dd><span className="dot" style={{ background: meta.llm_mode === 'mock' ? '#9aa1b2' : '#1f9d55' }} />
                {meta.llm_mode === 'mock' ? 'Offline helper' : meta.llm_mode}</dd>
            </dl>
            <div className="user-row"><Avatar name={meta.user} /><div><div className="name">{meta.user}</div><div className="tiny muted">{meta.role}</div></div></div>
          </div>}
        </aside>
        <main className="main">
          <Routes>
            <Route path="/" element={<Dashboard />} />
            <Route path="/runs/new" element={<NewRun meta={meta} />} />
            <Route path="/runs" element={<RunHistory />} />
            <Route path="/settings" element={<Settings meta={meta} onSaved={reloadMeta} />} />
            <Route path="/runs/:runId/*" element={<RunWorkspace meta={meta} />} />
          </Routes>
        </main>
      </div>
    </ToastProvider>
  )
}

/** Product mark: a check inside a table grid (generic, not a third-party logo). */
function Logo() {
  return (
    <svg className="brand-logo" viewBox="0 0 32 32" role="img" aria-label="S2T QA Agent">
      <rect width="32" height="32" rx="8" fill="var(--accent)" />
      <path d="M8 10h16M8 16h7M8 22h5" stroke="#fff" strokeOpacity=".55" strokeWidth="2" strokeLinecap="round" />
      <path d="M17 20.5l3 3 5.5-6.5" fill="none" stroke="#fff" strokeWidth="2.4" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  )
}
