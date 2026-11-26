import { screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { ModelStatusPill } from '@/components/layout/TopBar'
import { json, mockFetch, renderWithProviders } from './utils'
import { modelStatus } from './fixtures'

describe('ModelStatusPill', () => {
  it('shows a checking state first', () => {
    mockFetch({ 'GET /api/model/status': () => new Promise<Response>(() => {}) })
    renderWithProviders(<ModelStatusPill />)
    expect(screen.getByTestId('model-status-pill')).toHaveTextContent(/checking model/i)
  })

  it('shows the model name when ready', async () => {
    mockFetch({ 'GET /api/model/status': modelStatus() })
    renderWithProviders(<ModelStatusPill />)
    expect(await screen.findByText('qwen2.5-coder:7b')).toBeInTheDocument()
  })

  it('flags Ollama offline and missing models', async () => {
    mockFetch({ 'GET /api/model/status': modelStatus({ ollama_reachable: false, model_available: false, models: [] }) })
    const { unmount } = renderWithProviders(<ModelStatusPill />)
    expect(await screen.findByText('Ollama offline')).toBeInTheDocument()
    unmount()
    mockFetch({ 'GET /api/model/status': modelStatus({ model_available: false }) })
    renderWithProviders(<ModelStatusPill />)
    expect(await screen.findByText('qwen2.5-coder:7b missing')).toBeInTheDocument()
  })

  it('shows a backend error state', async () => {
    mockFetch({ 'GET /api/model/status': () => json({ error: 'boom', detail: 'boom', hint: null }, 500) })
    renderWithProviders(<ModelStatusPill />)
    expect(await screen.findByText('Backend error', {}, { timeout: 4000 })).toBeInTheDocument()
  })
})
