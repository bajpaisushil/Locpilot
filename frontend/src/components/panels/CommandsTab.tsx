import { useQuery } from '@tanstack/react-query'
import { ChevronRightIcon, ShieldIcon, TerminalIcon } from 'lucide-react'
import { useState } from 'react'
import { CommandStatusBadge, OutputBlock } from '@/components/chat/CommandCard'
import { EmptyState, ErrorState, LoadingState } from '@/components/common/States'
import { Badge } from '@/components/ui/badge'
import { api } from '@/lib/api'
import { qk, useTask } from '@/lib/queries'
import type { CommandOut } from '@/lib/types'
import { cn, formatDateTime, formatDuration, testSummaryText } from '@/lib/utils'
import { useTaskEvents } from '@/store/events'
import { useUi } from '@/store/ui'

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
        <dd className="font-mono break-all">{c.argv.join(' ')}</dd>
        <dt className="text-muted-foreground">Directory</dt>
        <dd className="font-mono break-all">{c.cwd}</dd>
        <dt className="text-muted-foreground">Started</dt>
        <dd>{formatDateTime(c.started_at)}</dd>
        <dt className="text-muted-foreground">Duration</dt>
        <dd>{formatDuration(c.duration_ms)}{c.timed_out && ' (timed out)'}</dd>
        <dt className="text-muted-foreground">Sandbox</dt>
        <dd>{c.sandbox || 'none'}</dd>
        {c.summary && (
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
        </>
      )}
    </div>
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
        description="Commands LocPilot runs (with your approval) are recorded here with real exit codes and output."
        className="h-full"
      />
    )
  if (task.isLoading) return <LoadingState />
  if (task.isError) return <div className="p-3"><ErrorState error={task.error} /></div>
  const commands = [...(task.data?.commands ?? [])].reverse()
  if (commands.length === 0)
    return (
      <EmptyState
        icon={<TerminalIcon />}
        title="No commands run"
        description={
          task.data?.mode === 'ask'
            ? 'Ask mode never executes project code.'
            : 'Verification commands run only after you approve them.'
        }
        className="h-full"
      />
    )
  return (
    <div className="h-full overflow-y-auto">
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
                  <code className="block truncate font-mono text-xs">{c.argv.join(' ')}</code>
                  <div className="flex flex-wrap items-center gap-x-2 text-[10.5px] text-muted-foreground">
                    <span>{c.stage}</span>
                    <span>·</span>
                    <span>{formatDuration(c.duration_ms)}</span>
                    {c.summary && (
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
              {isOpen && <CommandDetail taskId={taskId} command={c} live={ev.commandOutput[c.id]} />}
            </li>
          )
        })}
      </ul>
    </div>
  )
}
