import { useEffect, useRef } from 'react'
import { useDocumentVisible } from './useDocumentVisible'

// One AbortController per dashboard Tick, shared by every poller that reacts
// to that tick. The arena page owns seven of them (names, teams, enemy memory,
// waypoints, squad targets, holds, unit routes) plus the battle log; they must
// send seven requests carrying the SAME AbortSignal instance, so a tick change
// cancels the whole previous batch with a single abort instead of leaving
// seven orphan requests in flight. Reference counting keeps a hook that
// re-runs alone (e.g. an on-demand refresh, or a layer toggle) from aborting
// its siblings' requests.
interface TickAbortEntry {
  controller: AbortController
  refs: number
}

const tickAborts = new Map<number, TickAbortEntry>()

function acquireTickAbort(tick: number): TickAbortEntry {
  const existing = tickAborts.get(tick)
  if (existing) {
    existing.refs += 1
    return existing
  }
  // A newer tick supersedes every older tick: abort their in-flight requests
  // before the new batch starts.
  for (const [otherTick, entry] of tickAborts) {
    if (otherTick === tick) continue
    entry.controller.abort()
    tickAborts.delete(otherTick)
  }
  const entry = { controller: new AbortController(), refs: 1 }
  tickAborts.set(tick, entry)
  return entry
}

function releaseTickAbort(tick: number, entry: TickAbortEntry) {
  if (tickAborts.get(tick) !== entry) return
  entry.refs -= 1
  if (entry.refs > 0) return
  tickAborts.delete(tick)
  entry.controller.abort()
}

// Runs `load` once per tick (and once more when `refreshKey` is bumped, e.g.
// after a manual-target POST). Skips entirely while the tab is hidden and
// refetches when it becomes visible again. `onData` is kept in a ref so an
// inline setState closure never restarts the fetch loop.
export function useTickLoader<T>(
  tick: number | null,
  enabled: boolean,
  load: (signal: AbortSignal) => Promise<T>,
  onData: (data: T) => void,
  refreshKey = 0,
): void {
  const visible = useDocumentVisible()
  const onDataRef = useRef(onData)
  useEffect(() => { onDataRef.current = onData })
  useEffect(() => {
    if (!enabled || !visible || tick === null) return
    const entry = acquireTickAbort(tick)
    let active = true
    void load(entry.controller.signal).then((data) => {
      if (!active || entry.controller.signal.aborted) return
      onDataRef.current(data)
    })
    return () => {
      active = false
      releaseTickAbort(tick, entry)
    }
  }, [enabled, load, refreshKey, tick, visible])
}

// Test hook: drop the module-level controller registry between tests.
export function resetTickAbortsForTests() {
  for (const entry of tickAborts.values()) entry.controller.abort()
  tickAborts.clear()
}
