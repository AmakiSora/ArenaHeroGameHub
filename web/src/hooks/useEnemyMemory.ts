import { useEffect, useState } from 'react'
import { loadEnemyMemory, type EnemySighting } from '../lib/enemyMemory'

// Remembered enemies from the tactic bot's map memory; refreshed on every new
// tick so a confirmed kill or re-scout retires its stale marker promptly.
// Demo mode ships a fixed pair of markers so the feature stays visible.
const DEMO_SIGHTINGS: EnemySighting[] = [
  { position: [8, 4], type: 'VANGUARD' },
  { position: [-6, 9], type: 'CORE' },
]

export function useEnemyMemory(tick: number | null, enabled = true): EnemySighting[] {
  const [sightings, setSightings] = useState<EnemySighting[]>([])
  useEffect(() => {
    if (!enabled) { setSightings(DEMO_SIGHTINGS); return }
    let cancelled = false
    void loadEnemyMemory().then((next) => { if (!cancelled) setSightings(next) })
    return () => { cancelled = true }
  }, [tick, enabled])
  return sightings
}
