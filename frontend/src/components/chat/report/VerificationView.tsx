import {
  AlertTriangleIcon,
  CheckCircle2Icon,
  CircleDashedIcon,
  FlaskConicalIcon,
  TerminalSquareIcon,
  XCircleIcon,
} from 'lucide-react'
import { BulletList, Section } from '@/components/chat/report/Section'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Hint } from '@/components/ui/tooltip'
import type { Verification } from '@/lib/types'
import { cn, testSummaryText } from '@/lib/utils'
import { useUi } from '@/store/ui'

export function ExecutionBadge() {
  return (
    <Hint label="These results come from commands LocPilot actually ran, with their real exit codes and output.">
      <Badge variant="success" className="cursor-help">
        <TerminalSquareIcon />
        Established by execution
      </Badge>
    </Hint>
  )
}

const VERDICT = {
  passed: { label: 'Tests passed', icon: CheckCircle2Icon, cls: 'border-success/30 bg-success/8 text-success' },
  failed: { label: 'Tests failed', icon: XCircleIcon, cls: 'border-destructive/30 bg-destructive/8 text-destructive' },
  partial: { label: 'Partially verified', icon: AlertTriangleIcon, cls: 'border-warning/30 bg-warning/8 text-warning' },
  not_run: { label: 'Tests not run', icon: CircleDashedIcon, cls: 'border-warning/30 bg-warning/8 text-warning' },
} as const

const REGRESSION = {
  detected_bug: { label: 'Regression test failed before the fix and passes after it', variant: 'success' as const },
  did_not_fail_before_fix: {
    label: 'Regression test did not fail before the fix — it may not detect the bug',
    variant: 'warning' as const,
  },
  not_created: { label: 'No regression test was created', variant: 'muted' as const },
  unknown: { label: 'Regression test effectiveness unknown', variant: 'muted' as const },
}

export function VerificationView({ verification }: { verification: Verification }) {
  const setRightTab = useUi((s) => s.setRightTab)
  const verdict = VERDICT[verification.verdict] ?? VERDICT.not_run
  const VerdictIcon = verdict.icon
  const regression = REGRESSION[verification.regression_test?.status ?? 'unknown'] ?? REGRESSION.unknown
  return (
    <div className="space-y-3" data-testid="verification">
      <div className="flex flex-wrap items-center gap-2">
        <span className="flex items-center gap-1.5 text-xs font-semibold">
          <FlaskConicalIcon className="size-3.5 text-primary" />
          Verification
        </span>
        {verification.tests_run && <ExecutionBadge />}
      </div>

      <div className={cn('flex items-start gap-2.5 rounded-lg border px-3 py-2.5', verdict.cls)}>
        <VerdictIcon className="mt-0.5 size-4 shrink-0" />
        <div className="space-y-0.5">
          <div className="text-sm font-semibold">{verdict.label}</div>
          {verification.verdict === 'not_run' && (
            <div className="text-xs text-foreground/80">
              No verification commands were executed, so these changes are unverified.
            </div>
          )}
        </div>
      </div>

      {verification.runs.length > 0 && (
        <div className="overflow-hidden rounded-md border">
          <table className="w-full text-xs">
            <thead className="bg-muted/50 text-left text-[11px] text-muted-foreground">
              <tr>
                <th className="px-2.5 py-1.5 font-medium">Stage</th>
                <th className="px-2.5 py-1.5 font-medium">Command</th>
                <th className="px-2.5 py-1.5 font-medium">Exit</th>
                <th className="px-2.5 py-1.5 font-medium">Result</th>
              </tr>
            </thead>
            <tbody>
              {verification.runs.map((r) => (
                <tr key={r.command_id} className="border-t align-top">
                  <td className="px-2.5 py-1.5 whitespace-nowrap text-muted-foreground">{r.stage}</td>
                  <td className="max-w-[18rem] truncate px-2.5 py-1.5 font-mono" title={r.argv.join(' ')}>
                    {r.argv.join(' ')}
                  </td>
                  <td className="px-2.5 py-1.5 font-mono">
                    <Badge variant={r.exit_code === 0 ? 'success' : r.exit_code === null ? 'muted' : 'destructive'}>
                      {r.exit_code ?? '—'}
                    </Badge>
                  </td>
                  <td className="px-2.5 py-1.5 text-muted-foreground">{testSummaryText(r.summary)}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <div className="border-t px-2.5 py-1">
            <Button variant="link" size="xs" className="px-0" onClick={() => setRightTab('commands')}>
              View full command output
            </Button>
          </div>
        </div>
      )}

      <div className="flex flex-wrap items-center gap-2 text-xs">
        <Badge variant={regression.variant}>{regression.label}</Badge>
        {verification.regression_test?.detail && (
          <span className="text-muted-foreground">{verification.regression_test.detail}</span>
        )}
      </div>

      {(verification.preexisting_failures.length > 0 || verification.new_failures.length > 0) && (
        <div className="grid gap-3 sm:grid-cols-2">
          <Section title="New failures">
            <BulletList items={verification.new_failures} empty="None." />
          </Section>
          <Section title="Pre-existing failures (before LocPilot's change)">
            <BulletList items={verification.preexisting_failures} empty="None." />
          </Section>
        </div>
      )}
    </div>
  )
}
