import { afterEach, describe, expect, it, vi } from 'vitest'
import { addSquadTarget, clearSquadTargets, loadSquadTargets, removeSquadTarget } from './squadTargets'

const respond = (payload: unknown, ok = true) => vi.fn(async () => ({
  ok,
  json: async () => payload,
}) as Response)

describe('loadSquadTargets', () => {
  afterEach(() => vi.unstubAllGlobals())

  it('normalizes the per-squad queues', async () => {
    vi.stubGlobal('fetch', respond({
      ok: true,
      targets: {
        attack: [[-2000, -2000], [7, 2]],
        kite: [[1, 1]],
        bogus: [[9, 9]],
        BAD: [[1], 'junk'],
      },
    }))
    await expect(loadSquadTargets()).resolves.toEqual({
      attack: [[-2000, -2000], [7, 2]],
      kite: [[1, 1]],
    })
  })

  it('tolerates request failures', async () => {
    vi.stubGlobal('fetch', respond({}, false))
    await expect(loadSquadTargets()).resolves.toEqual({})
    vi.stubGlobal('fetch', vi.fn(async () => { throw new Error('network') }))
    await expect(loadSquadTargets()).resolves.toEqual({})
  })
})

const fetchResponse = { ok: true, json: async () => ({ ok: true }) } as Response
const fetchCalls = (mock: ReturnType<typeof vi.fn>) => mock.mock.calls as unknown as [string, RequestInit][]

describe('squad target mutations', () => {
  afterEach(() => vi.unstubAllGlobals())

  it('posts the append body and reports success', async () => {
    const fetchMock = vi.fn(async () => fetchResponse)
    vi.stubGlobal('fetch', fetchMock)
    await expect(addSquadTarget('attack', -2000, -2000)).resolves.toBe(true)
    const [path, init] = fetchCalls(fetchMock)[0]
    expect(path).toBe('/api/squad-target/add')
    expect(JSON.parse(init.body as string)).toEqual({ squad: 'attack', x: -2000, y: -2000 })
  })

  it('removes one target by index or clears the whole queue', async () => {
    const fetchMock = vi.fn(async () => fetchResponse)
    vi.stubGlobal('fetch', fetchMock)
    await removeSquadTarget('kite', 1)
    await clearSquadTargets('kite')
    const calls = fetchCalls(fetchMock)
    expect(calls[0][0]).toBe('/api/squad-target/remove')
    expect(JSON.parse(calls[0][1].body as string)).toEqual({ squad: 'kite', index: 1 })
    expect(calls[1][0]).toBe('/api/squad-target/clear')
    expect(JSON.parse(calls[1][1].body as string)).toEqual({ squad: 'kite' })
  })

  it('reports failure when the server rejects', async () => {
    vi.stubGlobal('fetch', respond({ ok: false, error: '目标队列已达上限 20 个' }))
    await expect(addSquadTarget('attack', 0, 0)).resolves.toBe(false)
    vi.stubGlobal('fetch', respond({ ok: true }, false))
    await expect(removeSquadTarget('kite', 0)).resolves.toBe(false)
  })
})
