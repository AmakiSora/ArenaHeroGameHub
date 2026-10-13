import { useMemo, useState } from 'react'
import type { RefObject } from 'react'
import { useTranslation } from 'react-i18next'
import { AccountDialog } from '../account/AccountDialog'
import { guerrillaTrackColors, tracksBounds, type GuerrillaTrack } from '../../lib/guerrillaTracks'

interface Props {
  returnFocusRef: RefObject<HTMLButtonElement | null>
  onClose: () => void
  date: string | null
  dates: string[]
  units: GuerrillaTrack[]
  onSelectDate: (date: string) => void
}

// The route recorder writes one row per guerrilla unit per Tick, so a day is
// thousands of cells per unit — far wider than the map canvas' zoom floor lets
// you see. This dialog draws the whole day fitted into its own viewBox instead.
const AGE_BUCKETS = 5
const OVERVIEW_MIN_PX = 300

function formatDate(stamp: string) {
  return stamp.length === 8 ? `${stamp.slice(0, 4)}-${stamp.slice(4, 6)}-${stamp.slice(6)}` : stamp
}

// Split a polyline into AGE_BUCKETS chunks, oldest first, overlapping by one
// point so the line stays continuous while opacity ramps up toward "now".
function ageSegments(points: [number, number][]) {
  const chunkSize = Math.max(1, Math.ceil(points.length / AGE_BUCKETS))
  const total = Math.ceil(points.length / chunkSize)
  const segments: { key: string; points: string; opacity: number }[] = []
  for (let index = 0, start = 0; start < points.length; index++) {
    const end = Math.min(points.length, start + chunkSize + 1)
    if (end - start > 1) {
      segments.push({
        key: `${start}`,
        points: points.slice(start, end).map(([x, y]) => `${x},${y}`).join(' '),
        opacity: 0.14 + (0.78 * (index + 1)) / total,
      })
    }
    start = start + chunkSize
  }
  return segments
}

export function GuerrillaTracksDialog({ returnFocusRef, onClose, date, dates, units, onSelectDate }: Props) {
  const { t } = useTranslation()
  const [focus, setFocus] = useState<string | null>(null)
  const colors = useMemo(() => guerrillaTrackColors(units), [units])
  // Focusing a row draws (and frames) only that unit: a day of 20 overlapping
  // lines is unreadable, one unit's line is the actual question.
  const shown = focus ? units.filter((unit) => unit.id === focus) : units
  const box = useMemo(() => {
    const bounds = tracksBounds(shown)
    if (!bounds) return null
    const [minX, minY, maxX, maxY] = bounds
    const padX = Math.max(8, (maxX - minX) * .05)
    const padY = Math.max(8, (maxY - minY) * .05)
    return `${minX - padX} ${minY - padY} ${maxX - minX + padX * 2} ${maxY - minY + padY * 2}`
  }, [shown])
  return <AccountDialog eyebrow={t('game.guerrillaTracks.eyebrow')} title={t('game.guerrillaTracks.title')} subtitle={t('game.guerrillaTracks.subtitle', { unitCount: units.length, date: date ? formatDate(date) : '—' })} returnFocusRef={returnFocusRef} onClose={onClose}>
    <div className="space-y-3">
      {dates.length > 1 && <div className="flex flex-wrap items-center gap-1.5">
        <span className="text-xs text-zinc-500">{t('game.guerrillaTracks.date')}</span>
        {dates.map((stamp) => <button
          key={stamp}
          type="button"
          onClick={() => onSelectDate(stamp)}
          aria-pressed={stamp === date}
          className={`focus-ring min-h-9 rounded-gold-sm border px-2.5 font-mono text-xs transition-colors ${stamp === date ? 'border-[#d9a62e]/45 bg-[#d9a62e]/[.14] text-[#e1b64e]' : 'border-white/[.08] bg-white/[.04] text-zinc-400 hover:bg-white/[.08] hover:text-zinc-200'}`}
        >{formatDate(stamp)}</button>)}
      </div>}
      {box && <div className="rounded-gold border border-white/[.07] bg-black/35 p-2">
        {/* World Y grows downward, same as the map canvas, so the day reads in
            the same orientation as the live view. */}
        <svg viewBox={box} role="img" aria-label={t('game.guerrillaTracks.overview')} className="block h-[var(--overview-height)] w-full" style={{ ['--overview-height' as string]: `${OVERVIEW_MIN_PX}px` }}>
          {shown.map((unit) => <g key={unit.id}>
            {ageSegments(unit.points).map((segment) => <polyline
              key={segment.key}
              points={segment.points}
              fill="none"
              stroke={colors.get(unit.id) ?? '#78a9ff'}
              strokeWidth={1.4}
              strokeLinejoin="round"
              strokeLinecap="round"
              vectorEffect="non-scaling-stroke"
              opacity={segment.opacity}
            />)}
          </g>)}
        </svg>
      </div>}
      {!units.length && <p className="rounded-gold border border-white/[.07] bg-black/25 px-3 py-4 text-center text-xs text-zinc-500">{t('game.guerrillaTracks.empty')}</p>}
      {!!units.length && <div className="overflow-x-auto rounded-gold border border-white/[.07]">
        <table className="w-full text-left font-mono text-xs">
          <thead className="bg-white/[.03] text-zinc-500">
            <tr>
              <th className="px-2 py-1.5 font-medium">{t('game.guerrillaTracks.unit')}</th>
              <th className="px-2 py-1.5 text-right font-medium">{t('game.guerrillaTracks.ticks')}</th>
              <th className="px-2 py-1.5 text-right font-medium">{t('game.guerrillaTracks.cells')}</th>
              <th className="px-2 py-1.5 text-right font-medium">{t('game.guerrillaTracks.net')}</th>
              <th className="px-2 py-1.5 text-right font-medium">{t('game.guerrillaTracks.span')}</th>
            </tr>
          </thead>
          <tbody>
            {units.map((unit) => {
              const active = unit.id === focus
              const span = unit.bounds ? `${unit.bounds[2] - unit.bounds[0]}×${unit.bounds[3] - unit.bounds[1]}` : '—'
              return <tr
                key={unit.id}
                onClick={() => setFocus(active ? null : unit.id)}
                aria-selected={active}
                className={`cursor-pointer border-t border-white/[.05] transition-colors ${active ? 'bg-white/[.07]' : 'hover:bg-white/[.04]'} ${focus && !active ? 'opacity-45' : ''}`}
              >
                <th className="px-2 py-1.5 text-left font-normal">
                  <span className="mr-1.5 inline-block size-2.5 rounded-full align-middle" style={{ background: colors.get(unit.id) ?? '#78a9ff' }} aria-hidden="true" />
                  {unit.name}
                </th>
                <td className="px-2 py-1.5 text-right text-zinc-300">{unit.ticks}</td>
                <td className="px-2 py-1.5 text-right text-zinc-300">{unit.distinct}</td>
                <td className="px-2 py-1.5 text-right text-zinc-300">{unit.net}</td>
                <td className="px-2 py-1.5 text-right text-zinc-500">{span}</td>
              </tr>
            })}
          </tbody>
        </table>
      </div>}
      {!!units.length && <p className="text-[.6875rem] leading-5 text-zinc-500">{t('game.guerrillaTracks.hint')}</p>}
    </div>
  </AccountDialog>
}
