import { describe, expect, it } from 'vitest'
import { FLOW, stepState } from './flow.js'
import { baseRun } from '../../test/render.jsx'

describe('straight flow', () => {
  it('has four numbered steps in order', () => {
    expect(FLOW.map(s => `${s.n}:${s.key}`)).toEqual(['1:requirements', '2:studio', '3:tests', '4:results'])
  })

  it('a step is done only when its condition holds', () => {
    const open = stepState(baseRun({ requirements_ready: false, transformation_version: null, approvals: { TRANSFORMATION: { status: 'PENDING' } },
      counts: { open_items: 2, executions: 0, reports: 0 } }))
    expect(Object.values(open).map(s => s.done)).toEqual([false, false, false, false])
    expect(open.requirements.todo).toBe('2 item(s) still need your input')
    expect(open.studio.todo).toBe('Approve the SQL to continue')

    const done = stepState(baseRun({ counts: { open_items: 0, executions: 1, reports: 1 } }))
    expect(Object.values(done).map(s => s.done)).toEqual([true, true, true, true])
  })

  it('requirements are not done until the SQL exists', () => {
    expect(stepState(baseRun({ transformation_version: null })).requirements.done).toBe(false)
  })
})
