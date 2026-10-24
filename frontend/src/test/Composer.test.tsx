import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it } from 'vitest'
import { Composer } from '@/components/chat/Composer'
import { useUi } from '@/store/ui'
import { json, mockFetch, renderWithProviders } from './utils'
import { modelStatus, taskDetail, userCommand } from './fixtures'

describe('Composer', () => {
  beforeEach(() => useUi.setState({ composerMode: 'ask', composerDraft: '', selectedTaskId: null, setupOpen: false }))

  it('is disabled and points to setup when the local model is unavailable', async () => {
    const user = userEvent.setup()
    mockFetch({ 'GET /api/model/status': modelStatus({ ollama_reachable: false, model_available: false }) })
    renderWithProviders(<Composer repoId="r_1" task={null} />)
    expect(await screen.findByText(/ollama is not running/i)).toBeInTheDocument()
    expect(screen.getByRole('textbox', { name: /message locpilot/i })).toBeDisabled()
    await user.click(screen.getByRole('button', { name: /open setup/i }))
    expect(useUi.getState().setupOpen).toBe(true)
  })

  it('creates a task in the selected mode and selects it', async () => {
    const user = userEvent.setup()
    const { calls } = mockFetch({
      'GET /api/model/status': modelStatus(),
      'POST /api/tasks': () => json(taskDetail({ id: 't_9', mode: 'code', status: 'running' }), 201),
      'GET /api/tasks': [],
    })
    renderWithProviders(<Composer repoId="r_1" task={null} />)
    const box = await screen.findByRole('textbox', { name: /message locpilot/i })
    await waitFor(() => expect(box).toBeEnabled())
    await user.click(screen.getByRole('radio', { name: /code/i }))
    await user.type(box, 'Fix the duplicate offers{Enter}')
    await waitFor(() => expect(useUi.getState().selectedTaskId).toBe('t_9'))
    expect(calls.find((c) => c.method === 'POST')?.body).toEqual({ repo_id: 'r_1', mode: 'code', message: 'Fix the duplicate offers' })
    expect(useUi.getState().composerDraft).toBe('')
  })

  it('says which of the user\'s runs LocPilot will see, and waits for a running one', async () => {
    mockFetch({ 'GET /api/model/status': modelStatus() })
    const done = userCommand({ id: 'c_1', argv: ['python main.py'], exit_code: 1 })
    const first = renderWithProviders(<Composer repoId="r_1" task={taskDetail({ mode: 'code', commands: [done] })} />)
    expect(await screen.findByTestId('shared-runs')).toHaveTextContent('python main.py')
    first.unmount()
    const running = userCommand({ id: 'c_2', argv: ['npm start'], status: 'running', exit_code: null })
    renderWithProviders(<Composer repoId="r_1" task={taskDetail({ mode: 'code', commands: [done, running] })} />)
    const box = screen.getByRole('textbox', { name: /message locpilot/i })
    await waitFor(() => expect(box).toHaveAttribute('placeholder', expect.stringMatching(/stop it in the commands tab/i)))
    expect(box).toBeDisabled()
    expect(screen.queryByTestId('shared-runs')).not.toBeInTheDocument()
  })

  it('offers Cancel while a task is running', async () => {
    mockFetch({ 'GET /api/model/status': modelStatus() })
    renderWithProviders(<Composer repoId="r_1" task={taskDetail({ status: 'running' })} />)
    expect(await screen.findByRole('button', { name: /cancel task/i })).toBeInTheDocument()
    expect(screen.getByRole('textbox', { name: /message locpilot/i })).toBeDisabled()
  })
})
