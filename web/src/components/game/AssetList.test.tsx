import { fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import '../../lib/i18n'
import type { TeamKey } from '../../lib/teamRoster'
import { AssetList } from './AssetList'

const state = { status: 'ACTIVE' as const, resources: 28, population: 6, champion_beacon: { position: [0, 0] as [number, number] }, objects: [], events: [] }

describe('AssetList', () => {
  it('places game stats below the Arena Hero title', () => {
    render(<AssetList state={state} objects={[]} selectedId={null} onSelect={() => undefined} />)

    const title = screen.getByLabelText('Arena Hero')
    const stats = screen.getByRole('group', { name: 'Status' })
    const fleetTitle = screen.getByText('FLEET INDEX')
    expect(screen.getByRole('heading', { name: 'FLEET INDEX Your assets' })).toBeInTheDocument()
    expect(title.compareDocumentPosition(stats) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    expect(stats.compareDocumentPosition(fleetTitle) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    expect(screen.getByText('28/30')).toBeInTheDocument()
    expect(screen.getByText('Resources / capacity')).toBeInTheDocument()
    expect(screen.getByText('Population')).toBeInTheDocument()
  })

  it('shows a compact asset chip whose tooltip carries position and health without leaking the object id', () => {
    const worker = { kind: 'UNIT' as const, id: 'worker-12345678', controlled: true, position: [3, -2] as [number, number], hp: 2, unit_type: 'WORKER' as const, cargo: 0 }
    render(<AssetList state={state} objects={[worker]} selectedId={null} onSelect={() => undefined} />)

    expect(screen.getByText('Worker')).toBeInTheDocument()
    expect(screen.getByTitle('Worker · (3, -2) · HP 2/2')).toBeInTheDocument()
    expect(screen.queryByText(/\[3, -2\]/)).not.toBeInTheDocument()
    expect(screen.queryByText(/worker-12/)).not.toBeInTheDocument()
  })

  it('prefers the tactic-dashboard display name over the unit type label', () => {
    const worker = { kind: 'UNIT' as const, id: 'aaaaaaaa-1111-4000-8000', controlled: true, position: [1, 0] as [number, number], hp: 2, unit_type: 'WORKER' as const, cargo: 0 }
    render(<AssetList state={state} objects={[worker]} selectedId={null} onSelect={() => undefined} unitNames={{ aaaaaaaa: 'W7' }} />)

    expect(screen.getByText('W7')).toBeInTheDocument()
    expect(screen.queryByText('Worker')).not.toBeInTheDocument()
  })

  it('groups controlled assets into Core, Worker, Ranger and Vanguard squads', () => {
    const core = { kind: 'CORE' as const, id: 'core-1', controlled: true, position: [0, 0] as [number, number], hp: 12 }
    const worker = { kind: 'UNIT' as const, id: 'worker-1', controlled: true, position: [1, 0] as [number, number], hp: 2, unit_type: 'WORKER' as const, cargo: 0 }
    const ranger = { kind: 'UNIT' as const, id: 'ranger-1', controlled: true, position: [2, 0] as [number, number], hp: 3, unit_type: 'RANGER' as const }
    const vanguard = { kind: 'UNIT' as const, id: 'vanguard-1', controlled: true, position: [3, 0] as [number, number], hp: 4, unit_type: 'VANGUARD' as const }
    render(<AssetList state={state} objects={[ranger, core, vanguard, worker]} selectedId={null} onSelect={() => undefined} />)

    const coreGroup = screen.getByRole('region', { name: 'Core' })
    const workerGroup = screen.getByRole('region', { name: 'Worker Group' })
    const rangerGroup = screen.getByRole('region', { name: 'Ranger Group' })
    const vanguardGroup = screen.getByRole('region', { name: 'Vanguard Group' })
    expect(coreGroup).toHaveTextContent('Core')
    expect(workerGroup).toHaveTextContent('Worker')
    expect(rangerGroup).toHaveTextContent('Ranger')
    expect(vanguardGroup).toHaveTextContent('Vanguard')
    expect(coreGroup.compareDocumentPosition(workerGroup) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    expect(workerGroup.compareDocumentPosition(rangerGroup) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    expect(rangerGroup.compareDocumentPosition(vanguardGroup) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
  })

  it('hides squad sections that have no members', () => {
    const worker = { kind: 'UNIT' as const, id: 'worker-1', controlled: true, position: [1, 0] as [number, number], hp: 2, unit_type: 'WORKER' as const, cargo: 0 }
    render(<AssetList state={state} objects={[worker]} selectedId={null} onSelect={() => undefined} />)

    expect(screen.getByRole('region', { name: 'Worker Group' })).toBeInTheDocument()
    expect(screen.queryByRole('region', { name: 'Ranger Group' })).not.toBeInTheDocument()
    expect(screen.queryByRole('region', { name: 'Vanguard Group' })).not.toBeInTheDocument()
  })

  it('collapses and expands a squad when its header is clicked without affecting other squads', () => {
    const worker = { kind: 'UNIT' as const, id: 'worker-1', controlled: true, position: [1, 0] as [number, number], hp: 2, unit_type: 'WORKER' as const, cargo: 0 }
    const ranger = { kind: 'UNIT' as const, id: 'ranger-1', controlled: true, position: [2, 0] as [number, number], hp: 3, unit_type: 'RANGER' as const }
    render(<AssetList state={state} objects={[worker, ranger]} selectedId={null} onSelect={() => undefined} />)

    const workerHeader = screen.getByRole('button', { name: /Worker Group/ })
    expect(workerHeader).toHaveAttribute('aria-expanded', 'true')
    expect(screen.getByTitle('Worker · (1, 0) · HP 2/2')).toBeInTheDocument()

    fireEvent.click(workerHeader)
    expect(workerHeader).toHaveAttribute('aria-expanded', 'false')
    expect(screen.queryByTitle('Worker · (1, 0) · HP 2/2')).not.toBeInTheDocument()
    expect(screen.getByTitle('Ranger · (2, 0) · HP 3/2')).toBeInTheDocument()

    fireEvent.click(workerHeader)
    expect(workerHeader).toHaveAttribute('aria-expanded', 'true')
    expect(screen.getByTitle('Worker · (1, 0) · HP 2/2')).toBeInTheDocument()
  })

  describe('fleet view tabs', () => {
    afterEach(() => localStorage.clear())

    const vanguard = { kind: 'UNIT' as const, id: 'v1aaaaaa-0000', controlled: true, position: [1, 0] as [number, number], hp: 4, unit_type: 'VANGUARD' as const }
    const ranger = { kind: 'UNIT' as const, id: 'r1bbbbbb-0000', controlled: true, position: [2, 0] as [number, number], hp: 2, unit_type: 'RANGER' as const }
    const worker = { kind: 'UNIT' as const, id: 'w1cccccc-0000', controlled: true, position: [3, 0] as [number, number], hp: 2, unit_type: 'WORKER' as const, cargo: 0 }
    const names = { v1aaaaaa: 'V1', r1bbbbbb: 'R1', w1cccccc: 'W1' }

    it('shows the unit-type groups by default and switches to combat squads', () => {
      render(<AssetList state={state} objects={[vanguard, ranger, worker]} selectedId={null} onSelect={() => undefined} unitNames={names} teamRoster={{ V1: 'home', R1: 'kite' }} />)

      expect(screen.getByRole('tab', { name: 'Unit Groups' })).toHaveAttribute('aria-selected', 'true')
      expect(screen.getByRole('region', { name: 'Worker Group' })).toBeInTheDocument()
      expect(screen.queryByRole('region', { name: 'Home Squad' })).not.toBeInTheDocument()

      fireEvent.click(screen.getByRole('tab', { name: 'Combat Squads' }))
      expect(screen.getByRole('tab', { name: 'Combat Squads' })).toHaveAttribute('aria-selected', 'true')
      expect(screen.queryByRole('region', { name: 'Worker Group' })).not.toBeInTheDocument()
      expect(screen.getByRole('region', { name: 'Home Squad' })).toHaveTextContent('V1')
      expect(screen.getByRole('region', { name: 'Kite Squad' })).toHaveTextContent('R1')
      // 进攻队 stays visible while empty; workers never join a combat squad.
      expect(screen.getByRole('region', { name: 'Attack Squad' })).toBeInTheDocument()
      expect(screen.queryByText('W1')).not.toBeInTheDocument()
      // Guerrilla / standby pool hide while they hold no members.
      expect(screen.queryByRole('region', { name: 'Guerrilla Squad' })).not.toBeInTheDocument()
      expect(screen.queryByRole('region', { name: 'Standby Pool' })).not.toBeInTheDocument()
    })

    it('drops unnamed combat units into the standby pool', () => {
      render(<AssetList state={state} objects={[ranger]} selectedId={null} onSelect={() => undefined} teamRoster={{}} />)

      fireEvent.click(screen.getByRole('tab', { name: 'Combat Squads' }))
      expect(screen.getByRole('region', { name: 'Standby Pool' })).toHaveTextContent('Ranger')
    })

    it('remembers the chosen fleet view across remounts', () => {
      const first = render(<AssetList state={state} objects={[worker]} selectedId={null} onSelect={() => undefined} />)
      fireEvent.click(screen.getByRole('tab', { name: 'Combat Squads' }))
      first.unmount()

      render(<AssetList state={state} objects={[worker]} selectedId={null} onSelect={() => undefined} />)
      expect(screen.getByRole('tab', { name: 'Combat Squads' })).toHaveAttribute('aria-selected', 'true')
    })

    const dragStart = (element: HTMLElement) => fireEvent.dragStart(element, { dataTransfer: { setData: vi.fn(), effectAllowed: '' } })

    it('drags a named chip onto another squad and reports the assignment at once', () => {
      const assignments: Array<[string, TeamKey]> = []
      render(<AssetList state={state} objects={[vanguard, ranger, worker]} selectedId={null} onSelect={() => undefined} unitNames={names} teamRoster={{ V1: 'home', R1: 'kite' }} onAssignTeam={(name, team) => assignments.push([name, team])} />)

      fireEvent.click(screen.getByRole('tab', { name: 'Combat Squads' }))
      const chip = screen.getByText('V1').closest('button')!
      expect(chip).toHaveAttribute('draggable', 'true')
      dragStart(chip)
      fireEvent.dragOver(screen.getByRole('region', { name: 'Attack Squad' }), { dataTransfer: { dropEffect: '' } })
      fireEvent.drop(screen.getByRole('region', { name: 'Attack Squad' }), { dataTransfer: { getData: () => 'V1' } })
      expect(assignments).toEqual([['V1', 'attack']])
    })

    it('reveals empty squads as drop targets while a chip is being dragged', () => {
      render(<AssetList state={state} objects={[vanguard, ranger]} selectedId={null} onSelect={() => undefined} unitNames={names} teamRoster={{ V1: 'home', R1: 'attack' }} onAssignTeam={() => undefined} />)

      fireEvent.click(screen.getByRole('tab', { name: 'Combat Squads' }))
      expect(screen.queryByRole('region', { name: 'Standby Pool' })).not.toBeInTheDocument()
      const chip = screen.getByText('V1').closest('button')!
      dragStart(chip)
      expect(screen.getByRole('region', { name: 'Standby Pool' })).toHaveTextContent('Drop here')
      fireEvent.dragEnd(chip)
      expect(screen.queryByRole('region', { name: 'Standby Pool' })).not.toBeInTheDocument()
    })

    it('does not drag in the unit-groups view or without an assignment handler', () => {
      render(<AssetList state={state} objects={[vanguard]} selectedId={null} onSelect={() => undefined} unitNames={names} teamRoster={{ V1: 'home' }} />)

      // Groups view: chips never drag even when named.
      expect(screen.getByText('V1').closest('button')).not.toHaveAttribute('draggable', 'true')
      fireEvent.click(screen.getByRole('tab', { name: 'Combat Squads' }))
      // Squads view without an onAssignTeam handler (e.g. demo) stays read-only.
      expect(screen.getByText('V1').closest('button')).not.toHaveAttribute('draggable', 'true')
    })
  })

  describe('squad settings', () => {
    afterEach(() => localStorage.clear())

    const vanguard = { kind: 'UNIT' as const, id: 'v1aaaaaa-0000', controlled: true, position: [1, 0] as [number, number], hp: 4, unit_type: 'VANGUARD' as const }
    const baseConfig = { home_patrol_radius: 5, home_engage_radius: 0, home_engage_memory_ticks: 0, combat_heal_hp_threshold: 1, combat_heal_return_limit: 0, attack_mode: 'coords', attack_target_x: 10, attack_target_y: -4, attack_auto_radius: 0, attack_retreat_radius: 0, attack_march_engage_radius: 0, ranger_attack_range: 2, ranger_lead_fire_enabled: false }

    const openSquadsTab = (props: { teamConfig: Record<string, number | boolean | string>; onUpdateConfig: (field: string, value: number | boolean | string) => void }) => {
      render(<AssetList state={state} objects={[vanguard]} selectedId={null} onSelect={() => undefined} unitNames={{ v1aaaaaa: 'V1' }} teamRoster={{ V1: 'home' }} onAssignTeam={() => undefined} teamConfig={props.teamConfig} onUpdateConfig={props.onUpdateConfig} />)
      fireEvent.click(screen.getByRole('tab', { name: 'Combat Squads' }))
    }

    it('opens the settings panel from the gear button and commits numbers on blur', () => {
      const updates: Array<[string, number | boolean | string]> = []
      openSquadsTab({ teamConfig: baseConfig, onUpdateConfig: (field, value) => updates.push([field, value]) })

      fireEvent.click(screen.getByRole('button', { name: 'Settings · Home Squad' }))
      expect(screen.getByText('Home Strategy')).toBeInTheDocument()
      const input = screen.getByDisplayValue('5')
      fireEvent.change(input, { target: { value: '8' } })
      // No POST per keystroke: only blur commits.
      expect(updates).toEqual([])
      fireEvent.blur(input)
      expect(updates).toEqual([['home_patrol_radius', 8]])
    })

    it('commits selects and switches at once', () => {
      const updates: Array<[string, number | boolean | string]> = []
      openSquadsTab({ teamConfig: baseConfig, onUpdateConfig: (field, value) => updates.push([field, value]) })

      fireEvent.click(screen.getByRole('button', { name: 'Settings · Attack Squad' }))
      fireEvent.change(screen.getByDisplayValue('2'), { target: { value: '3' } })
      fireEvent.click(screen.getByRole('checkbox'))
      expect(updates).toEqual([['ranger_attack_range', 3], ['ranger_lead_fire_enabled', true]])
    })

    it('hides mode-gated parameters exactly like the dashboard form', () => {
      openSquadsTab({ teamConfig: { ...baseConfig, attack_mode: 'auto' }, onUpdateConfig: () => undefined })

      fireEvent.click(screen.getByRole('button', { name: 'Settings · Attack Squad' }))
      // auto mode: search range visible, coordinate inputs hidden.
      expect(screen.getByText('Search range')).toBeInTheDocument()
      expect(screen.queryByText('Target X')).not.toBeInTheDocument()
      expect(screen.getByRole('radio', { name: 'Auto' })).toHaveAttribute('aria-checked', 'true')
    })

    it('switches the squad mode through the radio group', () => {
      const updates: Array<[string, number | boolean | string]> = []
      openSquadsTab({ teamConfig: baseConfig, onUpdateConfig: (field, value) => updates.push([field, value]) })

      fireEvent.click(screen.getByRole('button', { name: 'Settings · Attack Squad' }))
      fireEvent.click(screen.getByRole('radio', { name: 'Champion beacon' }))
      expect(updates).toEqual([['attack_mode', 'beacon']])
    })

    it('shows no gear for the standby pool and none without an update handler', () => {
      render(<AssetList state={state} objects={[vanguard]} selectedId={null} onSelect={() => undefined} unitNames={{ v1aaaaaa: 'V1' }} teamRoster={{}} teamConfig={baseConfig} />)
      fireEvent.click(screen.getByRole('tab', { name: 'Combat Squads' }))
      expect(screen.queryByRole('button', { name: /Settings ·/ })).not.toBeInTheDocument()
    })

    it('hides the pick button outside coords mode together with the coordinate rows', () => {
      render(<AssetList state={state} objects={[vanguard]} selectedId={null} onSelect={() => undefined} unitNames={{ v1aaaaaa: 'V1' }} teamRoster={{ V1: 'attack' }} teamConfig={{ ...baseConfig, attack_mode: 'auto' }} onUpdateConfig={() => undefined} onPickCoords={() => undefined} />)
      fireEvent.click(screen.getByRole('tab', { name: 'Combat Squads' }))
      fireEvent.click(screen.getByRole('button', { name: 'Settings · Attack Squad' }))
      expect(screen.queryByRole('button', { name: /Pick on map/ })).not.toBeInTheDocument()
    })

    it('invokes onPickCoords with the X/Y field pair and hides it without a handler', () => {
      const picks: Array<[string, string]> = []
      render(<AssetList state={state} objects={[vanguard]} selectedId={null} onSelect={() => undefined} unitNames={{ v1aaaaaa: 'V1' }} teamRoster={{ V1: 'attack' }} onAssignTeam={() => undefined} teamConfig={baseConfig} onUpdateConfig={() => undefined} onPickCoords={(xField, yField) => picks.push([xField, yField])} />)
      fireEvent.click(screen.getByRole('tab', { name: 'Combat Squads' }))
      fireEvent.click(screen.getByRole('button', { name: 'Settings · Attack Squad' }))
      fireEvent.click(screen.getByRole('button', { name: 'Pick on map · Target X' }))
      expect(picks).toEqual([['attack_target_x', 'attack_target_y']])

      // Without an onPickCoords handler (e.g. demo) there is no pick button.
      const readOnly = render(<AssetList state={state} objects={[vanguard]} selectedId={null} onSelect={() => undefined} unitNames={{ v1aaaaaa: 'V1' }} teamRoster={{ V1: 'attack' }} teamConfig={baseConfig} onUpdateConfig={() => undefined} />)
      fireEvent.click(within(readOnly.container).getByRole('tab', { name: 'Combat Squads' }))
      fireEvent.click(within(readOnly.container).getByRole('button', { name: 'Settings · Attack Squad' }))
      expect(within(readOnly.container).queryByRole('button', { name: /Pick on map/ })).not.toBeInTheDocument()
    })

    it('offers the same pick button for the kite squad target', () => {
      const picks: Array<[string, string]> = []
      render(<AssetList state={state} objects={[vanguard]} selectedId={null} onSelect={() => undefined} unitNames={{ v1aaaaaa: 'V1' }} teamRoster={{ V1: 'kite' }} teamConfig={{ kite_mode: 'coords', kite_target_x: 0, kite_target_y: 0 }} onUpdateConfig={() => undefined} onPickCoords={(xField, yField) => picks.push([xField, yField])} />)
      fireEvent.click(screen.getByRole('tab', { name: 'Combat Squads' }))
      fireEvent.click(screen.getByRole('button', { name: 'Settings · Kite Squad' }))
      fireEvent.click(screen.getByRole('button', { name: 'Pick on map · Target X' }))
      expect(picks).toEqual([['kite_target_x', 'kite_target_y']])
    })

    it('renders the squad target queue with pick, remove and clear actions', () => {
      const picks: Array<'attack' | 'kite'> = []
      const removals: Array<['attack' | 'kite', number]> = []
      const clears: Array<'attack' | 'kite'> = []
      render(<AssetList state={state} objects={[vanguard]} selectedId={null} onSelect={() => undefined} unitNames={{ v1aaaaaa: 'V1' }} teamRoster={{ V1: 'attack' }} teamConfig={baseConfig} onUpdateConfig={() => undefined} squadTargets={{ attack: [[-2000, -2000], [7, 2]] }} onPickSquadTarget={(squad) => picks.push(squad)} onRemoveSquadTarget={(squad, index) => removals.push([squad, index])} onClearSquadTargets={(squad) => clears.push(squad)} />)
      fireEvent.click(screen.getByRole('tab', { name: 'Combat Squads' }))
      fireEvent.click(screen.getByRole('button', { name: 'Settings · Attack Squad' }))

      expect(screen.getByText('Target queue')).toBeInTheDocument()
      // The head carries the ▶ marker; both chips render their coordinates.
      expect(screen.getByText('▶ (-2000, -2000)')).toBeInTheDocument()
      expect(screen.getByText('(7, 2)')).toBeInTheDocument()

      fireEvent.click(screen.getByRole('button', { name: 'Remove this target (7, 2)' }))
      expect(removals).toEqual([['attack', 1]])
      fireEvent.click(screen.getByRole('button', { name: 'Clear' }))
      expect(clears).toEqual(['attack'])
      fireEvent.click(screen.getByRole('button', { name: 'Add point' }))
      expect(picks).toEqual(['attack'])
    })

    it('shows the empty queue hint without a clear button', () => {
      render(<AssetList state={state} objects={[vanguard]} selectedId={null} onSelect={() => undefined} unitNames={{ v1aaaaaa: 'V1' }} teamRoster={{ V1: 'kite' }} teamConfig={{ kite_mode: 'coords', kite_target_x: 0, kite_target_y: 0 }} onUpdateConfig={() => undefined} squadTargets={{}} onPickSquadTarget={() => undefined} onClearSquadTargets={() => undefined} />)
      fireEvent.click(screen.getByRole('tab', { name: 'Combat Squads' }))
      fireEvent.click(screen.getByRole('button', { name: 'Settings · Kite Squad' }))

      expect(screen.getByText(/pick map points with/i)).toBeInTheDocument()
      expect(screen.queryByRole('button', { name: 'Clear' })).not.toBeInTheDocument()
    })
  })

  describe('worker strategy settings', () => {
    afterEach(() => localStorage.clear())

    const worker = { kind: 'UNIT' as const, id: 'w1cccccc-0000', controlled: true, position: [3, 0] as [number, number], hp: 2, unit_type: 'WORKER' as const, cargo: 0 }
    const workerConfig = { worker_bfs_enabled: true, bfs_max_steps: 2500, avoid_backtracking: true, backtrack_penalty: 10, enemy_threat_radius: 3, worker_mine_max_distance: 0, worker_explore_when_full: false }

    it('opens the worker strategy panel from the groups view and commits switches at once', () => {
      const updates: Array<[string, number | boolean | string]> = []
      render(<AssetList state={state} objects={[worker]} selectedId={null} onSelect={() => undefined} teamConfig={workerConfig} onUpdateConfig={(field, value) => updates.push([field, value])} />)

      fireEvent.click(screen.getByRole('button', { name: 'Settings · Worker Group' }))
      expect(screen.getByText('Worker Strategy')).toBeInTheDocument()
      fireEvent.click(screen.getByRole('checkbox', { name: /Explore when full/ }))
      expect(updates).toEqual([['worker_explore_when_full', true]])
    })

    it('commits worker number fields on blur with the dashboard ranges', () => {
      const updates: Array<[string, number | boolean | string]> = []
      render(<AssetList state={state} objects={[worker]} selectedId={null} onSelect={() => undefined} teamConfig={workerConfig} onUpdateConfig={(field, value) => updates.push([field, value])} />)

      fireEvent.click(screen.getByRole('button', { name: 'Settings · Worker Group' }))
      const input = screen.getByDisplayValue('3')
      fireEvent.change(input, { target: { value: '99' } })
      expect(updates).toEqual([])
      fireEvent.blur(input)
      // Clamped to the evasion-radius maximum (0–10).
      expect(updates).toEqual([['enemy_threat_radius', 10]])
    })

    it('shows no worker gear without an update handler', () => {
      render(<AssetList state={state} objects={[worker]} selectedId={null} onSelect={() => undefined} teamConfig={workerConfig} />)
      expect(screen.queryByRole('button', { name: /Settings ·/ })).not.toBeInTheDocument()
    })
  })

  describe('production demand', () => {
    const workerUnits = Array.from({ length: 7 }, (_, index) => ({
      kind: 'UNIT' as const, id: `w${index}aaaa-0000`, controlled: true,
      position: [index, 0] as [number, number], hp: 2, unit_type: 'WORKER' as const, cargo: 0,
    }))
    const ranger = { kind: 'UNIT' as const, id: 'r1aaaaaa-0000', controlled: true, position: [0, 1] as [number, number], hp: 3, unit_type: 'RANGER' as const }

    it('draws current/target progress bars from the dashboard production targets', () => {
      render(<AssetList state={state} objects={[...workerUnits, ranger]} selectedId={null} onSelect={() => undefined} teamConfig={{ target_workers: 10, target_vanguards: 2, target_rangers: 2 }} />)

      expect(screen.getByText('Production demand')).toBeInTheDocument()
      const workerBar = screen.getByRole('progressbar', { name: 'Worker production progress' })
      expect(workerBar).toHaveAttribute('aria-valuemax', '10')
      expect(workerBar).toHaveAttribute('aria-valuenow', '7')
      const workerFill = workerBar.firstElementChild as HTMLElement
      expect(workerFill.style.width).toBe('70%')
      expect(workerFill.className).toContain('bg-emerald-400')
      expect(screen.getByText('7/10')).toBeInTheDocument()

      // Vanguard at zero shows an empty bar; the counter still reads 0/2.
      const vanguardBar = screen.getByRole('progressbar', { name: 'Vanguard production progress' })
      expect((vanguardBar.firstElementChild as HTMLElement).style.width).toBe('0%')
      expect(screen.getByText('0/2', { exact: true })).toBeInTheDocument()
    })

    it('fills the bar and switches tone once the target is reached or exceeded', () => {
      const vanguards = Array.from({ length: 3 }, (_, index) => ({
        kind: 'UNIT' as const, id: `v${index}aaaa-0000`, controlled: true,
        position: [index, 2] as [number, number], hp: 4, unit_type: 'VANGUARD' as const,
      }))
      render(<AssetList state={state} objects={vanguards} selectedId={null} onSelect={() => undefined} teamConfig={{ target_vanguards: 2 }} />)

      const bar = screen.getByRole('progressbar', { name: 'Vanguard production progress' })
      // Over target: the value caps at the target and the bar stays full.
      expect(bar).toHaveAttribute('aria-valuenow', '2')
      const fill = bar.firstElementChild as HTMLElement
      expect(fill.style.width).toBe('100%')
      expect(fill.className).toContain('bg-blue-soft')
      expect(screen.getByText('3/2')).toBeInTheDocument()
    })

    it('hides the panel when no production targets are served (demo mode)', () => {
      render(<AssetList state={state} objects={[...workerUnits, ranger]} selectedId={null} onSelect={() => undefined} />)
      expect(screen.queryByText('Production demand')).not.toBeInTheDocument()
      expect(screen.queryByRole('progressbar')).not.toBeInTheDocument()
    })
  })
})
