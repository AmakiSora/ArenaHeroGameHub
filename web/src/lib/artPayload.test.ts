import { existsSync, readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { describe, expect, it } from 'vitest'
import { BEACON_SPRITE_PATH } from './beaconArt'
import { OBSTACLE_SPRITE_PATHS } from './obstacleArt'
import { RESOURCE_SPRITE_PATHS } from './resourceArt'
import { UNIT_SPRITE_PATHS } from './unitArt'

// Budget guard for the review's #19 finding: the arena used to ship 899.5 KB
// of 256-512 px PNG sprites for draws of at most 117 CSS px. Keep the whole
// game art payload (excluding the 38 KB background) well under 200 KB.
const MAX_ART_PAYLOAD_BYTES = 200 * 1024

const assetFiles = [BEACON_SPRITE_PATH, ...OBSTACLE_SPRITE_PATHS, ...RESOURCE_SPRITE_PATHS, ...Object.values(UNIT_SPRITE_PATHS)]

// The suite normally runs with the web package as cwd; fall back to the
// sibling web/ directory when vitest is started from the repository root.
// jsdom's URL does not resolve file: bases, so build paths from cwd.
const packageRoot = existsSync(resolve(process.cwd(), 'public', 'assets', 'game')) ? process.cwd() : resolve(process.cwd(), 'web')
const bytes = (assetPath: string) => readFileSync(resolve(packageRoot, 'public', assetPath.replace(/^\/+/, '')))

describe('game art payload', () => {
  it('ships every sprite as WebP', () => {
    for (const path of assetFiles) expect(path.endsWith('.webp')).toBe(true)
  })

  it('stays within the payload budget', () => {
    const total = assetFiles.reduce((sum, path) => sum + bytes(path).byteLength, 0)
    expect(total).toBeLessThan(MAX_ART_PAYLOAD_BYTES)
  })

  it('sends valid WebP containers', () => {
    for (const path of assetFiles) {
      const header = bytes(path)
      expect(header.subarray(0, 4).toString('ascii')).toBe('RIFF')
      expect(header.subarray(8, 12).toString('ascii')).toBe('WEBP')
    }
  })
})
