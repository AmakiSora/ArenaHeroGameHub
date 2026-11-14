import { dashboardRequest } from './dashboardRequest'

// Manual per-unit hold position (the dashboard's 驻守 toggle), kept in
// holds.json and consumed by the bot every Tick: a held unit stands in place
// and auto-attacks enemies entering its range instead of following its normal
// program. Clicking again releases the unit.
export type HoldMap = Record<string, boolean>

export async function loadHolds(signal?: AbortSignal): Promise<Set<string>> {
  const result = await dashboardRequest<{ ok?: boolean; holds?: string[] }>('/api/holds', { signal })
  if (!result.ok) return new Set()
  const data = result.data
  if (data?.ok !== true || !Array.isArray(data.holds)) return new Set()
  return new Set(data.holds.filter((name): name is string => typeof name === 'string'))
}

async function holdPost(path: string, body: Record<string, unknown>): Promise<boolean> {
  const result = await dashboardRequest<{ ok?: boolean }>(path, { method: 'POST', body })
  return result.ok && result.data?.ok === true
}

// Enter 驻守模式: the unit stays in place and auto-attacks in range.
export const setUnitHold = (name: string) => holdPost('/api/hold/set', { name })

// Cancel 驻守模式: the unit resumes its normal program.
export const clearUnitHold = (name: string) => holdPost('/api/hold/clear', { name })
