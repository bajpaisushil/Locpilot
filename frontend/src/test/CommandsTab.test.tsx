import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { toast } from 'sonner'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { CommandsTab } from '@/components/panels/CommandsTab'
import { useUi } from '@/store/ui'
import { json, mockFetch, renderWithProviders } from './utils'
import { taskDetail, userCommand } from './fixtures'

const codeTask = (overrides = {}) =>
  taskDetail({ mode: 'code', worktree_path: '/data/worktrees/t_1', status: 'completed', ...overrides })

describe('CommandsTab run bar', () => {
  beforeEach(() =>
    useUi.setState({ selectedTaskId: 't_1', runAllowNetwork: false, composerDraft: '', composerMode: 'ask' }),
  )

  it('runs a typed command in the worktree and opens its live output', async () => {
    const user = userEvent.setup()
    const commands: ReturnType<typeof userCommand>[] = []
    const { calls } = mockFetch({
      'GET /api/tasks/t_1': () => json(codeTask({ commands })),
      'POST /api/tasks/t_1/commands': () => {
        commands.push(userCommand({ id: 'c_9', status: 'running', exit_code: null, summary: null, finished_at: null }))
        return json(commands[0], 202)
      },
    })
    renderWithProviders(<CommandsTab />)
    const box = await screen.findByRole('textbox', { name: /command to run/i })
    await user.click(screen.getByRole('switch', { name: /allow network access/i }))
    await user.type(box, 'python main.py{Enter}')
    await waitFor(() =>
      expect(calls.find((c) => c.method === 'POST')?.body).toEqual({ command: 'python main.py', allow_network: true }),
    )
    expect(await screen.findByText(/live output/i)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /stop command/i })).toBeInTheDocument()
    expect(box).toHaveValue('')
    expect(useUi.getState().runAllowNetwork).toBe(true)
  })

  it('is unavailable until the task has a worktree', async () => {
    mockFetch({ 'GET /api/tasks/t_1': taskDetail() })
    renderWithProviders(<CommandsTab />)
    expect(await screen.findByRole('textbox', { name: /command to run/i })).toBeDisabled()
    expect(screen.getByText(/creates in code mode/i)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /^run$/i })).toBeDisabled()
  })

  it('stops the running command', async () => {
    const user = userEvent.setup()
    const running = userCommand({ id: 'c_5', status: 'running', exit_code: null, finished_at: null })
    const { calls } = mockFetch({
      'GET /api/tasks/t_1': codeTask({ commands: [running] }),
      'POST /api/tasks/t_1/commands/c_5/stop': userCommand({ id: 'c_5', status: 'cancelled', exit_code: null }),
    })
    renderWithProviders(<CommandsTab />)
    await user.click(await screen.findByRole('button', { name: /stop command/i }))
    await waitFor(() => expect(calls.some((c) => c.method === 'POST' && c.path.endsWith('/c_5/stop'))).toBe(true))
  })

  it('says so instead of silently ignoring Enter while a command runs', async () => {
    const user = userEvent.setup()
    const info = vi.spyOn(toast, 'info')
    const running = userCommand({ id: 'c_5', status: 'running', exit_code: null, finished_at: null })
    const { calls } = mockFetch({ 'GET /api/tasks/t_1': codeTask({ commands: [running] }) })
    renderWithProviders(<CommandsTab />)
    await user.type(await screen.findByRole('textbox', { name: /command to run/i }), 'npm test{Enter}')
    expect(info).toHaveBeenCalledWith('A command is still running', expect.anything())
    expect(calls.some((c) => c.method === 'POST')).toBe(false)
  })

  it('recalls earlier commands with the arrow keys', async () => {
    const user = userEvent.setup()
    mockFetch({
      'GET /api/tasks/t_1': codeTask({
        commands: [userCommand({ id: 'c_1', argv: ['npm run build'] }), userCommand({ id: 'c_2', argv: ['node index.js'] })],
      }),
    })
    renderWithProviders(<CommandsTab />)
    const box = await screen.findByRole('textbox', { name: /command to run/i })
    await user.click(box)
    await user.keyboard('{ArrowUp}')
    expect(box).toHaveValue('node index.js')
    await user.keyboard('{ArrowUp}')
    expect(box).toHaveValue('npm run build')
    await user.keyboard('{ArrowDown}{ArrowDown}')
    expect(box).toHaveValue('')
  })

  it('turns a failed run into a fix request for LocPilot', async () => {
    const user = userEvent.setup()
    const failed = userCommand({ id: 'c_7', exit_code: 1, summary: null })
    mockFetch({
      'GET /api/tasks/t_1': codeTask({ commands: [failed] }),
      'GET /api/tasks/t_1/commands/c_7': { ...failed, stdout: '', stderr: 'Traceback: KeyError price' },
    })
    renderWithProviders(<CommandsTab />)
    expect(await screen.findByText(/you ran/i)).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: /python main\.py/i }))
    expect(await screen.findByText(/KeyError price/)).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: /ask locpilot to fix/i }))
    expect(useUi.getState().composerDraft).toMatch(/^I ran `python main\.py` and it failed \(exit 1\)/)
    expect(useUi.getState().composerMode).toBe('code')
  })
})
