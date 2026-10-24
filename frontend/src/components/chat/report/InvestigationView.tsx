import { AlertTriangleIcon, MicroscopeIcon, SearchCheckIcon } from 'lucide-react'
import { CitationChip, CitationList } from '@/components/chat/Citation'
import { Markdown } from '@/components/chat/Markdown'
import { BulletList, Disclosure, Section } from '@/components/chat/report/Section'
import { Badge, type BadgeVariant } from '@/components/ui/badge'
import { Hint } from '@/components/ui/tooltip'
import type { ExaminedFile, Hypothesis, InvestigationReport } from '@/lib/types'
import { cn } from '@/lib/utils'

export function StaticAnalysisBadge() {
  return (
    <Hint label="Derived by reading source code. Nothing was executed to produce these findings.">
      <Badge variant="info" className="cursor-help">
        <MicroscopeIcon />
        Static analysis
      </Badge>
    </Hint>
  )
}

const CONFIDENCE: Record<Hypothesis['confidence'], BadgeVariant> = {
  high: 'success',
  medium: 'warning',
  low: 'muted',
}

const HYP_STATUS: Record<Hypothesis['status'], { label: string; variant: BadgeVariant }> = {
  supported: { label: 'Supported by evidence', variant: 'success' },
  weak: { label: 'Weak evidence', variant: 'warning' },
  unsupported: { label: 'Unsupported', variant: 'muted' },
  contradicted: { label: 'Contradicted', variant: 'destructive' },
}

function HypothesisCard({ h, index }: { h: Hypothesis; index: number }) {
  const status = HYP_STATUS[h.status] ?? HYP_STATUS.unsupported
  return (
    <div className="space-y-2 rounded-md border bg-background/40 p-3" data-testid="hypothesis">
      <div className="flex flex-wrap items-center gap-1.5">
        <span className="font-mono text-[11px] text-muted-foreground">H{index + 1}</span>
        <span className="text-sm font-medium">{h.title}</span>
        <Badge variant={CONFIDENCE[h.confidence] ?? 'muted'}>{h.confidence} confidence</Badge>
        <Badge variant={status.variant}>{status.label}</Badge>
      </div>
      {h.explanation && <p className="text-sm leading-relaxed text-foreground/90">{h.explanation}</p>}
      <div className="grid gap-2 sm:grid-cols-2">
        <div className="space-y-1">
          <div className="text-[11px] font-medium text-success">Supporting evidence</div>
          {h.supporting.length ? (
            <CitationList citations={h.supporting} />
          ) : (
            <div className="text-xs text-muted-foreground italic">None verified</div>
          )}
        </div>
        <div className="space-y-1">
          <div className="text-[11px] font-medium text-destructive">Contradicting evidence</div>
          {h.contradicting.length ? (
            <CitationList citations={h.contradicting} />
          ) : (
            <div className="text-xs text-muted-foreground italic">None found</div>
          )}
        </div>
      </div>
    </div>
  )
}

function FilesExamined({ files }: { files: ExaminedFile[] }) {
  if (!files?.length) return <p className="text-xs text-muted-foreground italic">No files recorded.</p>
  return (
    <ul className="space-y-1.5">
      {files.map((f) => (
        <li key={f.path} className="flex flex-wrap items-baseline gap-x-2 gap-y-1 text-xs">
          <CitationChip
            citation={{
              path: f.path,
              start_line: Number(f.ranges[0]?.split('-')[0]) || 0,
              end_line: Number(f.ranges[0]?.split('-')[1] ?? f.ranges[0]?.split('-')[0]) || 0,
              evidence_id: null,
            }}
          />
          {f.ranges.length > 0 && <span className="font-mono text-muted-foreground">lines {f.ranges.join(', ')}</span>}
          {f.symbols.length > 0 && (
            <span className="font-mono text-foreground/80">{f.symbols.slice(0, 6).join(', ')}</span>
          )}
          {f.reason && <span className="text-muted-foreground">— {f.reason}</span>}
        </li>
      ))}
    </ul>
  )
}

export function InvestigationView({ report }: { report: InvestigationReport }) {
  const isRca = report.kind === 'rca'
  return (
    <div className="space-y-4" data-testid="investigation-report">
      <div className="flex flex-wrap items-center gap-2">
        <span className="flex items-center gap-1.5 text-xs font-semibold">
          <SearchCheckIcon className="size-3.5 text-primary" />
          {isRca ? 'Root-cause analysis' : 'Investigation'}
        </span>
        <StaticAnalysisBadge />
        {!report.evidence_sufficient && <Badge variant="warning">Evidence insufficient</Badge>}
      </div>

      {report.answer_markdown && <Markdown>{report.answer_markdown}</Markdown>}

      {report.removed_citations?.length > 0 && (
        <div className="flex items-start gap-2 rounded-md border border-warning/25 bg-warning/6 px-3 py-2 text-xs text-muted-foreground">
          <AlertTriangleIcon className="mt-0.5 size-3.5 shrink-0 text-warning" />
          <span>
            {report.removed_citations.length} citation{report.removed_citations.length === 1 ? '' : 's'} produced by the
            model could not be verified against the repository and {report.removed_citations.length === 1 ? 'was' : 'were'}{' '}
            removed: <span className="font-mono">{report.removed_citations.slice(0, 6).join(', ')}</span>
            {report.removed_citations.length > 6 ? ' …' : ''}
          </span>
        </div>
      )}

      {isRca ? (
        <div className="space-y-4">
          <Section index={1} title="Problem summary">
            <p className="leading-relaxed">{report.problem_summary || '—'}</p>
          </Section>
          <Section index={2} title="Observed symptoms">
            <BulletList items={report.observed_symptoms} />
          </Section>
          <Section index={3} title="Relevant execution flow">
            <FlowSteps report={report} />
          </Section>
          <Section index={4} title="Files, functions and lines examined">
            <FilesExamined files={report.files_examined} />
          </Section>
          <Section index={5} title="Candidate root causes">
            {report.hypotheses.length ? (
              <div className="space-y-2">
                {report.hypotheses.map((h, i) => (
                  <HypothesisCard key={i} h={h} index={i} />
                ))}
              </div>
            ) : (
              <p className="text-xs text-muted-foreground italic">No candidate root causes were proposed.</p>
            )}
          </Section>
          <Section index={6} title="Evidence for and against each hypothesis">
            <p className="text-xs text-muted-foreground">
              Listed on each hypothesis above. Every reference was checked against real repository content.
            </p>
          </Section>
          {report.most_likely_root_cause && report.evidence_sufficient ? (
            <Section index={7} title="Most likely root cause" tone="highlight">
              <p className="leading-relaxed font-medium">{report.most_likely_root_cause}</p>
              <CitationList citations={report.root_cause_citations} className="mt-2" />
            </Section>
          ) : (
            <Section index={7} title="Most likely root cause" tone="warning">
              <p className="leading-relaxed">
                {report.most_likely_root_cause
                  ? `Tentative: ${report.most_likely_root_cause}`
                  : 'The evidence gathered is insufficient to identify a root cause with confidence.'}
              </p>
              <CitationList citations={report.root_cause_citations} className="mt-2" />
            </Section>
          )}
          <Section index={8} title="Minimal recommended fix">
            <p className="leading-relaxed whitespace-pre-wrap">{report.minimal_fix || '—'}</p>
          </Section>
          <Section index={9} title="Potential side effects">
            <BulletList items={report.side_effects} />
          </Section>
          <Section index={10} title="Reproduction and verification steps">
            <BulletList items={report.verification_steps} />
          </Section>
          <Section index={11} title="Remaining uncertainties">
            <BulletList items={report.uncertainties} />
          </Section>
        </div>
      ) : (
        <div className="space-y-4">
          {report.execution_flow.length > 0 && (
            <Section title="Execution flow">
              <FlowSteps report={report} />
            </Section>
          )}
          {report.uncertainties.length > 0 && (
            <Section title="Uncertainties">
              <BulletList items={report.uncertainties} />
            </Section>
          )}
          {report.files_examined.length > 0 && (
            <Disclosure title="Files examined" count={report.files_examined.length}>
              <FilesExamined files={report.files_examined} />
            </Disclosure>
          )}
        </div>
      )}

      {report.evidence.length > 0 && (
        <Disclosure title="Evidence gathered" count={report.evidence.length}>
          <ul className="space-y-1.5">
            {report.evidence.map((e) => (
              <li key={e.id} className="flex flex-wrap items-center gap-2 text-xs">
                <span className="w-8 font-mono text-muted-foreground">{e.id}</span>
                <CitationChip
                  citation={{ path: e.path, start_line: e.start_line, end_line: e.end_line, evidence_id: e.id }}
                />
                <Badge variant="muted">{e.kind}</Badge>
                <span className="min-w-0 truncate text-muted-foreground">{e.title}</span>
              </li>
            ))}
          </ul>
        </Disclosure>
      )}
    </div>
  )
}

function FlowSteps({ report }: { report: InvestigationReport }) {
  if (!report.execution_flow?.length) return <p className="text-xs text-muted-foreground italic">Not traced.</p>
  return (
    <ol className="space-y-2">
      {report.execution_flow.map((step, i) => (
        <li key={i} className="flex gap-2.5">
          <span
            className={cn(
              'mt-0.5 flex size-5 shrink-0 items-center justify-center rounded-full border bg-muted font-mono text-[10px]',
            )}
          >
            {i + 1}
          </span>
          <div className="min-w-0 flex-1 space-y-1">
            <p className="leading-relaxed">{step.description}</p>
            <CitationList citations={step.citations} />
          </div>
        </li>
      ))}
    </ol>
  )
}
