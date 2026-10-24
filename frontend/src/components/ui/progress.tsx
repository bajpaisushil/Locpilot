import { Progress as ProgressPrimitive } from 'radix-ui'
import * as React from 'react'
import { cn } from '@/lib/utils'

export function Progress({
  className,
  value,
  indeterminate = false,
  ...props
}: React.ComponentProps<typeof ProgressPrimitive.Root> & { indeterminate?: boolean }) {
  return (
    <ProgressPrimitive.Root
      data-slot="progress"
      className={cn('relative h-1.5 w-full overflow-hidden rounded-full bg-muted', className)}
      value={indeterminate ? null : value}
      {...props}
    >
      <ProgressPrimitive.Indicator
        data-slot="progress-indicator"
        className={cn(
          'h-full bg-primary transition-transform duration-300',
          indeterminate ? 'w-1/3 animate-[rp-indeterminate_1.2s_ease-in-out_infinite]' : 'w-full',
        )}
        style={indeterminate ? undefined : { transform: `translateX(-${100 - (value ?? 0)}%)` }}
      />
    </ProgressPrimitive.Root>
  )
}
