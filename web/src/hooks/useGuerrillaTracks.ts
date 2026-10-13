import { useEffect, useState } from 'react'
import { EMPTY_GUERRILLA_TRACKS, loadGuerrillaTracks, type GuerrillaTrackData } from '../lib/guerrillaTracks'

// A day of guerrilla exploration lines from /api/guerrilla-tracks. This is a
// review artifact, not a live layer: the dashboard re-parses the day file at
// most once a minute, so the dialog refreshes on the same rhythm instead of
// per Tick.
export function useGuerrillaTracks(enabled: boolean, date?: string | null): GuerrillaTrackData {
  const [data, setData] = useState<GuerrillaTrackData>(EMPTY_GUERRILLA_TRACKS)
  useEffect(() => {
    if (!enabled) return
    let cancelled = false
    const load = () => void loadGuerrillaTracks(date ?? undefined).then((next) => {
      if (!cancelled) setData(next)
    })
    load()
    const timer = setInterval(load, 60_000)
    return () => { cancelled = true; clearInterval(timer) }
  }, [enabled, date])
  return data
}
