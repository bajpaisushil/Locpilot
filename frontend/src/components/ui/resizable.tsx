import * as React from 'react'
import { Group, Panel, Separator } from 'react-resizable-panels'
import { cn } from '@/lib/utils'

export function ResizableGroup({ className, ...props }: React.ComponentProps<typeof Group>) {
  return <Group className={cn('flex h-full w-full', className)} {...props} />
}

export const ResizablePanel = Panel

export function ResizableHandle({
  className,
  orientation = 'horizontal',
  ...props
}: React.ComponentProps<typeof Separator> & { orientation?: 'horizontal' | 'vertical' }) {
  return (
    <Separator
      className={cn(
        'relative shrink-0 bg-border transition-colors hover:bg-primary/60 focus-visible:bg-primary focus-visible:outline-none',
        orientation === 'horizontal'
          ? 'w-px after:absolute after:inset-y-0 after:-left-1 after:w-2'
          : 'h-px after:absolute after:inset-x-0 after:-top-1 after:h-2',
        className,
      )}
      {...props}
    />
  )
}
