import { useState } from 'react'
import { loadUnitNames, type UnitNameMap } from '../lib/unitNames'
import { sameRecord } from '../lib/stableState'
import { useTickLoader } from './useTickLoader'

// The tactic assigns the dashboard display names (W1/V2/R3) once per Tick and
// writes them into the tick record, so refresh on every new Tick — this also
// picks up freshly spawned units the moment the tactic names them.
export function useUnitNames(tick: number | null, enabled = true) {
  const [names, setNames] = useState<UnitNameMap>({})
  useTickLoader(tick, enabled, loadUnitNames, (next) => setNames((current) => sameRecord(current, next) ? current : next))
  return names
}
