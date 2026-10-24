import { screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { TaskList } from '@/components/layout/Sidebar'
import { useUi } from '@/store/ui'
import { json, mockFetch, renderWithProviders } from './utils'

const summary = (id: string, title: string, status = 'completed', mode = 'ask') => ({
  id, repo_id: 'r_1', title, mode, status, phase: null,
  created_at: '2026-10-24T10:00:00Z', updated_at: new Date().toISOString(), model: 'qwen2.5-coder:7b',
})

describe('TaskList', () => {
  beforeEach(() => useUi.setState({ selectedRepoId: 'r_1', selectedTaskId: null }))

  it('shows a loading skeleton', () => {
    vi.stubGlobal('fetch', vi.fn(() => new Promise(() => {})))
    renderWithProviders(<TaskList />)
    expect(screen.getByRole('status', { name: /loading tasks/i })).toBeInTheDocument()
  })

  it('shows an empty state', async () => {
    mockFetch({ 'GET /api/tasks': [] })
    renderWithProviders(<TaskList />)
    expect(await screen.findByText(/no tasks yet/i)).toBeInTheDocument()
  })

  it('shows an error state', async () => {
    mockFetch({ 'GET /api/tasks': () => json({ error: 'db', detail: 'database is locked', hint: null }, 500) })
    renderWithProviders(<TaskList />)
    expect(await screen.findByText('database is locked', {}, { timeout: 5000 })).toBeInTheDocument()
  })

  it('lists tasks and selects one on click', async () => {
    const user = userEvent.setup()
    mockFetch({
      'GET /api/tasks': [summary('t_1', 'Explain authentication'), summary('t_2', 'Fix duplicate offers', 'awaiting_approval', 'code')],
    })
    renderWithProviders(<TaskList />)
    const rows = await screen.findAllByTestId('task-row')
    expect(rows).toHaveLength(2)
    expect(rows[1]).toHaveTextContent('awaiting approval')
    await user.click(rows[1])
    expect(useUi.getState().selectedTaskId).toBe('t_2')
    expect(useUi.getState().composerMode).toBe('code')
  })
})
