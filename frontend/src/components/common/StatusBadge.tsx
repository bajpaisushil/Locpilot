import {
  AlertTriangleIcon,
  BanIcon,
  CheckCircle2Icon,
  CircleDotIcon,
  HelpCircleIcon,
  Loader2Icon,
  PauseCircleIcon,
  ShieldQuestionIcon,
  XCircleIcon,
} from 'lucide-react'
import { Badge, type BadgeVariant } from '@/components/ui/badge'
import type { FinalStatus, Mode, TaskStatus } from '@/lib/types'
import { cn } from '@/lib/utils'

const TASK_STATUS: Record<TaskStatus, { label: string; variant: BadgeVariant; icon: typeof CircleDotIcon; spin?: boolean }> =
  {
    queued: { label: 'Queued', variant: 'muted', icon: CircleDotIcon },
    running: { label: 'Running', variant: 'info', icon: Loader2Icon, spin: true },
    awaiting_approval: { label: 'Needs approval', variant: 'warning', icon: ShieldQuestionIcon },
    needs_input: { label: 'Needs input', variant: 'warning', icon: HelpCircleIcon },
    completed: { label: 'Completed', variant: 'success', icon: CheckCircle2Icon },
    failed: { label: 'Failed', variant: 'destructive', icon: XCircleIcon },
    cancelled: { label: 'Cancelled', variant: 'muted', icon: BanIcon },
    interrupted: { label: 'Interrupted', variant: 'warning', icon: PauseCircleIcon },
  }

export function TaskStatusBadge({ status, className }: { status: TaskStatus; className?: string }) {
  const s = TASK_STATUS[status] ?? TASK_STATUS.queued
  const Icon = s.icon
  return (
    <Badge variant={s.variant} className={className}>
      <Icon className={cn(s.spin && 'animate-spin')} />
      {s.label}
    </Badge>
  )
}

const FINAL_STATUS: Record<FinalStatus, { label: string; variant: BadgeVariant; icon: typeof CircleDotIcon }> = {
  answered: { label: 'Answered', variant: 'success', icon: CheckCircle2Icon },
  needs_input: { label: 'Needs input', variant: 'warning', icon: HelpCircleIcon },
  changes_verified: { label: 'Changes verified by tests', variant: 'success', icon: CheckCircle2Icon },
  changes_unverified: { label: 'Changes not verified', variant: 'warning', icon: AlertTriangleIcon },
  changes_failed: { label: 'Changes failed verification', variant: 'destructive', icon: XCircleIcon },
  changes_rejected: { label: 'Changes rejected', variant: 'muted', icon: BanIcon },
  no_changes: { label: 'No changes made', variant: 'muted', icon: CircleDotIcon },
  failed: { label: 'Failed', variant: 'destructive', icon: XCircleIcon },
  cancelled: { label: 'Cancelled', variant: 'muted', icon: BanIcon },
}

export function FinalStatusBadge({ status }: { status: FinalStatus }) {
  const s = FINAL_STATUS[status] ?? FINAL_STATUS.answered
  const Icon = s.icon
  return (
    <Badge variant={s.variant}>
      <Icon />
      {s.label}
    </Badge>
  )
}

export function ModeBadge({ mode, className }: { mode: Mode | null | undefined; className?: string }) {
  if (!mode) return null
  return (
    <Badge variant={mode === 'code' ? 'default' : 'secondary'} className={cn('uppercase tracking-wide', className)}>
      {mode}
    </Badge>
  )
}
