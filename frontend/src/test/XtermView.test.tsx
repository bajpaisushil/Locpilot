import { act, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { TerminalOut } from '@/lib/types'
import { useUi } from '@/store/ui'
import { mockFetch, renderWithProviders } from './utils'

const fake = vi.hoisted(() => ({
  writes: [] as string[],
  onData: null as ((data: string) => void) | null,
  selection: '',
  resets: 0,
}))

vi.mock('@xterm/xterm', () => ({
  Terminal: class {
    cols = 120
    rows = 33
    options: Record<string, unknown> = {}
    buffer = { active: { length: 0, getLine: () => undefined } }
    loadAddon() {}
    open() {}
    focus() {}
    dispose() {}
    reset() {
      fake.resets++
      fake.writes = []
    }
    write(data: string) {
      fake.writes.push(data)
    }
    onData(cb: (data: string) => void) {
      fake.onData = cb
      return { dispose: () => {} }
    }
    getSelection() {
      return fake.selection
    }
  },
}))
vi.mock('@xterm/addon-fit', () => ({ FitAddon: class { fit() {} } }))
vi.mock('@xterm/addon-web-links', () => ({ WebLinksAddon: class {} }))

const { XtermView } = await import('@/components/terminal/XtermView')

const terminal: TerminalOut = {
  id: 'term_1', title: 'hotel', location: 'worktree', cwd: '/data/worktrees/t_1', shell: '/bin/zsh', repo_id: 'r_1',
  task_id: 't_1', running: true, exit_code: null, created_at: '2026-10-24T10:00:00Z',
}

const sse = (frames: [string, object][]) =>
  new Response(frames.map(([event, data]) => `event: ${event}\ndata: ${JSON.stringify(data)}\n\n`).join(''), {
    headers: { 'Content-Type': 'text/event-stream' },
  })

describe('XtermView', () => {
  beforeEach(() => {
    fake.writes = []
    fake.onData = null
    fake.selection = ''
    fake.resets = 0
    useUi.setState({ composerDraft: '' })
  })

  it('draws the scrollback and live output, then reports the exit', async () => {
    mockFetch({
      'GET /api/terminals/term_1/stream': () =>
        sse([['reset', { data: '$ npm run dev\r\n' }], ['output', { type: 'output', data: 'Local: http://localhost:5173/\r\n' }],
             ['exit', { type: 'exit', code: 0 }]]),
      'POST /api/terminals/term_1/resize': () => new Response(null, { status: 204 }),
    })
    renderWithProviders(<XtermView terminal={terminal} />)
    await waitFor(() => expect(fake.writes.join('')).toContain('[process exited with code 0]'))
    expect(fake.resets).toBe(1)
    expect(fake.writes.join('')).toContain('$ npm run dev\r\nLocal: http://localhost:5173/')
    expect(await screen.findByText(/exited \(0\)/)).toBeInTheDocument()
  })

  it('sends keystrokes in order, batched, and shares output with LocPilot', async () => {
    const user = userEvent.setup()
    const { calls } = mockFetch({
      'GET /api/terminals/term_1/stream': () => sse([['reset', { data: '' }], ['exit', { type: 'exit', code: 0 }]]),
      'POST /api/terminals/term_1/input': () => new Response(null, { status: 204 }),
      'POST /api/terminals/term_1/resize': () => new Response(null, { status: 204 }),
    })
    renderWithProviders(<XtermView terminal={terminal} />)
    await waitFor(() => expect(fake.onData).not.toBeNull())
    act(() => {
      fake.onData?.('npm ')
      fake.onData?.('test')
      fake.onData?.('\r')
    })
    await waitFor(() => expect(calls.filter((c) => c.path.endsWith('/input')).length).toBeGreaterThan(0))
    const typed = calls.filter((c) => c.path.endsWith('/input')).map((c) => (c.body as { data: string }).data).join('')
    expect(typed).toBe('npm test\r')
    fake.selection = 'TypeError: price is undefined'
    await user.click(screen.getByRole('button', { name: /send to locpilot/i }))
    expect(useUi.getState().composerDraft).toContain('```\nTypeError: price is undefined\n```')
    expect(useUi.getState().composerDraft).toMatch(/^Terminal output \(in LocPilot’s worktree\)/)
  })
})
