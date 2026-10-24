// Minimal unified-diff parser for rendering patches (git / difflib style).

export type DiffLineType = 'context' | 'add' | 'del' | 'meta'

export interface DiffLine {
  type: DiffLineType
  text: string
  oldNo: number | null
  newNo: number | null
}

export interface DiffHunk {
  header: string
  lines: DiffLine[]
}

export interface DiffFileView {
  oldPath: string | null
  newPath: string | null
  path: string
  change: 'modify' | 'create' | 'delete' | 'rename'
  hunks: DiffHunk[]
  additions: number
  deletions: number
  binary: boolean
}

const HUNK_RE = /^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)$/

function stripPrefix(p: string): string | null {
  const s = p.trim().split('\t')[0]
  if (s === '/dev/null') return null
  return s.replace(/^[ab]\//, '')
}

export function parseUnifiedDiff(text: string): DiffFileView[] {
  const files: DiffFileView[] = []
  if (!text) return files
  const lines = text.replace(/\r\n/g, '\n').split('\n')
  let file: DiffFileView | null = null
  let hunk: DiffHunk | null = null
  let oldNo = 0
  let newNo = 0

  const startFile = (): DiffFileView => {
    const f: DiffFileView = {
      oldPath: null,
      newPath: null,
      path: '',
      change: 'modify',
      hunks: [],
      additions: 0,
      deletions: 0,
      binary: false,
    }
    files.push(f)
    hunk = null
    return f
  }

  for (let i = 0; i < lines.length; i++) {
    const line = lines[i]
    if (line.startsWith('diff --git ')) {
      file = startFile()
      const m = /^diff --git a\/(.+?) b\/(.+)$/.exec(line)
      if (m) {
        file.oldPath = m[1]
        file.newPath = m[2]
        file.path = m[2]
      }
      continue
    }
    if (line.startsWith('--- ') && lines[i + 1]?.startsWith('+++ ')) {
      if (!file || file.hunks.length > 0) file = startFile()
      file.oldPath = stripPrefix(line.slice(4))
      file.newPath = stripPrefix(lines[i + 1].slice(4))
      file.path = file.newPath ?? file.oldPath ?? ''
      if (file.oldPath === null) file.change = 'create'
      else if (file.newPath === null) file.change = 'delete'
      i++
      continue
    }
    if (!file) continue
    if (line.startsWith('new file mode')) {
      file.change = 'create'
      continue
    }
    if (line.startsWith('deleted file mode')) {
      file.change = 'delete'
      continue
    }
    if (line.startsWith('rename from ') || line.startsWith('rename to ')) {
      file.change = 'rename'
      continue
    }
    if (line.startsWith('Binary files ') || line === 'GIT binary patch') {
      file.binary = true
      continue
    }
    const hm = HUNK_RE.exec(line)
    if (hm) {
      oldNo = Number(hm[1])
      newNo = Number(hm[3])
      hunk = { header: line, lines: [] }
      file.hunks.push(hunk)
      continue
    }
    if (!hunk) continue
    const h: DiffHunk = hunk
    if (line.startsWith('+')) {
      h.lines.push({ type: 'add', text: line.slice(1), oldNo: null, newNo: newNo++ })
      file.additions++
    } else if (line.startsWith('-')) {
      h.lines.push({ type: 'del', text: line.slice(1), oldNo: oldNo++, newNo: null })
      file.deletions++
    } else if (line.startsWith(' ')) {
      h.lines.push({ type: 'context', text: line.slice(1), oldNo: oldNo++, newNo: newNo++ })
    } else if (line.startsWith('\\')) {
      h.lines.push({ type: 'meta', text: line, oldNo: null, newNo: null })
    } else if (line === '' && i === lines.length - 1) {
      // trailing newline at end of diff text
    } else if (line === '') {
      // Some generators drop the leading space on empty context lines.
      h.lines.push({ type: 'context', text: '', oldNo: oldNo++, newNo: newNo++ })
    }
  }
  return files.filter((f) => f.path || f.hunks.length > 0)
}
