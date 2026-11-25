/** Test helpers: render a run page inside the router, toast and run contexts the app provides. */
import { render } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { ToastProvider } from '../components/ui.jsx'
import { RunCtx } from '../pages/RunWorkspace.jsx'

export const baseRun = (over = {}) => ({
  id: 'RUN-1', name: 'DATA-1234 validation', jira_key: 'DATA-1234', project: 'default', status: 'TEST_CASES_GENERATED',
  exception_state: null, created_by: 'dev.user', created_at: '2026-11-25T10:00:00Z', requirements_ready: true,
  transformation_version: { id: 'TRV-1', version: 1 },
  approvals: { TRANSFORMATION: { status: 'APPROVED' }, TEST_CASES: { status: 'PENDING' } },
  counts: { open_items: 0, open_gaps: 0, executions: 0, reports: 0, target_columns: 5 },
  ...over,
})

export function renderRunPage(ui, { run = baseRun(), meta = { role: 'admin' }, path = 'tests', reload = () => {} } = {}) {
  return render(
    <MemoryRouter initialEntries={[`/runs/${run.id}/${path}`]}>
      <ToastProvider>
        <RunCtx.Provider value={{ run, runId: run.id, meta, reload }}>
          <Routes>
            <Route path="/runs/:runId/*" element={ui} />
          </Routes>
        </RunCtx.Provider>
      </ToastProvider>
    </MemoryRouter>,
  )
}
