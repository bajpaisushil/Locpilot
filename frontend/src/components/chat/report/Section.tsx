import { ChevronDownIcon } from 'lucide-react'
import { useState, type ReactNode } from 'react'
import { cn } from '@/lib/utils'

export function Section({
  index,
  title,
  children,
  className,
  tone = 'default',
}: {
  index?: number
  title: string
  children: ReactNode
  className?: string
  tone?: 'default' | 'highlight' | 'warning'
}) {
  return (
    <section
      className={cn(
        'space-y-1.5',
        tone === 'highlight' && 'rounded-lg border border-primary/30 bg-primary/6 p-3',
        tone === 'warning' && 'rounded-lg border border-warning/30 bg-warning/8 p-3',
        className,
      )}
    >
      <h4 className="flex items-center gap-2 text-[11px] font-semibold tracking-wide text-muted-foreground uppercase">
        {index !== undefined && (
          <span className="inline-flex size-4 items-center justify-center rounded bg-muted font-mono text-[10px] text-foreground/80">
            {index}
          </span>
        )}
        {title}
      </h4>
      <div className="text-sm">{children}</div>
    </section>
  )
}

export function BulletList({ items, empty = 'None noted.' }: { items: string[]; empty?: string }) {
  if (!items?.length) return <p className="text-xs text-muted-foreground italic">{empty}</p>
  return (
    <ul className="list-disc space-y-1 pl-5 marker:text-muted-foreground/60">
      {items.map((it, i) => (
        <li key={i} className="leading-relaxed">
          {it}
        </li>
      ))}
    </ul>
  )
}

export function Disclosure({
  title,
  count,
  defaultOpen = false,
  children,
}: {
  title: string
  count?: number
  defaultOpen?: boolean
  children: ReactNode
}) {
  const [open, setOpen] = useState(defaultOpen)
  return (
    <div className="rounded-md border">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        className="flex w-full items-center gap-2 px-3 py-2 text-left text-xs font-medium hover:bg-muted/50"
      >
        <ChevronDownIcon className={cn('size-3.5 transition-transform', !open && '-rotate-90')} />
        {title}
        {count !== undefined && <span className="text-muted-foreground">({count})</span>}
      </button>
      {open && <div className="border-t px-3 py-3">{children}</div>}
    </div>
  )
}
