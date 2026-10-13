import { afterEach, describe, expect, it, vi } from 'vitest'
import {
  EMPTY_GUERRILLA_TRACKS,
  guerrillaTrackColors,
  loadGuerrillaTracks,
  tracksBounds,
  type GuerrillaTrack,
} from './guerrillaTracks'
import { WORKER_ROUTE_COLORS } from './unitRoutes'

const respond = (payload: unknown, ok = true) => vi.fn(async () => ({
  ok,
  json: async () => payload,
}) as Response)

const track = (over: Partial<GuerrillaTrack>): GuerrillaTrack => ({
  id: 'aaaa1111',
  name: 'V1',
  type: 'VANGUARD',
  points: [[0, 0], [1, 0], [2, 0]],
  ticks: 3,
  distinct: 3,
  net: 2,
  bounds: [0, 0, 2, 0],
  ...over,
})

describe('loadGuerrillaTracks', () => {
  afterEach(() => vi.unstubAllGlobals())

  it('normalizes the day payload and keeps the served date list', async () => {
    const fetchMock = respond({
      ok: true,
      date: '20261011',
      dates: ['20261011', '20261010'],
      units: [
        { id: 'aaaa1111', name: 'V1', type: 'VANGUARD', points: [[8, -3], [9, -3]], ticks: 400, distinct: 6, net: 1, bounds: [8, -3, 9, -3] },
        { id: 'bbbb2222', name: 'R2', type: 'RANGER', points: [[1, 2], ['x', 2], [3, 4], [4]], ticks: 10, distinct: 2, net: 0, bounds: null },
        { id: '', name: 'V9', type: 'VANGUARD', points: [[0, 0]] },
      ],
    })
    vi.stubGlobal('fetch', fetchMock)
    await expect(loadGuerrillaTracks()).resolves.toEqual({
      date: '20261011',
      dates: ['20261011', '20261010'],
      units: [
        track({ id: 'aaaa1111', points: [[8, -3], [9, -3]], ticks: 400, distinct: 6, net: 1, bounds: [8, -3, 9, -3] }),
        track({ id: 'bbbb2222', name: 'R2', type: 'RANGER', points: [[1, 2], [3, 4]], ticks: 10, distinct: 2, net: 0, bounds: null }),
      ],
    })
    expect(fetchMock).toHaveBeenCalledWith('/api/guerrilla-tracks', expect.anything())
  })

  it('requests a specific day when one is chosen', async () => {
    const fetchMock = respond({ ok: true, date: '20261010', dates: ['20261010'], units: [] })
    vi.stubGlobal('fetch', fetchMock)
    await loadGuerrillaTracks('20261010')
    expect(fetchMock).toHaveBeenCalledWith('/api/guerrilla-tracks?date=20261010', expect.anything())
  })

  it('falls back to the empty payload on failures and junk shapes', async () => {
    vi.stubGlobal('fetch', respond({}, false))
    await expect(loadGuerrillaTracks()).resolves.toEqual(EMPTY_GUERRILLA_TRACKS)
    vi.stubGlobal('fetch', respond({ ok: false, units: [] }))
    await expect(loadGuerrillaTracks()).resolves.toEqual(EMPTY_GUERRILLA_TRACKS)
    vi.stubGlobal('fetch', vi.fn(async () => { throw new Error('network') }))
    await expect(loadGuerrillaTracks()).resolves.toEqual(EMPTY_GUERRILLA_TRACKS)
  })
})

describe('track helpers', () => {
  it('unions every unit bounds so the overview frames the whole day', () => {
    const bounds = tracksBounds([
      track({ id: 'a', bounds: [0, 0, 10, 20] }),
      track({ id: 'b', bounds: [-30, -5, 4, 7] }),
      track({ id: 'c', bounds: null }),
    ])
    expect(bounds).toEqual([-30, -5, 10, 20])
    expect(tracksBounds([])).toBeNull()
  })

  it('cycles the palette per unit id, not per name', () => {
    const units = Array.from({ length: WORKER_ROUTE_COLORS.length + 1 }, (_, index) => track({ id: `id${index}`, name: `V${index}` }))
    const colors = guerrillaTrackColors(units)
    expect(colors.get('id0')).toBe(WORKER_ROUTE_COLORS[0])
    expect(colors.get(`id${WORKER_ROUTE_COLORS.length}`)).toBe(WORKER_ROUTE_COLORS[0])
    expect(colors.get('id1')).not.toBe(colors.get('id2'))
  })
})
