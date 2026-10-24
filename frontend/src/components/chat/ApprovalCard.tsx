import { useMutation, useQueryClient } from '@tanstack/react-query'
import {
  CheckIcon,
  FilePlusIcon,
  FlaskConicalIcon,
  GitBranchIcon,
  ShieldIcon,
  ShieldQuestionIcon,
  TriangleAlertIcon,
  XIcon,
} from 'lucide-react'
import { useState } from 'react'
import { ErrorState } from '@/components/common/States'
import { Markdown } from '@/components/chat/Markdown'
import { CommandTrustBadge } from '@/components/chat/report/PlanView'
import { UnifiedDiff } from '@/components/code/UnifiedDiff'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Checkbox } from '@/components/ui/checkbox'
import { Textarea } from '@/components/ui/textarea'
import { api } from '@/lib/api'
import { qk } from '@/lib/queries'
import type { ApprovalDecision, ApprovalOut } from '@/lib/types'
import { cn, formatRelativeTime } from '@/lib/utils'

export interface ApprovalCardProps {
  approval: ApprovalOut
  taskId: string
  /** True only when this approval is the task's current pending approval. */
  actionable: boolean
  resolved?: { decision: 'approve' | 'reject'; commands: string[][] | null } | null
}

const STATUS_BADGE = {
  pending: { label: 'Awaiting your review', variant: 'warning' as const },
  approved: { label: 'Approved', variant: 'success' as const },
  rejected: { label: 'Rejected', variant: 'muted' as const },
  expired: { label: 'Expired', variant: 'muted' as const },
}

export function ApprovalCard({ approval, taskId, actionable, resolved }: ApprovalCardProps) {
  const qc = useQueryClient()
  const p = approval.payload
  // Allowlisted commands are pre-selected; untrusted ones need an explicit opt-in.
  const [selected, setSelected] = useState<Set<number>>(
    () => new Set(p.commands.flatMap((c, i) => (c.trusted ? [i] : []))),
  )
  const [rejecting, setRejecting] = useState(false)
  const [note, setNote] = useState('')

  const decide = useMutation({
    mutationFn: (decision: ApprovalDecision) => api.decideApproval(taskId, approval.id, decision),
    onSuccess: (task) => {
      qc.setQueryData(qk.task(task.id), task)
      void qc.invalidateQueries({ queryKey: qk.tasks(task.repo_id) })
    },
  })

  const status = STATUS_BADGE[approval.status] ?? STATUS_BADGE.pending
  const isPending = approval.status === 'pending'
  const canAct = actionable && isPending && !decide.isSuccess
  const totalAdd = p.files.reduce((n, f) => n + f.additions, 0)
  const totalDel = p.files.reduce((n, f) => n + f.deletions, 0)
  const selectedCommands = p.commands.filter((_, i) => selected.has(i)).map((c) => c.argv)
  const hasUntrusted = p.commands.some((c) => !c.trusted)

  const toggle = (i: number, on: boolean) =>
    setSelected((prev) => {
      const next = new Set(prev)
      if (on) next.add(i)
      else next.delete(i)
      return next
    })

  return (
    <div
      className={cn(
        'overflow-hidden rounded-xl border bg-card shadow-xs',
        canAct && 'border-warning/45 ring-1 ring-warning/20',
      )}
      data-testid="approval-card"
    >
      <div className="flex flex-wrap items-center gap-2 border-b bg-muted/30 px-4 py-2.5">
        <ShieldQuestionIcon className="size-4 text-warning" />
        <span className="text-sm font-semibold">Review proposed changes</span>
        {p.attempt > 1 && <Badge variant="outline">Repair attempt {p.attempt - 1}</Badge>}
        <Badge variant={status.variant}>{status.label}</Badge>
        <span className="ml-auto text-[11px] text-muted-foreground">{formatRelativeTime(approval.created_at)}</span>
      </div>

      <div className="space-y-4 px-4 py-4">
        <div className="space-y-1.5">
          {p.title && <div className="text-sm font-medium">{p.title}</div>}
          {p.plan_summary && <Markdown>{p.plan_summary}</Markdown>}
        </div>

        <div className="flex items-start gap-2 rounded-md border bg-muted/30 px-3 py-2 text-xs text-muted-foreground">
          <GitBranchIcon className="mt-0.5 size-3.5 shrink-0" />
          <span>
            Approving applies these edits to LocPilot&apos;s isolated Git worktree for this task — never to your own
            working tree. You can roll them back from the Changes tab.
          </span>
        </div>

        <div className="space-y-2">
          <div className="flex items-center gap-2 text-xs">
            <span className="font-medium">
              {p.files.length} file{p.files.length === 1 ? '' : 's'}
            </span>
            <span className="font-mono text-success">+{totalAdd}</span>
            <span className="font-mono text-destructive">−{totalDel}</span>
          </div>
          <ul className="flex flex-wrap gap-1.5">
            {p.files.map((f) => (
              <li
                key={f.path}
                className="inline-flex items-center gap-1.5 rounded-md border px-2 py-0.5 font-mono text-[11px]"
              >
                {f.change === 'create' && <FilePlusIcon className="size-3 text-success" />}
                {f.path}
                <Badge variant={f.kind === 'test' ? 'info' : 'default'}>{f.kind}</Badge>
              </li>
            ))}
          </ul>
          <UnifiedDiff diff={p.diff} />
        </div>

        {p.warnings.length > 0 && (
          <ul className="space-y-1 rounded-md border border-warning/30 bg-warning/8 px-3 py-2 text-xs">
            {p.warnings.map((w, i) => (
              <li key={i} className="flex items-start gap-2">
                <TriangleAlertIcon className="mt-0.5 size-3.5 shrink-0 text-warning" />
                <span>{w}</span>
              </li>
            ))}
          </ul>
        )}

        <div className="space-y-2">
          <div className="flex items-center gap-1.5 text-xs font-semibold">
            <FlaskConicalIcon className="size-3.5 text-primary" />
            Verification commands to run after applying
          </div>
          {p.commands.length === 0 ? (
            <p className="text-xs text-muted-foreground">
              No verification commands were proposed — tests will not be run for this change.
            </p>
          ) : (
            <ul className="space-y-1.5">
              {p.commands.map((c, i) => (
                <li key={i} className="flex items-start gap-2.5 rounded-md border px-3 py-2">
                  <Checkbox
                    id={`${approval.id}-cmd-${i}`}
                    checked={selected.has(i)}
                    disabled={!canAct}
                    onCheckedChange={(v) => toggle(i, v === true)}
                    className="mt-0.5"
                    aria-label={`Run ${c.display || c.argv.join(' ')}`}
                  />
                  <label htmlFor={`${approval.id}-cmd-${i}`} className="min-w-0 flex-1 space-y-1">
                    <div className="flex flex-wrap items-center gap-2">
                      <code className="font-mono text-xs break-all">{c.display || c.argv.join(' ')}</code>
                      <CommandTrustBadge command={c} />
                    </div>
                    {c.reason && <div className="text-xs text-muted-foreground">{c.reason}</div>}
                  </label>
                </li>
              ))}
            </ul>
          )}
          {hasUntrusted && canAct && (
            <p className="text-xs text-muted-foreground">
              Commands marked untrusted run project code (for example its test suite), so they start unticked. Only the
              commands you tick — or allow with “Approve &amp; run all” — will run.
            </p>
          )}
          <div
            className={cn(
              'flex items-start gap-2 text-xs',
              p.sandbox && !/^none\b/i.test(p.sandbox) ? 'text-muted-foreground' : 'text-warning',
            )}
          >
            <ShieldIcon className="mt-0.5 size-3.5 shrink-0" />
            <span>
              {p.sandbox && !/^none\b/i.test(p.sandbox)
                ? `Sandbox: ${p.sandbox}`
                : 'No OS sandbox is available on this platform — approved commands run with your user permissions. Project tests can execute arbitrary code.'}
            </span>
          </div>
        </div>

        {decide.isError && <ErrorState error={decide.error} title="Could not record your decision" compact />}

        {resolved && (
          <div className="flex items-center gap-2 rounded-md bg-muted/40 px-3 py-2 text-xs">
            {resolved.decision === 'approve' ? (
              <CheckIcon className="size-3.5 text-success" />
            ) : (
              <XIcon className="size-3.5 text-muted-foreground" />
            )}
            {resolved.decision === 'approve'
              ? `Approved — ${resolved.commands?.length ?? 0} verification command${resolved.commands?.length === 1 ? '' : 's'} selected.`
              : 'Rejected.'}
          </div>
        )}
      </div>

      {canAct && (
        <div className="space-y-3 border-t bg-muted/20 px-4 py-3">
          {rejecting && (
            <Textarea
              autoFocus
              value={note}
              onChange={(e) => setNote(e.target.value)}
              placeholder="Optional: tell LocPilot what to do differently"
              className="min-h-14 bg-background text-xs"
            />
          )}
          <div className="flex flex-wrap items-center justify-end gap-2">
            {rejecting ? (
              <>
                <Button variant="ghost" size="sm" onClick={() => setRejecting(false)} disabled={decide.isPending}>
                  Back
                </Button>
                <Button
                  variant="destructive"
                  size="sm"
                  disabled={decide.isPending}
                  onClick={() => decide.mutate({ decision: 'reject', note: note.trim() || undefined })}
                >
                  <XIcon />
                  Confirm reject
                </Button>
              </>
            ) : (
              <>
                <Button variant="outline" size="sm" onClick={() => setRejecting(true)} disabled={decide.isPending}>
                  <XIcon />
                  Reject
                </Button>
                {selectedCommands.length === 0 && p.commands.length > 0 ? (
                  <>
                    <Button
                      variant="outline"
                      size="sm"
                      disabled={decide.isPending}
                      onClick={() => decide.mutate({ decision: 'approve', commands: [] })}
                    >
                      Approve without running tests
                    </Button>
                    <Button
                      size="sm"
                      disabled={decide.isPending}
                      onClick={() => decide.mutate({ decision: 'approve', commands: p.commands.map((c) => c.argv) })}
                    >
                      <CheckIcon />
                      {`Approve & run all ${p.commands.length} command${p.commands.length === 1 ? '' : 's'}`}
                    </Button>
                  </>
                ) : (
                  <Button
                    size="sm"
                    disabled={decide.isPending}
                    onClick={() => decide.mutate({ decision: 'approve', commands: selectedCommands })}
                  >
                    <CheckIcon />
                    {selectedCommands.length > 0
                      ? `Approve & run ${selectedCommands.length} command${selectedCommands.length === 1 ? '' : 's'}`
                      : 'Approve'}
                  </Button>
                )}
              </>
            )}
          </div>
        </div>
      )}
    </div>
  )
}
