// Manual per-unit hold position (the dashboard's 驻守 toggle), kept in
// holds.json and consumed by the bot every Tick: a held unit stands in place
// and auto-attacks enemies entering its range instead of following its normal
// program. Clicking again releases the unit.
export type HoldMap = Record<string, boolean>

export async function loadHolds(): Promise<Set<string>> {
  try {
    const response = await fetch('/api/holds', { credentials: 'same-origin' })
    if (!response.ok) return new Set()
    const data = await response.json() as { ok?: boolean; holds?: string[] }
    if (data?.ok !== true || !Array.isArray(data.holds)) return new Set()
    return new Set(data.holds.filter((name): name is string => typeof name === 'string'))
  } catch {
    return new Set()
  }
}

async function holdPost(path: string, body: Record<string, unknown>): Promise<boolean> {
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

// Enter 驻守模式: the unit stays in place and auto-attacks in range.
export const setUnitHold = (name: string) => holdPost('/api/hold/set', { name })

// Cancel 驻守模式: the unit resumes its normal program.
export const clearUnitHold = (name: string) => holdPost('/api/hold/clear', { name })
