import type { Position } from './types'

// Per-unit destinations and planned routes straight from the tactic bot's
// newest tick log (/api/unit-routes) — the same data the dashboard's 路径
// layer draws: the remaining A* polyline (already trimmed of walked cells)
// plus the current target cell.
export type UnitRouteType = 'WORKER' | 'VANGUARD' | 'RANGER'

export interface UnitRoute {
  name: string
  type: UnitRouteType
  target: Position | null
  path: Position[]
  // False while the unit is still marching: the dashboard draws such routes
  // dashed, solid once the path is complete.
  complete: boolean
}

// Same palette as the dashboard's route layer: workers cycle through the
// eight colors by index, vanguards are orange, rangers blue.
export const WORKER_ROUTE_COLORS = ['#63d8ff', '#57d6a3', '#ffc857', '#ff7aa9', '#b38cff', '#ff8a65', '#8fd14f', '#78a9ff'] as const
export const VANGUARD_ROUTE_COLOR = '#ff8c42'
export const RANGER_ROUTE_COLOR = '#6ea8ff'

export function unitRouteColors(routes: UnitRoute[]): Map<string, string> {
  const out = new Map<string, string>()
  let workerIndex = 0
  for (const route of routes) {
    out.set(route.name, route.type === 'WORKER'
      ? WORKER_ROUTE_COLORS[workerIndex++ % WORKER_ROUTE_COLORS.length]
      : route.type === 'VANGUARD' ? VANGUARD_ROUTE_COLOR : RANGER_ROUTE_COLOR)
  }
  return out
}

const parsePair = (raw: unknown): Position | null =>
  Array.isArray(raw) && raw.length === 2 && Number.isFinite(raw[0]) && Number.isFinite(raw[1]) ? [raw[0] as number, raw[1] as number] : null

export async function loadUnitRoutes(): Promise<UnitRoute[]> {
  try {
    const response = await fetch('/api/unit-routes', { credentials: 'same-origin' })
    if (!response.ok) return []
    const data = await response.json() as { ok?: boolean; units?: unknown[] }
    if (data?.ok !== true || !Array.isArray(data.units)) return []
    const out: UnitRoute[] = []
    for (const raw of data.units) {
      if (!raw || typeof raw !== 'object') continue
      const entry = raw as { name?: unknown; type?: unknown; target?: unknown; path?: unknown; complete?: unknown }
      if (typeof entry.name !== 'string' || !entry.name) continue
      const type: UnitRouteType = entry.type === 'VANGUARD' ? 'VANGUARD' : entry.type === 'RANGER' ? 'RANGER' : 'WORKER'
      const target = parsePair(entry.target)
      const path = (Array.isArray(entry.path) ? entry.path.map(parsePair) : []).filter((point): point is Position => point !== null)
      if (!target && path.length < 2) continue
      out.push({ name: entry.name, type, target, path, complete: entry.complete === true })
    }
    return out
  } catch {
    return []
  }
}
