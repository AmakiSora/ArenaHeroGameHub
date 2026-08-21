import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { createRef } from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import '../../lib/i18n'
import { StrategyConfigDialog } from './StrategyConfigDialog'

const CONFIG = { core_movement_enabled: true, repair_enabled: false, target_workers: 10, cargo_wait_distance: 5 }

const setup = (postOk = true) => {
  const fetchMock = vi.fn(async (_url: string, init?: RequestInit) => {
    if (init?.method === 'POST') return { ok: postOk, json: async () => ({ ok: postOk }) } as Response
    return { ok: true, json: async () => ({ ok: true, config: CONFIG }) } as Response
  })
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

const postCalls = (mock: ReturnType<typeof vi.fn>) =>
  (mock.mock.calls as unknown as [string, RequestInit][]).filter(([, init]) => init.method === 'POST')

const openDialog = (onClose = vi.fn()) =>
  render(<StrategyConfigDialog returnFocusRef={createRef<HTMLButtonElement>()} onClose={onClose} />)

describe('StrategyConfigDialog', () => {
  afterEach(() => vi.unstubAllGlobals())

  it('loads values from /api/config and renders the three groups', async () => {
    setup()
    openDialog()
    expect(await screen.findByLabelText('Allow core movement')).toBeChecked()
    expect(screen.getByLabelText('Allow shield repair')).not.toBeChecked()
    expect(screen.getByLabelText('Worker production target')).toHaveValue(10)
    expect(screen.getByRole('heading', { name: 'Core' })).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: 'Runtime' })).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: 'Production' })).toBeInTheDocument()
  })

  it('posts a partial patch when a switch is toggled', async () => {
    const fetchMock = setup()
    const user = userEvent.setup()
    openDialog()
    await user.click(await screen.findByLabelText('Allow core movement'))
    await waitFor(() => expect(postCalls(fetchMock)).toHaveLength(1))
    const [path, init] = postCalls(fetchMock)[0]
    expect(path).toBe('/api/config')
    expect(JSON.parse(init.body as string)).toEqual({ core_movement_enabled: false })
  })

  it('clamps number fields and commits on blur', async () => {
    const fetchMock = setup()
    openDialog()
    const input = await screen.findByLabelText('Ranger production target')
    fireEvent.change(input, { target: { value: '999' } })
    fireEvent.blur(input)
    await waitFor(() => expect(postCalls(fetchMock)).toHaveLength(1))
    expect(JSON.parse(postCalls(fetchMock)[0][1].body as string)).toEqual({ target_rangers: 100 })
  })

  it('rolls the value back and shows an error when the save fails', async () => {
    setup(false)
    const user = userEvent.setup()
    openDialog()
    const repair = await screen.findByLabelText('Allow shield repair')
    expect(repair).not.toBeChecked()
    await user.click(repair)
    expect(await screen.findByRole('alert')).toHaveTextContent('Save failed')
    expect(screen.getByLabelText('Allow shield repair')).not.toBeChecked()
  })

  it('asks the page to pick the core target coordinates on the map', async () => {
    setup()
    const user = userEvent.setup()
    const onPickCoords = vi.fn()
    render(<StrategyConfigDialog returnFocusRef={createRef<HTMLButtonElement>()} onClose={vi.fn()} onPickCoords={onPickCoords} />)
    await user.click(await screen.findByRole('button', { name: 'Pick on map · Core target X' }))
    expect(onPickCoords).toHaveBeenCalledWith('core_target_x', 'core_target_y')
  })

  it('hides the map-pick button without a pick handler', async () => {
    setup()
    openDialog()
    await screen.findByLabelText('Allow core movement')
    expect(screen.queryByRole('button', { name: /Pick on map/ })).not.toBeInTheDocument()
  })
})
