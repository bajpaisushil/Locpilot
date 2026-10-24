import { FileCode2Icon } from 'lucide-react'
import { useCitationScope } from '@/components/chat/citation-context'
import { Hint } from '@/components/ui/tooltip'
import type { Citation } from '@/lib/types'
import { citationLabel, cn } from '@/lib/utils'
import { useUi } from '@/store/ui'

export function CitationChip({ citation, className }: { citation: Citation; className?: string }) {
  const openFile = useUi((s) => s.openFile)
  const scope = useCitationScope()
  const label = citationLabel(citation)
  return (
    <Hint label={citation.evidence_id ? `Evidence ${citation.evidence_id} · open in viewer` : 'Open in viewer'}>
      <button
        type="button"
        onClick={() =>
          openFile({ path: citation.path, start: citation.start_line, end: citation.end_line, taskId: scope.taskId })
        }
        className={cn(
          'inline-flex max-w-full items-center gap-1 rounded border border-primary/25 bg-primary/8 px-1.5 py-px font-mono text-[11px] text-primary transition-colors hover:border-primary/50 hover:bg-primary/15',
          className,
        )}
      >
        <FileCode2Icon className="size-3 shrink-0" />
        <span className="truncate">{label}</span>
      </button>
    </Hint>
  )
}

export function CitationList({ citations, className }: { citations: Citation[]; className?: string }) {
  if (!citations?.length) return null
  return (
    <div className={cn('flex flex-wrap gap-1', className)}>
      {citations.map((c, i) => (
        <CitationChip key={`${c.path}:${c.start_line}-${c.end_line}-${i}`} citation={c} />
      ))}
    </div>
  )
}
