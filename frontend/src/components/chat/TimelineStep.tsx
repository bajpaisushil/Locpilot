import {
  AlertTriangleIcon,
  BrainCircuitIcon,
  ClockIcon,
  CheckIcon,
  ChevronRightIcon,
  CircleDashedIcon,
  InfoIcon,
  MinusIcon,
  PauseIcon,
  WrenchIcon,
  XIcon,
} from 'lucide-react'
import { useState } from 'react'
import { CitationChip } from '@/components/chat/Citation'
import { Spinner } from '@/components/common/States'
import type { StepChild, StepItem } from '@/lib/timeline'
import { cn, formatDuration, formatNumber } from '@/lib/utils'

function StepIcon({ state, live, waiting }: { state: StepItem['state']; live: boolean; waiting: boolean }) {
  const base = 'flex size-5 items-center justify-center rounded-full border bg-background'
  if (state === 'started' && waiting) {
    return (
      <span className={cn(base, 'border-warning/50 text-warning')} title="Waiting for you">
        <ClockIcon className="size-3" />
      </span>
    )
  }
  if (state === 'started') {
    return live ? (
      <span className={cn(base, 'border-info/50')}>
        <Spinner className="size-3 text-info" />
      </span>
    ) : (
      <span className={cn(base, 'border-warning/50 text-warning')} title="Did not finish">
        <PauseIcon className="size-3" />
      </span>
    )
  }
  if (state === 'completed')
    return (
      <span className={cn(base, 'border-success/50 bg-success/10 text-success')}>
        <CheckIcon className="size-3" />
      </span>
    )
  if (state === 'failed')
    return (
      <span className={cn(base, 'border-destructive/50 bg-destructive/10 text-destructive')}>
        <XIcon className="size-3" />
      </span>
    )
  return (
    <span className={cn(base, 'text-muted-foreground')}>
      <MinusIcon className="size-3" />
    </span>
  )
}

function argsPreview(args: Record<string, unknown>): string {
  const parts = Object.entries(args ?? {})
    .filter(([, v]) => v !== null && v !== undefined && v !== '')
    .map(([k, v]) => `${k}=${typeof v === 'string' ? JSON.stringify(v) : JSON.stringify(v)}`)
  const s = parts.join(' ')
  return s.length > 140 ? `${s.slice(0, 140)}…` : s
}

function ToolRow({ child }: { child: Extract<StepChild, { kind: 'tool' }> }) {
  const [open, setOpen] = useState(false)
  return (
    <div className="text-xs">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-start gap-1.5 rounded px-1 py-0.5 text-left hover:bg-muted/50"
        aria-expanded={open}
      >
        <ChevronRightIcon className={cn('mt-0.5 size-3 shrink-0 text-muted-foreground transition-transform', open && 'rotate-90')} />
        <WrenchIcon className="mt-0.5 size-3 shrink-0 text-muted-foreground" />
        <span className="font-mono text-foreground/90">{child.tool}</span>
        <span className="min-w-0 flex-1 truncate text-muted-foreground">{child.summary}</span>
      </button>
      {open && (
        <div className="mt-1 ml-6 space-y-1.5 rounded-md border bg-code p-2">
          <div className="font-mono text-[11px] break-all text-muted-foreground">{argsPreview(child.args) || '(no arguments)'}</div>
          {child.summary && <div className="text-foreground/85">{child.summary}</div>}
          {child.evidenceIds.length > 0 && (
            <div className="text-[11px] text-muted-foreground">Evidence: {child.evidenceIds.join(', ')}</div>
          )}
        </div>
      )}
    </div>
  )
}

function LlmRow({ child, liveTokens, live }: { child: Extract<StepChild, { kind: 'llm' }>; liveTokens?: number; live: boolean }) {
  const running = child.state === 'started' && live
  return (
    <div className="flex items-center gap-1.5 px-1 py-0.5 text-xs">
      <BrainCircuitIcon className={cn('size-3 shrink-0', running ? 'text-primary rp-pulse' : 'text-muted-foreground')} />
      <span className="text-foreground/90">
        Local model <span className="font-mono text-muted-foreground">{child.model}</span> · {child.step}
      </span>
      {running ? (
        <span className="text-muted-foreground tabular-nums">
          {liveTokens ? `${formatNumber(liveTokens)} tokens…` : 'thinking…'}
        </span>
      ) : child.state === 'failed' ? (
        <span className="text-destructive">failed</span>
      ) : child.state === 'completed' ? (
        <span className="text-muted-foreground tabular-nums">
          {child.promptTokens !== undefined && `${formatNumber(child.promptTokens)} in`}
          {child.completionTokens !== undefined && ` · ${formatNumber(child.completionTokens)} out`}
          {child.durationMs !== undefined && ` · ${formatDuration(child.durationMs)}`}
        </span>
      ) : (
        <span className="text-muted-foreground">did not finish</span>
      )}
    </div>
  )
}

function LogRow({ child }: { child: Extract<StepChild, { kind: 'log' }> }) {
  const Icon = child.level === 'error' ? XIcon : child.level === 'warning' ? AlertTriangleIcon : InfoIcon
  return (
    <div
      className={cn(
        'flex items-start gap-1.5 px-1 py-0.5 text-xs',
        child.level === 'error' && 'text-destructive',
        child.level === 'warning' && 'text-warning',
        child.level === 'info' && 'text-muted-foreground',
      )}
    >
      <Icon className="mt-0.5 size-3 shrink-0" />
      <span className="min-w-0 break-words">{child.text}</span>
    </div>
  )
}

function EvidenceRow({ child }: { child: Extract<StepChild, { kind: 'evidence' }> }) {
  const [all, setAll] = useState(false)
  const items = all ? child.items : child.items.slice(0, 8)
  return (
    <div className="flex flex-wrap items-center gap-1 px-1 py-0.5">
      {items.map((e) => (
        <CitationChip key={e.id} citation={{ path: e.path, start_line: e.start_line, end_line: e.end_line, evidence_id: e.id }} />
      ))}
      {!all && child.items.length > 8 && (
        <button type="button" className="text-[11px] text-muted-foreground hover:text-foreground" onClick={() => setAll(true)}>
          +{child.items.length - 8} more
        </button>
      )}
    </div>
  )
}

export function TimelineStep({
  step,
  live,
  waiting = false,
  llmTokens,
  turn,
  isLast,
}: {
  step: StepItem
  live: boolean
  /** The task is paused for user input; the newest unfinished step is waiting, not broken. */
  waiting?: boolean
  llmTokens: Record<string, number>
  turn: number
  isLast: boolean
}) {
  const [collapsed, setCollapsed] = useState(false)
  const hasChildren = step.children.length > 0
  const duration =
    step.endedAt && step.startedAt ? Date.parse(step.endedAt) - Date.parse(step.startedAt) : null
  return (
    <div className="relative flex gap-3" data-testid="timeline-step">
      {!isLast && <span className="absolute top-6 bottom-0 left-[9.5px] w-px bg-border" aria-hidden />}
      <div className="relative z-10 shrink-0 pt-0.5">
        <StepIcon state={step.state} live={live} waiting={waiting} />
      </div>
      <div className="min-w-0 flex-1 pb-3">
        <button
          type="button"
          onClick={() => hasChildren && setCollapsed((v) => !v)}
          className={cn('flex w-full items-baseline gap-2 text-left', hasChildren && 'cursor-pointer')}
        >
          <span
            className={cn(
              'shrink-0 text-sm whitespace-nowrap',
              step.state === 'skipped' ? 'text-muted-foreground' : 'font-medium',
            )}
          >
            {step.label}
          </span>
          {step.detail && <span className="min-w-0 truncate text-xs text-muted-foreground">{step.detail}</span>}
          {duration !== null && duration >= 0 && step.state !== 'started' && (
            <span className="ml-auto shrink-0 text-[11px] text-muted-foreground tabular-nums">{formatDuration(duration)}</span>
          )}
          {step.state === 'started' && waiting && (
            <span className="ml-auto inline-flex shrink-0 items-center gap-1 text-[11px] text-warning">
              <ClockIcon className="size-3" />
              waiting for you
            </span>
          )}
          {step.state === 'started' && !live && !waiting && (
            <span className="ml-auto inline-flex shrink-0 items-center gap-1 text-[11px] text-warning">
              <CircleDashedIcon className="size-3" />
              incomplete
            </span>
          )}
        </button>
        {hasChildren && !collapsed && (
          <div className="mt-1 space-y-0.5">
            {step.children.map((c) => {
              switch (c.kind) {
                case 'tool':
                  return <ToolRow key={c.key} child={c} />
                case 'llm':
                  return <LlmRow key={c.key} child={c} live={live} liveTokens={llmTokens[`${turn}:${c.step}`]} />
                case 'log':
                  return <LogRow key={c.key} child={c} />
                case 'evidence':
                  return <EvidenceRow key={c.key} child={c} />
                default:
                  return null
              }
            })}
          </div>
        )}
      </div>
    </div>
  )
}
