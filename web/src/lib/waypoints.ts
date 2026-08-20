import type { Position } from './types'

// Manual per-unit target queues (the tactic dashboard's 手动目标 panel), kept
// in waypoints.json and consumed by the bot every Tick: the unit marches to
// each queued point in order, clearing it on arrival. "attack" fights along
// the way, "rush" ignores fights and just travels.
export type WaypointMode = 'attack' | 'rush'

export interface WaypointEntry {
  queue: Position[]
  mode: WaypointMode
}

export type WaypointMap = Record<string, WaypointEntry>

export async function loadWaypoints(): Promise<WaypointMap> {
  try {
    const response = await fetch('/api/waypoints', { credentials: 'same-origin' })
    if (!response.ok) return {}
    const data = await response.json() as { ok?: boolean; waypoints?: Record<string, { queue?: number[][]; mode?: string }> }
    if (data?.ok !== true || !data.waypoints || typeof data.waypoints !== 'object') return {}
    const out: WaypointMap = {}
    for (const [name, raw] of Object.entries(data.waypoints)) {
      if (!raw || !Array.isArray(raw.queue)) continue
      const queue = raw.queue.filter((point) => Array.isArray(point) && point.length === 2 && Number.isFinite(point[0]) && Number.isFinite(point[1])).map((point) => [point[0], point[1]] as Position)
      if (!queue.length) continue
      out[name] = { queue, mode: raw.mode === 'rush' ? 'rush' : 'attack' }
    }
    return out
  } catch {
    return {}
  }
}

async function waypointPost(path: string, body: Record<string, unknown>): Promise<boolean> {
  try {
    const response = await fetch(path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      credentials: 'same-origin',
      body: JSON.stringify(body),
    })
    if (!response.ok) return false
    const data = await response.json() as { ok?: boolean }
    return data?.ok === true
  } catch {
    return false
  }
}

// Append one target to the unit's queue; mode keeps the unit's current march
// setting (the dashboard's per-unit 攻击/赶路 toggle).
export const addUnitWaypoint = (name: string, x: number, y: number, mode: WaypointMode) => waypointPost('/api/waypoint/set', { name, x, y, mode })

// index removes one queued target; omitting it clears the unit's whole queue.
export const removeUnitWaypoint = (name: string, index?: number) => waypointPost('/api/waypoint/remove', index === undefined ? { name } : { name, index })
