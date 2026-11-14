import { useEffect, useState } from 'react'
import { loadEnemyMemory, type EnemySighting } from '../lib/enemyMemory'
import { sameJSON } from '../lib/stableState'
import { useTickLoader } from './useTickLoader'

// Remembered enemies from the tactic bot's map memory; refreshed on every new
// tick so a confirmed kill or re-scout retires its stale marker promptly.
// Demo mode ships a fixed pair of markers so the feature stays visible.
const DEMO_SIGHTINGS: EnemySighting[] = [
  { position: [8, 4], type: 'VANGUARD', tick: 10570 },
  { position: [-6, 9], type: 'CORE', tick: 10581 },
]

export function useEnemyMemory(tick: number | null, enabled = true): EnemySighting[] {
  const [sightings, setSightings] = useState<EnemySighting[]>([])
  useTickLoader(tick, enabled, loadEnemyMemory, (next) => setSightings((current) => sameJSON(current, next) ? current : next))
  useEffect(() => { if (!enabled) setSightings(DEMO_SIGHTINGS) }, [enabled])
  return sightings
}
