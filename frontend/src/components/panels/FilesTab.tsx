import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ChevronRightIcon, FileIcon, FolderIcon, FolderOpenIcon, GitBranchIcon, HardDriveIcon, SaveIcon } from 'lucide-react'
import { useEffect, useId, useState } from 'react'
import { toast } from 'sonner'
import { CodeViewer } from '@/components/code/LazyViewers'
import { CopyButton } from '@/components/common/CopyButton'
import { EmptyState, ErrorState, LoadingState } from '@/components/common/States'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Kbd } from '@/components/ui/kbd'
import { ResizableGroup, ResizableHandle, ResizablePanel } from '@/components/ui/resizable'
import { isMac, SAVE_EVENT } from '@/hooks/useShortcuts'
import { api } from '@/lib/api'
import { isActive, qk, useTask } from '@/lib/queries'
import type { FileContent, TreeEntry } from '@/lib/types'
import { cn } from '@/lib/utils'
import { draftKey, useDrafts } from '@/store/drafts'
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
  const drafts = useDrafts((s) => s.drafts)
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
              {e.type === 'file' && drafts[draftKey(repoId, taskId, e.path)] && (
                <span className="ml-auto text-warning" aria-label={`${e.name} has unsaved changes`}>●</span>
              )}
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

/** What the editor holds for the open file. */
interface Loaded {
  key: string
  /** The saved version the editor's text is based on (sent as `expected_sha` on save). */
  sha: string
  content: string
  /** Text the editor model starts with: the saved content, or a kept draft. */
  initial: string
  /** Counts (re)loads: each gets a fresh editor model; saving keeps the model (and the cursor). */
  load: number
}

function FileView({ repoId, taskId, busy }: { repoId: string; taskId: string | null; busy: boolean }) {
  const target = useUi((s) => s.fileTarget)
  const path = target?.path ?? null
  const qc = useQueryClient()
  const file = useQuery({
    queryKey: qk.file(repoId, path ?? '', taskId),
    queryFn: () => api.file(repoId, path as string, { taskId }),
    enabled: !!path,
    staleTime: 5_000,
    refetchInterval: 5_000, // shows LocPilot's (and terminal) edits; unsaved edits are never replaced
  })
  const data = file.data
  const editable = !!data && !data.binary && !data.truncated
  const key = draftKey(repoId, taskId, path ?? '')
  const draft = useDrafts((s) => s.drafts[key])
  const { keep, drop } = useDrafts.getState()
  const [loaded, setLoaded] = useState<Loaded | null>(null)
  // Editor models outlive this view; never let a later mount reuse one that holds old, unsaved text.
  const instance = useId().replace(/[^a-zA-Z0-9]/g, '')

  // (Re)load into the editor when another file opens (restoring its unsaved draft, if any), or when this one
  // changed on disk and has no unsaved edits.
  if (data && editable && (loaded?.key !== key || (!draft && data.sha !== loaded.sha))) {
    const load = (loaded?.load ?? 0) + 1
    setLoaded(draft ? { key, sha: draft.sha, content: draft.base, initial: draft.text, load }
                    : { key, sha: data.sha, content: data.content, initial: data.content, load })
  }
  const dirty = !!draft && loaded?.key === key
  const changedOnDisk = dirty && !!data && !!loaded && data.sha !== loaded.sha

  const save = useMutation({
    mutationFn: (v: { current: Loaded; content: string }) =>
      api.saveFile(repoId, { path: path as string, content: v.content, expected_sha: v.current.sha, task_id: taskId }),
    onSuccess: (res, v) => {
      setLoaded((l) => (l && l.key === v.current.key ? { ...l, sha: res.sha, content: v.content } : l))
      const now = useDrafts.getState().drafts[v.current.key]
      if (!now || now.text === v.content) drop(v.current.key)
      else keep(v.current.key, { sha: res.sha, base: v.content, text: now.text }) // typed while saving
      qc.setQueryData<FileContent>(qk.file(repoId, path as string, taskId), (old) =>
        old ? { ...old, content: v.content, sha: res.sha, total_lines: v.content.split('\n').length } : old,
      )
      if (taskId) {
        void qc.invalidateQueries({ queryKey: qk.worktreeChanges(taskId) })
        void qc.invalidateQueries({ queryKey: qk.diff(taskId) })
      } else {
        void qc.invalidateQueries({ queryKey: qk.repo(repoId) })
      }
      toast.success(`Saved ${res.path}`)
    },
  })
  const { mutate: saveMutate, isPending: saving } = save

  useEffect(() => {
    if (!editable || !loaded || !draft) return
    const onSave = () => {
      if (!saving && !busy) saveMutate({ current: loaded, content: draft.text })
    }
    window.addEventListener(SAVE_EVENT, onSave)
    return () => window.removeEventListener(SAVE_EVENT, onSave)
  }, [editable, loaded, draft, saving, busy, saveMutate])

  if (!path)
    return (
      <EmptyState
        icon={<FileIcon />}
        title="No file open"
        description="Pick a file in the explorer, or click a citation in a report to jump to the exact lines."
        className="h-full"
      />
    )
  const reload = () => {
    if (!data) return
    drop(key)
    setLoaded({ key, sha: data.sha, content: data.content, initial: data.content, load: (loaded?.load ?? 0) + 1 })
    save.reset()
  }
  const showEditor = editable && loaded?.key === key
  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="flex h-8 shrink-0 items-center gap-2 border-b px-3">
        <span className="min-w-0 flex-1 truncate font-mono text-[11.5px]" title={path}>
          {dirty && <span className="mr-1 text-warning" aria-label="Unsaved changes">●</span>}
          {path}
          {target?.start ? <span className="text-primary">:{target.start}{target.end && target.end !== target.start ? `-${target.end}` : ''}</span> : null}
        </span>
        {file.data && (
          <span className="shrink-0 text-[10.5px] text-muted-foreground">
            {file.data.total_lines} lines · {file.data.language}
          </span>
        )}
        {editable && (
          <Button
            size="xs"
            variant={dirty ? 'default' : 'ghost'}
            disabled={!dirty || saving || busy}
            onClick={() => loaded && draft && save.mutate({ current: loaded, content: draft.text })}
            aria-label="Save file"
            title={busy ? 'LocPilot is editing this worktree; save when it finishes' : undefined}
          >
            <SaveIcon />
            Save <Kbd>{isMac() ? '⌘S' : 'Ctrl+S'}</Kbd>
          </Button>
        )}
        <CopyButton text={path} label="Copy path" />
      </div>
      {file.data?.truncated && (
        <div className="border-b bg-warning/8 px-3 py-1 text-[11px] text-warning">
          Large file — showing lines {file.data.start_line}–{file.data.end_line} of {file.data.total_lines} (read-only).
        </div>
      )}
      {changedOnDisk && (
        <div className="flex items-center gap-2 border-b bg-warning/8 px-3 py-1 text-[11px] text-warning">
          <span className="flex-1">This file changed on disk while you were editing it.</span>
          <Button size="xs" variant="outline" onClick={reload}>
            Reload — discard my edits
          </Button>
        </div>
      )}
      {save.isError && (
        <div className="border-b p-2">
          <ErrorState error={save.error} title="Not saved" compact />
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
        ) : showEditor && loaded ? (
          <CodeViewer
            modelPath={`${repoId}/${taskId ?? 'workdir'}/edit-${instance}-${loaded.load}/${path}`}
            path={path}
            content={loaded.initial}
            language={file.data?.language}
            highlight={target?.start ? { start: target.start, end: target.end } : null}
            revealNonce={target?.nonce}
            editable
            onChange={(value) => {
              if (value === loaded.content) drop(key)
              else keep(key, { sha: loaded.sha, base: loaded.content, text: value })
            }}
          />
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
          <FileView repoId={repoId} taskId={viewTaskId} busy={!!viewTaskId && isActive(task.data?.status)} />
        </ResizablePanel>
      </ResizableGroup>
    </div>
  )
}

