import { act, fireEvent, screen, waitFor, within } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { api } from '../../api.js'
import TestCases from './TestCases.jsx'
import { baseRun, renderRunPage } from '../../test/render.jsx'

vi.mock('../../api.js', () => ({ api: { get: vi.fn(), post: vi.fn(), put: vi.fn(), patch: vi.fn() } }))

const tc = (id, over = {}) => ({
  id, title: `Test ${id}`, test_type: 'POSITIVE', basis: 'ACCEPTANCE_CRITERIA', track: 'ACTUAL', ac_ids: ['AC1'], state: 'READY',
  selected: true, rationale: '', expected_result: 'ok', validation: { type: 'KEY_UNIQUE' }, blocked_reason: null, ...over,
})
const TESTS = [
  tc('TC-AC1-01'),
  tc('TC-AC1-02', { test_type: 'NEGATIVE', validation: { type: 'COLUMN_MATCH', scope: { desc: 'no matching transactions row', sql: 'SELECT 1' } } }),
  tc('TC-AC1-03', { state: 'BLOCKED', blocked_reason: 'Question GAP-1 not answered' }),
  tc('TC-TL-01', { ac_ids: [], basis: 'TRANSFORMATION_LOGIC', title: 'NAME is upper-case' }),
  tc('TC-GEN-01', { ac_ids: [], basis: 'GENERAL', test_type: 'GENERAL', selected: false }),
]
const pack = (over = {}) => ({
  built: true, stale: false, transformation: 'APPROVED', pack_approved: false, can_approve: true, blockers: [],
  test_cases: 5, blocked_tests: 1, selected: 4, by_type: { POSITIVE: 3, NEGATIVE: 1, EDGE: 0, GENERAL: 1 },
  by_basis: { ACCEPTANCE_CRITERIA: 3, TRANSFORMATION_LOGIC: 1, GENERAL: 1 }, logic_note: null,
  artifacts: { TEST_CASES: { status: 'PENDING', artifact_version: '5 test cases' } }, ignored_requirements: [], ignored_mappings: [], ...over,
})

function setup(packOver = {}, meta = { role: 'admin' }) {
  api.get.mockImplementation(async (path) => {
    if (path.endsWith('/test-cases')) return { test_cases: TESTS, coverage: { acceptance_criteria: [{ ac_id: 'AC1', text: 'Key is unique' }] } }
    if (path.endsWith('/test-pack')) return pack(packOver)
    if (path.endsWith('/executions')) return []
    return null
  })
  api.put.mockResolvedValue({})
  api.post.mockResolvedValue([])
  return renderRunPage(<TestCases />, { meta, run: baseRun() })
}

describe('Test cases', () => {
  beforeEach(() => vi.clearAllMocks())

  it('groups tests by acceptance criterion, transformation logic and general checks', async () => {
    setup()
    expect(await screen.findByText('4 of 5 test cases selected')).toBeInTheDocument()
    expect(screen.getByText('Transformation logic', { selector: '.badge.outline' })).toBeInTheDocument()
    expect(screen.getByText('General', { selector: '.badge.outline' })).toBeInTheDocument()
    expect(screen.getByText('Only records with no matching transactions row')).toBeInTheDocument()
    expect(screen.getAllByText('Whole uploaded target').length).toBeGreaterThan(0)
    expect(screen.getByText('(1 blocked: reported as BLOCKED)')).toBeInTheDocument()
  })

  it('selecting and deselecting is saved immediately', async () => {
    setup()
    const box = await screen.findByLabelText('Select TC-GEN-01')
    expect(box).not.toBeChecked()
    await act(async () => fireEvent.click(box))
    expect(api.put).toHaveBeenCalledWith('/runs/RUN-1/test-cases/selection', { select: ['TC-GEN-01'] })
    expect(screen.getByText('5 of 5 test cases selected')).toBeInTheDocument()
    await act(async () => fireEvent.click(screen.getByRole('button', { name: 'Select none' })))
    expect(api.put).toHaveBeenLastCalledWith('/runs/RUN-1/test-cases/selection', { deselect: TESTS.map(t => t.id) })
    expect(screen.getByText('0 of 5 test cases selected')).toBeInTheDocument()
  })

  it('a group checkbox selects the whole group', async () => {
    setup()
    await screen.findByText('4 of 5 test cases selected')
    const card = screen.getByText('General', { selector: '.badge.outline' }).closest('.card')
    await act(async () => fireEvent.click(within(card).getAllByRole('checkbox')[0]))
    expect(api.put).toHaveBeenCalledWith('/runs/RUN-1/test-cases/selection', { select: ['TC-GEN-01'] })
  })

  it('filters by source', async () => {
    setup()
    await screen.findByText('4 of 5 test cases selected')
    fireEvent.click(screen.getByRole('button', { name: 'Transformation logic' }))
    expect(screen.queryByText('Test TC-AC1-01')).not.toBeInTheDocument()
    expect(screen.getByText('NAME is upper-case')).toBeInTheDocument()
  })

  it('an approver approves the pack and runs the selection in one step', async () => {
    setup()
    const btn = await screen.findByRole('button', { name: 'Approve test pack and run selected (4)' })
    await act(async () => fireEvent.click(btn))
    await waitFor(() => expect(api.post).toHaveBeenCalledWith('/runs/RUN-1/run-validation', { force: false }))
    expect(api.post.mock.calls[0]).toEqual(['/runs/RUN-1/test-pack/approve', { decision: 'APPROVED', reason: '' }])
  })

  it('once approved, it only runs', async () => {
    setup({ pack_approved: true })
    expect(await screen.findByRole('button', { name: 'Run selected (4) and create report' })).toBeEnabled()
  })

  it('an analyst can choose tests but waits for an approver', async () => {
    setup({}, { role: 'analyst' })
    expect(await screen.findByText('Waiting for an approver')).toBeInTheDocument()
    expect(screen.getByText(/can choose tests but cannot approve/)).toBeInTheDocument()
  })

  it('points back to step 2 when the SQL is not approved', async () => {
    setup({ transformation: 'PENDING', can_approve: false })
    expect(await screen.findByText(/The SQL is not approved yet/)).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Go back to step 2' })).toHaveAttribute('href', '/runs/RUN-1/studio')
  })

  it('asks for a rebuild when the pack is out of date', async () => {
    setup({ stale: true })
    expect((await screen.findAllByRole('button', { name: /Rebuild test pack/ })).length).toBeGreaterThan(0)
  })
})
