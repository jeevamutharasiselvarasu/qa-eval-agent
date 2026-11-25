import { screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import StepNav from './StepNav.jsx'
import { baseRun, renderRunPage } from '../../test/render.jsx'

describe('StepNav', () => {
  it('blocks Next until the step is done and says what is missing', () => {
    renderRunPage(<StepNav step="studio" />, { run: baseRun({ approvals: { TRANSFORMATION: { status: 'PENDING' } } }), path: 'studio' })
    expect(screen.getByText('Step 2 of 4')).toBeInTheDocument()
    expect(screen.getByText('Review and approve the SQL')).toBeInTheDocument()
    const next = screen.getByText('Next: Test cases')
    expect(next).toHaveAttribute('aria-disabled', 'true')
    expect(screen.getByText('Approve the SQL to continue')).toBeInTheDocument()
    expect(screen.getByRole('link', { name: /Requirements/ })).toHaveAttribute('href', '/runs/RUN-1/requirements')
  })

  it('links to the next step once done', () => {
    renderRunPage(<StepNav step="studio" />, { path: 'studio' })
    expect(screen.getByRole('link', { name: /Next: Test cases/ })).toHaveAttribute('href', '/runs/RUN-1/tests')
    expect(screen.getByText('Done')).toBeInTheDocument()
  })

  it('first step has no back link and the last step no next', () => {
    const { unmount } = renderRunPage(<StepNav step="requirements" />, { path: 'requirements' })
    expect(screen.queryByTitle('Only needed if you want to change something')).not.toBeInTheDocument()
    unmount()
    renderRunPage(<StepNav step="results" />, { path: 'results' })
    expect(screen.queryByText(/^Next:/)).not.toBeInTheDocument()
  })

  it('renders extra actions such as the approve button', () => {
    renderRunPage(<StepNav step="studio"><button>Approve SQL v1</button></StepNav>, { path: 'studio' })
    expect(screen.getByRole('button', { name: 'Approve SQL v1' })).toBeInTheDocument()
  })
})
