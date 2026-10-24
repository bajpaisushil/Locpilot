import { useQuery } from '@tanstack/react-query'
import { ChevronRightIcon, PlayIcon, ShieldIcon, SquareIcon, TerminalIcon, WrenchIcon } from 'lucide-react'
import { useState, type KeyboardEvent } from 'react'
import { toast } from 'sonner'
import { CommandStatusBadge, OutputBlock } from '@/components/chat/CommandCard'
import { EmptyState, ErrorState, LoadingState } from '@/components/common/States'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Switch } from '@/components/ui/switch'
import { Hint } from '@/components/ui/tooltip'
import { api } from '@/lib/api'
import {
  commandLine,
  hasTestSummary,
  isFailedUserRun,
  isUserCommand,
  runningUserCommand,
  userCommandHistory,
} from '@/lib/commands'
import { isActive, qk, useRunCommand, useStopCommand, useTask } from '@/lib/queries'
import type { CommandOut, TaskDetail } from '@/lib/types'
import { cn, formatDateTime, formatDuration, testSummaryText } from '@/lib/utils'
import { useTaskEvents } from '@/store/events'
import { useUi } from '@/store/ui'

function AskToFixButton({ command }: { command: CommandOut }) {
  const setDraft = useUi((s) => s.setComposerDraft)
  const setMode = useUi((s) => s.setComposerMode)
  const outcome = command.status === 'timed_out' ? 'timed out' : `failed (exit ${command.exit_code ?? '—'})`
  return (
    <Button
      size="xs"
      variant="outline"
      onClick={() => {
        setMode('code')
        setDraft(`I ran \`${commandLine(command)}\` and it ${outcome}. Investigate the output and fix it.`)
      }}
    >
      <WrenchIcon />
      Ask LocPilot to fix
    </Button>
  )
}

function CommandDetail({ taskId, command, live }: { taskId: string; command: CommandOut; live?: string }) {
  const running = command.status === 'running'
  const full = useQuery({
    queryKey: qk.command(taskId, command.id),
    queryFn: () => api.command(taskId, command.id),
    enabled: !running,
  })
  const c = full.data ?? command
  return (
    <div className="space-y-2 border-t bg-muted/20 p-3 text-xs">
      <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1">
        <dt className="text-muted-foreground">Command</dt>
        <dd className="font-mono break-all">{commandLine(c)}</dd>
        <dt className="text-muted-foreground">Directory</dt>
        <dd className="font-mono break-all">{c.cwd}</dd>
        <dt className="text-muted-foreground">Started</dt>
        <dd>{formatDateTime(c.started_at)}</dd>
        <dt className="text-muted-foreground">Duration</dt>
        <dd>{formatDuration(c.duration_ms)}{c.timed_out && ' (timed out)'}</dd>
        <dt className="text-muted-foreground">Sandbox</dt>
        <dd>{c.sandbox || 'none'}</dd>
        {hasTestSummary(c) && c.summary && (
          <>
            <dt className="text-muted-foreground">Tests</dt>
            <dd>
              {c.summary.framework}: {testSummaryText(c.summary)}
            </dd>
          </>
        )}
      </dl>
      {c.summary?.failing_tests?.length ? (
        <div>
          <div className="mb-1 font-medium text-destructive">Failing tests</div>
          <ul className="space-y-0.5 font-mono text-[11px]">
            {c.summary.failing_tests.map((t) => (
              <li key={t} className="break-all">
                {t}
              </li>
            ))}
          </ul>
        </div>
      ) : null}
      {running ? (
        <OutputBlock text={live ?? ''} label="live output" />
      ) : full.isLoading ? (
        <LoadingState label="Loading output…" className="p-2" />
      ) : full.isError ? (
        <ErrorState error={full.error} compact />
      ) : (
        <>
          <OutputBlock text={c.stdout ?? ''} label="stdout" />
          <OutputBlock text={c.stderr ?? ''} label="stderr" />
          {c.truncated && <div className="text-warning">Output was truncated to the configured limit.</div>}
          {c.summary?.hints?.map((h) => (
            <div key={h} className="text-warning">
              {h}
            </div>
          ))}
          {isFailedUserRun(c) && <AskToFixButton command={c} />}
        </>
      )}
    </div>
  )
}

/** A command line for the task worktree: type, Enter, watch the output (↑/↓ recall earlier commands). */
function RunBar({ task, onStarted }: { task: TaskDetail; onStarted: (commandId: string) => void }) {
  const allowNetwork = useUi((s) => s.runAllowNetwork)
  const setAllowNetwork = useUi((s) => s.setRunAllowNetwork)
  const run = useRunCommand()
  const stop = useStopCommand()
  const [draft, setDraft] = useState('')
  const [recall, setRecall] = useState(-1)
  const running = runningUserCommand(task)
  const history = userCommandHistory(task)

  let blocked: string | null = null
  if (!task.worktree_path) blocked = 'Commands run in this task’s isolated worktree, which LocPilot creates in Code mode.'
  else if (isActive(task.status)) blocked = 'LocPilot is working on this task — run commands when the turn finishes.'

  const submit = async () => {
    const command = draft.trim()
    if (running && command) {
      toast.info('A command is still running', {
        description: 'Stop it first, or use the Terminal tab to run things side by side.',
      })
      return
    }
    if (!command || blocked || run.isPending) return
    try {
      const started = await run.mutateAsync({ taskId: task.id, command, allowNetwork })
      setDraft('')
      setRecall(-1)
      onStarted(started.id)
    } catch {
      // surfaced below via the mutation error
    }
  }

  const onKeyDown = (e: KeyboardEvent<HTMLInputElement>) => {
    if (e.key === 'Enter' && !e.nativeEvent.isComposing) {
      e.preventDefault() // explicit: works whether the Run or the Stop button is showing
      void submit()
      return
    }
    if ((e.key !== 'ArrowUp' && e.key !== 'ArrowDown') || !history.length) return
    e.preventDefault()
    const next = e.key === 'ArrowUp' ? Math.min(recall + 1, history.length - 1) : Math.max(recall - 1, -1)
    setRecall(next)
    setDraft(next >= 0 ? history[next] : '')
  }

  const error = run.error ?? stop.error
  return (
    <form
      className="shrink-0 space-y-2 border-b p-3"
      onSubmit={(e) => {
        e.preventDefault()
        void submit()
      }}
    >
      <div className="flex items-center gap-2">
        <div className="relative min-w-0 flex-1">
          <span
            aria-hidden
            className="pointer-events-none absolute top-1/2 left-2.5 -translate-y-1/2 font-mono text-xs text-muted-foreground"
          >
            $
          </span>
          <Input
            value={draft}
            onChange={(e) => {
              setDraft(e.target.value)
              setRecall(-1)
            }}
            onKeyDown={onKeyDown}
            disabled={!!blocked}
            placeholder={blocked ? 'Not available yet' : 'python main.py · npm run build · node index.js'}
            aria-label="Command to run"
            spellCheck={false}
            autoComplete="off"
            autoCapitalize="off"
            className="h-8 pl-6 font-mono text-xs"
          />
        </div>
        {running ? (
          <Button
            type="button"
            size="sm"
            variant="outline"
            onClick={() => stop.mutate({ taskId: task.id, commandId: running.id })}
            disabled={stop.isPending}
            aria-label="Stop command"
          >
            <SquareIcon className="size-3 fill-current" />
            Stop
          </Button>
        ) : (
          <Button type="submit" size="sm" disabled={!!blocked || !draft.trim() || run.isPending}>
            <PlayIcon />
            Run
          </Button>
        )}
      </div>
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px] text-muted-foreground">
        <Hint label="Off: no internet, like LocPilot’s own test runs. On: internet allowed (installs, APIs). Either way the command can only write inside the worktree.">
          <label className="inline-flex items-center gap-1.5">
            <Switch checked={allowNetwork} onCheckedChange={setAllowNetwork} aria-label="Allow network access" />
            Network
          </label>
        </Hint>
        <span>{blocked ?? 'Runs on LocPilot’s worktree (its changes, not your checkout) · no keyboard input'}</span>
      </div>
      {error && <ErrorState error={error} compact />}
    </form>
  )
}

export function CommandsTab() {
  const taskId = useUi((s) => s.selectedTaskId)
  const task = useTask(taskId)
  const ev = useTaskEvents(taskId)
  const [open, setOpen] = useState<string | null>(null)

  if (!taskId)
    return (
      <EmptyState
        icon={<TerminalIcon />}
        title="No task selected"
        description="Run your code here and see every command LocPilot runs (with your approval), with real exit codes and output."
        className="h-full"
      />
    )
  if (task.isLoading) return <LoadingState />
  if (task.isError) return <div className="p-3"><ErrorState error={task.error} /></div>
  const detail = task.data
  if (!detail) return null
  const commands = [...detail.commands].reverse()
  return (
    <div className="flex h-full min-h-0 flex-col">
      <RunBar task={detail} onStarted={setOpen} />
      <div className="min-h-0 flex-1 overflow-y-auto">
        {commands.length === 0 ? (
          <EmptyState
            icon={<TerminalIcon />}
            title="No commands run"
            description={
              detail.worktree_path
                ? 'Run your code above to check LocPilot’s changes. Tests LocPilot proposes run only after you approve them.'
                : detail.mode === 'ask'
                  ? 'Ask mode never executes project code.'
                  : 'Verification commands run only after you approve them.'
            }
            className="h-full"
          />
        ) : (
          <ul className="divide-y">
            {commands.map((c) => {
              const isOpen = open === c.id
              return (
                <li key={c.id}>
                  <button
                    type="button"
                    onClick={() => setOpen(isOpen ? null : c.id)}
                    className="flex w-full items-center gap-2 px-3 py-2 text-left hover:bg-accent/50"
                    aria-expanded={isOpen}
                  >
                    <ChevronRightIcon className={cn('size-3.5 shrink-0 text-muted-foreground transition-transform', isOpen && 'rotate-90')} />
                    <div className="min-w-0 flex-1 space-y-0.5">
                      <code className="block truncate font-mono text-xs">{commandLine(c)}</code>
                      <div className="flex flex-wrap items-center gap-x-2 text-[10.5px] text-muted-foreground">
                        <span>{isUserCommand(c) ? 'you ran' : c.stage}</span>
                        <span>·</span>
                        <span>{formatDuration(c.duration_ms)}</span>
                        {hasTestSummary(c) && c.summary && (
                          <>
                            <span>·</span>
                            <span className={cn(c.summary.outcome === 'passed' && 'text-success', c.summary.outcome === 'failed' && 'text-destructive')}>
                              {testSummaryText(c.summary)}
                            </span>
                          </>
                        )}
                        {c.sandbox && (
                          <span className="inline-flex items-center gap-0.5">
                            · <ShieldIcon className="size-2.5" /> {c.sandbox}
                          </span>
                        )}
                      </div>
                    </div>
                    <CommandStatusBadge command={c} />
                    {c.truncated && <Badge variant="warning">truncated</Badge>}
                  </button>
                  {isOpen && <CommandDetail taskId={detail.id} command={c} live={ev.commandOutput[c.id]} />}
                </li>
              )
            })}
          </ul>
        )}
      </div>
    </div>
  )
}
