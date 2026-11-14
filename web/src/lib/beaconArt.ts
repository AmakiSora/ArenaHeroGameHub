import { publicAsset } from './publicAsset'

// Re-encoded to WebP at 256x256, i.e. ~2x the largest drawn size
// (ceil(cell * 1.5) = 117 CSS px at MAX_WORLD_CELL_SIZE 78).
export const BEACON_SPRITE_PATH = publicAsset('/assets/game/beacon.webp')

export function beaconSpriteRect(screenX: number, screenY: number, cell: number, attached: boolean, aspectRatio = 1) {
  const maxSize = Math.max(1, Math.round(cell * (attached ? .58 : .98)))
  const aspect = Number.isFinite(aspectRatio) && aspectRatio > 0 ? aspectRatio : 1
  const width = Math.max(1, Math.round(aspect >= 1 ? maxSize : maxSize * aspect))
  const height = Math.max(1, Math.round(aspect >= 1 ? maxSize / aspect : maxSize))
  return { left: Math.round(screenX - width / 2), top: Math.round(screenY - height / 2), width, height }
}
