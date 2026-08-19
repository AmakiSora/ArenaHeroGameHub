import { afterEach, describe, expect, it, vi } from 'vitest'
import { loadEnemyMemory } from './enemyMemory'

const respond = (payload: unknown, ok = true) => vi.fn(async () => ({
  ok,
  json: async () => payload,
}) as Response)

describe('loadEnemyMemory', () => {
  afterEach(() => vi.unstubAllGlobals())

  it('normalizes sightings including the last-seen tick', async () => {
    vi.stubGlobal('fetch', respond({
      ok: true,
      sightings: [
        { pos: [3, -2], type: 'CORE', tick: 512 },
        { pos: [0, 1], type: 'unknown-legacy', tick: 7 },
        { pos: [9, 9], type: 'RANGER' },
      ],
    }))

    await expect(loadEnemyMemory()).resolves.toEqual([
      { position: [3, -2], type: 'CORE', tick: 512 },
      { position: [0, 1], type: 'ENEMY', tick: 7 },
      { position: [9, 9], type: 'RANGER', tick: 0 },
    ])
  })

  it('drops malformed entries and tolerates request failures', async () => {
    vi.stubGlobal('fetch', respond({
      ok: true,
      sightings: [{ pos: [1], type: 'CORE' }, null, { pos: [4, 4], type: 'WORKER', tick: 'bad' }, 'junk'],
    }))
    await expect(loadEnemyMemory()).resolves.toEqual([
      { position: [4, 4], type: 'WORKER', tick: 0 },
    ])

    vi.stubGlobal('fetch', respond({}, false))
    await expect(loadEnemyMemory()).resolves.toEqual([])

    vi.stubGlobal('fetch', vi.fn(async () => { throw new Error('network') }))
    await expect(loadEnemyMemory()).resolves.toEqual([])
  })
})
