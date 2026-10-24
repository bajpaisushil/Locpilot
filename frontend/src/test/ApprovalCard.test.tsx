import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it } from 'vitest'
import { ApprovalCard } from '@/components/chat/ApprovalCard'
import { json, mockFetch, renderWithProviders } from './utils'
import { approval, taskDetail } from './fixtures'

describe('ApprovalCard', () => {
  it('renders the diff, files, warnings and sandbox notice; untrusted commands start unchecked', () => {
    mockFetch({})
    renderWithProviders(<ApprovalCard approval={approval()} taskId="t_1" actionable />)
    expect(screen.getByText('Hoist the seen set out of the loop')).toBeInTheDocument()
    expect(screen.getAllByTestId('diff-file')).toHaveLength(2)
    expect(screen.getByText('tests/test_merge_regression.py is a new file')).toBeInTheDocument()
    expect(screen.getByText(/Sandbox: macOS Seatbelt/)).toBeInTheDocument()
    expect(screen.getByRole('checkbox', { name: /run python -m pytest -q/i })).toBeChecked()
    expect(screen.getByRole('checkbox', { name: /run make test/i })).not.toBeChecked()
    expect(screen.getByText('untrusted')).toBeInTheDocument()
  })

  it('approves with exactly the selected commands', async () => {
    const user = userEvent.setup()
    const { calls } = mockFetch({
      'POST /api/tasks/t_1/approvals/ap_1': () => json(taskDetail({ mode: 'code', status: 'running' })),
      'GET /api/tasks': [],
    })
    renderWithProviders(<ApprovalCard approval={approval()} taskId="t_1" actionable />)
    await user.click(screen.getByRole('checkbox', { name: /run make test/i }))
    await user.click(screen.getByRole('checkbox', { name: /run python -m pytest -q/i }))
    await user.click(screen.getByRole('button', { name: /approve & run 1 command/i }))
    await waitFor(() => expect(calls.some((c) => c.method === 'POST')).toBe(true))
    expect(calls.find((c) => c.method === 'POST')?.body).toEqual({ decision: 'approve', commands: [['make', 'test']] })
  })

  it('offers an explicit "run all" or "skip tests" choice when no command is ticked', async () => {
    const user = userEvent.setup()
    const { calls } = mockFetch({
      'POST /api/tasks/t_1/approvals/ap_1': () => json(taskDetail({ mode: 'code', status: 'running' })),
      'GET /api/tasks': [],
    })
    renderWithProviders(<ApprovalCard approval={approval()} taskId="t_1" actionable />)
    await user.click(screen.getByRole('checkbox', { name: /run python -m pytest -q/i }))
    expect(screen.getByRole('button', { name: /approve without running tests/i })).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: /approve & run all 2 commands/i }))
    await waitFor(() =>
      expect(calls.find((c) => c.method === 'POST')?.body).toEqual({
        decision: 'approve',
        commands: [['python', '-m', 'pytest', '-q'], ['make', 'test']],
      }),
    )
  })

  it('rejects with an optional note', async () => {
    const user = userEvent.setup()
    const { calls } = mockFetch({
      'POST /api/tasks/t_1/approvals/ap_1': () => json(taskDetail({ mode: 'code', status: 'completed' })),
      'GET /api/tasks': [],
    })
    renderWithProviders(<ApprovalCard approval={approval()} taskId="t_1" actionable />)
    await user.click(screen.getByRole('button', { name: /^reject$/i }))
    await user.type(screen.getByPlaceholderText(/what to do differently/i), 'Keep the old API')
    await user.click(screen.getByRole('button', { name: /confirm reject/i }))
    await waitFor(() => expect(calls.find((c) => c.method === 'POST')?.body).toEqual({ decision: 'reject', note: 'Keep the old API' }))
  })

  it('shows decision controls only when actionable and surfaces failures', async () => {
    const user = userEvent.setup()
    mockFetch({ 'POST /api/tasks/t_1/approvals/ap_1': () => json({ error: 'conflict', detail: 'Approval already decided', hint: null }, 409) })
    const { unmount } = renderWithProviders(
      <ApprovalCard approval={approval({ status: 'approved' })} taskId="t_1" actionable={false} resolved={{ decision: 'approve', commands: [] }} />,
    )
    expect(screen.queryByRole('button', { name: /approve/i })).not.toBeInTheDocument()
    expect(screen.getByText(/approved — 0 verification commands selected/i)).toBeInTheDocument()
    unmount()

    renderWithProviders(<ApprovalCard approval={approval()} taskId="t_1" actionable />)
    await user.click(screen.getByRole('button', { name: /approve & run/i }))
    expect(await screen.findByText('Approval already decided')).toBeInTheDocument()
  })
})
