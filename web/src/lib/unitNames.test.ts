import { afterEach, describe, expect, it, vi } from 'vitest'
import type { WorldObject } from './types'
import { loadUnitNames, unitDashboardName, unitShortId } from './unitNames'

const worker: WorldObject = { kind: 'UNIT', id: 'aaaaaaaa-1111-4000-8000-000000000002', unit_type: 'WORKER' }
const core: WorldObject = { kind: 'CORE', id: 'cccccccc-3333-4000-8000-000000000003' }

describe('unitNames', () => {
  afterEach(() => vi.restoreAllMocks())

  it('derives the short id exactly like the tactic records (str(id)[:8])', () => {
    expect(unitShortId('aaaaaaaa-1111-4000-8000')).toBe('aaaaaaaa')
    expect(unitShortId('abc')).toBe('abc')
  })

  it('resolves dashboard names only for units keyed by their short id', () => {
    const names = { aaaaaaaa: 'W3' }
    expect(unitDashboardName(worker, names)).toBe('W3')
    expect(unitDashboardName(core, names)).toBeUndefined()
    expect(unitDashboardName({ ...worker, id: undefined }, names)).toBeUndefined()
    expect(unitDashboardName(worker, {})).toBeUndefined()
  })

  it('loads the name map from the dashboard endpoint', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response(JSON.stringify({ ok: true, tick: 7, names: { aaaaaaaa: 'W1', bbbbbbbb: 'V2' } }), { status: 200, headers: { 'Content-Type': 'application/json' } }))

    await expect(loadUnitNames()).resolves.toEqual({ aaaaaaaa: 'W1', bbbbbbbb: 'V2' })

    expect(fetchMock).toHaveBeenCalledWith('/api/unit-names', { credentials: 'same-origin' })
  })

  it('drops invalid entries from the payload', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response(JSON.stringify({ ok: true, names: { good: 'V1', '': 'X', broken: 5 } }), { status: 200, headers: { 'Content-Type': 'application/json' } }))

    await expect(loadUnitNames()).resolves.toEqual({ good: 'V1' })
  })

  it('returns an empty map when the endpoint is missing or unreachable', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response('not found', { status: 404 }))
    await expect(loadUnitNames()).resolves.toEqual({})

    vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response(JSON.stringify({ ok: true }), { status: 200, headers: { 'Content-Type': 'application/json' } }))
    await expect(loadUnitNames()).resolves.toEqual({})

    vi.spyOn(globalThis, 'fetch').mockRejectedValue(new TypeError('Failed to fetch'))
    await expect(loadUnitNames()).resolves.toEqual({})
  })
})
