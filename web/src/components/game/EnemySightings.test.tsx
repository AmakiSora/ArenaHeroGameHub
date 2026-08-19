import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import '../../lib/i18n'
import type { EnemySighting } from '../../lib/enemyMemory'
import type { PlayerState, WorldObject } from '../../lib/types'
import { EnemySightings } from './EnemySightings'

const ownCore: WorldObject = { kind: 'CORE', id: 'core', controlled: true, position: [0, 0], hp: 5 }
const stateWith = (objects: WorldObject[]): PlayerState => ({ status: 'ACTIVE', resources: 0, population: 0, champion_beacon: { position: [9, 9] }, objects, events: [] })
const hpSegments = (chip: HTMLElement) => chip.querySelectorAll('[aria-hidden="true"] > i').length
const filledSegments = (chip: HTMLElement) => chip.querySelectorAll('[aria-hidden="true"] > i.bg-coral-hostile').length

describe('EnemySightings', () => {
  it('renders nothing when no enemy is in view', () => {
    const resource: WorldObject = { kind: 'RESOURCE', positions: [[4, 0]] }
    const { container } = render(<EnemySightings state={stateWith([ownCore, resource])} onJump={() => undefined} />)

    expect(container).toBeEmptyDOMElement()
  })

  it('renders one chip per enemy nearest to the own core first, skipping resources', () => {
    const near: WorldObject = { kind: 'UNIT', id: 'near', controlled: false, position: [3, 1], hp: 3, unit_type: 'VANGUARD' }
    const far: WorldObject = { kind: 'UNIT', id: 'far', controlled: false, position: [-9, 0], hp: 2, unit_type: 'WORKER' }
    const enemyCore: WorldObject = { kind: 'CORE', id: 'enemy-core', controlled: false, position: [6, 1], hp: 4 }
    const resource: WorldObject = { kind: 'RESOURCE', positions: [[1, 0]] }

    render(<EnemySightings state={stateWith([ownCore, far, enemyCore, near, resource])} onJump={() => undefined} />)

    const chips = screen.getAllByRole('button', { name: /./ })
    expect(chips.map((chip) => chip.getAttribute('aria-label'))).toEqual([
      'Vanguard [3, 1] 3 HP',
      'Core [6, 1] 4 HP',
      'Worker [-9, 0] 2 HP',
    ])
    // Each chip only exposes sprite + HP bar: segments follow the unit rules.
    expect(hpSegments(chips[0])).toBe(4)
    expect(filledSegments(chips[0])).toBe(3)
    expect(hpSegments(chips[1])).toBe(5)
    expect(filledSegments(chips[1])).toBe(4)
    expect(hpSegments(chips[2])).toBe(2)
    expect(filledSegments(chips[2])).toBe(2)
  })

  it('reports the clicked enemy so the page can jump to it', () => {
    const enemy: WorldObject = { kind: 'UNIT', id: 'near', controlled: false, position: [3, 1], hp: 4, unit_type: 'VANGUARD' }
    const onJump = vi.fn()

    render(<EnemySightings state={stateWith([ownCore, enemy])} onJump={onJump} />)
    fireEvent.click(screen.getByRole('button', { name: /Vanguard/ }))

    expect(onJump).toHaveBeenCalledWith(enemy)
  })

  it('appends remembered enemies after visible ones with dimmed dashed chips', () => {
    const visibleEnemy: WorldObject = { kind: 'UNIT', id: 'near', controlled: false, position: [3, 1], hp: 4, unit_type: 'VANGUARD' }
    const sightings: EnemySighting[] = [
      { position: [6, 2], type: 'RANGER', tick: 12 },
      { position: [-4, -4], type: 'ENEMY', tick: 9 },
    ]
    const onJumpTo = vi.fn()

    render(<EnemySightings state={stateWith([ownCore, visibleEnemy])} onJump={() => undefined} sightings={sightings} onJumpTo={onJumpTo} />)

    const chips = screen.getAllByRole('button')
    expect(chips).toHaveLength(3)
    // Memory chips read dimmer (dashed frame + lowered opacity) and carry no
    // HP bar, since the memory stores only the last-known position.
    expect(chips[1].getAttribute('aria-label')).toBe('Ranger [6, 2] · Last known position')
    expect(chips[1].className).toContain('border-dashed')
    expect(chips[1].className).toContain('opacity-70')
    expect(hpSegments(chips[1])).toBe(0)
    // Unknown types fall back to the generic Enemy label.
    expect(chips[2].getAttribute('aria-label')).toBe('Enemy [-4, -4] · Last known position')
    fireEvent.click(chips[1])
    expect(onJumpTo).toHaveBeenCalledWith([6, 2])
  })

  it('renders memory-only sightings even when no live enemy is visible', () => {
    const sightings: EnemySighting[] = [{ position: [2, 2], type: 'CORE', tick: 3 }]

    render(<EnemySightings state={stateWith([ownCore])} onJump={() => undefined} sightings={sightings} onJumpTo={() => undefined} />)

    expect(screen.getByRole('button', { name: 'Core [2, 2] · Last known position' })).toBeInTheDocument()
  })
})
