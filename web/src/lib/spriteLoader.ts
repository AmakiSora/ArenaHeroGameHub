// Sprite sheets are decoded on demand instead of all at mount: the arena used
// to allocate nine Image() objects (899.5 KB of PNG at the time of the review)
// before the first tick arrived, even when the obstacle layer was hidden or a
// unit type never appeared. A path is fetched at most once per session — the
// cache returns the same pending promise/file for every later request.
const spriteCache = new Map<string, Promise<HTMLImageElement | null>>()

export function loadSpriteImage(path: string): Promise<HTMLImageElement | null> {
  const cached = spriteCache.get(path)
  if (cached) return cached
  const pending = new Promise<HTMLImageElement | null>((resolve) => {
    if (typeof Image === 'undefined') { resolve(null); return }
    const image = new Image()
    image.decoding = 'async'
    image.onload = () => resolve(image)
    image.onerror = () => resolve(null)
    image.src = path
  })
  spriteCache.set(path, pending)
  return pending
}

// Keeps the result aligned with `paths` (a failed sheet is null) so callers can
// map loaded images back to their unit type / sprite index.
export function loadSpriteImages(paths: readonly string[]): Promise<Array<HTMLImageElement | null>> {
  return Promise.all(paths.map((path) => loadSpriteImage(path)))
}

// Test hook: forget decoded/failed sheets so a test starts from a clean cache.
export function resetSpriteCacheForTests() {
  spriteCache.clear()
}
