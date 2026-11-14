import { useEffect, useState } from 'react'

// Tracks document.visibilityState: every tick-driven dashboard poller skips
// its refetch while the tab is hidden and performs exactly one catch-up fetch
// when it becomes visible again. The WebSocket deliberately stays on its own
// watchdog (useGameStream) — it must keep the stream warm, polls must not.
export function useDocumentVisible(): boolean {
  const [visible, setVisible] = useState(() => typeof document === 'undefined' ? true : document.visibilityState !== 'hidden')
  useEffect(() => {
    const update = () => setVisible(document.visibilityState !== 'hidden')
    document.addEventListener('visibilitychange', update)
    update()
    return () => document.removeEventListener('visibilitychange', update)
  }, [])
  return visible
}
