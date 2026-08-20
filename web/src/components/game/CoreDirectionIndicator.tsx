import { Navigation2 } from 'lucide-react'
import { useId } from 'react'
import { useTranslation } from 'react-i18next'
import type { Position } from '../../lib/types'
import { UNIT_SPRITE_PATHS } from '../../lib/unitArt'
import { offscreenPlacement, type CameraView, type Viewport } from './BeaconDirectionIndicator'

// Off-screen arrow toward the player's own Core, mirroring the Champion
// Beacon indicator. A larger inset keeps the two rings apart when Beacon and
// Core sit in the same direction.
export function CoreDirectionIndicator({ corePosition, camera, viewport, onCenter }: { corePosition: Position; camera: CameraView; viewport: Viewport; onCenter: () => void }) {
  const { t } = useTranslation()
  const tooltipId = useId()
  const placement = offscreenPlacement(corePosition, camera, viewport, 60)
  if (!placement) return null

  const tooltipPosition = {
    top: 'left-1/2 top-full mt-2 -translate-x-1/2',
    right: 'right-full top-1/2 mr-2 -translate-y-1/2',
    bottom: 'bottom-full left-1/2 mb-2 -translate-x-1/2',
    left: 'left-full top-1/2 ml-2 -translate-y-1/2',
  }[placement.edge]
  return <button
    type="button"
    onClick={onCenter}
    aria-label={`${t('game.centerCore')} [${corePosition.join(', ')}]`}
    aria-describedby={tooltipId}
    style={{ left: placement.left, top: placement.top }}
    className="focus-ring group absolute z-20 grid size-11 -translate-x-1/2 -translate-y-1/2 place-items-center rounded-full"
  >
    <Navigation2 aria-hidden="true" size={45} strokeWidth={1.25} style={{ transform: `rotate(${placement.angle + 90}deg)` }} className="absolute fill-[#081820]/85 text-cyan-signal drop-shadow-[0_0_7px_rgba(69,145,197,.42)] transition-colors duration-200 group-hover:text-[#a8c8dd]" />
    <span aria-hidden="true" className="relative grid size-8 place-items-center rounded-full border border-cyan-signal/45 bg-space-900/95 shadow-[0_0_10px_rgba(69,145,197,.28)]">
      <img alt="" draggable={false} src={UNIT_SPRITE_PATHS.CORE} className="size-7 select-none object-contain" />
    </span>
    <span id={tooltipId} role="tooltip" className={`panel pointer-events-none invisible absolute w-56 rounded-gold p-3 text-left opacity-0 shadow-xl shadow-black/50 transition-opacity duration-200 group-hover:visible group-hover:opacity-100 group-focus-visible:visible group-focus-visible:opacity-100 ${tooltipPosition}`}>
      <span className="flex items-center justify-between gap-3"><span className="text-[11px] font-semibold text-cyan-signal">{t('game.homeCore')}</span><span className="font-mono text-[9px] text-zinc-500">[{corePosition.join(', ')}]</span></span>
      <span className="mt-1.5 block text-[10px] leading-4 text-zinc-400">{t('game.homeCoreHint')}</span>
    </span>
  </button>
}
