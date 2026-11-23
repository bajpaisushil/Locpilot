import { screen, waitFor, within } from '@testing-library/react'
import { beforeEach, describe, expect, it } from 'vitest'
import { SettingsDialog } from '@/components/settings/SettingsDialog'
import { useUi } from '@/store/ui'
import { mockFetch, renderWithProviders } from './utils'
import { modelStatus } from './fixtures'

const mimo = {
  name: 'mimo-v2.6:9b-q4_K_M',
  size_bytes: 6_550_811_033,
  parameter_size: '9.0B',
  quantization: 'Q4_K_M',
  family: 'qwen35',
}

describe('SettingsDialog model section', () => {
  beforeEach(() => useUi.setState({ settingsOpen: true }))

  it('says when LocPilot switched to an installed model by itself', async () => {
    mockFetch({
      'GET /api/model/status': modelStatus({
        configured_model: mimo.name,
        models: [mimo],
        switched_from: 'qwen2.5-coder:7b',
      }),
    })
    renderWithProviders(<SettingsDialog />)
    const note = await screen.findByTestId('model-switched-note')
    expect(note).toHaveTextContent('qwen2.5-coder:7b is not installed, so LocPilot uses mimo-v2.6:9b-q4_K_M')
    expect(screen.getByText('ready')).toBeInTheDocument()
  })

  it('shows no note for a model the user chose', async () => {
    mockFetch({ 'GET /api/model/status': modelStatus() })
    renderWithProviders(<SettingsDialog />)
    expect(await screen.findByText('ready')).toBeInTheDocument()
    expect(screen.queryByTestId('model-switched-note')).not.toBeInTheDocument()
  })
})

const GB = 1024 ** 3

const storage = {
  data_dir: '/Users/me/.locpilot',
  worktrees: [
    { task_id: 't1', repo_id: 'r1', repo_name: 'shop', title: 'Fix cart', status: 'completed', updated_at: null,
      state: 'active', bytes: 2 * GB, can_archive: true, reason: null },
    { task_id: 't2', repo_id: 'r1', repo_name: 'shop', title: 'Add test', status: 'awaiting_approval',
      updated_at: null, state: 'active', bytes: GB, can_archive: false, reason: 'waiting for your approval' },
  ],
  worktrees_bytes: 3 * GB,
  archivable_bytes: 2 * GB,
  archived_count: 0,
  archived_bytes: 0,
  history_bytes: 4 * 1024 ** 2,
  checkpoints_bytes: 300 * 1024 ** 2,
  logs_bytes: 20 * 1024 ** 2,
  caches_bytes: 500 * 1024 ** 2,
  index_bytes: 40 * 1024 ** 2,
  models_bytes: 6.1 * GB,
}

describe('SettingsDialog memory and storage', () => {
  beforeEach(() => useUi.setState({ settingsOpen: true }))

  it('shows how the model fits in memory and frees its RAM on request', async () => {
    const { calls } = mockFetch({
      'GET /api/model/status': modelStatus({
        memory: { total_bytes: 16 * GB, available_bytes: 7 * GB, loaded_bytes: 5.8 * GB, needed_bytes: 0,
                  context_bytes: GB, kv_cache_type: 'q8_0', short: false },
      }),
      'GET /api/storage': storage,
      'POST /api/model/unload': { unloaded: ['qwen2.5-coder:7b'] },
    })
    renderWithProviders(<SettingsDialog />)
    const line = await screen.findByTestId('model-memory')
    expect(line).toHaveTextContent('uses 5.8 GB of RAM · 7.0 GB of 16 GB free · context 1.0 GB (stored in 8 bits)')
    screen.getByRole('button', { name: 'Free RAM now' }).click()
    await waitFor(() => expect(calls.some((c) => c.method === 'POST' && c.path === '/api/model/unload')).toBe(true))
  })

  it('warns when the model would not fit in free memory', async () => {
    mockFetch({
      'GET /api/model/status': modelStatus({
        memory: { total_bytes: 16 * GB, available_bytes: 3 * GB, loaded_bytes: null, needed_bytes: 6.6 * GB,
                  context_bytes: GB, kv_cache_type: 'f16', short: true },
      }),
      'GET /api/storage': storage,
    })
    renderWithProviders(<SettingsDialog />)
    expect(await screen.findByText(/Not enough free memory/)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Free RAM now' })).not.toBeInTheDocument()
  })

  it('lists disk use and frees space: worktrees of finished tasks, then the rest', async () => {
    const { calls } = mockFetch({
      'GET /api/model/status': modelStatus(),
      'GET /api/storage': storage,
      'POST /api/storage/archive-worktrees': { archived: ['t1'], skipped: [], bytes_freed: 2 * GB },
      'POST /api/storage/clean-up': {
        bytes_freed: 800 * 1024 ** 2,
        details: { history: 0, checkpoints: 290 * 1024 ** 2, logs: 20 * 1024 ** 2, caches: 490 * 1024 ** 2, index: 0 },
      },
    })
    renderWithProviders(<SettingsDialog />)
    const section = await screen.findByTestId('storage-section')
    expect(await within(section).findByText('3.0 GB in 2 tasks')).toBeInTheDocument()
    expect(within(section).getByText(/1 in use: waiting for your approval/)).toBeInTheDocument()
    within(section).getByRole('button', { name: 'Free 2.0 GB' }).click()
    expect(await screen.findByTestId('archive-result')).toHaveTextContent('Freed 2.0 GB from 1 task.')
    within(section).getByRole('button', { name: 'Clean up' }).click()
    expect(await screen.findByTestId('clean-up-result')).toHaveTextContent('Freed 800 MB.')
    expect(calls.filter((c) => c.method === 'POST').map((c) => c.path)).toEqual([
      '/api/storage/archive-worktrees',
      '/api/storage/clean-up',
    ])
  })
})
