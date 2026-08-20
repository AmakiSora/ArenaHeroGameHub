import { Eye, EyeOff, Focus, Minus, Plus } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import type { EnemySightingType } from '../../lib/enemyMemory'
import { UnitArtIcon } from './UnitArtIcon'

// Types the operator can hide individually; unknown (ENEMY) markers follow
// the master toggle only.
export const ENEMY_FILTER_TYPES = ['WORKER', 'VANGUARD', 'RANGER', 'CORE'] as const

export function MapControls({ onCenter, onZoom, beaconIndicatorVisible = true, onToggleBeaconIndicator, coreIndicatorVisible = true, onToggleCoreIndicator, memoryVisible = false, onToggleMemory, memoryFilters, onToggleMemoryFilter }: { onCenter: () => void; onZoom: (direction: number) => void; beaconIndicatorVisible?: boolean; onToggleBeaconIndicator?: () => void; coreIndicatorVisible?: boolean; onToggleCoreIndicator?: () => void; memoryVisible?: boolean; onToggleMemory?: () => void; memoryFilters?: ReadonlySet<EnemySightingType>; onToggleMemoryFilter?: (type: EnemySightingType) => void }) {
  const { t } = useTranslation(); const buttons = [
    { label: t('game.center'), icon: Focus, action: onCenter },
    { label: t('game.zoomIn'), icon: Plus, action: () => onZoom(1) },
    { label: t('game.zoomOut'), icon: Minus, action: () => onZoom(-1) },
  ]
  const beaconIndicatorLabel = beaconIndicatorVisible ? t('game.hideBeaconIndicator') : t('game.showBeaconIndicator')
  const coreIndicatorLabel = coreIndicatorVisible ? t('game.hideCoreIndicator') : t('game.showCoreIndicator')
  const memoryLabel = memoryVisible ? t('game.hideEnemyMemory') : t('game.showEnemyMemory')
  return <div className="panel absolute bottom-4 left-4 z-20 flex items-center rounded-gold p-1">
    {buttons.map(({ label, icon: Icon, action }) => <button key={label} onClick={action} aria-label={label} title={label} className="focus-ring grid size-11 place-items-center rounded-gold-sm text-zinc-400 transition-colors hover:bg-white/[.06] hover:text-zinc-100"><Icon size={18} /></button>)}
    {onToggleBeaconIndicator && <button onClick={onToggleBeaconIndicator} aria-label={beaconIndicatorLabel} aria-pressed={beaconIndicatorVisible} title={beaconIndicatorLabel} className={`focus-ring grid size-11 place-items-center rounded-gold-sm transition-colors ${beaconIndicatorVisible ? 'bg-[#d9a62e]/[.14] text-[#e1b64e]' : 'text-zinc-400 hover:bg-white/[.06] hover:text-zinc-100'}`}>{beaconIndicatorVisible ? <Eye size={18} /> : <EyeOff size={18} />}</button>}
    {onToggleCoreIndicator && <button onClick={onToggleCoreIndicator} aria-label={coreIndicatorLabel} aria-pressed={coreIndicatorVisible} title={coreIndicatorLabel} className={`focus-ring grid size-11 place-items-center rounded-gold-sm transition-colors ${coreIndicatorVisible ? 'bg-cyan-signal/[.14] text-cyan-signal' : 'text-zinc-400 hover:bg-white/[.06] hover:text-zinc-100'}`}>{coreIndicatorVisible ? <Eye size={18} /> : <EyeOff size={18} />}</button>}
    {onToggleMemory && <button onClick={onToggleMemory} aria-label={memoryLabel} aria-pressed={memoryVisible} title={memoryLabel} className={`focus-ring grid size-11 place-items-center rounded-gold-sm transition-colors ${memoryVisible ? 'bg-coral-hostile/[.14] text-coral-hostile' : 'text-zinc-400 hover:bg-white/[.06] hover:text-zinc-100'}`}>{memoryVisible ? <Eye size={18} /> : <EyeOff size={18} />}</button>}
    {/* Per-type filters for remembered enemies, shown while the layer is on. */}
    {memoryVisible && onToggleMemoryFilter && memoryFilters && ENEMY_FILTER_TYPES.map((type) => {
      const label = t('game.enemyMemoryFilter', { type: t(`game.units.${type}`) })
      const active = memoryFilters.has(type)
      return <button
        key={type}
        onClick={() => onToggleMemoryFilter(type)}
        aria-label={label}
        aria-pressed={active}
        title={label}
        className={`focus-ring grid size-9 place-items-center rounded-gold-sm transition-all ${active ? 'bg-white/[.04]' : 'opacity-35 grayscale hover:opacity-60'}`}
      >
        <UnitArtIcon type={type} className="size-6" />
      </button>
    })}
  </div>
}
