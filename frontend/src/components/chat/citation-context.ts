import { createContext, useContext } from 'react'

/** Where citations resolve: the repo, and (for Code mode) the task whose worktree holds the cited lines. */
export interface CitationScope {
  repoId: string | null
  taskId: string | null
}

export const CitationContext = createContext<CitationScope>({ repoId: null, taskId: null })

export function useCitationScope() {
  return useContext(CitationContext)
}
