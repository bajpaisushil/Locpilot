import { useMutation, useQueryClient } from '@tanstack/react-query'
import { BrushCleaningIcon, MemoryStickIcon } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { DropdownMenu, DropdownMenuContent, DropdownMenuTrigger } from '@/components/ui/dropdown-menu'
import { Hint } from '@/components/ui/tooltip'
import { api } from '@/lib/api'
import { contextUse, ramShares } from '@/lib/memory'
import { qk, useModelStatus, useTask } from '@/lib/queries'
import { cn, formatBytes } from '@/lib/utils'
import { useUi } from '@/store/ui'

function tokens(n: number): string {
  return n >= 1000 ? `${(n / 1000).toFixed(n >= 10_000 ? 0 : 1)}k` : String(n)
}

function Bar({ segments, label }: { segments: { pct: number; className: string }[]; label: string }) {
  return (
    <div className="flex h-1.5 w-full overflow-hidden rounded-full bg-muted" role="img" aria-label={label}>
      {segments.map((s, i) => (
        <div key={i} className={cn('h-full', s.className)} style={{ width: `${s.pct}%` }} />
      ))}
    </div>
  )
}

/**
 * The top bar's memory meter: how much RAM the model occupies next to what is free, and — for the open task — how
 * full the context window was at the last step. The menu frees the model's RAM or clears the task's context.
 */
export function MemoryMeter() {
  const model = useModelStatus()
  const taskId = useUi((s) => s.selectedTaskId)
  const task = useTask(taskId)
  const queryClient = useQueryClient()
  const mem = model.data?.memory
  const ram = ramShares(mem)
  const ctx = contextUse(task.data)

  const unload = useMutation({
    mutationFn: () => api.unloadModel(),
    onSettled: () => queryClient.invalidateQueries({ queryKey: qk.model }),
  })
  const clear = useMutation({
    mutationFn: (id: string) => api.clearTaskContext(id),
    onSuccess: (detail) => queryClient.setQueryData(qk.task(detail.id), detail),
  })

  if (!ram) return null
  const busy = task.data && ['queued', 'running', 'awaiting_approval'].includes(task.data.status)
  const tight = mem?.short || ram.freePct < 10
  const summary = mem?.loaded_bytes
    ? `Model uses ${formatBytes(ram.model)} · ${formatBytes(ram.free)} of ${formatBytes(ram.total)} free`
    : `Model not loaded · ${formatBytes(ram.free)} of ${formatBytes(ram.total)} free`

  return (
    <DropdownMenu>
      <Hint label={summary}>
        <DropdownMenuTrigger asChild>
          <button
            type="button"
            className="inline-flex h-7 shrink-0 items-center gap-1.5 rounded-full border bg-background/60 px-2.5 text-xs transition-colors hover:bg-accent"
            aria-label={`Memory: ${summary}`}
            data-testid="memory-meter"
          >
            <MemoryStickIcon className={cn('size-3.5', tight ? 'text-warning' : 'text-muted-foreground')} />
            <span className="w-12">
              <Bar
                label={summary}
                segments={[
                  { pct: ram.modelPct, className: 'bg-primary' },
                  { pct: ram.otherPct, className: 'bg-muted-foreground/40' },
                ]}
              />
            </span>
            <span className="hidden font-mono text-[11px] tabular-nums @min-[640px]:block">
              {mem?.loaded_bytes ? formatBytes(ram.model) : 'idle'}
            </span>
          </button>
        </DropdownMenuTrigger>
      </Hint>
      <DropdownMenuContent align="end" className="w-80 space-y-3 p-3" data-testid="memory-panel">
        <section className="space-y-1.5">
          <div className="flex items-baseline justify-between text-xs font-medium">
            <span>Memory (RAM)</span>
            <span className="font-mono text-[11px] text-muted-foreground">{formatBytes(ram.total)}</span>
          </div>
          <Bar
            label={summary}
            segments={[
              { pct: ram.modelPct, className: 'bg-primary' },
              { pct: ram.otherPct, className: 'bg-muted-foreground/40' },
            ]}
          />
          <dl className="grid grid-cols-[auto_1fr] gap-x-3 text-[11px] text-muted-foreground">
            <dt className="flex items-center gap-1.5">
              <span className="size-2 rounded-full bg-primary" /> Model
            </dt>
            <dd className="text-right font-mono" data-testid="memory-model">
              {mem?.loaded_bytes
                ? formatBytes(ram.model)
                : mem?.needed_bytes
                  ? `not loaded (needs ~${formatBytes(mem.needed_bytes)})`
                  : 'not loaded'}
            </dd>
            <dt className="flex items-center gap-1.5">
              <span className="size-2 rounded-full bg-muted-foreground/40" /> Other apps
            </dt>
            <dd className="text-right font-mono">{formatBytes(ram.other)}</dd>
            <dt className="flex items-center gap-1.5">
              <span className="size-2 rounded-full border bg-muted" /> Free
            </dt>
            <dd className="text-right font-mono" data-testid="memory-free">
              {formatBytes(ram.free)}
            </dd>
          </dl>
          {mem?.short && (
            <p className="text-[11px] text-warning">Loading the model would push other apps into swap.</p>
          )}
          <Button
            size="sm"
            variant="outline"
            className="w-full"
            disabled={!mem?.loaded_bytes || unload.isPending}
            onClick={() => unload.mutate()}
          >
            Free RAM now
          </Button>
          {unload.isError && <p className="text-[11px] text-warning">{(unload.error as Error).message}</p>}
        </section>

        {task.data && (
          <section className="space-y-1.5 border-t pt-3" data-testid="context-section">
            <div className="flex items-baseline justify-between text-xs font-medium">
              <span>Context window (this task)</span>
              {ctx && (
                <span className="font-mono text-[11px] text-muted-foreground" data-testid="context-tokens">
                  {ctx.cleared ? 'cleared' : `${tokens(ctx.used)} / ${tokens(ctx.window)}`}
                </span>
              )}
            </div>
            <Bar
              label="Context window use"
              segments={[{ pct: ctx && !ctx.cleared ? ctx.pct : 0, className: ctx && ctx.pct > 85 ? 'bg-warning' : 'bg-primary' }]}
            />
            <p className="text-[11px] text-muted-foreground">
              {ctx?.cleared
                ? 'Your next message starts fresh. The worktree and the changes are kept.'
                : 'Each message brings earlier findings along. Clear them to give the next message the whole window.'}
            </p>
            <Button
              size="sm"
              variant="outline"
              className="w-full"
              disabled={!!busy || clear.isPending || task.data.turn === 0 || !!ctx?.cleared}
              onClick={() => clear.mutate(task.data!.id)}
            >
              <BrushCleaningIcon />
              Clear context
            </Button>
            {clear.isError && <p className="text-[11px] text-warning">{(clear.error as Error).message}</p>}
          </section>
        )}
      </DropdownMenuContent>
    </DropdownMenu>
  )
}
