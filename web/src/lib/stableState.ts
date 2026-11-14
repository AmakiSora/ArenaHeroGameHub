// Equality guards for polling hooks: the dashboard returns fresh objects on
// every fetch, and setState with a new identity re-renders the whole arena
// tree even when nothing actually changed between ticks.

export function sameRecord(a: Record<string, unknown>, b: Record<string, unknown>): boolean {
  if (a === b) return true
  const keys = Object.keys(a)
  if (keys.length !== Object.keys(b).length) return false
  return keys.every((key) => a[key] === b[key])
}

export function sameJSON(a: unknown, b: unknown): boolean {
  return a === b || JSON.stringify(a) === JSON.stringify(b)
}

export function sameSet(a: ReadonlySet<string>, b: ReadonlySet<string>): boolean {
  if (a === b) return true
  if (a.size !== b.size) return false
  for (const value of a) if (!b.has(value)) return false
  return true
}