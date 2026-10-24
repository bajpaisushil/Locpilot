import { DiffEditor } from '@monaco-editor/react'
import { LoadingState } from '@/components/common/States'
import { EDITOR_FONT, monacoTheme } from '@/lib/monaco'
import { monacoLanguage } from '@/lib/utils'
import { useUi } from '@/store/ui'

export function DiffViewer({
  modelKey,
  path,
  original,
  modified,
  sideBySide,
}: {
  modelKey: string
  path: string
  original: string
  modified: string
  sideBySide: boolean
}) {
  const theme = useUi((s) => s.theme)
  return (
    <DiffEditor
      original={original}
      modified={modified}
      language={monacoLanguage(path)}
      originalModelPath={`diff-original/${modelKey}/${path}`}
      modifiedModelPath={`diff-modified/${modelKey}/${path}`}
      // Keeping models avoids a known dispose-order error when switching files quickly.
      keepCurrentOriginalModel
      keepCurrentModifiedModel
      theme={monacoTheme(theme)}
      loading={<LoadingState label="Loading diff…" />}
      options={{
        readOnly: true,
        originalEditable: false,
        renderSideBySide: sideBySide,
        useInlineViewWhenSpaceIsLimited: true,
        minimap: { enabled: false },
        fontFamily: EDITOR_FONT,
        fontSize: 12.5,
        lineHeight: 19,
        scrollBeyondLastLine: false,
        automaticLayout: true,
        renderOverviewRuler: false,
        hideUnchangedRegions: { enabled: true, contextLineCount: 4, minimumLineCount: 6 },
        scrollbar: { verticalScrollbarSize: 10, horizontalScrollbarSize: 10 },
      }}
    />
  )
}
