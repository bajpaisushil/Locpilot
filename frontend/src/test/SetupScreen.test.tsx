import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'
import { SetupScreen } from '@/components/setup/SetupScreen'
import type { PullStatus } from '@/lib/types'
import { json, mockFetch, renderWithProviders } from './utils'
import { setupModelMissing, setupOllamaMissing, setupStatus } from './fixtures'

describe('SetupScreen', () => {
  it('shows a loading state while checking dependencies', () => {
    vi.stubGlobal('fetch', vi.fn(() => new Promise(() => {})))
    renderWithProviders(<SetupScreen onContinue={() => {}} />)
    expect(screen.getByRole('status')).toHaveTextContent(/checking local dependencies/i)
  })

  it('shows an error with retry when the backend cannot be reached', async () => {
    vi.stubGlobal('fetch', vi.fn(() => Promise.reject(new TypeError('Failed to fetch'))))
    renderWithProviders(<SetupScreen onContinue={() => {}} />)
    expect(await screen.findByText(/could not check dependencies/i)).toBeInTheDocument()
    expect(screen.getByText(/cannot reach the locpilot backend/i)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /retry/i })).toBeInTheDocument()
  })

  it('guides installing Ollama when it is missing and re-checks on demand', async () => {
    const user = userEvent.setup()
    const { calls } = mockFetch({ 'GET /api/setup/status': setupOllamaMissing() })
    const onContinue = vi.fn()
    renderWithProviders(<SetupScreen onContinue={onContinue} onBrowseWithoutAi={() => {}} />)
    expect(await screen.findByTestId('readiness')).toHaveTextContent(/2 required items need attention/i)
    expect(screen.getByTestId('check-ollama')).toHaveAttribute('data-status', 'missing')
    expect(screen.getByTestId('ollama-guide')).toHaveTextContent('brew install ollama')
    expect(screen.getByRole('button', { name: /open ollama.com/i })).toBeInTheDocument()
    expect(screen.queryByTestId('model-guide')).not.toBeInTheDocument()
    expect(screen.getByText(/never falls back to cloud AI APIs/i)).toBeInTheDocument()
    expect(screen.getByText(/separate from the LocPilot installer/i)).toBeInTheDocument()
    // Continue is disabled until ready; browse-only is offered.
    expect(screen.getByRole('button', { name: /waiting for dependencies/i })).toBeDisabled()
    expect(screen.getByRole('button', { name: /continue without ai/i })).toBeEnabled()

    const before = calls.filter((c) => c.path === '/api/setup/status').length
    await user.click(screen.getByRole('button', { name: /check again/i }))
    await waitFor(() => expect(calls.filter((c) => c.path === '/api/setup/status').length).toBeGreaterThan(before))
  })

  it('downloads a missing model through Ollama and shows progress', async () => {
    const user = userEvent.setup()
    let pullState: PullStatus = { model: 'qwen2.5-coder:7b', state: 'idle', status_text: '', completed: null, total: null, error: null }
    const { calls } = mockFetch({
      'GET /api/setup/status': setupModelMissing(),
      'GET /api/setup/pull-status': () => json(pullState),
      'POST /api/setup/pull-model': () => {
        pullState = { ...pullState, state: 'pulling', status_text: 'pulling manifest', completed: 1_000_000_000, total: 4_000_000_000 }
        return json(pullState)
      },
    })
    renderWithProviders(<SetupScreen onContinue={() => {}} />)
    const guide = await screen.findByTestId('model-guide')
    expect(guide).toHaveTextContent('ollama pull qwen2.5-coder:7b')
    expect(guide).toHaveTextContent('llama3.2:3b') // offers the already-installed model
    await user.click(screen.getByRole('button', { name: /download qwen2.5-coder:7b with ollama/i }))
    const progress = await screen.findByTestId('pull-progress')
    expect(progress).toHaveTextContent('pulling manifest')
    expect(progress).toHaveTextContent('25%')
    const post = calls.find((c) => c.method === 'POST' && c.path === '/api/setup/pull-model')
    expect(post?.body).toEqual({ model: 'qwen2.5-coder:7b' })
    expect(post?.headers['X-LocPilot-Client']).toBe('1')
  })

  it('lets the user continue once everything is ready', async () => {
    const user = userEvent.setup()
    mockFetch({ 'GET /api/setup/status': setupStatus() })
    const onContinue = vi.fn()
    renderWithProviders(<SetupScreen onContinue={onContinue} />)
    expect(await screen.findByTestId('readiness')).toHaveTextContent(/ready/i)
    await user.click(screen.getByRole('button', { name: /^continue$/i }))
    expect(onContinue).toHaveBeenCalled()
  })
})
