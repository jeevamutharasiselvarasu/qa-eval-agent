import { act, render, screen, fireEvent } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { Avatar, Badge, Loading, Modal, PageHeader, ResultBar, StatePill, ToastProvider, humanize, useAction } from './ui.jsx'

describe('labels', () => {
  it('humanizes codes', () => {
    expect(humanize('REPORT_GENERATED')).toBe('Report generated')
    expect(humanize('')).toBe('')
  })

  it('keeps result codes upper-case and turns other codes into words', () => {
    render(<><Badge s="PASS" /><Badge s="ACCEPTED_LIMITATION" /><Badge s="AI_GENERATED" /><Badge s="FAIL">custom</Badge></>)
    expect(screen.getByText('PASS')).toHaveClass('badge', 'PASS')
    expect(screen.getByText('Accepted limitation')).toBeInTheDocument()
    expect(screen.getByText('AI generated')).toBeInTheDocument()
    expect(screen.getByText('custom')).toHaveClass('FAIL')
  })

  it('shows a run state as a readable pill with the raw code as tooltip', () => {
    render(<><StatePill state="REPORT_GENERATED" /><StatePill state="REQUIREMENT_GAP" /><StatePill state="SOMETHING_NEW" /><StatePill /></>)
    expect(screen.getByText('Report ready')).toHaveClass('pill', 'ok')
    expect(screen.getByText('Report ready')).toHaveAttribute('title', 'REPORT_GENERATED')
    expect(screen.getByText('Needs input')).toHaveClass('warn')
    expect(screen.getByText('Something new')).toHaveClass('neutral')
  })
})

describe('layout pieces', () => {
  it('page header renders crumbs, actions and sets the browser tab title', () => {
    render(<PageHeader title="Run history" description="All runs" crumbs={<a href="/">Home</a>} actions={<button>Go</button>} />)
    expect(screen.getByRole('heading', { level: 1, name: 'Run history' })).toBeInTheDocument()
    expect(screen.getByRole('navigation', { name: 'Breadcrumb' })).toHaveTextContent('Home')
    expect(screen.getByRole('button', { name: 'Go' })).toBeInTheDocument()
    expect(document.title).toBe('Run history · S2T QA Agent')
  })

  it('loading placeholder is announced', () => {
    render(<Loading rows={2} />)
    expect(screen.getByRole('status', { name: 'Loading' }).children).toHaveLength(2)
  })

  it('avatar shows initials', () => {
    const { container } = render(<><Avatar name="dev.user" /><Avatar name="Priya" /><Avatar /></>)
    expect([...container.querySelectorAll('.avatar')].map(a => a.textContent)).toEqual(['DU', 'P', '?'])
  })

  it('result bar summarises counts and shows a dash without data', () => {
    const { rerender } = render(<ResultBar s={{ total: 4, PASS: 2, FAIL: 1, BLOCKED: 1 }} />)
    expect(screen.getByText('2 pass · 1 fail · 1 blocked')).toBeInTheDocument()
    rerender(<ResultBar s={{ total: 0 }} />)
    expect(screen.getByText('—')).toBeInTheDocument()
  })

  it('modal closes with Escape and the close button, not when clicking inside', () => {
    const onClose = vi.fn()
    render(<Modal title="Edit" onClose={onClose}><p>Body</p></Modal>)
    fireEvent.mouseDown(screen.getByText('Body'))
    expect(onClose).not.toHaveBeenCalled()
    fireEvent.keyDown(window, { key: 'Escape' })
    fireEvent.click(screen.getByRole('button', { name: 'Close' }))
    expect(onClose).toHaveBeenCalledTimes(2)
  })
})

function ActionProbe({ fn, ok }) {
  const [busy, act] = useAction()
  return <button onClick={() => act('k', fn, ok)}>{busy ? 'busy' : 'run'}</button>
}

describe('useAction', () => {
  it('shows a success toast', async () => {
    render(<ToastProvider><ActionProbe fn={async () => 1} ok="Saved" /></ToastProvider>)
    await act(async () => fireEvent.click(screen.getByRole('button')))
    expect(screen.getByText('Saved')).toBeInTheDocument()
  })

  it('turns an error into an error toast instead of throwing', async () => {
    render(<ToastProvider><ActionProbe fn={async () => { throw new Error('Jira rejected the credentials') }} ok="Saved" /></ToastProvider>)
    await act(async () => fireEvent.click(screen.getByRole('button')))
    expect(screen.getByText('Jira rejected the credentials').closest('.alert')).toHaveClass('err')
    expect(screen.queryByText('Saved')).not.toBeInTheDocument()
    expect(screen.getByRole('button')).toHaveTextContent('run')
  })
})
