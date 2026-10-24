import { ChevronDownIcon, Loader2Icon, ShieldIcon, TerminalIcon } from 'lucide-react'
import { useState } from 'react'
import { Badge } from '@/components/ui/badge'
import type { CommandOut } from '@/lib/types'
import { cn, formatDuration, testSummaryText } from '@/lib/utils'

export function CommandStatusBadge({ command }: { command: CommandOut }) {
  if (command.status === 'running') {
    return (
      <Badge variant="info">
        <Loader2Icon className="animate-spin" />
        running
      </Badge>
    )
  }
  if (command.status === 'timed_out') return <Badge variant="destructive">timed out</Badge>
  if (command.status === 'cancelled') return <Badge variant="muted">cancelled</Badge>
  if (command.status === 'interrupted') return <Badge variant="warning">interrupted</Badge>
  if (command.status === 'error') return <Badge variant="destructive">error</Badge>
  return (
    <Badge variant={command.exit_code === 0 ? 'success' : 'destructive'}>exit {command.exit_code ?? '—'}</Badge>
  )
}

export function OutputBlock({ text, label, className }: { text: string; label?: string; className?: string }) {
  return (
    <div className={cn('overflow-hidden rounded-md border bg-code', className)}>
      {label && <div className="border-b px-2.5 py-1 text-[10px] tracking-wide text-muted-foreground uppercase">{label}</div>}
      <pre className="max-h-80 overflow-auto px-2.5 py-2 font-mono text-[11px] leading-[17px] whitespace-pre-wrap break-all">
        {text || <span className="text-muted-foreground italic">(empty)</span>}
      </pre>
    </div>
  )
}

export function CommandCard({ command, liveOutput }: { command: CommandOut; liveOutput?: string }) {
  const running = command.status === 'running'
  const [open, setOpen] = useState(false)
  const showOutput = open || running
  const summary = command.summary
  return (
    <div className="overflow-hidden rounded-lg border bg-card" data-testid="command-card">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-center gap-2 px-3 py-2 text-left hover:bg-muted/40"
        aria-expanded={showOutput}
      >
        <TerminalIcon className="size-3.5 shrink-0 text-muted-foreground" />
        <code className="min-w-0 flex-1 truncate font-mono text-xs">{command.argv.join(' ')}</code>
        {command.stage && <Badge variant="outline">{command.stage}</Badge>}
        <CommandStatusBadge command={command} />
        {!running && <span className="text-[11px] text-muted-foreground">{formatDuration(command.duration_ms)}</span>}
        <ChevronDownIcon className={cn('size-3.5 text-muted-foreground transition-transform', !showOutput && '-rotate-90')} />
      </button>
      {(summary || command.sandbox) && (
        <div className="flex flex-wrap items-center gap-x-3 gap-y-1 border-t px-3 py-1.5 text-[11px] text-muted-foreground">
          {summary && (
            <span className={cn(summary.outcome === 'passed' && 'text-success', summary.outcome === 'failed' && 'text-destructive')}>
              {summary.framework}: {testSummaryText(summary)}
            </span>
          )}
          {command.sandbox && (
            <span className="inline-flex items-center gap-1">
              <ShieldIcon className="size-3" />
              {command.sandbox}
            </span>
          )}
          {command.truncated && <span className="text-warning">output truncated</span>}
        </div>
      )}
      {showOutput && (
        <div className="space-y-2 border-t p-2.5">
          {running ? (
            <OutputBlock text={liveOutput ?? ''} label="live output" />
          ) : (
            <>
              {command.stdout ? <OutputBlock text={command.stdout} label="stdout (tail)" /> : null}
              {command.stderr ? <OutputBlock text={command.stderr} label="stderr (tail)" /> : null}
              {!command.stdout && !command.stderr && (
                <div className="text-xs text-muted-foreground">No output captured. Full output is in the Commands tab.</div>
              )}
              {summary?.failing_tests?.length ? (
                <div className="text-xs">
                  <div className="mb-1 font-medium text-destructive">Failing tests</div>
                  <ul className="space-y-0.5 font-mono text-[11px]">
                    {summary.failing_tests.slice(0, 20).map((t) => (
                      <li key={t} className="truncate">
                        {t}
                      </li>
                    ))}
                  </ul>
                </div>
              ) : null}
            </>
          )}
        </div>
      )}
    </div>
  )
}
