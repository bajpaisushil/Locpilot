import { create } from 'zustand'
import { createJSONStorage, persist } from 'zustand/middleware'
import type { Mode } from '@/lib/types'

export type RightTab = 'files' | 'changes' | 'commands' | 'terminal' | 'details'
export type Theme = 'dark' | 'light'

export interface FileTarget {
  path: string
  start?: number
  end?: number
  /** When set, the file is read from that task's isolated worktree. */
  taskId?: string | null
  /** Changes on every request so re-opening the same citation re-reveals it. */
  nonce: number
}

interface UiState {
  selectedRepoId: string | null
  selectedTaskId: string | null
  composerMode: Mode
  composerDraft: string
  /** Network access for commands the user runs (writes stay confined to the worktree either way). */
  runAllowNetwork: boolean
  rightPanelOpen: boolean
  rightTab: RightTab
  fileTarget: FileTarget | null
  /** The terminal shown in the Terminal tab (kept while you switch tabs; ids live as long as the backend). */
  activeTerminalId: string | null
  theme: Theme
  setupOpen: boolean
  setupDismissed: boolean
  addRepoOpen: boolean
  settingsOpen: boolean

  selectRepo: (id: string | null) => void
  selectTask: (id: string | null) => void
  setComposerMode: (mode: Mode) => void
  setComposerDraft: (draft: string) => void
  setRunAllowNetwork: (allow: boolean) => void
  setRightPanelOpen: (open: boolean) => void
  setRightTab: (tab: RightTab) => void
  openFile: (target: Omit<FileTarget, 'nonce'>) => void
  setActiveTerminalId: (id: string | null) => void
  setTheme: (theme: Theme) => void
  setSetupOpen: (open: boolean) => void
  setSetupDismissed: (dismissed: boolean) => void
  setAddRepoOpen: (open: boolean) => void
  setSettingsOpen: (open: boolean) => void
}

let nonce = 0

export const useUi = create<UiState>()(
  persist(
    (set) => ({
      selectedRepoId: null,
      selectedTaskId: null,
      composerMode: 'ask',
      composerDraft: '',
      runAllowNetwork: false,
      rightPanelOpen: true,
      rightTab: 'files',
      fileTarget: null,
      activeTerminalId: null,
      theme: 'dark',
      setupOpen: false,
      setupDismissed: false,
      addRepoOpen: false,
      settingsOpen: false,

      selectRepo: (id) =>
        set((s) => (s.selectedRepoId === id ? s : { selectedRepoId: id, selectedTaskId: null, fileTarget: null })),
      selectTask: (id) => set({ selectedTaskId: id }),
      setComposerMode: (composerMode) => set({ composerMode }),
      setComposerDraft: (composerDraft) => set({ composerDraft }),
      setRunAllowNetwork: (runAllowNetwork) => set({ runAllowNetwork }),
      setRightPanelOpen: (rightPanelOpen) => set({ rightPanelOpen }),
      setRightTab: (rightTab) => set({ rightTab, rightPanelOpen: true }),
      openFile: (target) =>
        set({ fileTarget: { ...target, nonce: ++nonce }, rightPanelOpen: true, rightTab: 'files' }),
      setActiveTerminalId: (activeTerminalId) => set({ activeTerminalId }),
      setTheme: (theme) => set({ theme }),
      setSetupOpen: (setupOpen) => set({ setupOpen }),
      setSetupDismissed: (setupDismissed) => set({ setupDismissed }),
      setAddRepoOpen: (addRepoOpen) => set({ addRepoOpen }),
      setSettingsOpen: (settingsOpen) => set({ settingsOpen }),
    }),
    {
      name: 'locpilot-ui',
      version: 1,
      storage: createJSONStorage(() => {
        try {
          return window.localStorage
        } catch {
          // Storage can be unavailable (privacy modes); fall back to memory.
          const mem = new Map<string, string>()
          return {
            getItem: (k: string) => mem.get(k) ?? null,
            setItem: (k: string, v: string) => void mem.set(k, v),
            removeItem: (k: string) => void mem.delete(k),
          }
        }
      }),
      partialize: (s) => ({
        selectedRepoId: s.selectedRepoId,
        selectedTaskId: s.selectedTaskId,
        composerMode: s.composerMode,
        runAllowNetwork: s.runAllowNetwork,
        rightPanelOpen: s.rightPanelOpen,
        rightTab: s.rightTab,
        theme: s.theme,
      }),
    },
  ),
)
