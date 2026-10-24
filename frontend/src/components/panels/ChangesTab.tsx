import { useMutation, useQueryClient } from '@tanstack/react-query'
import { ClipboardCopyIcon, Columns2Icon, DownloadIcon, FileDiffIcon, FilePlusIcon, FileMinusIcon, Rows2Icon, TerminalIcon, Undo2Icon } from 'lucide-react'
import { useState } from 'react'
import { toast } from 'sonner'
import { DiffViewer } from '@/components/code/LazyViewers'
import { SourceControl } from '@/components/panels/SourceControl'
import { EmptyState, ErrorState, LoadingState } from '@/components/common/States'
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from '@/components/ui/alert-dialog'
import { Button } from '@/components/ui/button'
import { Hint } from '@/components/ui/tooltip'
import { api, errorMessage } from '@/lib/api'
import { copyText, saveTextFile } from '@/lib/desktop'
import { qk, useTask, useTaskDiff } from '@/lib/queries'
import type { DiffFile, RollbackResult } from '@/lib/types'
import { cn, shortSha } from '@/lib/utils'
import { useUi } from '@/store/ui'

function lineDelta(unified: string) {
  let add = 0
  let del = 0
  for (const line of unified.split('\n')) {
    if (line.startsWith('+++') || line.startsWith('---')) continue
    if (line.startsWith('+')) add++
    else if (line.startsWith('-')) del++
  }
  return { add, del }
}

function FileIconFor({ change }: { change: DiffFile['change'] }) {
  if (change === 'create') return <FilePlusIcon className="size-3.5 shrink-0 text-success" />
  if (change === 'delete') return <FileMinusIcon className="size-3.5 shrink-0 text-destructive" />
  return <FileDiffIcon className="size-3.5 shrink-0 text-muted-foreground" />
}

export function ChangesTab() {
  const taskId = useUi((s) => s.selectedTaskId)
  const setRightTab = useUi((s) => s.setRightTab)
  const task = useTask(taskId)
  const hasWorktree = !!task.data?.worktree_path
  const diff = useTaskDiff(taskId, hasWorktree)
  const qc = useQueryClient()
  const [selected, setSelected] = useState<string | null>(null)
  const [sideBySide, setSideBySide] = useState(false)
  const [confirmRollback, setConfirmRollback] = useState(false)
  const [rollbackResult, setRollbackResult] = useState<RollbackResult | null>(null)

  const rollback = useMutation({
    mutationFn: () => api.rollback(taskId as string),
    onSuccess: (res) => {
      setRollbackResult(res)
      setConfirmRollback(false)
      void qc.invalidateQueries({ queryKey: qk.diff(taskId as string) })
      void qc.invalidateQueries({ queryKey: qk.task(taskId as string) })
    },
  })

  if (!taskId) return <EmptyState icon={<FileDiffIcon />} title="No task selected" description="Changes made by a Code-mode task appear here." className="h-full" />
  if (task.isLoading) return <LoadingState />
  if (!hasWorktree)
    return (
      <EmptyState
        icon={<FileDiffIcon />}
        title="No isolated workspace"
        description="Ask-mode tasks are read-only and never modify files. Switch to Code mode to let LocPilot propose changes."
        className="h-full"
      />
    )
  if (diff.isLoading) return <LoadingState label="Computing diff…" />
  if (diff.isError) return <div className="p-3"><ErrorState error={diff.error} onRetry={() => void diff.refetch()} /></div>

  const files = diff.data?.files ?? []
  const current = files.find((f) => f.path === selected) ?? files[0] ?? null

  const savePatch = async () => {
    try {
      const text = await api.patchText(taskId)
      const saved = await saveTextFile(`locpilot-${taskId}.patch`, text)
      if (saved) toast.success('Patch saved', { description: saved })
    } catch (err) {
      toast.error('Could not save patch', { description: errorMessage(err) })
    }
  }
  const copyPatch = async () => {
    try {
      const text = await api.patchText(taskId)
      if (await copyText(text)) toast.success('Patch copied to clipboard')
    } catch (err) {
      toast.error('Could not copy patch', { description: errorMessage(err) })
    }
  }

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="flex shrink-0 flex-wrap items-center gap-2 border-b px-3 py-2">
        <div className="min-w-0 flex-1 text-[11px] text-muted-foreground">
          <div className="truncate">
            Worktree <span className="font-mono">{diff.data?.worktree_path}</span>
          </div>
          <div>
            Base commit <span className="font-mono">{shortSha(diff.data?.base_commit)}</span> · {files.length} changed file
            {files.length === 1 ? '' : 's'}
          </div>
        </div>
        <Hint label={sideBySide ? 'Inline view' : 'Side-by-side view'}>
          <Button variant="ghost" size="icon-sm" onClick={() => setSideBySide((v) => !v)} aria-label="Toggle diff layout">
            {sideBySide ? <Rows2Icon /> : <Columns2Icon />}
          </Button>
        </Hint>
        <Hint label="Copy patch">
          <Button variant="ghost" size="icon-sm" onClick={() => void copyPatch()} disabled={!files.length} aria-label="Copy patch">
            <ClipboardCopyIcon />
          </Button>
        </Hint>
        <Hint label="Try the changed code: run it in this worktree from the Commands tab">
          <Button variant="outline" size="xs" onClick={() => setRightTab('commands')}>
            <TerminalIcon />
            Run
          </Button>
        </Hint>
        <Button variant="outline" size="xs" onClick={() => void savePatch()} disabled={!files.length}>
          <DownloadIcon />
          .patch
        </Button>
        <Button variant="outline" size="xs" onClick={() => setConfirmRollback(true)} disabled={!files.length || rollback.isPending}>
          <Undo2Icon />
          Roll back
        </Button>
      </div>

      {task.data && <SourceControl task={task.data} />}

      {rollbackResult && (
        <div className="border-b bg-muted/30 px-3 py-2 text-xs">
          Restored {rollbackResult.restored.length} file{rollbackResult.restored.length === 1 ? '' : 's'}.
          {rollbackResult.skipped.length > 0 && (
            <ul className="mt-1 list-disc pl-5 text-warning">
              {rollbackResult.skipped.map((s) => (
                <li key={s.path}>
                  <span className="font-mono">{s.path}</span>: {s.reason}
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
      {rollback.isError && <div className="p-2"><ErrorState error={rollback.error} compact /></div>}

      {files.length === 0 ? (
        <EmptyState
          icon={<FileDiffIcon />}
          title="No LocPilot changes in the worktree"
          description="LocPilot has not applied any edits yet (or they were rolled back)."
          className="flex-1"
        />
      ) : (
        <div className="flex min-h-0 flex-1 flex-col">
          <ul className="max-h-40 shrink-0 overflow-y-auto border-b py-1">
            {files.map((f) => {
              const d = lineDelta(f.unified)
              return (
                <li key={f.path}>
                  <button
                    type="button"
                    onClick={() => setSelected(f.path)}
                    className={cn(
                      'flex w-full items-center gap-2 px-3 py-1 text-left text-xs hover:bg-accent/60',
                      current?.path === f.path && 'bg-accent text-accent-foreground',
                    )}
                  >
                    <FileIconFor change={f.change} />
                    <span className="min-w-0 flex-1 truncate font-mono">{f.path}</span>
                    <span className="font-mono text-success">+{d.add}</span>
                    <span className="font-mono text-destructive">−{d.del}</span>
                  </button>
                </li>
              )
            })}
          </ul>
          <div className="min-h-0 flex-1">
            {current &&
              (current.binary ? (
                <EmptyState title="Binary file changed" className="h-full" />
              ) : (
                <DiffViewer
                  modelKey={`${taskId}`}
                  path={current.path}
                  original={current.original ?? ''}
                  modified={current.modified ?? ''}
                  sideBySide={sideBySide}
                />
              ))}
          </div>
        </div>
      )}

      <AlertDialog open={confirmRollback} onOpenChange={setConfirmRollback}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Roll back LocPilot&apos;s changes?</AlertDialogTitle>
            <AlertDialogDescription>
              Files LocPilot modified in this task&apos;s isolated worktree will be restored to their original content.
              Files that were edited by someone else since LocPilot changed them are skipped. Your own working tree is
              never touched.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Cancel</AlertDialogCancel>
            <AlertDialogAction
              variant="destructive"
              onClick={(e) => {
                e.preventDefault()
                rollback.mutate()
              }}
            >
              Roll back
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  )
}
