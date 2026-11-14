import { useCallback, useEffect, useState } from 'react'
import { loadHolds } from '../lib/holds'
import { sameSet } from '../lib/stableState'
import { useTickLoader } from './useTickLoader'

// Manual per-unit hold position (驻守) from the tactic dashboard (/api/holds).
// Refreshed on every new Tick and on demand right after a mutation so the
// dialog reflects the toggled state.
export function useHolds(tick: number | null, enabled = true) {
  const [holds, setHolds] = useState<Set<string>>(new Set())
  const [epoch, setEpoch] = useState(0)
  useTickLoader(tick, enabled, loadHolds, (next) => setHolds((current) => sameSet(current, next) ? current : next), epoch)
  useEffect(() => { if (!enabled) setHolds(new Set()) }, [enabled])
  const refresh = useCallback(() => setEpoch((value) => value + 1), [])
  return { holds, refresh }
}
