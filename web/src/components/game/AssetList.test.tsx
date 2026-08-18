import { fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'
import '../../lib/i18n'
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
  })
})
