import { useEffect, useState } from 'react'
import { loadUnitRoutes, type UnitRoute } from '../lib/unitRoutes'
import { sameJSON } from '../lib/stableState'
import { useTickLoader } from './useTickLoader'

// Per-unit destinations + remaining paths from the tactic bot's newest tick
// (/api/unit-routes). Refreshed on every new Tick while the layer is
// enabled; the arena page only enables it while the filter is on.
export function useUnitRoutes(tick: number | null, enabled = true) {
  const [routes, setRoutes] = useState<UnitRoute[]>([])
  useTickLoader(tick, enabled, loadUnitRoutes, (next) => setRoutes((current) => sameJSON(current, next) ? current : next))
  useEffect(() => { if (!enabled) setRoutes([]) }, [enabled])
  return routes
}
