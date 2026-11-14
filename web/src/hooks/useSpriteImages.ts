import { useEffect, useState } from 'react'
import { loadSpriteImages } from '../lib/spriteLoader'

// Loads a sprite set only once `enabled` turns true, and keeps the decoded
// images afterwards (a layer can toggle off without dropping its art). The
// returned array is aligned with `paths`; null entries failed to load.
export function useSpriteImages(paths: readonly string[], enabled = true): ReadonlyArray<HTMLImageElement | null> {
  const [images, setImages] = useState<ReadonlyArray<HTMLImageElement | null>>([])
  useEffect(() => {
    if (!enabled || paths.length === 0) return
    let active = true
    void loadSpriteImages(paths).then((loaded) => { if (active) setImages(loaded) })
    return () => { active = false }
  }, [enabled, paths])
  return images
}
