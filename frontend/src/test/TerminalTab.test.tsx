import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { TerminalTab } from '@/components/panels/TerminalTab'
import type { TerminalOut } from '@/lib/types'
import { useUi } from '@/store/ui'
import { json, mockFetch, renderWithProviders } from './utils'
import { taskDetail } from './fixtures'

vi.mock('@/components/terminal/LazyXtermView', () => ({
  XtermView: ({ terminal }: { terminal: TerminalOut }) => <div data-testid="xterm">{`${terminal.location}:${terminal.cwd}`}</div>,
}))

const term = (overrides: Partial<TerminalOut> = {}): TerminalOut => ({
  id: 'term_1', title: 'hotel · Fix duplicates', location: 'worktree', cwd: '/data/worktrees/t_1', shell: '/bin/zsh',
  repo_id: 'r_1', task_id: 't_1', running: true, exit_code: null, created_at: '2026-10-24T10:00:00Z', ...overrides,
})

describe('TerminalTab', () => {
  beforeEach(() => useUi.setState({ selectedRepoId: 'r_1', selectedTaskId: 't_1', activeTerminalId: null }))

  it('opens a shell in the task worktree, and closes it', async () => {
    const user = userEvent.setup()
    let open: TerminalOut[] = []
    const { calls } = mockFetch({
      'GET /api/tasks/t_1': taskDetail({ mode: 'code', worktree_path: '/data/worktrees/t_1' }),
      'GET /api/terminals': () => json(open),
      'POST /api/terminals': () => {
        open = [term()]
        return json(open[0], 201)
      },
      'DELETE /api/terminals/term_1': () => {
        open = []
        return json({ ok: true })
      },
    })
    renderWithProviders(<TerminalTab />)
    await user.click(await screen.findByRole('button', { name: /in locpilot’s worktree/i }))
    expect(await screen.findByTestId('xterm')).toHaveTextContent('worktree:/data/worktrees/t_1')
    expect(calls.find((c) => c.method === 'POST')?.body).toEqual({ repo_id: 'r_1', task_id: 't_1', cols: 100, rows: 30 })
    await user.click(screen.getByRole('button', { name: /close terminal/i }))
    await waitFor(() => expect(screen.queryByTestId('xterm')).not.toBeInTheDocument())
    expect(calls.some((c) => c.method === 'DELETE')).toBe(true)
  })

  it('comes back to the terminal you were using after switching panels', async () => {
    const user = userEvent.setup()
    mockFetch({
      'GET /api/tasks/t_1': taskDetail({ mode: 'code', worktree_path: '/data/worktrees/t_1' }),
      'GET /api/terminals': [term(), term({ id: 'term_2', title: 'hotel', location: 'repository', cwd: '/home/me/hotel', task_id: null })],
    })
    const first = renderWithProviders(<TerminalTab />)
    expect(await screen.findByTestId('xterm')).toHaveTextContent('worktree:/data/worktrees/t_1')
    await user.click(screen.getByRole('tab', { name: /^hotel$/ }))
    expect(screen.getByTestId('xterm')).toHaveTextContent('repository:/home/me/hotel')
    first.unmount()
    renderWithProviders(<TerminalTab />)
    expect(await screen.findByTestId('xterm')).toHaveTextContent('repository:/home/me/hotel')
  })

  it('offers only the repository folder when the task has no worktree', async () => {
    const user = userEvent.setup()
    const { calls } = mockFetch({
      'GET /api/tasks/t_1': taskDetail(),
      'GET /api/terminals': [],
      'POST /api/terminals': () => json(term({ location: 'repository', cwd: '/home/me/hotel', task_id: null }), 201),
    })
    renderWithProviders(<TerminalTab />)
    expect(await screen.findByRole('button', { name: /in your repository folder/i })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /in locpilot’s worktree/i })).not.toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: /in your repository folder/i }))
    expect(await screen.findByTestId('xterm')).toHaveTextContent('repository:/home/me/hotel')
    expect(calls.find((c) => c.method === 'POST')?.body).toMatchObject({ task_id: null })
  })
})
