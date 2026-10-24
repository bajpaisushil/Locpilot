import { AlertCircleIcon, Loader2Icon, RefreshCwIcon } from 'lucide-react'
import type * as React from 'react'
import { Button } from '@/components/ui/button'
import { ApiError, errorMessage } from '@/lib/api'
import { cn } from '@/lib/utils'

export function Spinner({ className }: { className?: string }) {
  return <Loader2Icon className={cn('size-4 animate-spin text-muted-foreground', className)} aria-hidden />
}

export function LoadingState({ label = 'Loading…', className }: { label?: string; className?: string }) {
  return (
    <div role="status" className={cn('flex items-center justify-center gap-2 p-6 text-sm text-muted-foreground', className)}>
      <Spinner />
      {label}
    </div>
  )
}

export function ErrorState({
  error,
  title = 'Something went wrong',
  onRetry,
  className,
  compact = false,
}: {
  error: unknown
  title?: string
  onRetry?: () => void
  className?: string
  compact?: boolean
}) {
  const hint = error instanceof ApiError ? error.hint : null
  return (
    <div
      role="alert"
      className={cn(
        'flex gap-3 rounded-lg border border-destructive/30 bg-destructive/8 text-sm',
        compact ? 'items-center p-2.5' : 'items-start p-4',
        className,
      )}
    >
      <AlertCircleIcon className="mt-0.5 size-4 shrink-0 text-destructive" />
      <div className="min-w-0 flex-1 space-y-1">
        {!compact && <div className="font-medium">{title}</div>}
        <div className="break-words text-muted-foreground">{errorMessage(error)}</div>
        {hint && <div className="text-xs text-muted-foreground">{hint}</div>}
      </div>
      {onRetry && (
        <Button variant="outline" size="xs" onClick={onRetry}>
          <RefreshCwIcon />
          Retry
        </Button>
      )}
    </div>
  )
}

export function EmptyState({
  icon,
  title,
  description,
  children,
  className,
}: {
  icon?: React.ReactNode
  title: string
  description?: React.ReactNode
  children?: React.ReactNode
  className?: string
}) {
  return (
    <div className={cn('flex flex-col items-center justify-center gap-3 p-8 text-center', className)}>
      {icon && <div className="rounded-xl border bg-muted/50 p-3 text-muted-foreground [&_svg]:size-5">{icon}</div>}
      <div className="space-y-1">
        <div className="text-sm font-medium">{title}</div>
        {description && <div className="max-w-sm text-xs text-muted-foreground">{description}</div>}
      </div>
      {children}
    </div>
  )
}
