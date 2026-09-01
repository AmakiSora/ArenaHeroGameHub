import { useCallback, useEffect, useState } from 'react'
import { loadSquadTargets, type SquadTargetMap } from '../lib/squadTargets'

// Attack/kite squad target queues (/api/squad-targets). Refreshed on every new
// Tick — the bot pops a head once the squad arrives and clears it — and on
// demand right after a mutation so the settings panel reflects the queue.
export function useSquadTargets(tick: number | null, enabled = true) {
  const [squadTargets, setSquadTargets] = useState<SquadTargetMap>({})
  const [epoch, setEpoch] = useState(0)
  useEffect(() => {
    if (!enabled) { setSquadTargets({}); return }
    let cancelled = false
    void loadSquadTargets().then((next) => { if (!cancelled) setSquadTargets(next) })
    return () => { cancelled = true }
  }, [tick, epoch, enabled])
  const refresh = useCallback(() => setEpoch((value) => value + 1), [])
  return { squadTargets, refresh }
}
