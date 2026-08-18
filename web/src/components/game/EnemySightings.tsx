import { useMemo } from 'react'
import { useTranslation } from 'react-i18next'
import { maximumHealth } from '../../lib/gameRules'
import type { PlayerState, WorldObject } from '../../lib/types'
import { UnitArtIcon } from './UnitArtIcon'

// Compact top-left map overlay: one horizontal chip per visible enemy showing
// only its sprite and a segmented HP bar. Clicking a chip jumps the camera
// to that enemy so the operator can order units into combat.
export function EnemySightings({ state, onJump }: { state: PlayerState; onJump: (enemy: WorldObject) => void }) {
  const { t } = useTranslation()
  const enemies = useMemo(() => {
    const home = state.objects.find((object) => object.kind === 'CORE' && object.controlled)?.position
    const distance = (enemy: WorldObject) => home && enemy.position
      ? Math.abs(enemy.position[0] - home[0]) + Math.abs(enemy.position[1] - home[1])
      : Number.MAX_SAFE_INTEGER
    // Nearest threats first: the strip is a combat queue, not an inventory.
    return state.objects
      .filter((object) => object.controlled === false && (object.kind === 'UNIT' || object.kind === 'CORE') && object.position)
      .sort((left, right) => distance(left) - distance(right))
  }, [state.objects])
  if (!enemies.length) return null

  return <section aria-label={t('game.enemySightings')} className="absolute left-3 top-16 z-20 flex max-w-[min(20rem,calc(100%-1.5rem))] flex-wrap gap-1 lg:top-5">
    {enemies.map((enemy) => {
      const artType = enemy.kind === 'CORE' ? 'CORE' : enemy.unit_type ?? 'WORKER'
      const name = enemy.kind === 'CORE' ? t('game.units.CORE') : t(`game.units.${enemy.unit_type}`)
      const maxHp = maximumHealth(enemy)
      const hp = Math.max(0, Math.min(enemy.hp ?? 0, maxHp))
      return <button
        key={enemy.id ?? `${enemy.position}`}
        type="button"
        onClick={() => onJump(enemy)}
        aria-label={`${name} [${enemy.position?.join(', ')}] ${enemy.hp} HP`}
        title={t('game.jumpToEnemy')}
        className="focus-ring relative grid size-10 shrink-0 place-items-center rounded-gold-sm border border-coral-hostile/25 bg-space-900/80 pb-1.5 shadow-lg shadow-black/30 backdrop-blur transition-colors hover:border-coral-hostile/60 hover:bg-coral-hostile/[.14]"
      >
        <UnitArtIcon type={artType} className="size-6" />
        {maxHp > 0 && <span aria-hidden="true" className="absolute inset-x-1 bottom-1 flex gap-px">
          {Array.from({ length: maxHp }, (_, index) => <i key={index} className={`h-[3px] flex-1 rounded-full ${index < hp ? 'bg-coral-hostile' : 'bg-white/15'}`} />)}
        </span>}
      </button>
    })}
  </section>
}
