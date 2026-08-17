import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'
import { MemoryRouter } from 'react-router'
import '../lib/i18n'
import { AccountMenu } from './AccountMenu'

vi.mock('../context/AuthContext', () => ({
  useAuth: () => ({
    user: { username: 'pilot', email: '', auth_source: 'MANUAL' as const, oauth_providers: [] as Array<'github' | 'linux_do'> },
    loading: false,
    refresh: vi.fn(),
    login: vi.fn(),
    logout: vi.fn(),
  }),
}))

vi.mock('../lib/api', () => ({
  api: {
    stats: vi.fn().mockResolvedValue({ damage_dealt: 1, damage_received: 2, unit_destruction_participations: 3, core_destruction_participations: 4, resources_harvested: 5, resources_deposited: 6, units_spawned: 7, units_lost: 8, core_survival_ticks: 9, respawn_count: 10 }),
  },
}))

describe('AccountMenu', () => {
  it('opens the operator menu and the statistics dialog', async () => {
    const user = userEvent.setup()
    render(<MemoryRouter initialEntries={['/']}><AccountMenu /></MemoryRouter>)
    const accountButton = screen.getByRole('button', { name: 'Account' })
    expect(accountButton).toHaveTextContent('pilot')
    await user.click(accountButton)
    expect(screen.getByRole('menuitem', { name: 'Arena' })).toHaveAttribute('href', '/')
    expect(screen.getByRole('menuitem', { name: 'Leaderboard' })).toHaveAttribute('href', '/leaderboard')
    expect(screen.getByRole('menuitem', { name: 'Training' })).toBeInTheDocument()
    expect(screen.getByRole('menuitem', { name: 'Documentation' })).toHaveAttribute('href', 'https://doc.arenahero.io/')
    expect(screen.getByRole('menuitem', { name: 'Documentation' })).toHaveAttribute('target', '_blank')
    expect(screen.getByRole('button', { name: 'Language' })).toBeInTheDocument()
    // No API key management in the dashboard deployment, but the session
    // sign-out stays: it decides whether commands go to the MANUAL slot.
    expect(screen.getByRole('button', { name: 'Sign out' })).toBeInTheDocument()
    expect(screen.queryByRole('menuitem', { name: 'API Keys' })).not.toBeInTheDocument()
    await user.click(screen.getByRole('menuitem', { name: 'Statistics' }))
    expect(await screen.findByRole('dialog', { name: 'Operator statistics' })).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Close' }))
  })
})
