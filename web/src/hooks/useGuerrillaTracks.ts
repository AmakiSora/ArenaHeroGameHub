import { useEffect, useState } from 'react'
import { EMPTY_GUERRILLA_TRACKS, loadGuerrillaTracks, type GuerrillaTrackData } from '../lib/guerrillaTracks'
import { useDocumentVisible } from './useDocumentVisible'

// A day of guerrilla exploration lines from /api/guerrilla-tracks. This is a
// review artifact, not a live layer: the dashboard re-parses the day file at
// most once a minute, so the dialog refreshes on the same rhythm instead of
// per Tick. The request is abortable and pauses while the tab is hidden.
export function useGuerrillaTracks(enabled: boolean, date?: string | null): GuerrillaTrackData {
  const [data, setData] = useState<GuerrillaTrackData>(EMPTY_GUERRILLA_TRACKS)
  const visible = useDocumentVisible()
  useEffect(() => {
    if (!enabled || !visible) return
    const controller = new AbortController()
    const load = () => void loadGuerrillaTracks(date ?? undefined, controller.signal).then((next) => {
      if (!controller.signal.aborted) setData(next)
    })
    load()
    const timer = setInterval(load, 60_000)
    return () => { controller.abort(); clearInterval(timer) }
  }, [date, enabled, visible])
  return data
}
