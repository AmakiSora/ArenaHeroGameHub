import { fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import '../../lib/i18n'
import i18n from '../../lib/i18n'
import { CoreDirectionIndicator } from './CoreDirectionIndicator'

describe('CoreDirectionIndicator', () => {
  afterEach(() => void i18n.changeLanguage('en'))

  it('stays hidden while the Core is inside the viewport', () => {
    render(<CoreDirectionIndicator corePosition={[1, 1]} camera={{ x: 0, y: 0, cell: 44 }} viewport={{ width: 800, height: 600 }} onCenter={vi.fn()} />)
    expect(screen.queryByRole('button')).not.toBeInTheDocument()
  })

  it('shows the coordinate and explanation and centers on click', () => {
    const onCenter = vi.fn()
    render(<CoreDirectionIndicator corePosition={[20, -3]} camera={{ x: 0, y: 0, cell: 44 }} viewport={{ width: 800, height: 600 }} onCenter={onCenter} />)
    expect(screen.getAllByText('[20, -3]').length).toBeGreaterThan(0)
    expect(screen.getByText('Click to center the map on your Core.')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Center on Home Core [20, -3]' }))
    expect(onCenter).toHaveBeenCalledOnce()
  })
})
