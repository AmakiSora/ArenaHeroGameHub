import { describe, expect, it } from 'vitest'
import type { ExploredCell } from './exploration'
import { chunkHasFog, fogChunkSignature, indexFogTints, paintFogChunk, type FogPaintContext } from './fogOverlay'
import type { Position } from './types'
import { positionKey } from './visibility'

const explored = (...positions: Position[]) => new Map<string, ExploredCell>(positions.map((position) => [positionKey(position), { position, kind: 'EMPTY' }]))
const visible = (...positions: Position[]) => new Set(positions.map(positionKey))

interface RecordedFill {
  x: number
  y: number
  width: number
  height: number
  style: string | CanvasGradient | CanvasPattern
  alpha: number
}

function recordingContext() {
  const fills: RecordedFill[] = []
  const context: FogPaintContext = {
    fillStyle: '',
    globalAlpha: 1,
    fillRect(x: number, y: number, width: number, height: number) {
      fills.push({ x, y, width, height, style: context.fillStyle, alpha: context.globalAlpha })
    },
  }
  return { context, fills }
}

describe('fogChunkSignature', () => {
  const known = explored([0, 0], [1, 0], [2, 0])
  const seen = visible([0, 0])

  it('is stable for the same fog state and independent of camera state', () => {
    const first = fogChunkSignature(known, seen, new Map(), 0, 0)
    expect(fogChunkSignature(known, seen, new Map(), 0, 0)).toBe(first)
    // Nothing camera-related is an input; re-reading the same maps cannot change it.
    expect(first).toContain('f')
    expect(first).toContain('v')
  })

  it('changes when a remembered cell becomes visible (a new tick)', () => {
    const before = fogChunkSignature(known, seen, new Map(), 0, 0)
    expect(fogChunkSignature(known, visible([0, 0], [1, 0]), new Map(), 0, 0)).not.toBe(before)
  })

  it('changes when exploration discovers a new cell', () => {
    const before = fogChunkSignature(known, seen, new Map(), 0, 0)
    expect(fogChunkSignature(explored([0, 0], [1, 0], [2, 0], [3, 0]), seen, new Map(), 0, 0)).not.toBe(before)
  })

  it('changes when a route tint is painted inside the chunk', () => {
    const before = fogChunkSignature(known, seen, new Map(), 0, 0)
    const tinted = fogChunkSignature(known, seen, new Map([['1,0', '#ff8c42']]), 0, 0)
    expect(tinted).not.toBe(before)
    expect(tinted).toContain('#ff8c42')
    // A different chunk must not see that tint.
    expect(fogChunkSignature(known, seen, new Map([['1,0', '#ff8c42']]), 1, 0)).toBe(fogChunkSignature(known, seen, new Map(), 1, 0))
  })

  it('marks visible-but-not-yet-explored cells separately from empty chunks', () => {
    expect(fogChunkSignature(new Map(), visible([9, 9]), new Map(), 1, 1)).toContain('x')
    expect(fogChunkSignature(new Map(), new Set(), new Map(), 1, 1)).not.toContain('x')
  })
})

describe('chunkHasFog', () => {
  it('reports a chunk with any explored-but-unseen cell and clears once it is visible', () => {
    const known = explored([0, 0], [1, 0])
    expect(chunkHasFog(known, visible([0, 0]), 0, 0)).toBe(true)
    expect(chunkHasFog(known, visible([0, 0], [1, 0]), 0, 0)).toBe(false)
    expect(chunkHasFog(known, visible([0, 0]), 5, 5)).toBe(false)
  })
})

describe('indexFogTints', () => {
  it('groups tinted cells per chunk and drops cells the map does not know yet', () => {
    const known = explored([0, 0], [8, 0])
    const seen = visible([0, 0], [9, 0])
    const index = indexFogTints(known, seen, new Map([
      ['0,0', '#63d8ff'],
      ['8,0', '#57d6a3'],
      ['9,0', '#78a9ff'],
      ['20,20', '#8fd14f'],
      ['junk', '#ffffff'],
    ]))

    expect(index.get('0,0')).toEqual([{ x: 0, y: 0, color: '#63d8ff' }])
    expect(index.get('1,0')).toEqual([{ x: 8, y: 0, color: '#57d6a3' }, { x: 9, y: 0, color: '#78a9ff' }])
    // [20,20] is neither explored nor visible, and the malformed key is ignored.
    expect(index.size).toBe(2)
  })
})

describe('paintFogChunk', () => {
  it('fills exactly the explored-but-unseen cells with the unchanged fog color', () => {
    const known = explored([0, 0], [1, 0], [2, 0])
    const seen = visible([0, 0])
    const { context, fills } = recordingContext()

    paintFogChunk(context, known, seen, new Map(), 0, 0, 10)

    expect(fills).toEqual([
      { x: 10, y: 0, width: 10, height: 10, style: 'rgba(2,2,4,.62)', alpha: 1 },
      { x: 20, y: 0, width: 10, height: 10, style: 'rgba(2,2,4,.62)', alpha: 1 },
    ])
  })

  it('paints route tints after the fog with alpha .28 and the cell*.08 inset', () => {
    const known = explored([0, 0], [1, 0])
    const seen = visible([0, 0])
    const tints = indexFogTints(known, seen, new Map([['1,0', '#ff8c42'], ['0,0', '#63d8ff']]))
    const { context, fills } = recordingContext()

    paintFogChunk(context, known, seen, tints, 0, 0, 10)

    expect(fills).toHaveLength(3)
    // Fog first, then the tint layer over it.
    expect(fills[0]).toEqual({ x: 10, y: 0, width: 10, height: 10, style: 'rgba(2,2,4,.62)', alpha: 1 })
    expect(fills[1].style).toBe('#ff8c42')
    expect(fills[1].alpha).toBe(.28)
    expect(fills[1].x).toBeCloseTo(10.8, 10)
    expect(fills[1].y).toBeCloseTo(.8, 10)
    expect(fills[1].width).toBeCloseTo(8.4, 10)
    expect(fills[1].height).toBeCloseTo(8.4, 10)
    expect(fills[2].style).toBe('#63d8ff')
    // Alpha is restored so the shared tile context starts clean next time.
    expect(context.globalAlpha).toBe(1)
  })
})
