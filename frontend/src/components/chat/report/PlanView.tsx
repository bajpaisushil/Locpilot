import { ClipboardListIcon, ShieldAlertIcon, ShieldCheckIcon } from 'lucide-react'
import { CitationChip } from '@/components/chat/Citation'
import { Markdown } from '@/components/chat/Markdown'
import { BulletList, Section } from '@/components/chat/report/Section'
import { Badge } from '@/components/ui/badge'
import type { ChangeReview, PlanOut, ProposedCommand } from '@/lib/types'

export function CommandTrustBadge({ command }: { command: ProposedCommand }) {
  return command.trusted ? (
    <Badge variant="success">
      <ShieldCheckIcon />
      allowlisted
    </Badge>
  ) : (
    <Badge variant="warning">
      <ShieldAlertIcon />
      untrusted
    </Badge>
  )
}

export function PlanView({ plan }: { plan: PlanOut }) {
  return (
    <div className="space-y-3" data-testid="plan">
      <div className="flex items-center gap-1.5 text-xs font-semibold">
        <ClipboardListIcon className="size-3.5 text-primary" />
        Implementation plan
      </div>
      {plan.summary && <p className="text-sm leading-relaxed">{plan.summary}</p>}
      {plan.approach && <Markdown>{plan.approach}</Markdown>}
      {plan.files_to_modify.length > 0 && (
        <Section title="Files to modify">
          <ul className="space-y-1.5">
            {plan.files_to_modify.map((f) => (
              <li key={f.path} className="flex flex-wrap items-baseline gap-2 text-xs">
                <CitationChip citation={{ path: f.path, start_line: 0, end_line: 0, evidence_id: null }} />
                <span className="text-muted-foreground">{f.change}</span>
              </li>
            ))}
          </ul>
        </Section>
      )}
      {(plan.test_file || plan.test_description) && (
        <Section title="Regression test">
          <div className="space-y-1 text-xs">
            {plan.test_file && <div className="font-mono">{plan.test_file}</div>}
            {plan.test_description && <div className="text-muted-foreground">{plan.test_description}</div>}
          </div>
        </Section>
      )}
      {plan.commands.length > 0 && (
        <Section title="Verification commands">
          <ul className="space-y-1.5">
            {plan.commands.map((c, i) => (
              <li key={i} className="flex flex-wrap items-center gap-2 text-xs">
                <code className="rounded bg-muted px-1.5 py-0.5 font-mono">{c.display || c.argv.join(' ')}</code>
                <CommandTrustBadge command={c} />
                {c.reason && <span className="text-muted-foreground">{c.reason}</span>}
              </li>
            ))}
          </ul>
        </Section>
      )}
      {plan.risks.length > 0 && (
        <Section title="Risks">
          <BulletList items={plan.risks} />
        </Section>
      )}
    </div>
  )
}

export function ChangeReviewView({ review }: { review: ChangeReview }) {
  return (
    <div className="space-y-3" data-testid="change-review">
      {review.answer_markdown && <Markdown>{review.answer_markdown}</Markdown>}
      {review.file_notes.length > 0 && (
        <Section title="Per-file notes">
          <ul className="space-y-1.5">
            {review.file_notes.map((n, i) => (
              <li key={`${n.path}-${i}`} className="flex flex-wrap items-baseline gap-2 text-xs">
                <CitationChip citation={{ path: n.path, start_line: 0, end_line: 0, evidence_id: null }} />
                <span className="text-foreground/85">{n.note}</span>
              </li>
            ))}
          </ul>
        </Section>
      )}
      {review.side_effects.length > 0 && (
        <Section title="Possible side effects">
          <BulletList items={review.side_effects} />
        </Section>
      )}
      {review.risks.length > 0 && (
        <Section title="Risks">
          <BulletList items={review.risks} />
        </Section>
      )}
    </div>
  )
}
