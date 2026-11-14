import type { PlayerState, Position, WorldObject } from './types'
import { computeVisibility, positionKey } from './visibility'

export interface ExploredCell { position: Position; kind: 'EMPTY' | 'OBSTACLE' | 'RESOURCE' }
const STORE = 'cells'

// One connection per namespace for the whole session: opening IndexedDB is
// async and relatively expensive, and the game stream writes every tick.
const dbCache = new Map<string, Promise<IDBDatabase>>()

function openDB(namespace: string): Promise<IDBDatabase> {
  const cached = dbCache.get(namespace)
  if (cached) return cached
  const pending = new Promise<IDBDatabase>((resolve, reject) => {
    const request = indexedDB.open(`arena-hero-exploration-${namespace}`, 1)
    request.onupgradeneeded = () => request.result.createObjectStore(STORE)
    request.onsuccess = () => {
      const db = request.result
      db.onclose = () => dbCache.delete(namespace)
      db.onversionchange = () => { db.close(); dbCache.delete(namespace) }
      resolve(db)
    }
    request.onerror = () => { dbCache.delete(namespace); reject(request.error) }
  })
  dbCache.set(namespace, pending)
  pending.catch(() => dbCache.delete(namespace))
  return pending
}

export async function loadExplored(namespace: string): Promise<Map<string, ExploredCell>> {
  if (!('indexedDB' in window)) return new Map()
  const db = await openDB(namespace)
  return new Promise<Map<string, ExploredCell>>((resolve, reject) => {
    const transaction = db.transaction(STORE, 'readonly')
    const request = transaction.objectStore(STORE).getAll()
    request.onsuccess = () => resolve(new Map((request.result as ExploredCell[]).map((cell) => [positionKey(cell.position), cell])))
    request.onerror = () => reject(request.error)
  })
}

// `known` is the cumulative explored map. Only cells that are new or whose
// kind changed are written back, and when nothing changed the stored map is
// returned untouched so downstream memo identities stay stable across ticks.
export async function rememberVisible(namespace: string, state: PlayerState, known?: Map<string, ExploredCell>): Promise<Map<string, ExploredCell>> {
  const cells = visibleCells(state.objects)
  for (const raw of computeVisibility(state)) {
    if (cells.has(raw)) continue
    const [x, y] = raw.split(',').map(Number)
    cells.set(raw, { position: [x, y], kind: 'EMPTY' })
  }
  const changed: ExploredCell[] = []
  for (const [key, cell] of cells) {
    const previous = known?.get(key)
    if (!previous || previous.kind !== cell.kind) changed.push(cell)
  }
  if (known && changed.length === 0) return known
  const merged = known ? new Map(known) : new Map<string, ExploredCell>()
  for (const [key, cell] of cells) merged.set(key, cell)
  if (!('indexedDB' in window) || changed.length === 0) return merged
  const db = await openDB(namespace)
  await new Promise<void>((resolve, reject) => {
    const transaction = db.transaction(STORE, 'readwrite')
    const store = transaction.objectStore(STORE)
    for (const cell of changed) store.put(cell, positionKey(cell.position))
    transaction.oncomplete = () => resolve()
    transaction.onerror = () => reject(transaction.error)
  })
  return merged
}

export function visibleCells(objects: WorldObject[]): Map<string, ExploredCell> {
  const result = new Map<string, ExploredCell>()
  for (const object of objects) {
    if (object.kind === 'OBSTACLE') for (const position of object.positions ?? []) result.set(positionKey(position), { position, kind: 'OBSTACLE' })
    if (object.kind === 'RESOURCE') for (const position of object.positions ?? []) result.set(positionKey(position), { position, kind: 'RESOURCE' })
  }
  return result
}