import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'
import { SourceControl } from '@/components/panels/SourceControl'
import type { PublishState, WorktreeChanges } from '@/lib/types'
import { json, mockFetch, renderWithProviders } from './utils'
import { taskDetail } from './fixtures'

const task = taskDetail({ mode: 'code', worktree_path: '/data/worktrees/t_1', status: 'completed' })

function changes(overrides: Partial<WorktreeChanges> = {}): WorktreeChanges {
  return {
    base_commit: 'abc123',
    worktree_path: '/data/worktrees/t_1',
    files: [
      { path: 'offers/aggregator.py', status: 'M', locpilot: true },
      { path: 'tests/test_dupes.py', status: 'A', locpilot: true },
      { path: 'notes.txt', status: 'A', locpilot: false },
    ],
    suggested_branch: 'locpilot/fix-duplicate-offers',
    suggested_message: 'Deduplicate across suppliers',
    publish: null,
    ...overrides,
  }
}

function published(overrides: Partial<PublishState> = {}): PublishState {
  return {
    task_id: 't_1', branch: 'locpilot/fix-duplicate-offers', head: 'c0ffee1234567', pushed_head: null,
    committed_at: '2026-10-24T10:05:00Z', pushed_at: null, remote_url: 'https://github.com/acme/shop.git',
    pull_request_url: 'https://github.com/acme/shop/compare/locpilot/fix-duplicate-offers?expand=1',
    ...overrides,
  }
}

describe('SourceControl', () => {
  it('commits the checked files with ⌘↵, like VS Code', async () => {
    const user = userEvent.setup()
    const { calls } = mockFetch({
      'GET /api/tasks/t_1/worktree-changes': changes(),
      'POST /api/tasks/t_1/commit': { commit: 'c0ffee1234567', branch: 'locpilot/fix-duplicate-offers',
                                      files: ['offers/aggregator.py', 'tests/test_dupes.py'], publish: published() },
    })
    renderWithProviders(<SourceControl task={task} />)
    const box = await screen.findByRole('textbox', { name: /commit message/i })
    expect(await screen.findByRole('checkbox', { name: /include offers\/aggregator\.py/i })).toBeChecked()
    expect(screen.getByRole('checkbox', { name: /include notes\.txt/i })).not.toBeChecked()
    expect(screen.getByRole('textbox', { name: /branch/i })).toHaveValue('locpilot/fix-duplicate-offers')
    await user.type(box, 'Fix duplicate hotel offers')
    await user.keyboard('{Meta>}{Enter}{/Meta}')
    await waitFor(() =>
      expect(calls.find((c) => c.method === 'POST')?.body).toEqual({
        branch: 'locpilot/fix-duplicate-offers',
        message: 'Fix duplicate hotel offers',
        files: ['offers/aggregator.py', 'tests/test_dupes.py'],
      }),
    )
    await waitFor(() => expect(box).toHaveValue(''))
  })

  it('Commit & Push uses the suggested message, the typed branch and extra files', async () => {
    const user = userEvent.setup()
    const { calls } = mockFetch({
      'GET /api/tasks/t_1/worktree-changes': changes(),
      'POST /api/tasks/t_1/commit': { commit: 'c0ffee1234567', branch: 'feature/dedupe', files: [], publish: null },
      'POST /api/tasks/t_1/push': { publish: published({ branch: 'feature/dedupe', pushed_head: 'c0ffee1234567' }) },
    })
    renderWithProviders(<SourceControl task={task} />)
    await user.click(await screen.findByRole('checkbox', { name: /include notes\.txt/i }))
    const branch = screen.getByRole('textbox', { name: /branch/i })
    await user.clear(branch)
    await user.type(branch, 'feature/dedupe')
    await user.click(screen.getByRole('button', { name: /commit & push/i }))
    await waitFor(() => expect(calls.some((c) => c.method === 'POST' && c.path.endsWith('/push'))).toBe(true))
    expect(calls.find((c) => c.path.endsWith('/commit'))?.body).toEqual({
      branch: 'feature/dedupe',
      message: 'Deduplicate across suppliers',
      files: ['offers/aggregator.py', 'tests/test_dupes.py', 'notes.txt'],
    })
  })

  it('applies the selected files to the repository only after confirming', async () => {
    const user = userEvent.setup()
    const { calls } = mockFetch({
      'GET /api/tasks/t_1/worktree-changes': changes(),
      'POST /api/tasks/t_1/apply-to-repo': { applied: ['offers/aggregator.py', 'tests/test_dupes.py'], repo_path: '/home/me/hotel' },
    })
    renderWithProviders(<SourceControl task={task} />)
    await user.click(await screen.findByRole('button', { name: /apply to my repo/i }))
    const dialog = await screen.findByRole('alertdialog')
    expect(within(dialog).getByText('offers/aggregator.py')).toBeInTheDocument()
    expect(calls.some((c) => c.method === 'POST')).toBe(false)
    await user.click(within(dialog).getByRole('button', { name: /apply to my repo/i }))
    await waitFor(() =>
      expect(calls.find((c) => c.path.endsWith('/apply-to-repo'))?.body).toEqual({
        files: ['offers/aggregator.py', 'tests/test_dupes.py'],
      }),
    )
  })

  it('shows the pushed branch with a pull request link, and offers Push when behind', async () => {
    const user = userEvent.setup()
    const open = vi.spyOn(window, 'open').mockImplementation(() => null)
    mockFetch({
      'GET /api/tasks/t_1/worktree-changes': changes({ files: [], publish: published({ pushed_head: 'c0ffee1234567' }) }),
    })
    renderWithProviders(<SourceControl task={task} />)
    expect(await screen.findByTestId('publish-state')).toHaveTextContent(/pushed to origin/)
    expect(screen.queryByRole('button', { name: /^push$/i })).not.toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: /create pull request/i }))
    expect(open).toHaveBeenCalledWith(published().pull_request_url, '_blank', 'noopener,noreferrer')
  })

  it('surfaces a failed push with its hint', async () => {
    const user = userEvent.setup()
    mockFetch({
      'GET /api/tasks/t_1/worktree-changes': changes({ files: [], publish: published() }),
      'POST /api/tasks/t_1/push': () =>
        json({ error: 'push_failed', detail: 'Could not resolve host: github.com',
               hint: 'Check your internet connection, then push again — the commit is saved locally.' }, 409),
    })
    renderWithProviders(<SourceControl task={task} />)
    await user.click(await screen.findByRole('button', { name: /^push$/i }))
    expect(await screen.findByText(/could not resolve host/i)).toBeInTheDocument()
    expect(screen.getByText(/the commit is saved locally/i)).toBeInTheDocument()
  })
})
