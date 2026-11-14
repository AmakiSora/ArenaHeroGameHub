import type { ExploredCell } from './exploration'
import { TERRAIN_CHUNK_CELLS } from './worldCanvasPerformance'

// Pure helpers behind the baked fog-of-war overlay in WorldCanvas. They are
// kept out of the component so the cache-key discipline can be tested without
// a canvas: the overlay tile signature must change exactly when the per-chunk
// fog or route tint changes, and not when the camera moves.
export interface FogTintEntry {
  x: number
  y: number
  color: string
}

// Per-chunk state that decides both whether an overlay tile needs repainting
// and what it contains:
//  ' ' nothing known here, 'f' explored but currently unseen (fogged),
//  'v' explored and visible, 'x' visible but not yet in the explored map.
// Tint colors are appended for tinted cells so a route change repaints only
// the chunks the route actually crosses.
export function fogChunkSignature(
  explored: ReadonlyMap<string, ExploredCell>,
  visible: ReadonlySet<string>,
  tints: ReadonlyMap<string, string>,
  chunkX: number,
  chunkY: number,
): string {
  const firstX = chunkX * TERRAIN_CHUNK_CELLS, firstY = chunkY * TERRAIN_CHUNK_CELLS
  let signature = ''
  for (let y = firstY; y < firstY + TERRAIN_CHUNK_CELLS; y++) {
    for (let x = firstX; x < firstX + TERRAIN_CHUNK_CELLS; x++) {
      const key = `${x},${y}`
      const known = explored.has(key)
      const seen = visible.has(key)
      if (!known && !seen) { signature += ' '; continue }
      signature += known ? (seen ? 'v' : 'f') : 'x'
      const tint = tints.get(key)
      if (tint) signature += tint
    }
  }
  return signature
}

export function chunkHasFog(explored: ReadonlyMap<string, ExploredCell>, visible: ReadonlySet<string>, chunkX: number, chunkY: number): boolean {
  const firstX = chunkX * TERRAIN_CHUNK_CELLS, firstY = chunkY * TERRAIN_CHUNK_CELLS
  for (let y = firstY; y < firstY + TERRAIN_CHUNK_CELLS; y++) {
    for (let x = firstX; x < firstX + TERRAIN_CHUNK_CELLS; x++) {
      const key = `${x},${y}`
      if (explored.has(key) && !visible.has(key)) return true
    }
  }
  return false
}

// Buckets the route tint cells by chunk once per fog version: repainting a
// fogged chunk then only walks the few tinted cells that land inside it.
export function indexFogTints(
  explored: ReadonlyMap<string, ExploredCell>,
  visible: ReadonlySet<string>,
  tints: ReadonlyMap<string, string>,
): Map<string, FogTintEntry[]> {
  const byChunk = new Map<string, FogTintEntry[]>()
  for (const [key, color] of tints) {
    const comma = key.indexOf(',')
    const x = Number(key.slice(0, comma)), y = Number(key.slice(comma + 1))
    if (!Number.isFinite(x) || !Number.isFinite(y)) continue
    if (!visible.has(key) && !explored.has(key)) continue
    const chunkKey = `${Math.floor(x / TERRAIN_CHUNK_CELLS)},${Math.floor(y / TERRAIN_CHUNK_CELLS)}`
    let list = byChunk.get(chunkKey)
    if (!list) { list = []; byChunk.set(chunkKey, list) }
    list.push({ x, y, color })
  }
  return byChunk
}

// Minimal 2D-context surface needed to paint one overlay tile. Painting is
// split out from canvas creation so the exact fills (rgba(2,2,4,.62) fog,
// .28-alpha route tint with the cell*.08 inset) are testable without raster.
export interface FogPaintContext {
  fillStyle: string | CanvasGradient | CanvasPattern
  globalAlpha: number
  fillRect(x: number, y: number, width: number, height: number): void
}

export function paintFogChunk(
  context: FogPaintContext,
  explored: ReadonlyMap<string, ExploredCell>,
  visible: ReadonlySet<string>,
  tintsByChunk: ReadonlyMap<string, FogTintEntry[]>,
  chunkX: number,
  chunkY: number,
  cell: number,
): void {
  const firstX = chunkX * TERRAIN_CHUNK_CELLS, firstY = chunkY * TERRAIN_CHUNK_CELLS
  context.fillStyle = 'rgba(2,2,4,.62)'
  for (let y = firstY; y < firstY + TERRAIN_CHUNK_CELLS; y++) {
    for (let x = firstX; x < firstX + TERRAIN_CHUNK_CELLS; x++) {
      const key = `${x},${y}`
      if (!explored.has(key) || visible.has(key)) continue
      context.fillRect((x - firstX) * cell, (y - firstY) * cell, cell, cell)
    }
  }
  const chunkTints = tintsByChunk.get(`${chunkX},${chunkY}`)
  if (!chunkTints?.length) return
  context.globalAlpha = .28
  const inset = cell * .08
  for (const tint of chunkTints) {
    context.fillStyle = tint.color
    context.fillRect((tint.x - firstX) * cell + inset, (tint.y - firstY) * cell + inset, cell - inset * 2, cell - inset * 2)
  }
  context.globalAlpha = 1
}
