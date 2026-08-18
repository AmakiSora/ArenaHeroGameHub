import { ChevronDown } from 'lucide-react'
import { useMemo, useState } from 'react'
import { useTranslation } from 'react-i18next'
import type { PlayerState, UnitType, WorldObject } from '../../lib/types'
import { Logo } from '../Logo'
import { GameStats } from './GameStats'
import { UnitArtIcon } from './UnitArtIcon'

// Fleet sections in display order: the Core on top, then squads by unit type.
const UNIT_GROUP_KEYS = ['CORE', 'WORKER', 'RANGER', 'VANGUARD'] as const
type UnitGroupKey = typeof UNIT_GROUP_KEYS[number]

function groupKeyOf(object: WorldObject): UnitGroupKey {
  if (object.kind === 'CORE') return 'CORE'
  const type: UnitType = object.unit_type ?? 'WORKER'
  return type === 'RANGER' ? 'RANGER' : type === 'VANGUARD' ? 'VANGUARD' : 'WORKER'
}

export function AssetList({ state, objects, selectedId, onSelect }: { state: PlayerState; objects: WorldObject[]; selectedId: string | null; onSelect: (object: WorldObject) => void }) {
  const { t } = useTranslation(); const controlled = useMemo(() => objects.filter((object) => object.controlled), [objects])
  const groups = useMemo(() => UNIT_GROUP_KEYS.map((key) => ({ key, members: controlled.filter((object) => groupKeyOf(object) === key) })), [controlled])
  const [collapsedGroups, setCollapsedGroups] = useState<Partial<Record<UnitGroupKey, boolean>>>({})
  const toggleGroup = (key: UnitGroupKey) => setCollapsedGroups((current) => ({ ...current, [key]: !current[key] }))
  return <aside className="panel-strong hidden h-full min-h-0 flex-col border-y-0 border-l-0 lg:flex">
    <div className="border-b border-white/[.07]">
      <div className="px-5 py-4"><Logo /><GameStats state={state} className="mt-4" /></div>
      <div className="flex min-h-10 items-center justify-between gap-3 border-t border-white/[.07] px-4 py-2">
        <h2 className="flex min-w-0 items-center gap-2">
          <span className="eyebrow shrink-0">FLEET INDEX</span>
          {' '}
          <span className="truncate font-display text-xs font-medium text-zinc-400">{t('game.objects')}</span>
        </h2>
        <span className="rounded-gold-sm bg-white/[.04] px-2 py-1 font-mono text-[9px] text-zinc-500">{controlled.length}</span>
      </div>
    </div>
    <div className="min-h-0 flex-1 overflow-y-auto p-2">
      {groups.map(({ key, members }) => members.length === 0 ? null : <section key={key} aria-label={t(`game.unitGroups.${key}`)}>
        <button type="button" onClick={() => toggleGroup(key)} aria-expanded={!collapsedGroups[key]} className="focus-ring mb-0.5 flex w-full items-center justify-between rounded-gold px-2.5 py-1.5 text-left transition-colors hover:bg-white/[.04]">
          <span className="flex min-w-0 items-center gap-1.5"><ChevronDown aria-hidden="true" size={12} className={`shrink-0 text-zinc-600 transition-transform ${collapsedGroups[key] ? '-rotate-90' : ''}`} /><span className="eyebrow truncate">{t(`game.unitGroups.${key}`)}</span></span>
          <span className="shrink-0 font-mono text-[9px] text-zinc-600">{members.length}</span>
        </button>
        {!collapsedGroups[key] && members.map((object) => { const artType = object.kind === 'CORE' ? 'CORE' : object.unit_type ?? 'WORKER'; const name = object.kind === 'CORE' ? t('game.units.CORE') : t(`game.units.${object.unit_type}`); return <button key={object.id} onClick={() => onSelect(object)} style={{ contentVisibility: 'auto', containIntrinsicSize: '44px' }} className={`focus-ring mb-0.5 flex min-h-11 w-full items-center gap-2 rounded-gold px-2.5 text-left transition-colors ${selectedId === object.id ? 'bg-indigo-deep/55 text-blue-soft' : 'text-zinc-400 hover:bg-white/[.04] hover:text-zinc-100'}`}>
        <span className="grid size-7 shrink-0 place-items-center rounded-gold-sm border border-violet-cosmic/15 bg-indigo-deep/45"><UnitArtIcon type={artType} className="size-5" /></span><span className="flex min-w-0 flex-1 items-baseline gap-1.5"><span className="truncate text-xs font-medium">{name}</span><span className="shrink-0 font-mono text-[9px] text-zinc-600">[{object.position?.join(', ') ?? '—'}]</span></span><span className="shrink-0 font-mono text-[9px]">{object.hp} HP</span>
      </button> })}
      </section>)}
    </div>
  </aside>
}
