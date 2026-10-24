import { useQuery } from '@tanstack/react-query'
import { ChevronRightIcon, FileIcon, FolderIcon, FolderOpenIcon, GitBranchIcon, HardDriveIcon } from 'lucide-react'
import { useState } from 'react'
import { CodeViewer } from '@/components/code/LazyViewers'
import { CopyButton } from '@/components/common/CopyButton'
import { EmptyState, ErrorState, LoadingState } from '@/components/common/States'
import { Badge } from '@/components/ui/badge'
import { ResizableGroup, ResizableHandle, ResizablePanel } from '@/components/ui/resizable'
import { api } from '@/lib/api'
import { qk, useTask } from '@/lib/queries'
import type { TreeEntry } from '@/lib/types'
import { cn } from '@/lib/utils'
import { useUi } from '@/store/ui'

function ancestors(path: string): string[] {
  const parts = path.split('/').filter(Boolean)
  const out: string[] = []
  for (let i = 1; i < parts.length; i++) out.push(parts.slice(0, i).join('/'))
  return out
}

function DirChildren({
  repoId,
  taskId,
  path,
  depth,
  expanded,
  toggle,
  selectedPath,
  onOpen,
}: {
  repoId: string
  taskId: string | null
  path: string
  depth: number
  expanded: Set<string>
  toggle: (p: string) => void
  selectedPath: string | null
  onOpen: (p: string) => void
}) {
  const tree = useQuery({
    queryKey: qk.tree(repoId, path, taskId),
    queryFn: () => api.tree(repoId, path, taskId),
    staleTime: 15_000,
  })
  if (tree.isLoading)
    return (
      <div className="py-1 text-[11px] text-muted-foreground" style={{ paddingLeft: depth * 12 + 22 }}>
        Loading…
      </div>
    )
  if (tree.isError)
    return (
      <div className="py-1 text-[11px] text-destructive" style={{ paddingLeft: depth * 12 + 22 }}>
        Failed to load
      </div>
    )
  const entries = [...(tree.data ?? [])].sort((a, b) =>
    a.type === b.type ? a.name.localeCompare(b.name) : a.type === 'dir' ? -1 : 1,
  )
  if (entries.length === 0 && depth === 0)
    return <div className="px-3 py-2 text-xs text-muted-foreground">No visible files.</div>
  return (
    <ul role={depth === 0 ? 'tree' : 'group'}>
      {entries.map((e: TreeEntry) => {
        const isOpen = expanded.has(e.path)
        return (
          <li key={e.path} role="treeitem" aria-expanded={e.type === 'dir' ? isOpen : undefined}>
            <button
              type="button"
              onClick={() => (e.type === 'dir' ? toggle(e.path) : onOpen(e.path))}
              className={cn(
                'flex w-full items-center gap-1 py-[3px] pr-2 text-left text-[12.5px] hover:bg-accent/60',
                selectedPath === e.path && 'bg-accent text-accent-foreground',
              )}
              style={{ paddingLeft: depth * 12 + 6 }}
              title={e.path}
            >
              {e.type === 'dir' ? (
                <>
                  <ChevronRightIcon
                    className={cn('size-3 shrink-0 text-muted-foreground transition-transform', isOpen && 'rotate-90')}
                  />
                  {isOpen ? (
                    <FolderOpenIcon className="size-3.5 shrink-0 text-primary/80" />
                  ) : (
                    <FolderIcon className="size-3.5 shrink-0 text-primary/80" />
                  )}
                </>
              ) : (
                <>
                  <span className="w-3 shrink-0" />
                  <FileIcon className="size-3.5 shrink-0 text-muted-foreground" />
                </>
              )}
              <span className="truncate">{e.name}</span>
            </button>
            {e.type === 'dir' && isOpen && (
              <DirChildren
                repoId={repoId}
                taskId={taskId}
                path={e.path}
                depth={depth + 1}
                expanded={expanded}
                toggle={toggle}
                selectedPath={selectedPath}
                onOpen={onOpen}
              />
            )}
          </li>
        )
      })}
    </ul>
  )
}

function FileView({ repoId, taskId }: { repoId: string; taskId: string | null }) {
  const target = useUi((s) => s.fileTarget)
  const path = target?.path ?? null
  const file = useQuery({
    queryKey: qk.file(repoId, path ?? '', taskId),
    queryFn: () => api.file(repoId, path as string, { taskId }),
    enabled: !!path,
    staleTime: 5_000,
  })
  if (!path)
    return (
      <EmptyState
        icon={<FileIcon />}
        title="No file open"
        description="Pick a file in the explorer, or click a citation in a report to jump to the exact lines."
        className="h-full"
      />
    )
  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="flex h-8 shrink-0 items-center gap-2 border-b px-3">
        <span className="min-w-0 flex-1 truncate font-mono text-[11.5px]" title={path}>
          {path}
          {target?.start ? <span className="text-primary">:{target.start}{target.end && target.end !== target.start ? `-${target.end}` : ''}</span> : null}
        </span>
        {file.data && (
          <span className="shrink-0 text-[10.5px] text-muted-foreground">
            {file.data.total_lines} lines · {file.data.language}
          </span>
        )}
        <CopyButton text={path} label="Copy path" />
      </div>
      {file.data?.truncated && (
        <div className="border-b bg-warning/8 px-3 py-1 text-[11px] text-warning">
          Large file — showing lines {file.data.start_line}–{file.data.end_line} of {file.data.total_lines}.
        </div>
      )}
      <div className="min-h-0 flex-1">
        {file.isLoading ? (
          <LoadingState label="Loading file…" />
        ) : file.isError ? (
          <div className="p-3">
            <ErrorState error={file.error} title="Cannot open this file" onRetry={() => void file.refetch()} />
          </div>
        ) : file.data?.binary ? (
          <EmptyState icon={<FileIcon />} title="Binary file" description="Binary files are not displayed." className="h-full" />
        ) : file.data ? (
          <CodeViewer
            modelPath={`${repoId}/${taskId ?? 'workdir'}/${file.data.sha}/${path}`}
            path={path}
            content={file.data.content}
            language={file.data.language}
            highlight={target?.start ? { start: target.start - (file.data.start_line - 1), end: target.end ? target.end - (file.data.start_line - 1) : undefined } : null}
            revealNonce={target?.nonce}
          />
        ) : null}
      </div>
    </div>
  )
}

export function FilesTab() {
  const repoId = useUi((s) => s.selectedRepoId)
  const taskId = useUi((s) => s.selectedTaskId)
  const target = useUi((s) => s.fileTarget)
  const openFile = useUi((s) => s.openFile)
  const task = useTask(taskId)
  const hasWorktree = !!task.data?.worktree_path
  const [expanded, setExpanded] = useState<Set<string>>(() => new Set())
  const source: 'workdir' | 'worktree' = target?.taskId && hasWorktree ? 'worktree' : 'workdir'

  // Reveal the opened file in the tree (derived-state update during render, not in an effect).
  const targetPath = target?.path ?? null
  const [revealed, setRevealed] = useState<string | null>(null)
  if (targetPath && targetPath !== revealed) {
    setRevealed(targetPath)
    const missing = ancestors(targetPath).filter((a) => !expanded.has(a))
    if (missing.length) setExpanded(new Set([...expanded, ...missing]))
  }

  if (!repoId)
    return <EmptyState icon={<FolderIcon />} title="No repository selected" className="h-full" />

  const toggle = (p: string) =>
    setExpanded((prev) => {
      const next = new Set(prev)
      if (next.has(p)) next.delete(p)
      else next.add(p)
      return next
    })
  const viewTaskId = source === 'worktree' ? (task.data?.id ?? null) : null

  return (
    <div className="flex h-full min-h-0 flex-col">
      {hasWorktree && (
        <div className="flex h-8 shrink-0 items-center gap-1 border-b px-2 text-[11px]">
          <span className="mr-1 text-muted-foreground">Viewing:</span>
          <button
            type="button"
            className={cn(
              'inline-flex items-center gap-1 rounded px-1.5 py-0.5',
              source === 'workdir' ? 'bg-accent text-accent-foreground' : 'text-muted-foreground hover:text-foreground',
            )}
            onClick={() => target && openFile({ ...target, taskId: null })}
          >
            <HardDriveIcon className="size-3" />
            Your working tree
          </button>
          <button
            type="button"
            className={cn(
              'inline-flex items-center gap-1 rounded px-1.5 py-0.5',
              source === 'worktree' ? 'bg-accent text-accent-foreground' : 'text-muted-foreground hover:text-foreground',
            )}
            onClick={() => target && openFile({ ...target, taskId: task.data?.id ?? null })}
            disabled={!target}
          >
            <GitBranchIcon className="size-3" />
            Task worktree
          </button>
          {source === 'worktree' && <Badge variant="default" className="ml-auto">LocPilot changes visible</Badge>}
        </div>
      )}
      <ResizableGroup orientation="vertical" className="min-h-0 flex-1 flex-col">
        <ResizablePanel id="tree" defaultSize="34" minSize={80}>
          <div className="h-full overflow-auto py-1" aria-label="File explorer">
            <DirChildren
              repoId={repoId}
              taskId={viewTaskId}
              path=""
              depth={0}
              expanded={expanded}
              toggle={toggle}
              selectedPath={target?.path ?? null}
              onOpen={(p) => openFile({ path: p, taskId: viewTaskId })}
            />
          </div>
        </ResizablePanel>
        <ResizableHandle orientation="vertical" />
        <ResizablePanel id="viewer" minSize={120}>
          <FileView repoId={repoId} taskId={viewTaskId} />
        </ResizablePanel>
      </ResizableGroup>
    </div>
  )
}

