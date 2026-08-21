import { afterEach, describe, expect, it, vi } from 'vitest'
import { loadStrategyValues, STRATEGY_GROUPS } from './strategyConfig'

const respond = (payload: unknown, ok = true) => vi.fn(async () => ({
  ok,
  json: async () => payload,
}) as Response)

describe('loadStrategyValues', () => {
  afterEach(() => vi.unstubAllGlobals())

  it('returns the config map from /api/config', async () => {
    const config = { core_movement_enabled: true, target_workers: 8, attack_mode: 'auto' }
    vi.stubGlobal('fetch', respond({ ok: true, config }))
    await expect(loadStrategyValues()).resolves.toEqual(config)
  })

  it('tolerates request failures and malformed payloads', async () => {
    vi.stubGlobal('fetch', respond({}, false))
    await expect(loadStrategyValues()).resolves.toEqual({})
    vi.stubGlobal('fetch', respond({ ok: false, config: { repair_enabled: true } }))
    await expect(loadStrategyValues()).resolves.toEqual({})
    vi.stubGlobal('fetch', vi.fn(async () => { throw new Error('network') }))
    await expect(loadStrategyValues()).resolves.toEqual({})
  })
})

describe('STRATEGY_GROUPS', () => {
  it('covers core/runtime/production without duplicating sidebar-owned fields', () => {
    expect(STRATEGY_GROUPS.map((group) => group.key)).toEqual(['core', 'runtime', 'production'])
    const fields = STRATEGY_GROUPS.flatMap((group) => group.fields.map((field) => field.field))
    // Worker strategy and squad settings already live in the sidebar panels.
    for (const owned of ['worker_bfs_enabled', 'home_patrol_radius', 'attack_mode', 'attack_target_x', 'home_team', 'ranger_attack_range']) {
      expect(fields).not.toContain(owned)
    }
    expect(new Set(fields).size).toBe(fields.length)
  })
})
