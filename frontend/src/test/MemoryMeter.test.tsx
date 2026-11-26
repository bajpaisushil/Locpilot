import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it } from 'vitest'
import { MemoryMeter } from '@/components/layout/MemoryMeter'
import { useUi } from '@/store/ui'
import { modelStatus, taskDetail } from './fixtures'
import { mockFetch, renderWithProviders } from './utils'

const GB = 1024 ** 3
const loaded = {
  total_bytes: 16 * GB, available_bytes: 4 * GB, loaded_bytes: 5.5 * GB, needed_bytes: 0,
  context_bytes: 0.5 * GB, kv_cache_type: 'f16', short: false,
}

describe('MemoryMeter', () => {
  beforeEach(() => useUi.setState({ selectedTaskId: null }))

  it('shows the RAM the model occupies next to what is free, and frees it on request', async () => {
    const { calls } = mockFetch({
      'GET /api/model/status': modelStatus({ memory: loaded }),
      'POST /api/model/unload': { unloaded: ['qwen2.5-coder:7b'] },
    })
    const user = userEvent.setup()
    renderWithProviders(<MemoryMeter />)
    await user.click(await screen.findByTestId('memory-meter'))
    expect(await screen.findByTestId('memory-model')).toHaveTextContent('5.5 GB')
    expect(screen.getByTestId('memory-free')).toHaveTextContent('4.0 GB')
    expect(screen.queryByTestId('context-section')).not.toBeInTheDocument() // no task open
    await user.click(screen.getByRole('button', { name: /free ram now/i }))
    await waitFor(() => expect(calls.some((c) => c.method === 'POST' && c.path === '/api/model/unload')).toBe(true))
  })

  it('says what loading the model would take when it is not loaded', async () => {
    mockFetch({
      'GET /api/model/status': modelStatus({ memory: { ...loaded, loaded_bytes: null, needed_bytes: 5.4 * GB } }),
    })
    const user = userEvent.setup()
    renderWithProviders(<MemoryMeter />)
    await user.click(await screen.findByTestId('memory-meter'))
    expect(await screen.findByTestId('memory-model')).toHaveTextContent('not loaded (needs ~5.4 GB)')
    expect(screen.getByRole('button', { name: /free ram now/i })).toBeDisabled()
  })

  it("shows how full the task's context window is and clears it", async () => {
    useUi.setState({ selectedTaskId: 't_1' })
    const stats = { context_tokens: 7900, context_window: 16384 }
    const { calls } = mockFetch({
      'GET /api/model/status': modelStatus({ memory: loaded }),
      'GET /api/tasks/t_1': taskDetail({ stats }),
      'POST /api/tasks/t_1/context/clear': taskDetail({ stats: { ...stats, context_tokens: 0, context_cleared_turn: 1 } }),
    })
    const user = userEvent.setup()
    renderWithProviders(<MemoryMeter />)
    await user.click(await screen.findByTestId('memory-meter'))
    expect(await screen.findByTestId('context-tokens')).toHaveTextContent('7.9k / 16k')
    await user.click(screen.getByRole('button', { name: /clear context/i }))
    await waitFor(() => expect(screen.getByTestId('context-tokens')).toHaveTextContent('cleared'))
    expect(calls.some((c) => c.method === 'POST' && c.path === '/api/tasks/t_1/context/clear')).toBe(true)
    expect(screen.getByRole('button', { name: /clear context/i })).toBeDisabled()
  })

  it('does not clear the context while the task is working', async () => {
    useUi.setState({ selectedTaskId: 't_1' })
    mockFetch({
      'GET /api/model/status': modelStatus({ memory: loaded }),
      'GET /api/tasks/t_1': taskDetail({ status: 'running', stats: { context_tokens: 3000, context_window: 16384 } }),
    })
    const user = userEvent.setup()
    renderWithProviders(<MemoryMeter />)
    await user.click(await screen.findByTestId('memory-meter'))
    expect(await screen.findByRole('button', { name: /clear context/i })).toBeDisabled()
  })
})
