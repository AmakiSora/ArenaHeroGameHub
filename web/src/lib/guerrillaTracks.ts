import { WORKER_ROUTE_COLORS } from './unitRoutes'

// A whole day of guerrilla positions, straight from the tactic bot's route
// recorder: /api/guerrilla-tracks reads
// runtime/guerrilla_routes-YYYYMMDD.jsonl, which the sim appends one row per
// guerrilla unit per Tick. tactic_log.jsonl rotates after ~890 Ticks (~4h at the
// live Tick cycle), so these files are the only source that holds a full day of
// exploration lines.
export interface GuerrillaTrack {
  id: string
  name: string
  type: 'VANGUARD' | 'RANGER'
  // The walked cells, decimated server-side to a bounded polyline.
  points: [number, number][]
  // Rows for this unit today vs the distinct cells among them: ~1 row per cell
  // means real roaming, 400 rows over 6 cells means a deterministic loop.
  ticks: number
  distinct: number
  // Chebyshev distance between the first and the newest cell.
  net: number
  // [minX, minY, maxX, maxY] over every distinct cell of the day.
  bounds: [number, number, number, number] | null
}

export interface GuerrillaTrackData {
  date: string | null
  dates: string[]
  units: GuerrillaTrack[]
}

export const EMPTY_GUERRILLA_TRACKS: GuerrillaTrackData = { date: null, dates: [], units: [] }

// Palette shared with the route layer, cycled per unit so neighbouring lines
// stay distinguishable in the overview.
export function guerrillaTrackColors(tracks: GuerrillaTrack[]): Map<string, string> {
  const colors = new Map<string, string>()
  tracks.forEach((track, index) => colors.set(track.id, WORKER_ROUTE_COLORS[index % WORKER_ROUTE_COLORS.length]))
  return colors
}

// Union bounds of every track, so the overview can scale a whole day (often
// 1000+ cells wide) into one box regardless of the map camera's zoom floor.
export function tracksBounds(tracks: GuerrillaTrack[]): [number, number, number, number] | null {
  let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity
  for (const track of tracks) {
    const bounds = track.bounds
    if (!bounds) continue
    minX = Math.min(minX, bounds[0]); minY = Math.min(minY, bounds[1])
    maxX = Math.max(maxX, bounds[2]); maxY = Math.max(maxY, bounds[3])
  }
  return Number.isFinite(minX) ? [minX, minY, maxX, maxY] : null
}

const parsePoints = (raw: unknown): [number, number][] => {
  if (!Array.isArray(raw)) return []
  const out: [number, number][] = []
  for (const item of raw) {
    if (Array.isArray(item) && item.length === 2 && Number.isFinite(item[0]) && Number.isFinite(item[1])) {
      out.push([item[0] as number, item[1] as number])
    }
  }
  return out
}

const parseBounds = (raw: unknown): [number, number, number, number] | null =>
  Array.isArray(raw) && raw.length === 4 && raw.every((value) => Number.isFinite(value))
    ? raw as [number, number, number, number]
    : null

export async function loadGuerrillaTracks(date?: string): Promise<GuerrillaTrackData> {
  try {
    const response = await fetch(`/api/guerrilla-tracks${date ? `?date=${date}` : ''}`, { credentials: 'same-origin' })
    if (!response.ok) return EMPTY_GUERRILLA_TRACKS
    const data = await response.json() as { ok?: boolean; date?: unknown; dates?: unknown; units?: unknown[] }
    if (data?.ok !== true || !Array.isArray(data.units)) return EMPTY_GUERRILLA_TRACKS
    const units: GuerrillaTrack[] = []
    for (const raw of data.units) {
      if (!raw || typeof raw !== 'object') continue
      const entry = raw as Record<string, unknown>
      const id = typeof entry.id === 'string' ? entry.id : ''
      if (!id) continue
      const points = parsePoints(entry.points)
      if (!points.length) continue
      units.push({
        id,
        name: typeof entry.name === 'string' ? entry.name : id,
        type: entry.type === 'RANGER' ? 'RANGER' : 'VANGUARD',
        points,
        ticks: Number(entry.ticks) || points.length,
        distinct: Number(entry.distinct) || new Set(points.map((point) => point.join(','))).size,
        net: Number(entry.net) || 0,
        bounds: parseBounds(entry.bounds),
      })
    }
    return {
      date: typeof data.date === 'string' ? data.date : null,
      dates: Array.isArray(data.dates) ? data.dates.filter((item): item is string => typeof item === 'string') : [],
      units,
    }
  } catch {
    return EMPTY_GUERRILLA_TRACKS
  }
}
