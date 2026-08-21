// Strategy settings surfaced by the arena's 配置 dialog — the tactic
// dashboard's 核心 / 运行 / 生产 config groups. Everything else is already
// editable in the arena (worker strategy + squad settings in the sidebar,
// rosters via drag & drop, manual targets in the unit dialog) and stays out
// of this panel to avoid duplicate controls.
export type StrategyFieldValue = number | boolean | string

export type StrategyConfigValues = Record<string, StrategyFieldValue>

export type StrategyField =
  | { kind: 'switch'; field: string; labelKey: string }
  | { kind: 'number'; field: string; labelKey: string; min: number; max: number; step?: number }

export interface StrategyGroupSpec {
  key: string
  titleKey: string
  fields: StrategyField[]
}

// Labels/ranges mirror CONFIG_FIELDS in tactic_config.py; the server
// re-validates every value, so these bounds are a convenience clamp.
export const STRATEGY_GROUPS: StrategyGroupSpec[] = [
  {
    key: 'core',
    titleKey: 'coreGroup',
    fields: [
      { kind: 'switch', field: 'core_movement_enabled', labelKey: 'coreMovementEnabled' },
      { kind: 'switch', field: 'prefer_resources_for_core', labelKey: 'preferResourcesForCore' },
      { kind: 'switch', field: 'core_target_enabled', labelKey: 'coreTargetEnabled' },
      { kind: 'number', field: 'core_target_x', labelKey: 'coreTargetX', min: -1000, max: 1000 },
      { kind: 'number', field: 'core_target_y', labelKey: 'coreTargetY', min: -1000, max: 1000 },
      { kind: 'number', field: 'cargo_wait_distance', labelKey: 'cargoWaitDistance', min: 0, max: 20 },
      { kind: 'switch', field: 'repair_enabled', labelKey: 'repairEnabled' },
      { kind: 'switch', field: 'heal_enabled', labelKey: 'healEnabled' },
      { kind: 'number', field: 'peace_shield_target', labelKey: 'peaceShieldTarget', min: 0, max: 10 },
      { kind: 'number', field: 'combat_shield_target', labelKey: 'combatShieldTarget', min: 0, max: 10 },
      { kind: 'number', field: 'resource_reserve', labelKey: 'resourceReserve', min: 0, max: 100 },
    ],
  },
  {
    key: 'runtime',
    titleKey: 'runtimeGroup',
    fields: [
      { kind: 'number', field: 'map_save_interval_ticks', labelKey: 'mapSaveInterval', min: 1, max: 200 },
    ],
  },
  {
    key: 'production',
    titleKey: 'productionGroup',
    fields: [
      { kind: 'number', field: 'target_workers', labelKey: 'targetWorkers', min: 0, max: 100 },
      { kind: 'number', field: 'target_vanguards', labelKey: 'targetVanguards', min: 0, max: 100 },
      { kind: 'number', field: 'target_rangers', labelKey: 'targetRangers', min: 0, max: 100 },
    ],
  },
]

export async function loadStrategyValues(): Promise<StrategyConfigValues> {
  try {
    const response = await fetch('/api/config', { credentials: 'same-origin' })
    if (!response.ok) return {}
    const data = await response.json() as { ok?: boolean; config?: Record<string, StrategyFieldValue> }
    if (data?.ok !== true || !data.config || typeof data.config !== 'object') return {}
    return data.config
  } catch {
    return {}
  }
}

// Saving reuses teamRoster's saveStrategyConfig: same /api/config partial
// merge (only the keys present are updated, server answers 400 on invalid).
export { saveStrategyConfig as saveStrategyValues } from './teamRoster'
