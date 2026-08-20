import { afterEach, describe, expect, it, vi } from 'vitest'
import { addUnitWaypoint, loadWaypoints, removeUnitWaypoint } from './waypoints'

const respond = (payload: unknown, ok = true) => vi.fn(async () => ({
  ok,
  json: async () => payload,
}) as Response)

describe('loadWaypoints', () => {
  afterEach(() => vi.unstubAllGlobals())

  it('normalizes queues and march modes', async () => {
    vi.stubGlobal('fetch', respond({
      ok: true,
      waypoints: {
        W1: { queue: [[3, -2], [4, 0]], mode: 'attack' },
        V2: { queue: [[1, 1]], mode: 'rush' },
        R3: { queue: [], mode: 'attack' },
        BAD: { queue: [[1], 'junk'], mode: 'unknown' },
      },
    }))
    await expect(loadWaypoints()).resolves.toEqual({
      W1: { queue: [[3, -2], [4, 0]], mode: 'attack' },
      V2: { queue: [[1, 1]], mode: 'rush' },
    })
  })

  it('tolerates request failures', async () => {
    vi.stubGlobal('fetch', respond({}, false))
    await expect(loadWaypoints()).resolves.toEqual({})
    vi.stubGlobal('fetch', vi.fn(async () => { throw new Error('network') }))
    await expect(loadWaypoints()).resolves.toEqual({})
  })
})

const fetchResponse = { ok: true, json: async () => ({ ok: true }) } as Response
const fetchCalls = (mock: ReturnType<typeof vi.fn>) => mock.mock.calls as unknown as [string, RequestInit][]

describe('waypoint mutations', () => {
  afterEach(() => vi.unstubAllGlobals())

  it('posts the append body and reports success', async () => {
    const fetchMock = vi.fn(async () => fetchResponse)
    vi.stubGlobal('fetch', fetchMock)
    await expect(addUnitWaypoint('W1', 7, -3, 'attack')).resolves.toBe(true)
    const [path, init] = fetchCalls(fetchMock)[0]
    expect(path).toBe('/api/waypoint/set')
    expect(JSON.parse(init.body as string)).toEqual({ name: 'W1', x: 7, y: -3, mode: 'attack' })
  })

  it('removes one target by index or the whole queue', async () => {
    const fetchMock = vi.fn(async () => fetchResponse)
    vi.stubGlobal('fetch', fetchMock)
    await removeUnitWaypoint('V2', 1)
    await removeUnitWaypoint('V2')
    const calls = fetchCalls(fetchMock)
    expect(JSON.parse(calls[0][1].body as string)).toEqual({ name: 'V2', index: 1 })
    expect(JSON.parse(calls[1][1].body as string)).toEqual({ name: 'V2' })
  })

  it('reports failure when the server rejects', async () => {
    vi.stubGlobal('fetch', respond({ ok: false, error: '目标不存在' }))
    await expect(addUnitWaypoint('W1', 0, 0, 'rush')).resolves.toBe(false)
    vi.stubGlobal('fetch', respond({ ok: true }, false))
    await expect(removeUnitWaypoint('W1')).resolves.toBe(false)
  })
})
