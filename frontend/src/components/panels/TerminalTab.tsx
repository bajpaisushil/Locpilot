import { useMutation, useQueryClient } from '@tanstack/react-query'
import { ChevronDownIcon, FolderGitIcon, PlusIcon, SquareTerminalIcon, WrenchIcon, XIcon } from 'lucide-react'
import { EmptyState, ErrorState, LoadingState } from '@/components/common/States'
import { XtermView } from '@/components/terminal/LazyXtermView'
import { Button } from '@/components/ui/button'
import { Kbd } from '@/components/ui/kbd'
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuTrigger } from '@/components/ui/dropdown-menu'
import { isMac } from '@/hooks/useShortcuts'
import { api } from '@/lib/api'
import { qk, useTask, useTerminals } from '@/lib/queries'
import type { TerminalOut } from '@/lib/types'
import { cn } from '@/lib/utils'
import { useUi } from '@/store/ui'

type Where = 'worktree' | 'repository'

export function TerminalTab() {
  const repoId = useUi((s) => s.selectedRepoId)
  const taskId = useUi((s) => s.selectedTaskId)
  const task = useTask(taskId)
  const terminals = useTerminals(repoId)
  const qc = useQueryClient()
  const activeId = useUi((s) => s.activeTerminalId)
  const setActiveId = useUi((s) => s.setActiveTerminalId)
  const worktreeTaskId = task.data?.worktree_path ? task.data.id : null

  const setList = (update: (old: TerminalOut[]) => TerminalOut[]) =>
    qc.setQueryData<TerminalOut[]>(qk.terminals(repoId ?? ''), (old) => update(old ?? []))

  const open = useMutation({
    mutationFn: (where: Where) =>
      api.openTerminal({ repo_id: repoId as string, task_id: where === 'worktree' ? worktreeTaskId : null, cols: 100, rows: 30 }),
    onSuccess: (t) => {
      setList((old) => [...old.filter((o) => o.id !== t.id), t])
      setActiveId(t.id)
    },
  })
  const close = useMutation({
    mutationFn: (id: string) => api.closeTerminal(id),
    onSettled: (_r, _e, id) => {
      setList((old) => old.filter((o) => o.id !== id))
      void qc.invalidateQueries({ queryKey: qk.terminals(repoId ?? '') })
    },
  })

  if (!repoId)
    return <EmptyState icon={<SquareTerminalIcon />} title="No repository selected" className="h-full" />
  if (terminals.isLoading) return <LoadingState />
  if (terminals.isError) return <div className="p-3"><ErrorState error={terminals.error} /></div>

  const list = terminals.data ?? []
  const active =
    list.find((t) => t.id === activeId) ??
    list.find((t) => worktreeTaskId && t.task_id === worktreeTaskId) ??
    list[list.length - 1] ??
    null

  const newIn = (where: Where) => open.mutate(where)
  const options: { where: Where; label: string; icon: typeof WrenchIcon }[] = [
    ...(worktreeTaskId ? [{ where: 'worktree' as const, label: 'In LocPilot’s worktree (this task)', icon: WrenchIcon }] : []),
    { where: 'repository', label: 'In your repository folder', icon: FolderGitIcon },
  ]

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="flex shrink-0 items-center gap-1 border-b px-2 py-1.5">
        <div className="flex min-w-0 flex-1 items-center gap-1 overflow-x-auto" role="tablist" aria-label="Terminals">
          {list.map((t) => (
            <div
              key={t.id}
              className={cn(
                'group flex max-w-48 shrink-0 items-center gap-1 rounded-md border px-2 py-0.5 text-[11px]',
                active?.id === t.id ? 'border-primary/40 bg-accent text-accent-foreground' : 'border-transparent text-muted-foreground hover:bg-accent/50',
              )}
            >
              <button
                type="button"
                role="tab"
                aria-selected={active?.id === t.id}
                onClick={() => setActiveId(t.id)}
                className="flex min-w-0 items-center gap-1"
                title={t.cwd}
              >
                {t.location === 'worktree' ? <WrenchIcon className="size-3 shrink-0" /> : <FolderGitIcon className="size-3 shrink-0" />}
                <span className="truncate">{t.title}</span>
                {!t.running && <span className="text-muted-foreground">(exited)</span>}
              </button>
              <button
                type="button"
                onClick={() => close.mutate(t.id)}
                className="rounded opacity-60 hover:opacity-100"
                aria-label={`Close terminal ${t.title}`}
              >
                <XIcon className="size-3" />
              </button>
            </div>
          ))}
        </div>
        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <Button variant="outline" size="xs" disabled={open.isPending} aria-label="New terminal">
              <PlusIcon />
              Terminal
              <ChevronDownIcon />
            </Button>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="end">
            {options.map((o) => (
              <DropdownMenuItem key={o.where} onSelect={() => newIn(o.where)}>
                <o.icon />
                {o.label}
              </DropdownMenuItem>
            ))}
          </DropdownMenuContent>
        </DropdownMenu>
      </div>
      {(open.error || close.error) && (
        <div className="border-b p-2"><ErrorState error={open.error ?? close.error} compact /></div>
      )}
      <div className="min-h-0 flex-1">
        {active ? (
          <XtermView key={active.id} terminal={active} />
        ) : (
          <EmptyState
            icon={<SquareTerminalIcon />}
            title="No terminal open"
            description={
              worktreeTaskId
                ? 'A real shell, like VS Code’s. In LocPilot’s worktree it runs LocPilot’s version of the code: start your app here (e.g. npm run dev) and LocPilot’s next edits reload live.'
                : 'A real shell in your repository folder, like VS Code’s terminal.'
            }
            className="h-full"
          >
            <div className="flex flex-wrap justify-center gap-2">
              {options.map((o) => (
                <Button key={o.where} size="sm" variant={o.where === 'worktree' ? 'default' : 'outline'} onClick={() => newIn(o.where)} disabled={open.isPending}>
                  <o.icon />
                  {o.label}
                </Button>
              ))}
            </div>
            <p className="text-[11px] text-muted-foreground">
              <Kbd>{isMac() ? '⌘J' : 'Ctrl+J'}</Kbd> shows or hides the terminal
            </p>
          </EmptyState>
        )}
      </div>
    </div>
  )
}
