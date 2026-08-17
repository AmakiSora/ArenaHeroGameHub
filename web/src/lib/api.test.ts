import { afterEach, describe, expect, it, vi } from 'vitest'
import { api, apiURL, setCSRF } from './api'

describe('API URL', () => {
  it('keeps dashboard-proxied requests relative', () => {
    expect(apiURL('/api/v1/me', '')).toBe('/api/v1/me')
  })

  it('supports an explicit API origin without a duplicate slash', () => {
    expect(apiURL('/api/v1/me', 'https://api.arenahero.io/')).toBe('https://api.arenahero.io/api/v1/me')
  })
})

describe('manual command API', () => {
  afterEach(() => {
    vi.restoreAllMocks()
    vi.unstubAllEnvs()
  })

  it('loads the public leaderboard without authentication headers', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response(JSON.stringify({ beacon_ticks_held: [], damage_dealt: [], core_destruction_participations: [] }), { status: 200, headers: { 'Content-Type': 'application/json' } }))

    await api.leaderboard()

    const [path, init] = fetchMock.mock.calls[0]
    expect(path).toBe('/api/v1/leaderboard')
    expect(new Headers(init?.headers).has('Authorization')).toBe(false)
  })

  it('posts the plan with a unique idempotency key and the CSRF token', async () => {
    setCSRF('csrf-test')
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response(JSON.stringify({ accepted: true, tick: 7, source: 'MANUAL', received_at: '2026-07-15T00:00:00Z' }), { status: 202, headers: { 'Content-Type': 'application/json' } }))
    const plan = { tick: 7, unit_actions: { unit: { type: 'WAIT' as const } } }
    await api.submitCommands(plan)
    const [path, init] = fetchMock.mock.calls[0]
    const headers = new Headers(init?.headers)
    expect(path).toBe('/api/v1/game/commands')
    expect(headers.get('Idempotency-Key')).toMatch(/[0-9a-f-]{36}/)
    expect(headers.get('X-CSRF-Token')).toBe('csrf-test')
    expect(JSON.parse(init?.body as string)).toEqual(plan)
  })

  it('falls back to a generated idempotency key outside secure contexts', async () => {
    // The dashboard is served over plain http, where crypto.randomUUID does
    // not exist; command submissions must still work (this used to throw
    // before fetch ran, surfacing as REQUEST_FAILED with zero server logs).
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response(JSON.stringify({ accepted: true, tick: 7, source: 'MANUAL', received_at: '2026-07-15T00:00:00Z' }), { status: 202, headers: { 'Content-Type': 'application/json' } }))
    vi.stubGlobal('crypto', { randomUUID: undefined })
    try {
      await api.submitCommands({ tick: 7, unit_actions: {} })
    } finally {
      vi.unstubAllGlobals()
    }
    const [, init] = fetchMock.mock.calls[0]
    const key = new Headers(init?.headers).get('Idempotency-Key') ?? ''
    expect(key.length).toBeGreaterThanOrEqual(8)
    expect(key).toMatch(/^[\x21-\x7e]+$/)
  })

  it('surfaces the browser failure reason when fetch itself throws', async () => {
    vi.spyOn(globalThis, 'fetch').mockRejectedValue(new TypeError('Failed to fetch'))
    await expect(api.submitCommands({ tick: 7, unit_actions: {} })).rejects.toThrow('REQUEST_FAILED Failed to fetch')
  })

  it('stores the CSRF token returned by the session login', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response(JSON.stringify({ csrf_token: 'csrf-new', expires_at: '2026-08-18T00:00:00Z', username: 'operator' }), { status: 201, headers: { 'Content-Type': 'application/json' } }))

    await api.login('player@example.com', 'secret')

    const [path, init] = fetchMock.mock.calls[0]
    expect(path).toBe('/api/v1/auth/login')
    expect(JSON.parse(init?.body as string)).toEqual({ email: 'player@example.com', password: 'secret' })
    expect(localStorage.getItem('arena-hero.csrf')).toBe('csrf-new')
  })

  it('posts pasted cookies and stores the CSRF token via the session importer', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response(JSON.stringify({ ok: true, user: { username: 'operator' }, csrf_token: 'csrf-imported' }), { status: 200, headers: { 'Content-Type': 'application/json' } }))

    await api.importSession('arena_session=abc; other=1', 'csrf-imported')

    const [path, init] = fetchMock.mock.calls[0]
    expect(path).toBe('/api/v1/session/import')
    expect(JSON.parse(init?.body as string)).toEqual({ cookies: 'arena_session=abc; other=1', csrf: 'csrf-imported' })
    expect(localStorage.getItem('arena-hero.csrf')).toBe('csrf-imported')
  })
})
