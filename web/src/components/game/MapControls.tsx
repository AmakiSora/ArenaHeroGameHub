import { Eye, EyeOff, Focus, Minus, Plus } from 'lucide-react'
import { useTranslation } from 'react-i18next'

export function MapControls({ onCenter, onZoom, memoryVisible = false, onToggleMemory }: { onCenter: () => void; onZoom: (direction: number) => void; memoryVisible?: boolean; onToggleMemory?: () => void }) {
  const { t } = useTranslation(); const buttons = [
    { label: t('game.center'), icon: Focus, action: onCenter },
    { label: t('game.zoomIn'), icon: Plus, action: () => onZoom(1) },
    { label: t('game.zoomOut'), icon: Minus, action: () => onZoom(-1) },
  ]
  const memoryLabel = memoryVisible ? t('game.hideEnemyMemory') : t('game.showEnemyMemory')
  return <div className="panel absolute bottom-4 left-4 z-20 flex rounded-gold p-1">
    {buttons.map(({ label, icon: Icon, action }) => <button key={label} onClick={action} aria-label={label} title={label} className="focus-ring grid size-11 place-items-center rounded-gold-sm text-zinc-400 transition-colors hover:bg-white/[.06] hover:text-zinc-100"><Icon size={18} /></button>)}
    {onToggleMemory && <button onClick={onToggleMemory} aria-label={memoryLabel} aria-pressed={memoryVisible} title={memoryLabel} className={`focus-ring grid size-11 place-items-center rounded-gold-sm transition-colors ${memoryVisible ? 'bg-coral-hostile/[.14] text-coral-hostile' : 'text-zinc-400 hover:bg-white/[.06] hover:text-zinc-100'}`}>{memoryVisible ? <Eye size={18} /> : <EyeOff size={18} />}</button>}
  </div>
}
