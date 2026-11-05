import React from 'react'
import { NavLink, Route, Routes } from 'react-router-dom'
import { LayoutDashboard, PlusCircle, History, ShieldCheck } from 'lucide-react'
import { ToastProvider, useLoad, Badge } from './components/ui.jsx'
import { api } from './api.js'
import Dashboard from './pages/Dashboard.jsx'
import NewRun from './pages/NewRun.jsx'
import RunHistory from './pages/RunHistory.jsx'
import RunWorkspace from './pages/RunWorkspace.jsx'

export default function App() {
  const [meta] = useLoad(() => api.get('/meta'), [])
  return (
    <ToastProvider>
      <div className="shell">
        <aside className="sidebar">
          <div className="brand">
            <div className="brand-mark">S2T</div>
            <div><div className="brand-name">QA Agent</div><div className="brand-sub">Source-to-Target validation</div></div>
          </div>
          <nav className="nav col gap-s" style={{ gap: 2 }}>
            <NavLink to="/" end><LayoutDashboard size={16} />Dashboard</NavLink>
            <NavLink to="/runs/new"><PlusCircle size={16} />New QA run</NavLink>
            <NavLink to="/runs"><History size={16} />Run history</NavLink>
          </nav>
          <div className="sidebar-foot">
            <div className="row gap-s"><ShieldCheck size={14} /> AI proposes; deterministic execution proves.</div>
            {meta && <div className="row wrap gap-s">
              <Badge s={meta.jira_mode === 'mock' ? 'MOCK' : 'APPROVED'}>Jira: {meta.jira_mode}</Badge>
              <Badge s={meta.llm_mode === 'mock' ? 'MOCK' : 'APPROVED'}>AI: {meta.llm_mode}</Badge>
            </div>}
            {meta && <div>{meta.user} · {meta.role}</div>}
          </div>
        </aside>
        <main className="main">
          <Routes>
            <Route path="/" element={<Dashboard />} />
            <Route path="/runs/new" element={<NewRun meta={meta} />} />
            <Route path="/runs" element={<RunHistory />} />
            <Route path="/runs/:runId/*" element={<RunWorkspace meta={meta} />} />
          </Routes>
        </main>
      </div>
    </ToastProvider>
  )
}
