import { afterEach, describe, expect, it, vi } from 'vitest'
import { loadSpriteImage, loadSpriteImages, resetSpriteCacheForTests } from './spriteLoader'

// jsdom's Image never loads network resources; this double reports a decoded
// sheet on the next microtask so the loader's resolution path is exercised.
class FakeImage {
  static created: string[] = []
  decoding = ''
  onload: (() => void) | null = null
  onerror: (() => void) | null = null
  naturalWidth = 0
  complete = false
  private source = ''

  set src(value: string) {
    this.source = value
    FakeImage.created.push(value)
    queueMicrotask(() => {
      this.complete = true
      this.naturalWidth = 128
      this.onload?.()
    })
  }

  get src() {
    return this.source
  }
}

describe('spriteLoader', () => {
  afterEach(() => { resetSpriteCacheForTests(); FakeImage.created = []; vi.unstubAllGlobals() })

  it('fetches each sprite path at most once and reuses the decoded image', async () => {
    vi.stubGlobal('Image', FakeImage)
    const first = await loadSpriteImage('/assets/game/units/worker.webp')
    const second = await loadSpriteImage('/assets/game/units/worker.webp')

    expect(second).toBe(first)
    expect(first?.complete).toBe(true)
    expect(FakeImage.created).toEqual(['/assets/game/units/worker.webp'])
  })

  it('keeps the result aligned with the requested paths', async () => {
    vi.stubGlobal('Image', FakeImage)
    const images = await loadSpriteImages(['/a.webp', '/b.webp'])
    expect(images).toHaveLength(2)
    expect(FakeImage.created).toEqual(['/a.webp', '/b.webp'])
  })
})
