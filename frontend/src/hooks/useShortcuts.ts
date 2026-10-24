import { useEffect } from 'react'
import { useUi } from '@/store/ui'

/** Fired on ⌘S / Ctrl+S; the open file editor saves when it has unsaved edits. */
export const SAVE_EVENT = 'locpilot:save'

export const isMac = () => typeof navigator !== 'undefined' && /mac/i.test(navigator.platform)

/** VS Code-style shortcuts: ⌘S saves the open file, ⌘J toggles the terminal (Ctrl on Windows/Linux). */
export function useShortcuts() {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const mod = isMac() ? e.metaKey && !e.ctrlKey : e.ctrlKey && !e.metaKey
      if (!mod || e.altKey || e.shiftKey) return
      const key = e.key.toLowerCase()
      if (key === 's') {
        e.preventDefault() // never the browser's "Save page"
        window.dispatchEvent(new Event(SAVE_EVENT))
      } else if (key === 'j') {
        e.preventDefault()
        const ui = useUi.getState()
        if (ui.rightPanelOpen && ui.rightTab === 'terminal') ui.setRightPanelOpen(false)
        else ui.setRightTab('terminal')
      }
    }
    window.addEventListener('keydown', onKey, true)
    return () => window.removeEventListener('keydown', onKey, true)
  }, [])
}
