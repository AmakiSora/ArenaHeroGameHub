import { act, renderHook } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'
import { useDocumentVisible } from './useDocumentVisible'

const setVisibility = (state: 'visible' | 'hidden') => {
  Object.defineProperty(document, 'visibilityState', { value: state, configurable: true })
  document.dispatchEvent(new Event('visibilitychange'))
}

describe('useDocumentVisible', () => {
  afterEach(() => setVisibility('visible'))

  it('starts from the current visibility state', () => {
    setVisibility('hidden')
    const { result } = renderHook(() => useDocumentVisible())
    expect(result.current).toBe(false)
  })

  it('follows visibilitychange events in both directions', () => {
    setVisibility('visible')
    const { result } = renderHook(() => useDocumentVisible())
    expect(result.current).toBe(true)

    act(() => setVisibility('hidden'))
    expect(result.current).toBe(false)

    act(() => setVisibility('visible'))
    expect(result.current).toBe(true)
  })
})
