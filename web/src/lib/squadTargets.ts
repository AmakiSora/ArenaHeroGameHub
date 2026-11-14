import type { Position } from './types'
import { dashboardRequest } from './dashboardRequest'

// Ordered attack-target queues for the attack / kite squads, kept in
// squad_targets.json and consumed by the bot in coords mode: the squad
// marches on each queued point in order, advancing to the next once it has
// arrived and the area is cleared of enemies.
export type SquadKey = 'attack' | 'kite'

export type SquadTargetMap = Partial<Record<SquadKey, Position[]>>

const SQUAD_KEYS: SquadKey[] = ['attack', 'kite']

export async function loadSquadTargets(signal?: AbortSignal): Promise<SquadTargetMap> {
  const result = await dashboardRequest<{ ok?: boolean; targets?: Record<string, number[][]> }>('/api/squad-targets', { signal })
  if (!result.ok) return {}
  const data = result.data
  if (data?.ok !== true || !data.targets || typeof data.targets !== 'object') return {}
  const out: SquadTargetMap = {}
  for (const squad of SQUAD_KEYS) {
    const raw = data.targets[squad]
    if (!Array.isArray(raw)) continue
    const queue = raw.filter((point) => Array.isArray(point) && point.length === 2 && Number.isFinite(point[0]) && Number.isFinite(point[1])).map((point) => [point[0], point[1]] as Position)
    if (queue.length) out[squad] = queue
  }
  return out
}

async function squadTargetPost(path: string, body: Record<string, unknown>): Promise<boolean> {
  const result = await dashboardRequest<{ ok?: boolean }>(path, { method: 'POST', body })
  return result.ok && result.data?.ok === true
}

// Append one coordinate to the squad's queue (the bot pops it on arrival).
export const addSquadTarget = (squad: SquadKey, x: number, y: number) => squadTargetPost('/api/squad-target/add', { squad, x, y })

// Remove one queued target by index.
export const removeSquadTarget = (squad: SquadKey, index: number) => squadTargetPost('/api/squad-target/remove', { squad, index })

// Clear the squad's whole queue.
export const clearSquadTargets = (squad: SquadKey) => squadTargetPost('/api/squad-target/clear', { squad })
