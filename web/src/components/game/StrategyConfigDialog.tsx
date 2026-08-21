import { useEffect, useState } from 'react'
import type { RefObject } from 'react'
import { useTranslation } from 'react-i18next'
import { AccountDialog } from '../account/AccountDialog'
import { loadStrategyValues, saveStrategyValues, STRATEGY_GROUPS, type StrategyConfigValues, type StrategyField } from '../../lib/strategyConfig'

interface Props {
  returnFocusRef: RefObject<HTMLButtonElement | null>
  onClose: () => void
}

// Strategy settings dialog (核心/运行/生产): values load from /api/config on
// open, switches commit at once, numbers on blur/Enter (no POST per
// keystroke, same rhythm as the sidebar panels). A failed save rolls the
// local value back and surfaces an error line.
export function StrategyConfigDialog({ returnFocusRef, onClose }: Props) {
  const { t } = useTranslation()
  const [values, setValues] = useState<StrategyConfigValues>({})
  const [error, setError] = useState<string | null>(null)
  useEffect(() => {
    let cancelled = false
    void loadStrategyValues().then((next) => { if (!cancelled) setValues(next) })
    return () => { cancelled = true }
  }, [])
  const commit = (field: string, value: number | boolean) => {
    if (values[field] === value) return
    const previous = values
    setValues({ ...values, [field]: value })
    void saveStrategyValues({ [field]: value }).then((ok) => {
      if (ok) { setError(null); return }
      setValues(previous)
      setError(t('game.strategyConfig.saveFailed'))
    })
  }
  const commitNumber = (field: StrategyField & { kind: 'number' }, raw: string) => {
    const parsed = Number(raw)
    if (Number.isNaN(parsed)) return
    commit(field.field, Math.min(field.max, Math.max(field.min, Math.round(parsed))))
  }
  return <AccountDialog eyebrow={t('game.strategyConfig.eyebrow')} title={t('game.strategyConfig.title')} subtitle={t('game.strategyConfig.subtitle')} size="medium" returnFocusRef={returnFocusRef} onClose={onClose}>
    <div className="grid gap-4 sm:grid-cols-2">
      {STRATEGY_GROUPS.map((group) => <section key={group.key} className="rounded-gold border border-white/[.07] bg-black/25 p-3">
        <h3 className="mb-2 border-b border-white/[.06] pb-1.5 text-xs font-medium text-zinc-200">{t(`game.strategyConfig.${group.titleKey}`)}</h3>
        <div className="space-y-2">
          {group.fields.map((field) => field.kind === 'number'
            ? <label key={field.field} className="flex items-center justify-between gap-3 text-xs text-zinc-300">
              <span>{t(`game.strategyConfig.${field.labelKey}`)}</span>
              <input type="number" min={field.min} max={field.max} step={field.step ?? 1} key={`${field.field}=${String(values[field.field] ?? '')}`} defaultValue={Number(values[field.field] ?? 0)} onBlur={(event) => commitNumber(field, event.currentTarget.value)} onKeyDown={(event) => { if (event.key === 'Enter') event.currentTarget.blur() }} className="w-20 shrink-0 rounded-gold-sm border border-white/[.08] bg-white/[.04] px-1.5 py-1 text-right font-mono text-xs text-zinc-200" />
            </label>
            : <label key={field.field} className="flex items-center justify-between gap-3 text-xs text-zinc-300">
              <span>{t(`game.strategyConfig.${field.labelKey}`)}</span>
              <input type="checkbox" checked={Boolean(values[field.field])} onChange={(event) => commit(field.field, event.target.checked)} className="size-4 shrink-0 accent-indigo-400" />
            </label>)}
        </div>
      </section>)}
    </div>
    {error && <p role="alert" className="mt-3 text-xs text-coral-hostile">{error}</p>}
  </AccountDialog>
}
