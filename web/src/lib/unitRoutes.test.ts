import { afterEach, describe, expect, it, vi } from 'vitest'
import { loadUnitRoutes, RANGER_ROUTE_COLOR, unitRouteColors, VANGUARD_ROUTE_COLOR, WORKER_ROUTE_COLORS, type UnitRoute } from './unitRoutes'

const respond = (payload: unknown, ok = true) => vi.fn(async () => ({
  ok,
  json: async () => payload,
}) as Response)

describe('loadUnitRoutes', () => {
  afterEach(() => vi.unstubAllGlobals())

  it('normalizes destinations, paths and completion flags', async () => {
    vi.stubGlobal('fetch', respond({
      ok: true,
      tick: 42,
      units: [
        { name: 'W1', type: 'WORKER', pos: [0, 0], target: [3, -2], path: [[0, 0], [1, -1], [3, -2]], complete: false },
        { name: 'V1', type: 'VANGUARD', pos: [2, 2], target: [5, 5], path: [[5, 5]], complete: true },
        { name: 'R1', type: 'RANGER', pos: [1, 1], target: null, path: [], complete: false },
        { name: '', type: 'WORKER', target: [1, 1], path: [], complete: true },
        { name: 'BAD', type: 'WORKER', target: ['x'], path: [[1], 'junk', [2, 3], [4, 5]], complete: false },
      ],
    }))
    await expect(loadUnitRoutes()).resolves.toEqual([
      { name: 'W1', type: 'WORKER', target: [3, -2], path: [[0, 0], [1, -1], [3, -2]], complete: false },
      { name: 'V1', type: 'VANGUARD', target: [5, 5], path: [[5, 5]], complete: true },
      { name: 'BAD', type: 'WORKER', target: null, path: [[2, 3], [4, 5]], complete: false },
    ])
  })

  it('tolerates request failures and malformed payloads', async () => {
    vi.stubGlobal('fetch', respond({}, false))
    await expect(loadUnitRoutes()).resolves.toEqual([])
    vi.stubGlobal('fetch', respond({ ok: false, units: [] }))
    await expect(loadUnitRoutes()).resolves.toEqual([])
    vi.stubGlobal('fetch', vi.fn(async () => { throw new Error('network') }))
    await expect(loadUnitRoutes()).resolves.toEqual([])
  })
})

describe('unitRouteColors', () => {
  const route = (name: string, type: UnitRoute['type']): UnitRoute => ({ name, type, target: null, path: [], complete: true })

  it('cycles the worker palette by index and fixes combat colors', () => {
    const routes = [...Array.from({ length: WORKER_ROUTE_COLORS.length + 1 }, (_, index) => route(`W${index + 1}`, 'WORKER')), route('V1', 'VANGUARD'), route('R1', 'RANGER')]
    const colors = unitRouteColors(routes)
    expect(colors.get('W1')).toBe(WORKER_ROUTE_COLORS[0])
    expect(colors.get(`W${WORKER_ROUTE_COLORS.length + 1}`)).toBe(WORKER_ROUTE_COLORS[0])
    expect(colors.get('V1')).toBe(VANGUARD_ROUTE_COLOR)
    expect(colors.get('R1')).toBe(RANGER_ROUTE_COLOR)
  })
})
