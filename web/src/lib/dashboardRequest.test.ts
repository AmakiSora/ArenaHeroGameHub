import { afterEach, describe, expect, it, vi } from 'vitest'
import { dashboardRequest } from './dashboardRequest'

const jsonResponse = (payload: unknown, status = 200) => new Response(JSON.stringify(payload), { status, headers: { 'Content-Type': 'application/json' } })

describe('dashboardRequest', () => {
  afterEach(() => { vi.restoreAllMocks(); vi.unstubAllGlobals() })

  it('returns the parsed payload on success with same-origin credentials', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(jsonResponse({ ok: true, value: 7 }))
    await expect(dashboardRequest<{ ok: boolean; value: number }>('/api/example')).resolves.toEqual({ ok: true, data: { ok: true, value: 7 } })
    expect(fetchMock).toHaveBeenCalledWith('/api/example', expect.objectContaining({ credentials: 'same-origin', method: 'GET' }))
  })

  it('distinguishes an HTTP failure (401/500) from an empty payload', async () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(jsonResponse({ error: 'UNAUTHORIZED' }, 401))

    const result = await dashboardRequest('/api/example')
    expect(result).toEqual({ ok: false, failure: { kind: 'http', path: '/api/example', status: 401, error: 'UNAUTHORIZED' } })
    expect(warn).toHaveBeenCalledWith('[dashboard] request failed', '/api/example', 401)
  })

  it('distinguishes a transport failure from an empty payload', async () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
    vi.spyOn(globalThis, 'fetch').mockRejectedValue(new TypeError('Failed to fetch'))

    const result = await dashboardRequest('/api/example')
    expect(result).toEqual({ ok: false, failure: { kind: 'network', path: '/api/example', error: 'Failed to fetch' } })
    expect(warn).toHaveBeenCalled()
  })

  it('reports an aborted request as such without warning', async () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
    const controller = new AbortController()
    controller.abort()
    vi.spyOn(globalThis, 'fetch').mockRejectedValue(new DOMException('Aborted', 'AbortError'))

    await expect(dashboardRequest('/api/example', { signal: controller.signal })).resolves.toEqual({ ok: false, failure: { kind: 'aborted', path: '/api/example' } })
    expect(warn).not.toHaveBeenCalled()
  })

  it('applies the deadline to a hung request', async () => {
    vi.spyOn(console, 'warn').mockImplementation(() => {})
    vi.spyOn(globalThis, 'fetch').mockImplementation((_url, init) => new Promise<Response>((_resolve, reject) => {
      const signal = (init as RequestInit).signal
      signal?.addEventListener('abort', () => reject(new DOMException('Aborted', 'AbortError')))
    }))

    const result = await dashboardRequest('/api/example', { timeoutMs: 5 })
    expect(result.ok).toBe(false)
    if (!result.ok) expect(result.failure.kind).toBe('network')
  })

  it('serialises a JSON POST body', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(jsonResponse({ ok: true }))
    await expect(dashboardRequest<{ ok: boolean }>('/api/teams', { method: 'POST', body: { home_team: 'V1' } })).resolves.toEqual({ ok: true, data: { ok: true } })
    const init = fetchMock.mock.calls[0][1] as RequestInit
    expect(init.method).toBe('POST')
    expect(init.body).toBe(JSON.stringify({ home_team: 'V1' }))
    expect(init.headers).toMatchObject({ 'Content-Type': 'application/json' })
  })
})
