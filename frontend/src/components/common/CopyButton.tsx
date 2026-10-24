import { CheckIcon, CopyIcon } from 'lucide-react'
import { useEffect, useState } from 'react'
import { Button } from '@/components/ui/button'
import { Hint } from '@/components/ui/tooltip'
import { copyText } from '@/lib/desktop'
import { cn } from '@/lib/utils'

export function CopyButton({ text, label = 'Copy', className }: { text: string; label?: string; className?: string }) {
  const [copied, setCopied] = useState(false)
  useEffect(() => {
    if (!copied) return
    const t = setTimeout(() => setCopied(false), 1500)
    return () => clearTimeout(t)
  }, [copied])
  return (
    <Hint label={copied ? 'Copied' : label}>
      <Button
        variant="ghost"
        size="icon-xs"
        aria-label={label}
        className={cn('text-muted-foreground', className)}
        onClick={async () => setCopied(await copyText(text))}
      >
        {copied ? <CheckIcon className="text-success" /> : <CopyIcon />}
      </Button>
    </Hint>
  )
}

/** A copyable shell command line. */
export function CommandLine({ command, className }: { command: string; className?: string }) {
  return (
    <div className={cn('group flex items-center gap-2 rounded-md border bg-code px-3 py-2 font-mono text-xs', className)}>
      <span className="text-muted-foreground select-none">$</span>
      <code className="min-w-0 flex-1 overflow-x-auto whitespace-pre">{command}</code>
      <CopyButton text={command} label="Copy command" />
    </div>
  )
}
