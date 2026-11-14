import { useCallback, useEffect, useState } from 'react'
import { loadWaypoints, type WaypointMap } from '../lib/waypoints'
import { sameJSON } from '../lib/stableState'
import { useTickLoader } from './useTickLoader'

// Manual per-unit target queues from the tactic dashboard (/api/waypoints).
// Refreshed on every new Tick — the bot clears targets as units arrive — and
// on demand right after a mutation so the dialog reflects the new queue.
export function useWaypoints(tick: number | null, enabled = true) {
  const [waypoints, setWaypoints] = useState<WaypointMap>({})
  const [epoch, setEpoch] = useState(0)
  useTickLoader(tick, enabled, loadWaypoints, (next) => setWaypoints((current) => sameJSON(current, next) ? current : next), epoch)
  useEffect(() => { if (!enabled) setWaypoints({}) }, [enabled])
  const refresh = useCallback(() => setEpoch((value) => value + 1), [])
  return { waypoints, refresh }
}
