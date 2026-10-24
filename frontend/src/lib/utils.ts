import { clsx, type ClassValue } from 'clsx'
import { twMerge } from 'tailwind-merge'
import type { Citation, TaskStatus, TestSummary } from './types'

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs))
}

export function formatRelativeTime(iso: string | null | undefined, now: number = Date.now()): string {
  if (!iso) return ''
  const t = Date.parse(iso)
  if (Number.isNaN(t)) return ''
  const s = Math.round((now - t) / 1000)
  if (s < 10) return 'just now'
  if (s < 60) return `${s}s ago`
  const m = Math.round(s / 60)
  if (m < 60) return `${m}m ago`
  const h = Math.round(m / 60)
  if (h < 24) return `${h}h ago`
  const d = Math.round(h / 24)
  if (d < 30) return `${d}d ago`
  return new Date(t).toLocaleDateString()
}

export function formatDateTime(iso: string | null | undefined): string {
  if (!iso) return '—'
  const t = Date.parse(iso)
  return Number.isNaN(t) ? iso : new Date(t).toLocaleString()
}

export function formatDuration(ms: number | null | undefined): string {
  if (ms === null || ms === undefined || Number.isNaN(ms)) return '—'
  if (ms < 1000) return `${Math.round(ms)}ms`
  const s = ms / 1000
  if (s < 60) return `${s.toFixed(s < 10 ? 1 : 0)}s`
  const m = Math.floor(s / 60)
  const rest = Math.round(s - m * 60)
  return `${m}m ${String(rest).padStart(2, '0')}s`
}

export function formatBytes(n: number | null | undefined): string {
  if (n === null || n === undefined) return '—'
  const units = ['B', 'KB', 'MB', 'GB', 'TB']
  let v = n
  let i = 0
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024
    i++
  }
  return `${v.toFixed(v >= 10 || i === 0 ? 0 : 1)} ${units[i]}`
}

export function formatNumber(n: number | null | undefined): string {
  if (n === null || n === undefined) return '—'
  return n.toLocaleString()
}

export function shortSha(sha: string | null | undefined): string {
  return sha ? sha.slice(0, 8) : '—'
}

export function basename(path: string): string {
  const parts = path.split(/[\\/]/).filter(Boolean)
  return parts[parts.length - 1] ?? path
}

export function dirname(path: string): string {
  const idx = Math.max(path.lastIndexOf('/'), path.lastIndexOf('\\'))
  return idx <= 0 ? '' : path.slice(0, idx)
}

export function citationLabel(c: Pick<Citation, 'path' | 'start_line' | 'end_line'>): string {
  if (!c.start_line) return c.path
  return c.end_line && c.end_line !== c.start_line
    ? `${c.path}:${c.start_line}-${c.end_line}`
    : `${c.path}:${c.start_line}`
}

export interface LocpilotLink {
  path: string
  start?: number
  end?: number
}

/** Parses `locpilot://open?path=..&start=..&end=..` links emitted in report markdown. */
export function parseLocpilotLink(href: string | undefined | null): LocpilotLink | null {
  if (!href || !href.startsWith('locpilot://')) return null
  const qIndex = href.indexOf('?')
  if (qIndex === -1) return null
  const params = new URLSearchParams(href.slice(qIndex + 1))
  const path = params.get('path')
  if (!path) return null
  const start = Number(params.get('start'))
  const end = Number(params.get('end'))
  return {
    path,
    start: Number.isFinite(start) && start > 0 ? start : undefined,
    end: Number.isFinite(end) && end > 0 ? end : undefined,
  }
}

const EXT_LANG: Record<string, string> = {
  py: 'python',
  pyi: 'python',
  ts: 'typescript',
  tsx: 'typescript',
  mts: 'typescript',
  cts: 'typescript',
  js: 'javascript',
  jsx: 'javascript',
  mjs: 'javascript',
  cjs: 'javascript',
  json: 'json',
  md: 'markdown',
  markdown: 'markdown',
  go: 'go',
  rs: 'rust',
  java: 'java',
  kt: 'kotlin',
  kts: 'kotlin',
  rb: 'ruby',
  php: 'php',
  cs: 'csharp',
  c: 'c',
  h: 'c',
  cc: 'cpp',
  cpp: 'cpp',
  cxx: 'cpp',
  hpp: 'cpp',
  swift: 'swift',
  scala: 'scala',
  sh: 'shell',
  bash: 'shell',
  zsh: 'shell',
  yml: 'yaml',
  yaml: 'yaml',
  toml: 'ini',
  ini: 'ini',
  cfg: 'ini',
  html: 'html',
  htm: 'html',
  vue: 'html',
  css: 'css',
  scss: 'scss',
  less: 'less',
  sql: 'sql',
  xml: 'xml',
  svg: 'xml',
  graphql: 'graphql',
  gql: 'graphql',
  dockerfile: 'dockerfile',
  lua: 'lua',
  r: 'r',
  dart: 'dart',
  ex: 'elixir',
  exs: 'elixir',
  ps1: 'powershell',
  bat: 'bat',
  proto: 'proto',
  tf: 'hcl',
  hcl: 'hcl',
}

/** Maps a file path (or the backend's language hint) to a Monaco language id. */
export function monacoLanguage(path: string, hint?: string | null): string {
  const name = basename(path).toLowerCase()
  if (name === 'dockerfile' || name.startsWith('dockerfile.')) return 'dockerfile'
  const ext = name.includes('.') ? name.slice(name.lastIndexOf('.') + 1) : ''
  if (ext && EXT_LANG[ext]) return EXT_LANG[ext]
  if (hint) {
    const h = hint.toLowerCase()
    if (Object.values(EXT_LANG).includes(h)) return h
  }
  return 'plaintext'
}

export function testSummaryText(s: TestSummary | null | undefined): string {
  if (!s) return 'no summary'
  if (s.outcome === 'no_tests') return 'no tests collected'
  const parts: string[] = []
  if (s.passed !== null) parts.push(`${s.passed} passed`)
  if (s.failed) parts.push(`${s.failed} failed`)
  if (s.errors) parts.push(`${s.errors} errors`)
  if (s.skipped) parts.push(`${s.skipped} skipped`)
  if (parts.length === 0) return s.outcome
  return parts.join(', ')
}

export function taskStatusDotClass(status: TaskStatus): string {
  switch (status) {
    case 'running':
    case 'queued':
      return 'bg-info rp-pulse'
    case 'awaiting_approval':
    case 'needs_input':
    case 'interrupted':
      return 'bg-warning'
    case 'completed':
      return 'bg-success'
    case 'failed':
      return 'bg-destructive'
    default:
      return 'bg-muted-foreground/50'
  }
}
