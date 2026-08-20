import type { Position } from './types'

// Last-known enemy positions remembered by the tactic bot (map_memory.json),
// served by the dashboard as /api/enemy-memory — the very same data its own
// map draws as the 敌人踪迹 (enemy-trace) layer, minus enemies visible right
// now. Unknown/legacy entries keep the ENEMY sentinel type.
export type EnemySightingType = 'WORKER' | 'VANGUARD' | 'RANGER' | 'CORE' | 'ENEMY'

export interface EnemySighting {
  position: Position
  type: EnemySightingType
  // Last tick an enemy was actually seen here; the server ranks by recency
  // and caps each type separately, this is kept for display/debugging only.
  tick: number
}

const KNOWN_TYPES: ReadonlySet<string> = new Set(['WORKER', 'VANGUARD', 'RANGER', 'CORE'])

export async function loadEnemyMemory(): Promise<EnemySighting[]> {
  try {
    const response = await fetch('/api/enemy-memory', { credentials: 'same-origin' })
    if (!response.ok) return []
    const data = await response.json() as { ok?: boolean; sightings?: Array<{ pos?: number[]; type?: string; tick?: number }> }
    if (data?.ok !== true || !Array.isArray(data.sightings)) return []
    return data.sightings.flatMap((item) => {
      if (!item || !Array.isArray(item.pos) || item.pos.length !== 2) return []
      const rawType = String(item.type ?? '').toUpperCase()
      const type = (KNOWN_TYPES.has(rawType) ? rawType : 'ENEMY') as EnemySightingType
      const tick = Number(item.tick)
      return [{ position: [item.pos[0], item.pos[1]] as Position, type, tick: Number.isFinite(tick) ? tick : 0 }]
    })
  } catch {
    return []
  }
}
