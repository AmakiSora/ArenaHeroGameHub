import type { Position } from './types'

// Categorized battle log rows (battle_log.jsonl), served by the dashboard as
// /api/battle-log — the very same data the server-rendered dashboard shows in
// its 「战斗日志」panel. The `cat` field drives the filter chips; messages end
// in "(x,y)" coordinates the panel turns into clickable map jumps.
export const BATTLE_LOG_CATEGORIES = ['discover', 'kill', 'defeat', 'combat', 'economy', 'config', 'warn'] as const
export type BattleLogCategory = (typeof BATTLE_LOG_CATEGORIES)[number]

// Noisy categories default off so the panel starts readable (same defaults
// as the dashboard's log panel).
export const BATTLE_LOG_DEFAULT_OFF: readonly BattleLogCategory[] = ['combat', 'economy']

export interface BattleLogEntry {
  tick: number | null
  ts: number | null
  cat: string
  msg: string
}

const KNOWN_CATEGORIES: ReadonlySet<string> = new Set(BATTLE_LOG_CATEGORIES)

export async function loadBattleLog(limit = 3000): Promise<BattleLogEntry[]> {
  try {
    const response = await fetch(`/api/battle-log?limit=${limit}`, { credentials: 'same-origin' })
    if (!response.ok) return []
    const data = await response.json() as { ok?: boolean; entries?: Array<{ tick?: unknown; ts?: unknown; cat?: unknown; msg?: unknown }> }
    if (data?.ok !== true || !Array.isArray(data.entries)) return []
    return data.entries.flatMap((item) => {
      if (!item || typeof item.msg !== 'string' || !item.msg) return []
      const tick = Number(item.tick)
      const ts = Number(item.ts)
      const rawCat = String(item.cat ?? '')
      return [{
        tick: Number.isFinite(tick) && item.tick !== null ? tick : null,
        ts: Number.isFinite(ts) && item.ts !== null ? ts : null,
        cat: KNOWN_CATEGORIES.has(rawCat) ? rawCat : '',
        msg: item.msg,
      }]
    })
  } catch {
    return []
  }
}

// Messages from the tactic carry "(x,y)" or "(x1,y1)→(x2,y2)" coordinates
// (see tactic._fmt_cell). Split a message into plain-text and coordinate
// segments so the panel can render each coordinate as a clickable jump.
export type BattleLogSegment =
  | { kind: 'text'; text: string }
  | { kind: 'coord'; text: string; position: Position }

export function splitLogMessage(msg: string): BattleLogSegment[] {
  const segments: BattleLogSegment[] = []
  const pattern = /\((-?\d+)\s*,\s*(-?\d+)\)/g
  let last = 0
  for (const match of msg.matchAll(pattern)) {
    const index = match.index ?? 0
    if (index > last) segments.push({ kind: 'text', text: msg.slice(last, index) })
    segments.push({
      kind: 'coord',
      text: match[0],
      position: [Number(match[1]), Number(match[2])],
    })
    last = index + match[0].length
  }
  if (last < msg.length) segments.push({ kind: 'text', text: msg.slice(last) })
  return segments.length ? segments : [{ kind: 'text', text: msg }]
}

// How many rows to fetch for a given time window — bigger windows need more
// rows, otherwise 「全部」 would still cap at a small newest-N (mirrors the
// dashboard's logLimitFor).
export function battleLogLimitFor(window: number | 'all'): number {
  if (window === 'all') return 3000
  if (window >= 21600) return 2000
  if (window >= 3600) return 1000
  if (window >= 1800) return 600
  return 300
}
