import Editor from '@monaco-editor/react'
import type * as Monaco from 'monaco-editor'
import { useCallback, useEffect, useRef } from 'react'
import { LoadingState } from '@/components/common/States'
import { EDITOR_FONT, monacoTheme } from '@/lib/monaco'
import { monacoLanguage } from '@/lib/utils'
import { useUi } from '@/store/ui'

export interface LineRange {
  start: number
  end?: number
}

export function CodeViewer({
  modelPath,
  path,
  content,
  language,
  highlight,
  revealNonce,
  editable = false,
  onChange,
}: {
  /** Unique model id (e.g. includes repo/worktree) so different versions of a file don't collide. */
  modelPath: string
  path: string
  content: string
  language?: string | null
  highlight?: LineRange | null
  revealNonce?: number
  /** Writable and uncontrolled: edits live in the editor until the caller saves them. */
  editable?: boolean
  onChange?: (value: string) => void
}) {
  const theme = useUi((s) => s.theme)
  const editorRef = useRef<Monaco.editor.IStandaloneCodeEditor | null>(null)
  const monacoRef = useRef<typeof Monaco | null>(null)
  const decorations = useRef<Monaco.editor.IEditorDecorationsCollection | null>(null)

  const hlStart = highlight?.start
  const hlEnd = highlight?.end
  const applyHighlight = useCallback(() => {
    const editor = editorRef.current
    const monaco = monacoRef.current
    if (!editor || !monaco) return
    decorations.current?.clear()
    if (!hlStart) return
    const model = editor.getModel()
    const lineCount = model?.getLineCount() ?? 1
    const start = Math.min(Math.max(1, hlStart), lineCount)
    const end = Math.min(Math.max(start, hlEnd ?? start), lineCount)
    decorations.current = editor.createDecorationsCollection([
      {
        range: new monaco.Range(start, 1, end, 1),
        options: { isWholeLine: true, className: 'rp-cite-line', linesDecorationsClassName: 'rp-cite-gutter' },
      },
    ])
    editor.revealLinesInCenterIfOutsideViewport(start, end)
    editor.setSelection(new monaco.Range(start, 1, start, 1))
  }, [hlStart, hlEnd])

  useEffect(() => {
    applyHighlight()
  }, [applyHighlight, revealNonce, content])

  return (
    <Editor
      path={modelPath}
      {...(editable ? { defaultValue: content } : { value: content })}
      onChange={editable ? (value) => onChange?.(value ?? '') : undefined}
      language={monacoLanguage(path, language)}
      theme={monacoTheme(theme)}
      loading={<LoadingState label="Loading editor…" />}
      onMount={(editor, monaco) => {
        editorRef.current = editor
        monacoRef.current = monaco
        applyHighlight()
      }}
      options={{
        readOnly: !editable,
        domReadOnly: !editable,
        minimap: { enabled: false },
        fontFamily: EDITOR_FONT,
        fontSize: 12.5,
        lineHeight: 19,
        scrollBeyondLastLine: false,
        renderLineHighlight: 'line',
        automaticLayout: true,
        lineNumbersMinChars: 4,
        glyphMargin: false,
        folding: true,
        stickyScroll: { enabled: true },
        padding: { top: 8, bottom: 8 },
        scrollbar: { verticalScrollbarSize: 10, horizontalScrollbarSize: 10 },
        contextmenu: true,
        links: false,
      }}
    />
  )
}
