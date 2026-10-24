import { AlertTriangleIcon, ArrowRightIcon, HelpCircleIcon } from 'lucide-react'
import type { ReactNode } from 'react'
import { FinalStatusBadge } from '@/components/common/StatusBadge'
import { InvestigationView } from '@/components/chat/report/InvestigationView'
import { ChangeReviewView, PlanView } from '@/components/chat/report/PlanView'
import { BulletList } from '@/components/chat/report/Section'
import { VerificationView } from '@/components/chat/report/VerificationView'
import { Separator } from '@/components/ui/separator'
import type { FinalReport } from '@/lib/types'
import { formatDuration, formatNumber } from '@/lib/utils'

export function ReportStats({ stats }: { stats: FinalReport['stats'] }) {
  if (!stats) return null
  const tokens = (stats.prompt_tokens ?? 0) + (stats.completion_tokens ?? 0)
  return (
    <div className="flex flex-wrap gap-x-3 gap-y-1 text-[11px] text-muted-foreground">
      <span>{formatNumber(stats.llm_calls)} model calls</span>
      <span>{formatNumber(tokens)} tokens</span>
      <span>{formatNumber(stats.tool_calls)} tool calls</span>
      <span>{formatDuration(stats.duration_ms)}</span>
    </div>
  )
}

/** The final, persisted result of one turn. Every value shown comes from the backend report. */
export function ReportCard({ report, showInvestigation = true }: { report: FinalReport; showInvestigation?: boolean }) {
  const sections: ReactNode[] = []
  if (report.clarifying_question) {
    sections.push(
      <div
        key="clarify"
        className="flex items-start gap-2.5 rounded-lg border border-warning/30 bg-warning/8 px-3 py-2.5 text-sm"
      >
        <HelpCircleIcon className="mt-0.5 size-4 shrink-0 text-warning" />
        <div>
          <div className="font-medium">LocPilot needs more information</div>
          <div className="mt-0.5 text-foreground/85">{report.clarifying_question}</div>
        </div>
      </div>,
    )
  }
  if (showInvestigation && report.investigation) {
    sections.push(<InvestigationView key="inv" report={report.investigation} />)
  }
  if (report.plan) sections.push(<PlanView key="plan" plan={report.plan} />)
  if (report.verification) sections.push(<VerificationView key="ver" verification={report.verification} />)
  if (report.change_review) sections.push(<ChangeReviewView key="review" review={report.change_review} />)
  if (report.unresolved_issues?.length) {
    sections.push(
      <div key="unresolved" className="space-y-1.5">
        <div className="flex items-center gap-1.5 text-xs font-semibold text-warning">
          <AlertTriangleIcon className="size-3.5" />
          Unresolved issues
        </div>
        <BulletList items={report.unresolved_issues} />
      </div>,
    )
  }
  if (report.next_steps?.length) {
    sections.push(
      <div key="next" className="space-y-1.5">
        <div className="flex items-center gap-1.5 text-xs font-semibold">
          <ArrowRightIcon className="size-3.5 text-primary" />
          Suggested next steps
        </div>
        <BulletList items={report.next_steps} />
      </div>,
    )
  }

  return (
    <div className="overflow-hidden rounded-xl border bg-card shadow-xs" data-testid="final-report">
      <div className="flex flex-wrap items-center gap-2 border-b bg-muted/30 px-4 py-2.5">
        <FinalStatusBadge status={report.status} />
        <div className="min-w-0 flex-1 text-sm font-medium">{report.headline}</div>
        {report.retries > 0 && (
          <span className="text-[11px] text-muted-foreground">
            {report.retries} repair attempt{report.retries === 1 ? '' : 's'}
          </span>
        )}
      </div>
      <div className="space-y-5 px-4 py-4">
        {sections.map((s, i) => (
          <div key={i} className="space-y-5">
            {i > 0 && <Separator />}
            {s}
          </div>
        ))}
        {sections.length === 0 && <p className="text-sm text-muted-foreground">No details were reported.</p>}
      </div>
      <div className="border-t px-4 py-2">
        <ReportStats stats={report.stats} />
      </div>
    </div>
  )
}
