import { fireEvent, render, screen } from '@testing-library/react'
import { createRef } from 'react'
import type { ComponentProps } from 'react'
import { describe, expect, it, vi } from 'vitest'
import '../../lib/i18n'
import { GuerrillaTracksDialog } from './GuerrillaTracksDialog'
import type { GuerrillaTrack } from '../../lib/guerrillaTracks'

const UNITS: GuerrillaTrack[] = [
  { id: 'aaaa1111', name: 'V1', type: 'VANGUARD', points: [[0, 0], [1, 0], [2, 0], [3, 0], [4, 0], [5, 0]], ticks: 620, distinct: 6, net: 5, bounds: [0, 0, 5, 0] },
  { id: 'bbbb2222', name: 'R2', type: 'RANGER', points: [[800, -900], [810, -905]], ticks: 40, distinct: 2, net: 10, bounds: [800, -905, 810, -900] },
]

const open = (props: Partial<ComponentProps<typeof GuerrillaTracksDialog>> = {}) => {
  const onSelectDate = vi.fn()
  // AccountDialog portals to document.body, so queries go through `document`.
  render(<GuerrillaTracksDialog
    returnFocusRef={createRef<HTMLButtonElement>()}
    onClose={vi.fn()}
    date="20261011"
    dates={['20261011', '20261010']}
    units={UNITS}
    onSelectDate={onSelectDate}
    {...props}
  />)
  return { onSelectDate }
}

const polylines = () => document.querySelectorAll('svg polyline').length

describe('GuerrillaTracksDialog', () => {
  it('draws every unit line and the per-unit counters', () => {
    open()
    expect(screen.getByRole('heading', { name: 'Guerrilla exploration lines' })).toBeInTheDocument()
    expect(screen.getByText(/recorded on 2026-10-11/)).toBeInTheDocument()
    // V1's 6 cells split into 3 age-ramped chunks, R2's 2 cells into 1.
    expect(polylines()).toBe(4)
    expect(screen.getByRole('row', { name: /V1/ })).toHaveTextContent('620')
    expect(screen.getByRole('row', { name: /R2/ })).toHaveTextContent('40')
  })

  it('isolates one unit when its row is clicked, and restores on a second click', () => {
    open()
    const all = polylines()
    fireEvent.click(screen.getByRole('row', { name: /V1/ }))
    expect(polylines()).toBeLessThan(all)
    fireEvent.click(screen.getByRole('row', { name: /V1/ }))
    expect(polylines()).toBe(all)
  })

  it('switches the recorded day through the date buttons', () => {
    const { onSelectDate } = open()
    fireEvent.click(screen.getByRole('button', { name: '2026-10-10' }))
    expect(onSelectDate).toHaveBeenCalledWith('20261010')
  })

  it('explains the recorder when the day holds no rows yet', () => {
    open({ units: [] })
    expect(screen.getByText(/guerrilla_routes-YYYYMMDD\.jsonl/)).toBeInTheDocument()
    expect(screen.queryByRole('table')).not.toBeInTheDocument()
  })
})
