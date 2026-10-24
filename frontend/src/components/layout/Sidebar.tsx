import { useMutation, useQueryClient } from '@tanstack/react-query'
import {
  ChevronsUpDownIcon,
  FolderGit2Icon,
  GitForkIcon,
  MessageSquarePlusIcon,
  MoonIcon,
  PlusIcon,
  SettingsIcon,
  SunIcon,
  Trash2Icon,
  WrenchIcon,
} from 'lucide-react'
import { useState } from 'react'
import { Logo } from '@/components/common/Logo'
import { ModeBadge } from '@/components/common/StatusBadge'
import { ErrorState } from '@/components/common/States'
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
import {
  DropdownMenu,
  DropdownMenuCheckItem,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'
import { Skeleton } from '@/components/ui/skeleton'
import { Hint } from '@/components/ui/tooltip'
import { api } from '@/lib/api'
import { qk, useRepos, useTasks } from '@/lib/queries'
import type { RepoSummary, TaskSummary } from '@/lib/types'
import { cn, formatRelativeTime, taskStatusDotClass } from '@/lib/utils'
import { useEventsStore } from '@/store/events'
import { useUi } from '@/store/ui'

function RepoSwitcher() {
  const repos = useRepos()
  const selectedRepoId = useUi((s) => s.selectedRepoId)
  const selectRepo = useUi((s) => s.selectRepo)
  const setAddRepoOpen = useUi((s) => s.setAddRepoOpen)
  const current = repos.data?.find((r) => r.id === selectedRepoId) ?? null

  if (repos.isLoading) return <Skeleton className="h-11 w-full" />
  if (repos.isError) return <ErrorState error={repos.error} compact onRetry={() => void repos.refetch()} />

  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <button
          type="button"
          className="flex w-full items-center gap-2.5 rounded-lg border bg-background/60 px-2.5 py-2 text-left transition-colors hover:bg-accent/60"
          aria-label="Switch repository"
        >
          <span className="flex size-7 shrink-0 items-center justify-center rounded-md border bg-muted text-muted-foreground">
            {current?.source_type === 'github' ? <GitForkIcon className="size-3.5" /> : <FolderGit2Icon className="size-3.5" />}
          </span>
          <span className="min-w-0 flex-1">
            <span className="block truncate text-sm font-medium">{current?.name ?? 'Select a repository'}</span>
            <span className="block truncate font-mono text-[10.5px] text-muted-foreground">
              {current ? current.path : `${repos.data?.length ?? 0} connected`}
            </span>
          </span>
          <ChevronsUpDownIcon className="size-4 shrink-0 text-muted-foreground" />
        </button>
      </DropdownMenuTrigger>
      <DropdownMenuContent className="w-72" align="start">
        <DropdownMenuLabel>Recent repositories</DropdownMenuLabel>
        {repos.data?.length === 0 && <div className="px-2 py-1.5 text-xs text-muted-foreground">None yet.</div>}
        {repos.data?.map((r: RepoSummary) => (
          <DropdownMenuCheckItem key={r.id} checked={r.id === selectedRepoId} onSelect={() => selectRepo(r.id)}>
            {r.source_type === 'github' ? <GitForkIcon /> : <FolderGit2Icon />}
            <span className="min-w-0">
              <span className="block truncate">{r.name}</span>
              <span className="block truncate font-mono text-[10.5px] text-muted-foreground">{r.path}</span>
            </span>
          </DropdownMenuCheckItem>
        ))}
        <DropdownMenuSeparator />
        <DropdownMenuItem onSelect={() => setAddRepoOpen(true)}>
          <PlusIcon />
          Add repository…
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  )
}

function TaskRow({ task, active, onSelect, onDelete }: { task: TaskSummary; active: boolean; onSelect: () => void; onDelete: () => void }) {
  return (
    <div
      className={cn(
        'group relative flex w-full items-start gap-2 rounded-md px-2 py-1.5 transition-colors',
        active ? 'bg-accent text-accent-foreground' : 'hover:bg-accent/50',
      )}
    >
      <button type="button" onClick={onSelect} className="flex min-w-0 flex-1 items-start gap-2 text-left" data-testid="task-row">
        <span className={cn('mt-1.5 size-1.5 shrink-0 rounded-full', taskStatusDotClass(task.status))} aria-hidden />
        <span className="min-w-0 flex-1">
          <span className="block truncate text-[13px] leading-5">{task.title}</span>
          <span className="flex items-center gap-1.5 text-[10.5px] text-muted-foreground">
            <ModeBadge mode={task.mode} className="px-1 py-0 text-[9px]" />
            <span>{task.status.replace('_', ' ')}</span>
            <span>·</span>
            <span>{formatRelativeTime(task.updated_at)}</span>
          </span>
        </span>
      </button>
      <Hint label="Delete task">
        <Button
          variant="ghost"
          size="icon-xs"
          className="absolute top-1.5 right-1 hidden text-muted-foreground group-hover:inline-flex"
          onClick={onDelete}
          aria-label={`Delete task ${task.title}`}
        >
          <Trash2Icon className="size-3.5" />
        </Button>
      </Hint>
    </div>
  )
}

export function TaskList() {
  const repoId = useUi((s) => s.selectedRepoId)
  const selectedTaskId = useUi((s) => s.selectedTaskId)
  const selectTask = useUi((s) => s.selectTask)
  const setComposerMode = useUi((s) => s.setComposerMode)
  const tasks = useTasks(repoId)
  const qc = useQueryClient()
  const [pendingDelete, setPendingDelete] = useState<TaskSummary | null>(null)
  const del = useMutation({
    mutationFn: (id: string) => api.deleteTask(id),
    onSuccess: (_r, id) => {
      if (selectedTaskId === id) selectTask(null)
      useEventsStore.getState().reset(id)
      qc.removeQueries({ queryKey: qk.task(id) })
      void qc.invalidateQueries({ queryKey: qk.tasks(repoId) })
      setPendingDelete(null)
    },
  })

  if (!repoId) return null
  if (tasks.isLoading)
    return (
      <div className="space-y-2 px-1" role="status" aria-label="Loading tasks">
        <Skeleton className="h-9" />
        <Skeleton className="h-9" />
        <Skeleton className="h-9" />
      </div>
    )
  if (tasks.isError) return <ErrorState error={tasks.error} compact onRetry={() => void tasks.refetch()} />
  if (!tasks.data?.length)
    return <p className="px-2 py-3 text-xs text-muted-foreground">No tasks yet. Ask a question to start one.</p>

  return (
    <>
      <div className="space-y-0.5">
        {tasks.data.map((t) => (
          <TaskRow
            key={t.id}
            task={t}
            active={t.id === selectedTaskId}
            onSelect={() => {
              selectTask(t.id)
              setComposerMode(t.mode)
            }}
            onDelete={() => setPendingDelete(t)}
          />
        ))}
      </div>
      <AlertDialog open={!!pendingDelete} onOpenChange={(o) => !o && setPendingDelete(null)}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Delete this task?</AlertDialogTitle>
            <AlertDialogDescription>
              “{pendingDelete?.title}” and its history will be removed, along with LocPilot&apos;s isolated worktree for
              it. Your own repository working tree is not touched.
            </AlertDialogDescription>
          </AlertDialogHeader>
          {del.isError && <ErrorState error={del.error} compact />}
          <AlertDialogFooter>
            <AlertDialogCancel>Cancel</AlertDialogCancel>
            <AlertDialogAction
              variant="destructive"
              onClick={(e) => {
                e.preventDefault()
                if (pendingDelete) del.mutate(pendingDelete.id)
              }}
            >
              Delete
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </>
  )
}

export function Sidebar() {
  const repoId = useUi((s) => s.selectedRepoId)
  const selectTask = useUi((s) => s.selectTask)
  const setSettingsOpen = useUi((s) => s.setSettingsOpen)
  const setSetupOpen = useUi((s) => s.setSetupOpen)
  const theme = useUi((s) => s.theme)
  const setTheme = useUi((s) => s.setTheme)

  return (
    <aside className="flex h-full min-h-0 flex-col bg-sidebar" aria-label="Sidebar">
      <div className="flex h-12 shrink-0 items-center gap-2 border-b px-3" data-tauri-drag-region>
        <Logo className="size-5" />
        <span className="text-sm font-semibold tracking-tight">LocPilot</span>
        <span className="rounded border px-1 py-px text-[9px] font-medium text-muted-foreground">LOCAL</span>
      </div>
      <div className="space-y-3 p-3">
        <RepoSwitcher />
        {repoId && (
          <Button variant="outline" size="sm" className="w-full justify-start" onClick={() => selectTask(null)}>
            <MessageSquarePlusIcon />
            New task
          </Button>
        )}
      </div>
      <div className="flex items-center justify-between px-4 pb-1">
        <span className="text-[10.5px] font-semibold tracking-wider text-muted-foreground uppercase">Task history</span>
      </div>
      <div className="min-h-0 flex-1 overflow-y-auto px-2 pb-2">
        <TaskList />
      </div>
      <div className="flex items-center gap-1 border-t p-2">
        <Button variant="ghost" size="sm" className="flex-1 justify-start" onClick={() => setSettingsOpen(true)}>
          <SettingsIcon />
          Settings
        </Button>
        <Hint label="Dependency setup">
          <Button variant="ghost" size="icon-sm" onClick={() => setSetupOpen(true)} aria-label="Open dependency setup">
            <WrenchIcon />
          </Button>
        </Hint>
        <Hint label={theme === 'dark' ? 'Light theme' : 'Dark theme'}>
          <Button
            variant="ghost"
            size="icon-sm"
            onClick={() => setTheme(theme === 'dark' ? 'light' : 'dark')}
            aria-label="Toggle theme"
          >
            {theme === 'dark' ? <SunIcon /> : <MoonIcon />}
          </Button>
        </Hint>
      </div>
    </aside>
  )
}
