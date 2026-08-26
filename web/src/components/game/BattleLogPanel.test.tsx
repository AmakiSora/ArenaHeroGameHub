import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'
import '../../lib/i18n'
import i18n from '../../lib/i18n'
import { BattleLogPanel } from './BattleLogPanel'

const respond = (payload: unknown) => vi.fn(async () => ({
  ok: true,
  json: async () => payload,
}) as Response)

describe('BattleLogPanel', () => {
  afterEach(() => { vi.unstubAllGlobals(); localStorage.clear(); void i18n.changeLanguage('en') })

  it('renders fetched rows and jumps the map when a coordinate is clicked', async () => {
    await i18n.changeLanguage('en')
    vi.stubGlobal('fetch', respond({
      ok: true,
      entries: [
        { tick: 7, ts: 1700000000, cat: 'discover', msg: 'Spotted enemy worker (12,-7)' },
      ],
    }))
    const onJump = vi.fn()
    render(<BattleLogPanel tick={42} enabled onJump={onJump} />)

    expect(await screen.findByText('Battle log')).toBeInTheDocument()
    expect(screen.getByText(/Spotted enemy worker/)).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: '(12,-7)' }))
    expect(onJump).toHaveBeenCalledWith([12, -7])
  })

  it('hides noisy categories by default and restores them via the filter chip', async () => {
    const user = userEvent.setup()
    await i18n.changeLanguage('en')
    vi.stubGlobal('fetch', respond({
      ok: true,
      entries: [
        { tick: 1, ts: 1700000000, cat: 'economy', msg: 'W1 harvest +1 (3,4)' },
        { tick: 2, ts: 1700000001, cat: 'kill', msg: 'V2 killed an enemy worker (5,5)' },
      ],
    }))
    render(<BattleLogPanel tick={42} enabled onJump={vi.fn()} />)

    expect(await screen.findByText(/V2 killed an enemy worker/)).toBeInTheDocument()
    expect(screen.queryByText(/W1 harvest/)).not.toBeInTheDocument()
    // The visible-row count only includes rows passing the filters.
    expect(screen.getByText('1 row')).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: 'Economy' }))
    expect(screen.getByText(/W1 harvest/)).toBeInTheDocument()
    expect(screen.getByText('2 rows')).toBeInTheDocument()
  })

  it('filters rows older than the chosen time window', async () => {
    const user = userEvent.setup()
    await i18n.changeLanguage('en')
    const now = Math.floor(Date.now() / 1000)
    vi.stubGlobal('fetch', respond({
      ok: true,
      entries: [
        { tick: 9, ts: now, cat: 'kill', msg: 'fresh kill (1,1)' },
        { tick: 1, ts: now - 7200, cat: 'kill', msg: 'stale kill (2,2)' },
      ],
    }))
    render(<BattleLogPanel tick={42} enabled onJump={vi.fn()} />)

    expect(await screen.findByText(/stale kill/)).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: '10m' }))
    expect(screen.getByText(/fresh kill/)).toBeInTheDocument()
    expect(screen.queryByText(/stale kill/)).not.toBeInTheDocument()
  })

  it('shows the demo sample without a dashboard backend', async () => {
    await i18n.changeLanguage('en')
    const onJump = vi.fn()
    render(<BattleLogPanel tick={null} enabled={false} onJump={onJump} />)

    expect(screen.getByText('Battle log')).toBeInTheDocument()
    await userEvent.click(await screen.findByRole('button', { name: '(-6,9)' }))
    expect(onJump).toHaveBeenCalledWith([-6, 9])
  })
})
