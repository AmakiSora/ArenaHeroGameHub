import { ChevronDown } from 'lucide-react'
import { useMemo, useState } from 'react'
import { useTranslation } from 'react-i18next'
import type { PlayerState, UnitType, WorldObject } from '../../lib/types'
import { TEAM_KEYS, teamOfName, type TeamKey, type TeamRoster } from '../../lib/teamRoster'
import { unitDashboardName, type UnitNameMap } from '../../lib/unitNames'
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

// Two sidebar views behind one tab switch: unit-type groups (工人/游侠/先锋)
// and tactic combat squads (守家/进攻/风筝...). The choice survives reloads.
const FLEET_VIEW_KEY = 'arena-hero.asset-list-view'
type FleetView = 'groups' | 'teams'
const FLEET_VIEWS: FleetView[] = ['groups', 'teams']
// The three primary squads always render (even empty); guerrilla / standby
// only appear while they hold members to keep the list uncluttered.
const ALWAYS_SHOWN_SQUADS: TeamKey[] = ['home', 'attack', 'kite']

function readFleetView(): FleetView {
  return localStorage.getItem(FLEET_VIEW_KEY) === 'teams' ? 'teams' : 'groups'
}

export function AssetList({ state, objects, selectedId, onSelect, unitNames = {}, teamRoster = {} }: { state: PlayerState; objects: WorldObject[]; selectedId: string | null; onSelect: (object: WorldObject) => void; unitNames?: UnitNameMap; teamRoster?: TeamRoster }) {
  const { t } = useTranslation(); const controlled = useMemo(() => objects.filter((object) => object.controlled), [objects])
  const groups = useMemo(() => UNIT_GROUP_KEYS.map((key) => ({ key, members: controlled.filter((object) => groupKeyOf(object) === key) })), [controlled])
  // Squad view: only combat units have a team assignment. Membership is
  // looked up by the dashboard display name (V1/R2...) shared with the
  // tactic; unnamed or freshly spawned units land in the standby pool.
  const squadGroups = useMemo(() => {
    const combat = controlled.filter((object) => object.kind === 'UNIT' && (object.unit_type === 'RANGER' || object.unit_type === 'VANGUARD'))
    return TEAM_KEYS.map((key) => ({ key, members: combat.filter((object) => teamOfName(unitDashboardName(object, unitNames), teamRoster) === key) }))
      .filter(({ key, members }) => ALWAYS_SHOWN_SQUADS.includes(key) || members.length > 0)
  }, [controlled, teamRoster, unitNames])
  const [view, setView] = useState<FleetView>(readFleetView)
  const [collapsedSections, setCollapsedSections] = useState<Partial<Record<string, boolean>>>({})
  const toggleSection = (key: string) => setCollapsedSections((current) => ({ ...current, [key]: !current[key] }))
  const switchView = (next: FleetView) => { setView(next); localStorage.setItem(FLEET_VIEW_KEY, next) }
  const sections = view === 'teams' ? squadGroups : groups
  const sectionLabel = (key: string) => view === 'teams' ? t(`game.squads.${key}`) : t(`game.unitGroups.${key}`)
  const unitRow = (object: WorldObject) => { const artType = object.kind === 'CORE' ? 'CORE' : object.unit_type ?? 'WORKER'; const name = object.kind === 'CORE' ? t('game.units.CORE') : unitDashboardName(object, unitNames) ?? t(`game.units.${object.unit_type}`); return <button key={object.id} onClick={() => onSelect(object)} style={{ contentVisibility: 'auto', containIntrinsicSize: '44px' }} className={`focus-ring mb-0.5 flex min-h-11 w-full items-center gap-2 rounded-gold px-2.5 text-left transition-colors ${selectedId === object.id ? 'bg-indigo-deep/55 text-blue-soft' : 'text-zinc-400 hover:bg-white/[.04] hover:text-zinc-100'}`}>
        <span className="grid size-7 shrink-0 place-items-center rounded-gold-sm border border-violet-cosmic/15 bg-indigo-deep/45"><UnitArtIcon type={artType} className="size-5" /></span><span className="flex min-w-0 flex-1 items-baseline gap-1.5"><span className="truncate text-xs font-medium">{name}</span><span className="shrink-0 font-mono text-[9px] text-zinc-600">[{object.position?.join(', ') ?? '—'}]</span></span><span className="shrink-0 font-mono text-[9px]">{object.hp} HP</span>
      </button> }
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
      <div role="tablist" aria-label={t('game.fleetTabs.label')} className="flex gap-1.5 border-t border-white/[.07] px-3 py-2">
        {FLEET_VIEWS.map((key) => <button key={key} type="button" role="tab" aria-selected={view === key} onClick={() => switchView(key)} className={`focus-ring flex-1 rounded-gold px-2 py-1.5 text-xs font-medium transition-colors ${view === key ? 'bg-indigo-deep/55 text-blue-soft' : 'text-zinc-500 hover:bg-white/[.04] hover:text-zinc-200'}`}>{t(`game.fleetTabs.${key}`)}</button>)}
      </div>
    </div>
    <div className="min-h-0 flex-1 overflow-y-auto p-2">
      {sections.map(({ key, members }) => { const alwaysShown = view === 'teams' && ALWAYS_SHOWN_SQUADS.includes(key as TeamKey); return members.length === 0 && !alwaysShown ? null : <section key={key} aria-label={sectionLabel(key)}>
        <button type="button" onClick={() => toggleSection(key)} aria-expanded={!collapsedSections[key]} className="focus-ring mb-0.5 flex w-full items-center justify-between rounded-gold px-2.5 py-1.5 text-left transition-colors hover:bg-white/[.04]">
          <span className="flex min-w-0 items-center gap-1.5"><ChevronDown aria-hidden="true" size={12} className={`shrink-0 text-zinc-600 transition-transform ${collapsedSections[key] ? '-rotate-90' : ''}`} /><span className="eyebrow truncate">{sectionLabel(key)}</span></span>
          <span className="shrink-0 font-mono text-[9px] text-zinc-600">{members.length}</span>
        </button>
        {!collapsedSections[key] && (members.length > 0 ? members.map(unitRow) : <p className="px-2.5 py-1.5 text-[10px] text-zinc-600">—</p>)}
      </section> })}
    </div>
  </aside>
}
