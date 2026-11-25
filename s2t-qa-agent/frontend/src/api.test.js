import { afterEach, describe, expect, it, vi } from 'vitest'
import { api } from './api.js'

function mockFetch(status, body) {
  const fn = vi.fn().mockResolvedValue({ ok: status < 400, status, statusText: 'Status', text: async () => (body === undefined ? '' : JSON.stringify(body)) })
  vi.stubGlobal('fetch', fn)
  return fn
}

afterEach(() => { vi.unstubAllGlobals(); localStorage.clear() })

describe('api client', () => {
  it('sends JSON to /api and parses the reply', async () => {
    const f = mockFetch(200, { id: 'RUN-1' })
    await expect(api.post('/runs', { jira_key: 'DATA-1' })).resolves.toEqual({ id: 'RUN-1' })
    const [url, opts] = f.mock.calls[0]
    expect(url).toBe('/api/runs')
    expect(opts.method).toBe('POST')
    expect(opts.headers['Content-Type']).toBe('application/json')
    expect(JSON.parse(opts.body)).toEqual({ jira_key: 'DATA-1' })
    expect(opts.headers.Authorization).toBeUndefined()
  })

  it('adds the bearer token when one is stored', async () => {
    localStorage.setItem('qa_token', 'abc')
    const f = mockFetch(200, {})
    await api.get('/meta')
    expect(f.mock.calls[0][1].headers.Authorization).toBe('Bearer abc')
  })

  it('throws the server detail as the error message, with the status', async () => {
    mockFetch(409, { detail: 'The requirements are not complete' })
    await expect(api.post('/runs/R/test-pack')).rejects.toMatchObject({ message: 'The requirements are not complete', status: 409 })
  })

  it('serialises structured validation errors', async () => {
    mockFetch(422, { detail: [{ loc: ['body', 'jira_key'], msg: 'too short' }] })
    await expect(api.post('/runs', {})).rejects.toThrow('too short')
  })

  it('handles empty replies', async () => {
    mockFetch(200)
    await expect(api.del('/runs/R/datasets/D')).resolves.toBeNull()
  })
})
