import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { useEffect } from 'react'
import '../lib/i18n'
import { ArenaPage } from './ArenaPage'

const game = vi.hoisted(() => ({
  tick: 42,
  state: {
    status: 'ACTIVE' as const,
    resources: 8,
    population: 2,
    champion_beacon: { position: [0, 0] as [number, number] },
    objects: [
      { kind: 'CORE' as const, id: 'core', controlled: true, position: [0, 0] as [number, number], hp: 5, shield: 5, state: 'NORMAL' as const },
      { kind: 'UNIT' as const, id: 'worker', controlled: true, position: [12, -7] as [number, number], hp: 2, unit_type: 'WORKER' as const, cargo: 0 },
      { kind: 'UNIT' as const, id: 'ranger', controlled: true, position: [0, 0] as [number, number], hp: 2, unit_type: 'RANGER' as const },
      { kind: 'UNIT' as const, id: 'high', controlled: false, position: [3, 1] as [number, number], hp: 4, unit_type: 'VANGUARD' as const },
      { kind: 'UNIT' as const, id: 'low', controlled: false, position: [4, 0] as [number, number], hp: 1, unit_type: 'WORKER' as const },
    ],
    events: [],
  },
  explored: new Map(),
  phase: 'open' as const,
  stateReceivedAt: Date.now(),
  receipts: {},
  submit: vi.fn(),
  error: null,
}))

vi.mock('../hooks/useGameStream', () => ({ useGameStream: () => game }))
vi.mock('../context/AuthContext', () => ({ useAuth: () => ({ user: { username: 'player' } }) }))
const teams = vi.hoisted(() => ({
  updateConfig: vi.fn(),
  config: { attack_mode: 'coords', attack_target_x: 10, attack_target_y: -4 } as Record<string, number | boolean | string>,
}))
vi.mock('../hooks/useUnitTeams', () => ({ useUnitTeams: () => ({ roster: {}, config: teams.config, assignTeam: vi.fn(), updateConfig: teams.updateConfig }) }))
vi.mock('../hooks/useUnitNames', () => ({ useUnitNames: () => ({}) }))
const memory = vi.hoisted(() => ({
  // [3, 1] overlaps a live enemy and must be filtered out by the page.
  sightings: [
    { position: [8, 4] as [number, number], type: 'VANGUARD' as const, tick: 900 },
    { position: [3, 1] as [number, number], type: 'CORE' as const, tick: 905 },
    { position: [-6, 9] as [number, number], type: 'ENEMY' as const, tick: 910 },
  ],
}))
vi.mock('../hooks/useEnemyMemory', () => ({ useEnemyMemory: () => memory.sightings }))
vi.mock('../components/game/WorldCanvas', () => ({
  WorldCanvas: ({ centerPosition, centerRequest, selectedId, attackPositions = [], coordPicking = false, memoryEnemies = [], onAttackPosition, onCoordPick, onAnchorChange }: { centerPosition?: [number, number] | null; centerRequest: number; selectedId: string | null; attackPositions?: [number, number][]; coordPicking?: boolean; memoryEnemies?: Array<{ position: [number, number]; type: string }>; onAttackPosition?: (position: [number, number]) => void; onCoordPick?: (position: [number, number]) => void; onAnchorChange: (anchor: { x: number; y: number; side: 'right' } | null) => void }) => {
    useEffect(() => { onAnchorChange(selectedId ? { x: 100, y: 100, side: 'right' } : null) }, [onAnchorChange, selectedId])
    return <div
        data-testid="world-canvas"
        data-center-position={centerPosition ? JSON.stringify(centerPosition) : ''}
        data-center-request={centerRequest}
        data-memory={JSON.stringify(memoryEnemies)}
      >
        {attackPositions.some(([x, y]) => x === 3 && y === 0) && <button type="button" onClick={() => onAttackPosition?.([3, 0])}>Attack predicted cell</button>}
        {coordPicking && <button type="button" onClick={() => onCoordPick?.([7, -3])}>Pick map cell</button>}
      </div>
  },
}))

describe('ArenaPage asset selection', () => {
  beforeEach(() => { game.submit.mockReset(); teams.updateConfig.mockReset(); localStorage.clear() })

  it('centers the map on a Unit selected from the asset list', async () => {
    render(<ArenaPage demo />)
    const map = screen.getByTestId('world-canvas')
    expect(map).toHaveAttribute('data-center-position', '')
    expect(map).toHaveAttribute('data-center-request', '0')

    // The enemy-sightings panel also shows a 'Worker' row, so target the
    // asset-list entry by its position text.
    await userEvent.click(screen.getByRole('button', { name: /Worker.*12, -7/ }))

    expect(map).toHaveAttribute('data-center-position', '[12,-7]')
    expect(map).toHaveAttribute('data-center-request', '1')
  })

  it('centers the map on an enemy clicked in the sightings panel', async () => {
    render(<ArenaPage demo />)
    const map = screen.getByTestId('world-canvas')

    await userEvent.click(screen.getByRole('button', { name: /Vanguard.*3, 1/ }))

    expect(map).toHaveAttribute('data-center-position', '[3,1]')
    expect(map).toHaveAttribute('data-center-request', '1')
  })

	it('submits a Ranger cell shot without requiring a visible target', async () => {
    const user = userEvent.setup()
    render(<ArenaPage demo />)

    await user.click(screen.getByText('Ranger'))
    await user.click(screen.getByRole('button', { name: 'Shoot' }))
    await user.click(screen.getByRole('button', { name: 'Attack predicted cell' }))

		await waitFor(() => expect(game.submit).toHaveBeenCalledWith({
			tick: 42,
			unit_actions: { ranger: { type: 'SHOOT', expected_cell: [3, 0] } },
		}))
  })

  it('picks the attack squad target coordinates from the map and saves both fields', async () => {
    const user = userEvent.setup()
    render(<ArenaPage />)

    await user.click(screen.getByRole('tab', { name: 'Combat Squads' }))
    await user.click(screen.getByRole('button', { name: 'Settings · Attack Squad' }))
    await user.click(screen.getByRole('button', { name: 'Pick on map · Target X' }))
    expect(screen.getByText('Click the map to choose the target coordinates')).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: 'Pick map cell' }))
    expect(teams.updateConfig).toHaveBeenNthCalledWith(1, 'attack_target_x', 7)
    expect(teams.updateConfig).toHaveBeenNthCalledWith(2, 'attack_target_y', -3)
    expect(screen.queryByText('Click the map to choose the target coordinates')).not.toBeInTheDocument()
  })
})

describe('ArenaPage remembered enemies', () => {
  beforeEach(() => localStorage.clear())

  it('shows memory markers on the map and dimmed chips, skipping cells a live enemy occupies', async () => {
    const user = userEvent.setup()
    render(<ArenaPage />)
    const map = screen.getByTestId('world-canvas')

    // [3, 1] collides with the live Vanguard, so only two markers survive.
    expect(map).toHaveAttribute('data-memory', JSON.stringify([
      { position: [8, 4], type: 'VANGUARD', tick: 900 },
      { position: [-6, 9], type: 'ENEMY', tick: 910 },
    ]))
    expect(screen.getByRole('button', { name: 'Vanguard [8, 4] · Last known position' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Enemy [-6, 9] · Last known position' })).toBeInTheDocument()

    // Clicking a memory chip centers the camera on the remembered position.
    await user.click(screen.getByRole('button', { name: 'Vanguard [8, 4] · Last known position' }))
    expect(map).toHaveAttribute('data-center-position', '[8,4]')
    expect(map).toHaveAttribute('data-center-request', '1')
  })

  it('toggles memory markers from the bottom-left control and remembers the choice', async () => {
    const user = userEvent.setup()
    render(<ArenaPage />)
    const map = screen.getByTestId('world-canvas')

    await user.click(screen.getByRole('button', { name: 'Hide remembered enemies' }))
    expect(map).toHaveAttribute('data-memory', '[]')
    expect(screen.queryByRole('button', { name: /Last known position/ })).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Show remembered enemies' })).toHaveAttribute('aria-pressed', 'false')
    expect(localStorage.getItem('arena-hero.enemy-memory-visible.player')).toBe('false')

    await user.click(screen.getByRole('button', { name: 'Show remembered enemies' }))
    expect(map.getAttribute('data-memory')).not.toBe('[]')
    expect(localStorage.getItem('arena-hero.enemy-memory-visible.player')).toBe('true')
  })

  it('filters memory markers by unit type and remembers the filter set', async () => {
    const user = userEvent.setup()
    render(<ArenaPage />)
    const map = screen.getByTestId('world-canvas')
    const vanguardFilter = screen.getByRole('button', { name: 'Vanguard memory filter' })
    expect(vanguardFilter).toHaveAttribute('aria-pressed', 'true')

    await user.click(vanguardFilter)
    expect(map).toHaveAttribute('data-memory', JSON.stringify([
      { position: [-6, 9], type: 'ENEMY', tick: 910 },
    ]))
    expect(screen.queryByRole('button', { name: 'Vanguard [8, 4] · Last known position' })).not.toBeInTheDocument()
    expect(vanguardFilter).toHaveAttribute('aria-pressed', 'false')
    expect(localStorage.getItem('arena-hero.enemy-memory-filters.player')).toBe(JSON.stringify(['WORKER', 'RANGER', 'CORE']))

    // Unknown (ENEMY) markers ignore the per-type filters.
    await user.click(screen.getByRole('button', { name: 'Worker memory filter' }))
    await user.click(screen.getByRole('button', { name: 'Ranger memory filter' }))
    await user.click(screen.getByRole('button', { name: 'Core memory filter' }))
    expect(map).toHaveAttribute('data-memory', JSON.stringify([
      { position: [-6, 9], type: 'ENEMY', tick: 910 },
    ]))

    await user.click(vanguardFilter)
    expect(map.getAttribute('data-memory')).toContain('[8,4]')
  })
})
