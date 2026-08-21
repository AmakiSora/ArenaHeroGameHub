import { useCallback, useEffect, useState } from 'react'
import { loadHolds } from '../lib/holds'

// Manual per-unit hold position (驻守) from the tactic dashboard (/api/holds).
// Refreshed on every new Tick and on demand right after a mutation so the
// dialog reflects the toggled state.
export function useHolds(tick: number | null, enabled = true) {
  const [holds, setHolds] = useState<Set<string>>(new Set())
  const [epoch, setEpoch] = useState(0)
  useEffect(() => {
    if (!enabled) { setHolds(new Set()); return }
    let cancelled = false
    void loadHolds().then((next) => { if (!cancelled) setHolds(next) })
    return () => { cancelled = true }
  }, [tick, epoch, enabled])
  const refresh = useCallback(() => setEpoch((value) => value + 1), [])
  return { holds, refresh }
}
