// Monaco is bundled locally (no CDN) so the app works fully offline.
import { loader } from '@monaco-editor/react'
import * as monaco from 'monaco-editor'
import EditorWorker from 'monaco-editor/editor/editor.worker?worker'
import CssWorker from 'monaco-editor/language/css/css.worker?worker'
import HtmlWorker from 'monaco-editor/language/html/html.worker?worker'
import JsonWorker from 'monaco-editor/language/json/json.worker?worker'
import TsWorker from 'monaco-editor/language/typescript/ts.worker?worker'

declare global {
  interface Window {
    MonacoEnvironment?: { getWorker: (workerId: string, label: string) => Worker }
  }
}

self.MonacoEnvironment = {
  getWorker(_workerId: string, label: string) {
    switch (label) {
      case 'json':
        return new JsonWorker()
      case 'css':
      case 'scss':
      case 'less':
        return new CssWorker()
      case 'html':
      case 'handlebars':
      case 'razor':
        return new HtmlWorker()
      case 'typescript':
      case 'javascript':
        return new TsWorker()
      default:
        return new EditorWorker()
    }
  },
}

// Files are shown read-only without project context, so language diagnostics would only be noise.
const ts = (monaco as unknown as { typescript?: Record<string, { setDiagnosticsOptions?: (o: object) => void }> })
  .typescript
for (const defaults of [ts?.typescriptDefaults, ts?.javascriptDefaults]) {
  defaults?.setDiagnosticsOptions?.({ noSemanticValidation: true, noSyntaxValidation: true, noSuggestionDiagnostics: true })
}

monaco.editor.defineTheme('locpilot-dark', {
  base: 'vs-dark',
  inherit: true,
  rules: [],
  colors: {
    'editor.background': '#121216',
    'editorGutter.background': '#121216',
    'editor.lineHighlightBackground': '#ffffff08',
    'editorLineNumber.foreground': '#5b5b6b',
    'editorLineNumber.activeForeground': '#a1a1b5',
    'diffEditor.insertedTextBackground': '#2ea04326',
    'diffEditor.removedTextBackground': '#f8514926',
    'diffEditor.insertedLineBackground': '#2ea0431a',
    'diffEditor.removedLineBackground': '#f851491a',
    'scrollbarSlider.background': '#ffffff1a',
    'scrollbarSlider.hoverBackground': '#ffffff2a',
  },
})

monaco.editor.defineTheme('locpilot-light', {
  base: 'vs',
  inherit: true,
  rules: [],
  colors: {
    'editor.background': '#fbfbfc',
    'editorGutter.background': '#fbfbfc',
    'editor.lineHighlightBackground': '#0000000a',
    'editorLineNumber.foreground': '#a0a0ae',
    'diffEditor.insertedLineBackground': '#2ea04314',
    'diffEditor.removedLineBackground': '#cf222e12',
  },
})

loader.config({ monaco })

export { monaco }

export const EDITOR_FONT =
  'ui-monospace, "SF Mono", SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", monospace'

export function monacoTheme(theme: 'dark' | 'light') {
  return theme === 'dark' ? 'locpilot-dark' : 'locpilot-light'
}
