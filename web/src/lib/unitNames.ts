import type { WorldObject } from './types'

// Display names assigned by the tactic dashboard (W1/V2/R3...), keyed by the
// short unit id. The dashboard serves them from /api/unit-names; the arena
// page uses them so both surfaces label the same unit identically.
export type UnitNameMap = Record<string, string>

// The tactic records key units by str(object_id)[:8] (dashboard.short_id).
export function unitShortId(id: string) {
  return id.slice(0, 8)
}

export function unitDashboardName(object: WorldObject, names: UnitNameMap): string | undefined {
  if (object.kind !== 'UNIT' || !object.id) return undefined
  return names[unitShortId(object.id)]
}

export async function loadUnitNames(): Promise<UnitNameMap> {
  try {
    const response = await fetch('/api/unit-names', { credentials: 'same-origin' })
    if (!response.ok) return {}
    const data = await response.json() as { ok?: boolean; names?: unknown }
    if (!data || data.ok !== true || !data.names || typeof data.names !== 'object') return {}
    const names: UnitNameMap = {}
    for (const [id, name] of Object.entries(data.names as Record<string, unknown>)) {
      if (id && name && typeof id === 'string' && typeof name === 'string') names[id] = name
    }
    return names
  } catch {
    // The dashboard endpoint is unavailable outside the deployed proxy (dev
    // server / demo) — fall back to the plain type-based labels.
    return {}
  }
}
