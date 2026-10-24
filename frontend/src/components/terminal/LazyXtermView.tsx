import { lazy, Suspense, type ComponentProps } from 'react'
import { LoadingState } from '@/components/common/States'

// xterm.js is loaded only when a terminal is shown.
const XtermViewImpl = lazy(() => import('./XtermView').then((m) => ({ default: m.XtermView })))

export function XtermView(props: ComponentProps<typeof XtermViewImpl>) {
  return (
    <Suspense fallback={<LoadingState label="Starting terminal…" />}>
      <XtermViewImpl {...props} />
    </Suspense>
  )
}
