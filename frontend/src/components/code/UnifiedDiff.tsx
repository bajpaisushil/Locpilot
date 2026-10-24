import { ChevronDownIcon, ChevronRightIcon, FileDiffIcon, FilePlusIcon } from 'lucide-react'
import { useMemo, useState } from 'react'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { parseUnifiedDiff, type DiffFileView } from '@/lib/diff'
import { cn } from '@/lib/utils'

const MAX_LINES = 300

function FileBlock({ file, defaultOpen }: { file: DiffFileView; defaultOpen: boolean }) {
  const [open, setOpen] = useState(defaultOpen)
  const [showAll, setShowAll] = useState(false)
  const totalLines = file.hunks.reduce((n, h) => n + h.lines.length + 1, 0)
  let budget = showAll ? Number.POSITIVE_INFINITY : MAX_LINES
  return (
    <div className="overflow-hidden rounded-md border bg-code" data-testid="diff-file">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-center gap-2 border-b bg-muted/40 px-2.5 py-1.5 text-left text-xs hover:bg-muted/70"
      >
        {open ? <ChevronDownIcon className="size-3.5" /> : <ChevronRightIcon className="size-3.5" />}
        {file.change === 'create' ? (
          <FilePlusIcon className="size-3.5 text-success" />
        ) : (
          <FileDiffIcon className="size-3.5 text-muted-foreground" />
        )}
        <span className="min-w-0 flex-1 truncate font-mono">{file.path}</span>
        {file.change === 'create' && <Badge variant="success">new</Badge>}
        <span className="font-mono text-success">+{file.additions}</span>
        <span className="font-mono text-destructive">−{file.deletions}</span>
      </button>
      {open && (
        <div className="overflow-x-auto">
          {file.binary ? (
            <div className="px-3 py-2 text-xs text-muted-foreground">Binary file changed</div>
          ) : (
            <table className="w-full border-collapse font-mono text-[11.5px] leading-[18px]">
              <tbody>
                {file.hunks.map((hunk, hi) => {
                  if (budget <= 0) return null
                  const rows = []
                  rows.push(
                    <tr key={`h${hi}`} className="bg-diff-hunk text-muted-foreground">
                      <td colSpan={3} className="px-3 py-0.5 whitespace-pre">
                        {hunk.header}
                      </td>
                    </tr>,
                  )
                  budget -= 1
                  for (let li = 0; li < hunk.lines.length && budget > 0; li++, budget--) {
                    const line = hunk.lines[li]
                    rows.push(
                      <tr
                        key={`h${hi}l${li}`}
                        className={cn(
                          line.type === 'add' && 'bg-diff-add',
                          line.type === 'del' && 'bg-diff-del',
                          line.type === 'meta' && 'text-muted-foreground italic',
                        )}
                      >
                        <td className="w-10 border-r px-2 text-right text-muted-foreground/70 select-none">
                          {line.oldNo ?? ''}
                        </td>
                        <td className="w-10 border-r px-2 text-right text-muted-foreground/70 select-none">
                          {line.newNo ?? ''}
                        </td>
                        <td className="px-3 whitespace-pre">
                          <span
                            className={cn(
                              'mr-2 inline-block w-2 select-none',
                              line.type === 'add' && 'text-diff-add-strong',
                              line.type === 'del' && 'text-diff-del-strong',
                            )}
                          >
                            {line.type === 'add' ? '+' : line.type === 'del' ? '-' : ' '}
                          </span>
                          {line.text}
                        </td>
                      </tr>,
                    )
                  }
                  return rows
                })}
              </tbody>
            </table>
          )}
          {!showAll && totalLines > MAX_LINES && (
            <div className="border-t px-3 py-1.5">
              <Button variant="link" size="xs" onClick={() => setShowAll(true)}>
                Show all {totalLines} lines
              </Button>
            </div>
          )}
        </div>
      )}
    </div>
  )
}

/** Lightweight unified diff renderer (no Monaco) used inside timeline cards. */
export function UnifiedDiff({ diff, className, collapseAfter = 3 }: { diff: string; className?: string; collapseAfter?: number }) {
  const files = useMemo(() => parseUnifiedDiff(diff), [diff])
  if (files.length === 0) {
    return <div className={cn('rounded-md border px-3 py-2 text-xs text-muted-foreground', className)}>No changes.</div>
  }
  return (
    <div className={cn('space-y-2', className)}>
      {files.map((f, i) => (
        <FileBlock key={`${f.path}-${i}`} file={f} defaultOpen={i < collapseAfter} />
      ))}
    </div>
  )
}
