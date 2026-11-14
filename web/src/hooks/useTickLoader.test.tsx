import { act, render, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { resetTickAbortsForTests, useTickLoader } from './useTickLoader'

const signals: AbortSignal[] = []
const requests: string[] = []
const noop = () => {}
const loader = (label: string) => (signal: AbortSignal) => {
  signals.push(signal)
  requests.push(label)
  return Promise.resolve(undefined)
}

const loadNames = loader('names')
const loadTeams = loader('teams')
const loadMemory = loader('memory')
const loadWaypoints = loader('waypoints')
const loadSquadTargets = loader('squad-targets')
const loadHolds = loader('holds')
const loadRoutes = loader('routes')

// Mirrors the seven tick-driven pollers the arena page owns.
function ArenaPollers({ tick, enabled = true }: { tick: number; enabled?: boolean }) {
  useTickLoader(tick, enabled, loadNames, noop)
  useTickLoader(tick, enabled, loadTeams, noop)
  useTickLoader(tick, enabled, loadMemory, noop)
  useTickLoader(tick, enabled, loadWaypoints, noop)
  useTickLoader(tick, enabled, loadSquadTargets, noop)
  useTickLoader(tick, enabled, loadHolds, noop)
  useTickLoader(tick, enabled, loadRoutes, noop)
  return null
}

function TwoPollers({ tick, refresh }: { tick: number; refresh: number }) {
  useTickLoader(tick, true, loadNames, noop)
  useTickLoader(tick, true, loadTeams, noop, refresh)
  return null
}

const setVisibility = (state: 'visible' | 'hidden') => {
  Object.defineProperty(document, 'visibilityState', { value: state, configurable: true })
  document.dispatchEvent(new Event('visibilitychange'))
}

describe('useTickLoader', () => {
  beforeEach(() => {
    signals.length = 0
    requests.length = 0
    setVisibility('visible')
  })
  afterEach(() => resetTickAbortsForTests())

  it('sends one request per poller per tick with the SAME AbortSignal instance', async () => {
    render(<ArenaPollers tick={42} />)
    await waitFor(() => expect(signals).toHaveLength(7))

    expect(new Set(signals).size).toBe(1)
    expect(requests.sort()).toEqual(['holds', 'memory', 'names', 'routes', 'squad-targets', 'teams', 'waypoints'])
  })

  it('aborts the previous tick signal and reuses one instance for the next batch', async () => {
    const first = render(<ArenaPollers tick={42} />)
    await waitFor(() => expect(signals).toHaveLength(7))
    const previous = [...signals]
    signals.length = 0

    first.rerender(<ArenaPollers tick={43} />)
    await waitFor(() => expect(signals).toHaveLength(7))

    expect(previous.every((signal) => signal.aborted)).toBe(true)
    expect(new Set(signals).size).toBe(1)
    expect(signals[0]).not.toBe(previous[0])
  })

  it('skips fetching while the tab is hidden and refetches once on becoming visible', async () => {
    const view = render(<ArenaPollers tick={42} />)
    await waitFor(() => expect(signals).toHaveLength(7))
    signals.length = 0

    act(() => setVisibility('hidden'))
    view.rerender(<ArenaPollers tick={43} />)
    expect(signals).toHaveLength(0)

    act(() => setVisibility('visible'))
    await waitFor(() => expect(signals).toHaveLength(7))
    expect(new Set(signals).size).toBe(1)
  })

  it('does not abort sibling requests when one poller refreshes on its own', async () => {
    const view = render(<TwoPollers tick={42} refresh={0} />)
    await waitFor(() => expect(signals).toHaveLength(2))
    const shared = signals[0]
    signals.length = 0

    view.rerender(<TwoPollers tick={42} refresh={1} />)
    await waitFor(() => expect(signals).toHaveLength(1))
    // The refresh re-joined the same tick entry, so the signal is unchanged —
    // and the sibling's in-flight request was not aborted.
    expect(signals[0]).toBe(shared)
    expect(shared.aborted).toBe(false)
  })
})
