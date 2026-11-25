import { act, fireEvent, screen, waitFor, within } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { api } from '../../api.js'
import Requirements from './Requirements.jsx'
import { baseRun, renderRunPage } from '../../test/render.jsx'

vi.mock('../../api.js', () => ({ api: { get: vi.fn(), post: vi.fn(), put: vi.fn(), patch: vi.fn() } }))

const col = (target, over = {}) => ({
  target, type: 'VARCHAR', in_target_file: true, is_key: false, not_null: false, source_columns: [`customers.${target}`],
  rule: `customers.${target}`, expression: `customers.${target}`, mapping_type: 'DIRECT', summary: `Copied as-is from customers.${target}`,
  group_by: [], default_when_unmatched: null, origin: 'JIRA', evidence: '', requirement_id: 'REQ-1', ac_id: 'AC1', jira_rule: null,
  state: 'CLEAR', message: '', notes: [], ai_notes: [], open_questions: [], ...over,
})
const spec = (over = {}) => ({
  ready: false,
  open_items: [{ kind: 'COLUMN', ref: 'NET_AMOUNT', state: 'QUESTION', message: '1 open question(s) affect this column', action: 'edit_column' }],
  columns: [
    col('CUSTOMER_ID', { is_key: true }),
    col('NET_AMOUNT', { state: 'QUESTION', rule: 'ROUND(SUM(transactions.AMOUNT - transactions.DISCOUNT), 2)', source_columns: ['transactions.AMOUNT'],
      open_questions: [{ id: 'GAP-1', description: 'What should happen when DISCOUNT is null?', gap_type: 'NULL_HANDLING' }] }),
  ],
  table: { key: ['CUSTOMER_ID'], key_origin: 'JIRA', not_null: [], joins: [], filters: [], defaults: {}, rounding: null, join_specs: [], filter_specs: [] },
  questions: [{ id: 'GAP-1', status: 'OPEN', description: 'What should happen when DISCOUNT is null?', columns: ['DISCOUNT'] }],
  other_notes: [], sources: [{ alias: 'customers', columns: ['CUSTOMER_ID'] }, { alias: 'transactions', columns: ['AMOUNT', 'DISCOUNT'] }],
  sql: { state: 'WAITING_FOR_REQUIREMENTS', version: null },
  ...over,
})

function setup(s = spec()) {
  api.get.mockImplementation(async (path) => {
    if (path.endsWith('/target-spec')) return s
    if (path.endsWith('/jira')) return { source: 'MOCK', payload: { key: 'DATA-1234', summary: 'Build CUSTOMER_MASTER', description: 'AC1 ...',
      comments: [], attachments: [], issue_type: 'Story', status: 'In QA', project: { name: 'Data' } } }
    if (path.endsWith('/requirements')) return { warnings: [] }
    return null
  })
  return renderRunPage(<Requirements />, { run: baseRun({ requirements_ready: s.ready }), path: 'requirements' })
}

describe('Requirements', () => {
  beforeEach(() => vi.clearAllMocks())

  it('lists what needs input before the SQL can be generated', async () => {
    setup()
    expect(await screen.findByText('1 item(s) need your input before the SQL can be generated.')).toBeInTheDocument()
    expect(screen.getAllByText('NET_AMOUNT').length).toBeGreaterThan(0)
    expect(screen.getByText('Step 1 of 4')).toBeInTheDocument()
  })

  it('editing a rule sends the expression and the answers to its questions', async () => {
    api.put.mockResolvedValue({ sync: { generated: true, ready: true, version: 1, test_pack_built: true } })
    setup()
    fireEvent.click(within(await screen.findByText('item(s) need your input', { exact: false }).then(e => e.closest('.card'))).getByRole('button', { name: 'Edit' }))
    expect(await screen.findByText('How is NET_AMOUNT built?')).toBeInTheDocument()
    fireEvent.change(screen.getByLabelText('Rule for NET_AMOUNT'), { target: { value: 'ROUND(SUM(transactions.AMOUNT - COALESCE(transactions.DISCOUNT, 0)), 2)' } })
    await act(async () => fireEvent.click(screen.getByRole('button', { name: 'Save rule' })))
    await waitFor(() => expect(api.put).toHaveBeenCalled())
    const [path, body] = api.put.mock.calls[0]
    expect(path).toBe('/runs/RUN-1/target-spec/columns/NET_AMOUNT')
    expect(body.expression).toContain('COALESCE')
    expect(body.resolve).toEqual({ 'GAP-1': { status: 'CLARIFIED', note: '' } })
    expect(await screen.findByText(/SQL v1 generated and the test pack built/)).toBeInTheDocument()
  })

  it('a rejected rule shows the server message inside the editor', async () => {
    api.put.mockRejectedValue(new Error('Column NOPE not found in any source dataset'))
    setup()
    fireEvent.click(within(await screen.findByText('item(s) need your input', { exact: false }).then(e => e.closest('.card'))).getByRole('button', { name: 'Edit' }))
    await act(async () => fireEvent.click(screen.getByRole('button', { name: 'Save rule' })))
    expect((await screen.findAllByText('Column NOPE not found in any source dataset')).length).toBeGreaterThan(0)
    expect(screen.getByText('How is NET_AMOUNT built?')).toBeInTheDocument()
  })

  it('when everything is clear it says so and offers the next step', async () => {
    setup(spec({ ready: true, open_items: [], sql: { state: 'CURRENT', version: 2 }, columns: [col('CUSTOMER_ID')] }))
    expect(await screen.findByText('All requirements are clear.')).toBeInTheDocument()
    expect(screen.getByText(/SQL v2 was generated from them/)).toBeInTheDocument()
  })
})
