import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it } from 'vitest'
import { ChangesTab } from '@/components/panels/ChangesTab'
import type { TaskDiff, WorktreeChanges } from '@/lib/types'
import { useUi } from '@/store/ui'
import { json, mockFetch, renderWithProviders } from './utils'
import { taskDetail } from './fixtures'

const diff = (rolledBack: boolean): TaskDiff => ({
  base_commit: 'abc123',
  worktree_path: '/data/worktrees/t_1',
  files: rolledBack
    ? []
    : [{ path: 'offers/aggregator.py', change: 'modify', original: 'a\n', modified: 'b\n', unified: '-a\n+b\n', binary: false }],
  unified: '',
})

const changes = (rolledBack: boolean): WorktreeChanges => ({
  base_commit: 'abc123',
  worktree_path: '/data/worktrees/t_1',
  files: rolledBack ? [] : [{ path: 'offers/aggregator.py', status: 'M', locpilot: true }],
  suggested_branch: 'locpilot/fix-duplicate-offers',
  suggested_message: 'Deduplicate across suppliers',
  publish: null,
})

describe('ChangesTab', () => {
  beforeEach(() => useUi.setState({ selectedTaskId: 't_1' }))

  it('refreshes the uncommitted-changes list right after a rollback', async () => {
    const user = userEvent.setup()
    let rolledBack = false
    mockFetch({
      'GET /api/tasks/t_1': taskDetail({ mode: 'code', worktree_path: '/data/worktrees/t_1', status: 'completed' }),
      'GET /api/tasks/t_1/diff': () => json(diff(rolledBack)),
      'GET /api/tasks/t_1/worktree-changes': () => json(changes(rolledBack)),
      'POST /api/tasks/t_1/rollback': () => {
        rolledBack = true
        return json({ restored: ['offers/aggregator.py'], skipped: [] })
      },
    })
    renderWithProviders(<ChangesTab />)
    expect(await screen.findByRole('checkbox', { name: /include offers\/aggregator\.py/i })).toBeChecked()
    await user.click(screen.getByRole('button', { name: /^roll back$/i }))
    await user.click(within(await screen.findByRole('alertdialog')).getByRole('button', { name: /roll back/i }))
    await waitFor(() =>
      expect(screen.queryByRole('checkbox', { name: /include offers\/aggregator\.py/i })).not.toBeInTheDocument(),
    )
    expect(screen.getByText(/no uncommitted changes/i)).toBeInTheDocument()
  })
})
