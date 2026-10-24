import { AlertOctagonIcon, BanIcon, ChevronRightIcon, FileDiffIcon, PauseCircleIcon, UserIcon } from 'lucide-react'
import { useState } from 'react'
import { ApprovalCard } from '@/components/chat/ApprovalCard'
import { CommandCard } from '@/components/chat/CommandCard'
import { Markdown } from '@/components/chat/Markdown'
import { InvestigationView } from '@/components/chat/report/InvestigationView'
import { PlanView } from '@/components/chat/report/PlanView'
import { ReportCard } from '@/components/chat/report/ReportCard'
import { TimelineStep } from '@/components/chat/TimelineStep'
import { Logo } from '@/components/common/Logo'
import { ModeBadge } from '@/components/common/StatusBadge'
import { UnifiedDiff } from '@/components/code/UnifiedDiff'
import { Badge } from '@/components/ui/badge'
import type { TimelineItem, Turn } from '@/lib/timeline'
import type { PatchOut } from '@/lib/types'
import { cn, formatRelativeTime } from '@/lib/utils'

function PatchItem({ patch }: { patch: PatchOut }) {
  const [open, setOpen] = useState(false)
  const adds = patch.files.reduce((n, f) => n + f.additions, 0)
  const dels = patch.files.reduce((n, f) => n + f.deletions, 0)
  const variant =
    patch.status === 'applied'
      ? 'success'
      : patch.status === 'failed'
        ? 'destructive'
        : patch.status === 'rejected' || patch.status === 'superseded' || patch.status === 'rolled_back'
          ? 'muted'
          : 'warning'
  return (
    <div className="overflow-hidden rounded-lg border bg-card" data-testid="patch-item">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-center gap-2 px-3 py-2 text-left text-xs hover:bg-muted/40"
        aria-expanded={open}
      >
        <ChevronRightIcon className={cn('size-3.5 text-muted-foreground transition-transform', open && 'rotate-90')} />
        <FileDiffIcon className="size-3.5 text-muted-foreground" />
        <span className="font-medium">Patch {patch.attempt > 1 ? `(repair ${patch.attempt - 1})` : ''}</span>
        <span className="min-w-0 flex-1 truncate text-muted-foreground">{patch.summary}</span>
        <span className="font-mono text-success">+{adds}</span>
        <span className="font-mono text-destructive">−{dels}</span>
        <Badge variant={variant}>{patch.status.replace('_', ' ')}</Badge>
      </button>
      {open && (
        <div className="space-y-2 border-t p-3">
          {patch.warnings.length > 0 && (
            <ul className="list-disc pl-5 text-xs text-warning">
              {patch.warnings.map((w, i) => (
                <li key={i}>{w}</li>
              ))}
            </ul>
          )}
          <UnifiedDiff diff={patch.diff} />
        </div>
      )}
    </div>
  )
}

function StatusNotice({ status }: { status: string }) {
  const map: Record<string, { icon: typeof BanIcon; text: string; cls: string }> = {
    cancelled: { icon: BanIcon, text: 'Cancelled. Any running command was stopped.', cls: 'text-muted-foreground' },
    interrupted: {
      icon: PauseCircleIcon,
      text: 'Interrupted (the backend stopped while this turn was running). It can be resumed from its last checkpoint.',
      cls: 'text-warning',
    },
    failed: { icon: AlertOctagonIcon, text: 'This turn failed.', cls: 'text-destructive' },
  }
  const m = map[status]
  if (!m) return null
  const Icon = m.icon
  return (
    <div className={cn('flex items-center gap-2 px-1 text-xs', m.cls)}>
      <Icon className="size-3.5" />
      {m.text}
    </div>
  )
}

export function TurnView({
  turn,
  taskId,
  actionableApprovalId,
  llmTokens,
  commandOutput,
}: {
  turn: Turn
  taskId: string
  actionableApprovalId: string | null
  llmTokens: Record<string, number>
  commandOutput: Record<string, string>
}) {
  const final = turn.final
  const items = turn.items.filter((it) => {
    if (it.kind === 'report' && final?.investigation) return false
    if (it.kind === 'plan' && final?.plan) return false
    return true
  })

  const renderItem = (it: TimelineItem, idx: number) => {
    switch (it.kind) {
      case 'step': {
        const next = items[idx + 1]
        const lastStep = !items.slice(idx + 1).some((later) => later.kind === 'step')
        return (
          <TimelineStep
            key={it.key}
            step={it}
            live={turn.live}
            waiting={turn.waiting && lastStep}
            llmTokens={llmTokens}
            turn={turn.turn}
            isLast={!next || next.kind !== 'step'}
          />
        )
      }
      case 'report':
        return (
          <div key={it.key} className="rounded-xl border bg-card p-4">
            <InvestigationView report={it.report} />
          </div>
        )
      case 'plan':
        return (
          <div key={it.key} className="rounded-xl border bg-card p-4">
            <PlanView plan={it.plan} />
          </div>
        )
      case 'patch':
        return <PatchItem key={it.key} patch={it.patch} />
      case 'approval':
        return (
          <ApprovalCard
            key={it.key}
            approval={it.approval}
            taskId={taskId}
            actionable={actionableApprovalId === it.approval.id}
            resolved={it.resolved}
          />
        )
      case 'command':
        return <CommandCard key={it.key} command={it.command} liveOutput={commandOutput[it.command.id]} />
      case 'error':
        return (
          <div
            key={it.key}
            role="alert"
            className="flex items-start gap-2 rounded-lg border border-destructive/30 bg-destructive/8 px-3 py-2 text-sm"
          >
            <AlertOctagonIcon className="mt-0.5 size-4 shrink-0 text-destructive" />
            <div className="min-w-0">
              <div className="break-words">{it.message}</div>
              {it.recoverable && <div className="text-xs text-muted-foreground">You can send a follow-up to try again.</div>}
            </div>
          </div>
        )
      case 'status':
        return <StatusNotice key={it.key} status={it.status} />
      default:
        return null
    }
  }

  return (
    <div className="space-y-4" data-testid="turn">
      {turn.userMessage && (
        <div className="flex gap-3">
          <div className="flex size-7 shrink-0 items-center justify-center rounded-full border bg-muted text-muted-foreground">
            <UserIcon className="size-3.5" />
          </div>
          <div className="min-w-0 flex-1 space-y-1">
            <div className="flex items-center gap-2 text-xs text-muted-foreground">
              <span className="font-medium text-foreground">You</span>
              <ModeBadge mode={turn.userMessage.mode} />
              <span>{formatRelativeTime(turn.userMessage.created_at)}</span>
            </div>
            <div className="text-sm leading-relaxed whitespace-pre-wrap">{turn.userMessage.content}</div>
          </div>
        </div>
      )}

      <div className="flex gap-3">
        <div className="flex size-7 shrink-0 items-center justify-center">
          <Logo className="size-6" />
        </div>
        <div className="min-w-0 flex-1 space-y-3">
          <div className="text-xs font-medium">LocPilot</div>
          {items.length === 0 && !final && turn.live && (
            <div className="text-xs text-muted-foreground">Starting…</div>
          )}
          <div className="space-y-3">{items.map(renderItem)}</div>
          {final && <ReportCard report={final} />}
          {!final &&
            turn.assistantMessages.map((m) => (
              <div key={m.id} className="rounded-lg border bg-card px-4 py-3">
                <Markdown>{m.content}</Markdown>
              </div>
            ))}
        </div>
      </div>
    </div>
  )
}
