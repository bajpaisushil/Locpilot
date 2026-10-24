import { lazy, Suspense, type ComponentProps } from 'react'
import { LoadingState } from '@/components/common/States'

// Monaco is large; load it only when a file or diff is actually shown.
const CodeViewerImpl = lazy(() => import('./CodeViewer').then((m) => ({ default: m.CodeViewer })))
const DiffViewerImpl = lazy(() => import('./DiffViewer').then((m) => ({ default: m.DiffViewer })))

export function CodeViewer(props: ComponentProps<typeof CodeViewerImpl>) {
  return (
    <Suspense fallback={<LoadingState label="Loading editor…" />}>
      <CodeViewerImpl {...props} />
    </Suspense>
  )
}

export function DiffViewer(props: ComponentProps<typeof DiffViewerImpl>) {
  return (
    <Suspense fallback={<LoadingState label="Loading diff viewer…" />}>
      <DiffViewerImpl {...props} />
    </Suspense>
  )
}
