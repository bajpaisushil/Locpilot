import { create } from 'zustand'

/** Unsaved editor text for one file, and the saved version it started from. */
export interface Draft {
  sha: string
  base: string
  text: string
}

/** Unsaved edits survive switching files and panels (not a page reload), like an editor's dirty buffers. */
interface DraftsStore {
  drafts: Record<string, Draft>
  keep: (key: string, draft: Draft) => void
  drop: (key: string) => void
}

export const useDrafts = create<DraftsStore>()((set) => ({
  drafts: {},
  keep: (key, draft) => set((s) => ({ drafts: { ...s.drafts, [key]: draft } })),
  drop: (key) =>
    set((s) => {
      if (!(key in s.drafts)) return s
      const drafts = { ...s.drafts }
      delete drafts[key]
      return { drafts }
    }),
}))

export const draftKey = (repoId: string, taskId: string | null, path: string) => `${repoId}|${taskId ?? ''}|${path}`
