import { useEffect, useState } from 'react'
import { loadUnitRoutes, type UnitRoute } from '../lib/unitRoutes'
import { sameJSON } from '../lib/stableState'

// Per-unit destinations + remaining paths from the tactic bot's newest tick
// (/api/unit-routes). Refreshed on every new Tick while the layer is
// enabled; the arena page only enables it while the filter is on.
export function useUnitRoutes(tick: number | null, enabled = true) {
  const [routes, setRoutes] = useState<UnitRoute[]>([])
  useEffect(() => {
    if (!enabled) { setRoutes([]); return }
    let cancelled = false
    void loadUnitRoutes().then((next) => { if (!cancelled) setRoutes((current) => sameJSON(current, next) ? current : next) })
    return () => { cancelled = true }
  }, [tick, enabled])
  return routes
}
