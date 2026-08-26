import { ChevronDown, ScrollText } from 'lucide-react'
import { useEffect, useMemo, useState } from 'react'
import { useTranslation } from 'react-i18next'
import {
  BATTLE_LOG_CATEGORIES,
  BATTLE_LOG_DEFAULT_OFF,
  battleLogLimitFor,
  loadBattleLog,
  splitLogMessage,
  type BattleLogCategory,
  type BattleLogEntry,
} from '../../lib/battleLog'
import type { Position } from '../../lib/types'

interface Props {
  // Refresh trigger: a new tick (or a window change) re-fetches the rows.
  tick: number | null
  // Live mode fetches from the dashboard; the demo ships a fixed sample so
  // the panel stays visible without a tactic process behind it.
  enabled: boolean
  onJump: (position: Position) => void
}

const FILTER_STORAGE_KEY = 'arena-hero.battle-log-filters'
const WINDOW_STORAGE_KEY = 'arena-hero.battle-log-window'
export const BATTLE_LOG_WINDOWS = [600, 1800, 3600, 21600] as const
// The overlay panel renders at most this many rows; older rows stay fetched
// (filters still count them) but never reach the DOM.
const ROW_RENDER_CAP = 300

// Message tones mirror the dashboard log panel's per-category colors.
const CATEGORY_TONES: Record<string, string> = {
  discover: 'text-[#ffe08a]',
  kill: 'text-[#ff9b9b]',
  defeat: 'text-[#ff7aa9]',
  combat: 'text-[#c9a2ff]',
  economy: 'text-[#8ef0c4]',
  config: 'text-[#6ea8ff]',
  warn: 'text-[#ffc857]',
}
const CHIP_ACTIVE_TONES: Record<string, string> = {
  discover: 'border-[#ffe08a66] bg-[#ffe08a1a] text-[#ffe08a]',
  kill: 'border-[#ff9b9b66] bg-[#ff9b9b1a] text-[#ff9b9b]',
  defeat: 'border-[#ff7aa966] bg-[#ff7aa91a] text-[#ff7aa9]',
  combat: 'border-[#c9a2ff66] bg-[#c9a2ff1a] text-[#c9a2ff]',
  economy: 'border-[#8ef0c466] bg-[#8ef0c41a] text-[#8ef0c4]',
  config: 'border-[#6ea8ff66] bg-[#6ea8ff1a] text-[#6ea8ff]',
  warn: 'border-[#ffc85766] bg-[#ffc8571a] text-[#ffc857]',
}

function demoEntries(): BattleLogEntry[] {
  const now = Math.floor(Date.now() / 1000)
  return [
    { tick: 10582, ts: now, cat: 'kill', msg: 'V2 横扫击杀敌方工人 (-6,9)' },
    { tick: 10581, ts: now - 8, cat: 'discover', msg: '发现敌方 Core (8,4)' },
    { tick: 10580, ts: now - 15, cat: 'economy', msg: 'W1 采矿 +1 (3,-2)' },
    { tick: null, ts: now - 20, cat: 'config', msg: '配置调整： 工人目标=6' },
  ]
}

function loadCategoryFilters(): Record<string, boolean> {
  const out: Record<string, boolean> = {}
  for (const category of BATTLE_LOG_CATEGORIES) out[category] = !BATTLE_LOG_DEFAULT_OFF.includes(category)
  try {
    const raw = JSON.parse(localStorage.getItem(FILTER_STORAGE_KEY) ?? 'null') as Record<string, unknown> | null
    if (raw && typeof raw === 'object') {
      for (const category of BATTLE_LOG_CATEGORIES) out[category] = raw[category] !== false
    }
  } catch {
    // Corrupt storage: fall back to the defaults.
  }
  return out
}

function loadWindow(): number | 'all' {
  const raw = localStorage.getItem(WINDOW_STORAGE_KEY)
  if (raw === 'all') return 'all'
  const value = Number(raw)
  return raw !== null && Number.isFinite(value) && value >= 1 ? value : 'all'
}

export function BattleLogPanel({ tick, enabled, onJump }: Props) {
  const { t, i18n } = useTranslation()
  const [expanded, setExpanded] = useState(true)
  const [entries, setEntries] = useState<BattleLogEntry[]>(() => enabled ? [] : demoEntries())
  const [filters, setFilters] = useState(loadCategoryFilters)
  const [logWindow, setLogWindow] = useState<number | 'all'>(loadWindow)
  // Wall-clock cutoff reference: refreshed every 5 s while a finite window is
  // selected so rows age out without waiting for the next tick.
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    if (logWindow === 'all') return
    setNow(Date.now())
    const timer = window.setInterval(() => setNow(Date.now()), 5000)
    return () => window.clearInterval(timer)
  }, [logWindow, tick])
  useEffect(() => {
    if (!enabled) { setEntries(demoEntries()); return }
    let cancelled = false
    void loadBattleLog(battleLogLimitFor(logWindow)).then((next) => { if (!cancelled) setEntries(next) })
    return () => { cancelled = true }
  }, [tick, enabled, logWindow])
  const timeFormat = useMemo(() =>
    new Intl.DateTimeFormat(i18n.language, { hour: '2-digit', minute: '2-digit', second: '2-digit' }), [i18n.language])
  // Rows older than the selected time window are hidden; 'all' disables it.
  const cutoff = logWindow === 'all' ? null : now / 1000 - logWindow
  const visible = entries.filter((entry) =>
    filters[entry.cat] !== false
    && (cutoff === null || (entry.ts !== null && entry.ts >= cutoff)))
  const toggleFilter = (category: BattleLogCategory) => setFilters((current) => {
    const next = { ...current, [category]: current[category] === false }
    try { localStorage.setItem(FILTER_STORAGE_KEY, JSON.stringify(next)) } catch { /* storage full is non-fatal */ }
    return next
  })
  const chooseWindow = (window: number | 'all') => {
    setLogWindow(window)
    try { localStorage.setItem(WINDOW_STORAGE_KEY, String(window)) } catch { /* storage full is non-fatal */ }
  }

  return <section aria-label={t('game.battleLog')} className="panel pointer-events-auto w-full overflow-hidden rounded-gold-lg shadow-[0_18px_48px_rgba(0,0,0,.38)]">
    <button
      type="button"
      aria-expanded={expanded}
      onClick={() => setExpanded((current) => !current)}
      className="focus-ring flex min-h-11 w-full items-center gap-2.5 px-3.5 text-left"
    >
      <span className="grid size-7 shrink-0 place-items-center rounded-gold-sm bg-[#ffc857]/10 text-[#ffc857]">
        <ScrollText size={15} />
      </span>
      <span className="min-w-0 flex-1">
        <span className="block font-display text-xs font-semibold text-zinc-100">{t('game.battleLog')}</span>
        <span className="mt-0.5 block font-mono text-[9px] text-zinc-500">{t('game.battleLogCount', { count: visible.length })}</span>
      </span>
      <ChevronDown size={15} className={`shrink-0 text-zinc-500 transition-transform duration-200 ${expanded ? 'rotate-180' : ''}`} />
    </button>
    {expanded && <div className="border-t border-white/[.07] px-3.5 py-2">
      <div className="flex flex-wrap gap-1" role="group" aria-label={t('game.battleLog')}>
        {BATTLE_LOG_CATEGORIES.map((category) => {
          const on = filters[category] !== false
          return <button
            key={category}
            type="button"
            aria-pressed={on}
            onClick={() => toggleFilter(category)}
            className={`focus-ring rounded-full border px-1.5 py-0.5 text-[9px] font-medium leading-none transition-colors ${on ? CHIP_ACTIVE_TONES[category] : 'border-white/[.07] text-zinc-600 hover:text-zinc-400'}`}
          >{t(`game.battleLogCats.${category}`)}</button>
        })}
      </div>
      <div className="mt-1.5 flex flex-wrap gap-1">
        {([...BATTLE_LOG_WINDOWS, 'all'] as Array<number | 'all'>).map((window) => {
          const active = logWindow === window
          return <button
            key={window}
            type="button"
            aria-pressed={active}
            onClick={() => chooseWindow(window)}
            className={`focus-ring rounded-full border px-1.5 py-0.5 font-mono text-[9px] leading-none transition-colors ${active ? 'border-cyan-signal/45 bg-cyan-signal/10 text-cyan-signal' : 'border-white/[.07] text-zinc-600 hover:text-zinc-400'}`}
          >{t(`game.battleLogWindows.${window}`)}</button>
        })}
      </div>
    </div>}
    {expanded && <div className="max-h-[min(30dvh,16rem)] overflow-y-auto border-t border-white/[.07]">
      {visible.length ? <ul className="px-3.5 py-2">
        {visible.slice(0, ROW_RENDER_CAP).map((entry, index) => <li key={`${entry.ts ?? ''}-${entry.tick ?? ''}-${index}`} className="flex gap-2 py-1">
          <span className="w-16 shrink-0 pt-px text-right font-mono text-[9px] leading-4 text-zinc-600">
            {entry.ts !== null ? timeFormat.format(new Date(entry.ts * 1000)) : ''}
            {entry.tick !== null ? ` ·t${entry.tick}` : ''}
          </span>
          <span className={`min-w-0 flex-1 break-words text-[10px] leading-4 ${CATEGORY_TONES[entry.cat] ?? 'text-zinc-300'}`}>
            {splitLogMessage(entry.msg).map((segment, segmentIndex) => segment.kind === 'coord'
              ? <button
                key={segmentIndex}
                type="button"
                onClick={() => onJump(segment.position)}
                title={t('game.jumpToLogPosition')}
                className="cursor-pointer rounded-[3px] border-b border-dashed border-white/45 px-px whitespace-nowrap transition-colors hover:border-[#6ea8ff] hover:bg-[#6ea8ff38]"
              >{segment.text}</button>
              : <span key={segmentIndex}>{segment.text}</span>)}
          </span>
        </li>)}
        {visible.length > ROW_RENDER_CAP && <li className="pt-1 text-[9px] text-zinc-600">{t('game.battleLogMore', { count: visible.length - ROW_RENDER_CAP })}</li>}
      </ul> : <p className="px-3.5 py-3 text-[11px] text-zinc-500">{t('game.battleLogEmpty')}</p>}
    </div>}
  </section>
}
