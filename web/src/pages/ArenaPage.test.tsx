import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { useEffect } from 'react'
import '../lib/i18n'
import type { EnemySightingType } from '../lib/enemyMemory'
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
const unitNames = vi.hoisted(() => ({ names: {} as Record<string, string> }))
vi.mock('../hooks/useUnitNames', () => ({ useUnitNames: () => unitNames.names }))
const waypoints = vi.hoisted(() => ({
  waypoints: {} as Record<string, { queue: [number, number][]; mode: 'attack' | 'rush' }>,
  refresh: vi.fn(),
}))
vi.mock('../hooks/useWaypoints', () => ({ useWaypoints: () => waypoints }))
const memory = vi.hoisted(() => ({
  // [3, 1] overlaps a live enemy and must be filtered out by the page.
  sightings: [
    { position: [8, 4] as [number, number], type: 'VANGUARD' as const, tick: 900 },
    { position: [3, 1] as [number, number], type: 'CORE' as const, tick: 905 },
    { position: [-6, 9] as [number, number], type: 'ENEMY' as const, tick: 910 },
  ] as Array<{ position: [number, number]; type: EnemySightingType; tick: number }>,
}))
vi.mock('../hooks/useEnemyMemory', () => ({ useEnemyMemory: () => memory.sightings }))
const originalMemorySightings = memory.sightings
vi.mock('../components/game/WorldCanvas', () => ({
  WorldCanvas: ({ centerPosition, centerRequest, selectedId, attackPositions = [], coordPicking = false, memoryEnemies = [], beaconIndicatorVisible = true, coreIndicatorVisible = true, onAttackPosition, onCoordPick, onAnchorChange }: { centerPosition?: [number, number] | null; centerRequest: number; selectedId: string | null; attackPositions?: [number, number][]; coordPicking?: boolean; memoryEnemies?: Array<{ position: [number, number]; type: string }>; beaconIndicatorVisible?: boolean; coreIndicatorVisible?: boolean; onAttackPosition?: (position: [number, number]) => void; onCoordPick?: (position: [number, number]) => void; onAnchorChange: (anchor: { x: number; y: number; side: 'right' } | null) => void }) => {
    useEffect(() => { onAnchorChange(selectedId ? { x: 100, y: 100, side: 'right' } : null) }, [onAnchorChange, selectedId])
    return <div
        data-testid="world-canvas"
        data-center-position={centerPosition ? JSON.stringify(centerPosition) : ''}
        data-center-request={centerRequest}
        data-beacon-indicator={String(beaconIndicatorVisible)}
        data-core-indicator={String(coreIndicatorVisible)}
        data-memory={JSON.stringify(memoryEnemies)}
      >
        {attackPositions.some(([x, y]) => x === 3 && y === 0) && <button type="button" onClick={() => onAttackPosition?.([3, 0])}>Attack predicted cell</button>}
        {coordPicking && <button type="button" onClick={() => onCoordPick?.([7, -3])}>Pick map cell</button>}
      </div>
  },
}))

describe('ArenaPage asset selection', () => {
  beforeEach(() => { game.submit.mockReset(); teams.updateConfig.mockReset(); localStorage.clear(); unitNames.names = {}; waypoints.waypoints = {}; waypoints.refresh.mockReset(); memory.sightings = originalMemorySightings; vi.unstubAllGlobals() })

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
  beforeEach(() => { localStorage.clear(); memory.sightings = originalMemorySightings })

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

  it('toggles the Beacon and Core direction indicators and remembers both choices', async () => {
    const user = userEvent.setup()
    render(<ArenaPage />)
    const map = screen.getByTestId('world-canvas')
    expect(map).toHaveAttribute('data-beacon-indicator', 'true')
    expect(map).toHaveAttribute('data-core-indicator', 'true')

    await user.click(screen.getByRole('button', { name: 'Hide Beacon indicator' }))
    expect(map).toHaveAttribute('data-beacon-indicator', 'false')
    expect(screen.getByRole('button', { name: 'Show Beacon indicator' })).toHaveAttribute('aria-pressed', 'false')
    expect(localStorage.getItem('arena-hero.beacon-indicator-visible.player')).toBe('false')

    await user.click(screen.getByRole('button', { name: 'Hide Core indicator' }))
    expect(map).toHaveAttribute('data-core-indicator', 'false')
    expect(screen.getByRole('button', { name: 'Show Core indicator' })).toHaveAttribute('aria-pressed', 'false')
    expect(localStorage.getItem('arena-hero.core-indicator-visible.player')).toBe('false')

    await user.click(screen.getByRole('button', { name: 'Show Beacon indicator' }))
    await user.click(screen.getByRole('button', { name: 'Show Core indicator' }))
    expect(map).toHaveAttribute('data-beacon-indicator', 'true')
    expect(map).toHaveAttribute('data-core-indicator', 'true')
    expect(localStorage.getItem('arena-hero.beacon-indicator-visible.player')).toBe('true')
    expect(localStorage.getItem('arena-hero.core-indicator-visible.player')).toBe('true')
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

  const memoryChips = () => screen.getAllByRole('button', { name: /· Last known position/ })

  it('caps the top-left memory strip at the 21 most recent entries while the map keeps them all', () => {
    memory.sightings = Array.from({ length: 30 }, (_, index) => ({
      position: [index, 100 + index] as [number, number],
      type: 'CORE' as const,
      tick: index + 1,
    }))
    render(<ArenaPage />)

    // Only the 21 most recently seen cores fill the strip (ticks 30..10).
    const chips = memoryChips()
    expect(chips).toHaveLength(21)
    expect(chips[0].getAttribute('aria-label')).toBe('Core [29, 129] · Last known position')
    expect(chips[20].getAttribute('aria-label')).toBe('Core [9, 109] · Last known position')
    expect(screen.queryByRole('button', { name: 'Core [8, 108] · Last known position' })).not.toBeInTheDocument()

    // The map markers are unaffected by the strip cap.
    const map = screen.getByTestId('world-canvas')
    expect(JSON.parse(map.getAttribute('data-memory') ?? '[]')).toHaveLength(30)
  })

  it('counts the 21-entry cap against the enabled filter combination only', async () => {
    memory.sightings = [
      ...Array.from({ length: 15 }, (_, index) => ({ position: [index, 100 + index] as [number, number], type: 'VANGUARD' as const, tick: 300 + index })),
      ...Array.from({ length: 15 }, (_, index) => ({ position: [index, 200 + index] as [number, number], type: 'WORKER' as const, tick: 200 + index })),
      ...Array.from({ length: 15 }, (_, index) => ({ position: [index, 300 + index] as [number, number], type: 'CORE' as const, tick: 100 + index })),
    ]
    const user = userEvent.setup()
    render(<ArenaPage />)

    // All three filters on: 21 across the combined types (the newest 15
    // vanguards + 6 workers), not 21 per type.
    expect(memoryChips()).toHaveLength(21)
    expect(memoryChips().some((chip) => chip.getAttribute('aria-label')!.startsWith('Core'))).toBe(false)

    // Dropping the core filter keeps the cap relative to the remaining
    // vanguard + worker pool (30 entries -> still 21).
    await user.click(screen.getByRole('button', { name: 'Core memory filter' }))
    expect(memoryChips()).toHaveLength(21)

    // Vanguard-only: 15 entries fit under the cap, so all of them show.
    await user.click(screen.getByRole('button', { name: 'Worker memory filter' }))
    expect(memoryChips()).toHaveLength(15)
    expect(memoryChips()[0].getAttribute('aria-label')).toBe('Vanguard [14, 114] · Last known position')

    // Core-only: the cap restarts for that single-type combination.
    await user.click(screen.getByRole('button', { name: 'Vanguard memory filter' }))
    await user.click(screen.getByRole('button', { name: 'Core memory filter' }))
    expect(memoryChips()).toHaveLength(15)
    expect(memoryChips()[0].getAttribute('aria-label')).toBe('Core [14, 314] · Last known position')
  })

  it('adds a manual target by picking a map cell from the unit dialog', async () => {
    unitNames.names = { worker: 'W1' }
    waypoints.waypoints = { W1: { queue: [[7, -3]], mode: 'attack' } }
    const fetchMock = vi.fn(async () => ({ ok: true, json: async () => ({ ok: true }) }) as Response)
    vi.stubGlobal('fetch', fetchMock)
    const user = userEvent.setup()
    render(<ArenaPage />)

    // With dashboard names loaded the asset list labels the Worker as W1.
    await user.click(screen.getByRole('button', { name: /W1.*12, -7/ }))
    expect(screen.getByText('Manual targets')).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: 'Choose target point' }))
    expect(screen.queryByText('Manual targets')).not.toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Pick map cell' }))

    expect(fetchMock).toHaveBeenCalledOnce()
    const [path, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit]
    expect(path).toBe('/api/waypoint/set')
    expect(JSON.parse(init.body as string)).toEqual({ name: 'W1', x: 7, y: -3, mode: 'attack' })
    expect(waypoints.refresh).toHaveBeenCalled()
    // The dialog reopens and shows the queue from the refreshed waypoint state.
    expect(screen.getByText('[7, -3]')).toBeInTheDocument()
  })
})
