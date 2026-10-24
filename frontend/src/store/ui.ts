import { create } from 'zustand'
import { createJSONStorage, persist } from 'zustand/middleware'
import type { Mode } from '@/lib/types'

export type RightTab = 'files' | 'changes' | 'commands' | 'details'
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
  rightPanelOpen: boolean
  rightTab: RightTab
  fileTarget: FileTarget | null
  theme: Theme
  setupOpen: boolean
  setupDismissed: boolean
  addRepoOpen: boolean
  settingsOpen: boolean

  selectRepo: (id: string | null) => void
  selectTask: (id: string | null) => void
  setComposerMode: (mode: Mode) => void
  setComposerDraft: (draft: string) => void
  setRightPanelOpen: (open: boolean) => void
  setRightTab: (tab: RightTab) => void
  openFile: (target: Omit<FileTarget, 'nonce'>) => void
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
      rightPanelOpen: true,
      rightTab: 'files',
      fileTarget: null,
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
      setRightPanelOpen: (rightPanelOpen) => set({ rightPanelOpen }),
      setRightTab: (rightTab) => set({ rightTab, rightPanelOpen: true }),
      openFile: (target) =>
        set({ fileTarget: { ...target, nonce: ++nonce }, rightPanelOpen: true, rightTab: 'files' }),
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
        rightPanelOpen: s.rightPanelOpen,
        rightTab: s.rightTab,
        theme: s.theme,
      }),
    },
  ),
)
