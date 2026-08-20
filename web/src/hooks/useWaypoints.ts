import { useCallback, useEffect, useState } from 'react'
import { loadWaypoints, type WaypointMap } from '../lib/waypoints'

// Manual per-unit target queues from the tactic dashboard (/api/waypoints).
// Refreshed on every new Tick — the bot clears targets as units arrive — and
// on demand right after a mutation so the dialog reflects the new queue.
export function useWaypoints(tick: number | null, enabled = true) {
  const [waypoints, setWaypoints] = useState<WaypointMap>({})
  const [epoch, setEpoch] = useState(0)
  useEffect(() => {
    if (!enabled) { setWaypoints({}); return }
    let cancelled = false
    void loadWaypoints().then((next) => { if (!cancelled) setWaypoints(next) })
    return () => { cancelled = true }
  }, [tick, epoch, enabled])
  const refresh = useCallback(() => setEpoch((value) => value + 1), [])
  return { waypoints, refresh }
}
