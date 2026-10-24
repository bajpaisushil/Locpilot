import { Label as LabelPrimitive } from 'radix-ui'
import * as React from 'react'
import { cn } from '@/lib/utils'

export function Label({ className, ...props }: React.ComponentProps<typeof LabelPrimitive.Root>) {
  return (
    <LabelPrimitive.Root
      data-slot="label"
      className={cn('flex items-center gap-2 text-xs font-medium leading-none select-none', className)}
      {...props}
    />
  )
}
