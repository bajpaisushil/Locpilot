import { renderHook } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { SAVE_EVENT, useShortcuts } from '@/hooks/useShortcuts'
import { useUi } from '@/store/ui'

// jsdom's navigator.platform is empty, so the Windows/Linux modifier (Ctrl) applies here.
const press = (key: string, init: KeyboardEventInit = { ctrlKey: true }) => {
  const event = new KeyboardEvent('keydown', { key, bubbles: true, cancelable: true, ...init })
  document.body.dispatchEvent(event)
  return event
}

describe('useShortcuts', () => {
  beforeEach(() => useUi.setState({ rightPanelOpen: true, rightTab: 'files' }))

  it('toggles the terminal with Ctrl/⌘+J', () => {
    renderHook(() => useShortcuts())
    expect(press('j').defaultPrevented).toBe(true)
    expect(useUi.getState()).toMatchObject({ rightPanelOpen: true, rightTab: 'terminal' })
    press('j')
    expect(useUi.getState().rightPanelOpen).toBe(false)
    press('j')
    expect(useUi.getState()).toMatchObject({ rightPanelOpen: true, rightTab: 'terminal' })
  })

  it('turns Ctrl/⌘+S into a save of the open file, never the browser dialog', () => {
    renderHook(() => useShortcuts())
    const onSave = vi.fn()
    window.addEventListener(SAVE_EVENT, onSave)
    expect(press('s').defaultPrevented).toBe(true)
    expect(onSave).toHaveBeenCalledTimes(1)
    press('s', { ctrlKey: true, shiftKey: true }) // other combinations are left alone
    press('s', {})
    expect(onSave).toHaveBeenCalledTimes(1)
    window.removeEventListener(SAVE_EVENT, onSave)
  })
})
