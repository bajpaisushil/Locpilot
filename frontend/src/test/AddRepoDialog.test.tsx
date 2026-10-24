import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it } from 'vitest'
import { AddRepoDialog } from '@/components/repos/AddRepoDialog'
import { validateRepoUrl } from '@/lib/validation'
import { useUi } from '@/store/ui'
import { json, mockFetch, renderWithProviders } from './utils'
import { repoDetail } from './fixtures'

describe('validateRepoUrl', () => {
  it('accepts GitHub https and scp-style URLs', () => {
    expect(validateRepoUrl('https://github.com/pallets/flask')).toBeNull()
    expect(validateRepoUrl('https://github.com/pallets/flask.git')).toBeNull()
    expect(validateRepoUrl('git@github.com:pallets/flask.git')).toBeNull()
  })
  it('rejects credentials, http and unknown formats', () => {
    expect(validateRepoUrl('https://ghp_secret@github.com/a/b')).toMatch(/credentials/i)
    expect(validateRepoUrl('http://github.com/a/b')).toMatch(/https/i)
    expect(validateRepoUrl('file:///etc/passwd')).toMatch(/expected/i)
    expect(validateRepoUrl('')).toMatch(/enter/i)
  })
})

describe('AddRepoDialog', () => {
  beforeEach(() => {
    useUi.setState({ addRepoOpen: true, selectedRepoId: null })
  })

  it('shows the backend validation error for a bad local path', async () => {
    const user = userEvent.setup()
    mockFetch({
      'POST /api/repos': () =>
        json({ error: 'not_a_git_repo', detail: '/tmp/nope is not a Git repository', hint: 'Run git init first' }, 400),
    })
    renderWithProviders(<AddRepoDialog />)
    await user.type(screen.getByLabelText(/repository folder/i), '/tmp/nope')
    await user.click(screen.getByRole('button', { name: /open repository/i }))
    expect(await screen.findByText('/tmp/nope is not a Git repository')).toBeInTheDocument()
    expect(screen.getByText('Run git init first')).toBeInTheDocument()
    expect(useUi.getState().addRepoOpen).toBe(true)
  })

  it('registers a local repository, selects it and closes', async () => {
    const user = userEvent.setup()
    const { calls } = mockFetch({ 'POST /api/repos': () => json(repoDetail({ id: 'r_new' }), 201), 'GET /api/repos': [] })
    renderWithProviders(<AddRepoDialog />)
    await user.type(screen.getByLabelText(/repository folder/i), '/Users/dev/code/hotel-api')
    await user.click(screen.getByRole('button', { name: /open repository/i }))
    await waitFor(() => expect(useUi.getState().selectedRepoId).toBe('r_new'))
    expect(useUi.getState().addRepoOpen).toBe(false)
    expect(calls.find((c) => c.method === 'POST')?.body).toEqual({ source: 'local', path: '/Users/dev/code/hotel-api' })
  })

  it('blocks URLs with embedded credentials before calling the backend', async () => {
    const user = userEvent.setup()
    const { calls } = mockFetch({})
    renderWithProviders(<AddRepoDialog />)
    await user.click(screen.getByRole('tab', { name: /clone from url/i }))
    await user.type(screen.getByLabelText(/repository url/i), 'https://token123@github.com/a/b')
    await user.click(screen.getByRole('button', { name: /clone repository/i }))
    expect(await screen.findByText(/remove credentials/i)).toBeInTheDocument()
    expect(calls).toHaveLength(0)
  })
})
