import { act, fireEvent, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { FilesTab } from '@/components/panels/FilesTab'
import { SAVE_EVENT } from '@/hooks/useShortcuts'
import type { FileContent } from '@/lib/types'
import { useDrafts } from '@/store/drafts'
import { useUi } from '@/store/ui'
import { json, mockFetch, renderWithProviders, type RecordedRequest } from './utils'

// Monaco can't run in jsdom: an uncontrolled textarea behaves like the editable editor (new model = remount).
vi.mock('@/components/code/LazyViewers', () => ({
  CodeViewer: (p: { modelPath: string; content: string; editable?: boolean; onChange?: (v: string) => void }) => (
    <textarea
      key={p.modelPath}
      aria-label="Editor"
      defaultValue={p.content}
      readOnly={!p.editable}
      onChange={(e) => p.onChange?.(e.target.value)}
    />
  ),
  DiffViewer: () => null,
}))

const SHA_A = 'a'.repeat(64)
const SHA_B = 'b'.repeat(64)
const SHA_C = 'c'.repeat(64)

function fileContent(overrides: Partial<FileContent> = {}): FileContent {
  return {
    path: 'README.md', content: 'hello\n', start_line: 1, end_line: 1, total_lines: 1, language: 'markdown',
    truncated: false, binary: false, sha: SHA_A, ...overrides,
  }
}

describe('FilesTab editor', () => {
  beforeEach(() => {
    useDrafts.setState({ drafts: {} })
    useUi.setState({ selectedRepoId: 'r_1', selectedTaskId: null, fileTarget: { path: 'README.md', taskId: null, nonce: 1 } })
  })

  it('saves edits with the Save button and ⌘S, each against the latest version', async () => {
    const user = userEvent.setup()
    let onDisk = fileContent()
    const { calls } = mockFetch({
      'GET /api/repos/r_1/tree': [],
      'GET /api/repos/r_1/file': () => json(onDisk),
      'GET /api/repos/r_1': {},
      'PUT /api/repos/r_1/file': (req: RecordedRequest) => {
        const { content } = req.body as { content: string }
        onDisk = fileContent({ content, sha: calls.filter((c) => c.method === 'PUT').length === 1 ? SHA_B : SHA_C })
        return json({ path: 'README.md', sha: onDisk.sha, size: content.length })
      },
    })
    renderWithProviders(<FilesTab />)
    const editor = await screen.findByRole('textbox', { name: 'Editor' })
    const save = screen.getByRole('button', { name: /save file/i })
    expect(save).toBeDisabled()
    fireEvent.change(editor, { target: { value: 'hello world\n' } })
    expect(screen.getByLabelText(/unsaved changes/i)).toBeInTheDocument()
    await user.click(save)
    await waitFor(() => expect(screen.queryByLabelText(/unsaved changes/i)).not.toBeInTheDocument())
    expect(calls.find((c) => c.method === 'PUT')?.body).toEqual({
      path: 'README.md', content: 'hello world\n', expected_sha: SHA_A, task_id: null,
    })
    // ⌘S (dispatched app-wide by useShortcuts) saves the next edit against the version just saved.
    fireEvent.change(editor, { target: { value: 'hello again\n' } })
    act(() => {
      window.dispatchEvent(new Event(SAVE_EVENT))
    })
    await waitFor(() => expect(calls.filter((c) => c.method === 'PUT')).toHaveLength(2))
    expect(calls.filter((c) => c.method === 'PUT')[1].body).toMatchObject({ content: 'hello again\n', expected_sha: SHA_B })
    expect(screen.getByRole('textbox', { name: 'Editor' })).toBe(editor) // same model: cursor kept
  })

  it('refuses to overwrite a file changed on disk and offers a reload', async () => {
    const user = userEvent.setup()
    let onDisk = fileContent()
    mockFetch({
      'GET /api/repos/r_1/tree': [],
      'GET /api/repos/r_1/file': () => json(onDisk),
      'PUT /api/repos/r_1/file': () =>
        json({ error: 'file_changed', detail: 'README.md changed on disk since you opened it.',
               hint: 'Reload the file to see the latest version, then redo your edit.' }, 409),
    })
    const { queryClient } = renderWithProviders(<FilesTab />)
    const editor = await screen.findByRole('textbox', { name: 'Editor' })
    fireEvent.change(editor, { target: { value: 'my edit\n' } })
    onDisk = fileContent({ content: 'changed by LocPilot\n', sha: SHA_B })
    await user.click(screen.getByRole('button', { name: /save file/i }))
    expect(await screen.findByText(/changed on disk since you opened it/i)).toBeInTheDocument()
    await act(() => queryClient.refetchQueries({ queryKey: ['repos', 'r_1', 'file'] }))
    expect(await screen.findByText(/changed on disk while you were editing/i)).toBeInTheDocument()
    expect(screen.getByRole('textbox', { name: 'Editor' })).toHaveValue('my edit\n') // edits never replaced silently
    await user.click(screen.getByRole('button', { name: /reload — discard my edits/i }))
    await waitFor(() => expect(screen.getByRole('textbox', { name: 'Editor' })).toHaveValue('changed by LocPilot\n'))
    expect(screen.queryByLabelText(/unsaved changes/i)).not.toBeInTheDocument()
  })

  it('keeps unsaved edits while you open another file, and restores them', async () => {
    mockFetch({
      'GET /api/repos/r_1/tree': [{ name: 'README.md', path: 'README.md', type: 'file' }, { name: 'app.py', path: 'app.py', type: 'file' }],
      'GET /api/repos/r_1/file': () => json(fileContent()),
    })
    renderWithProviders(<FilesTab />)
    fireEvent.change(await screen.findByRole('textbox', { name: 'Editor' }), { target: { value: 'draft text\n' } })
    act(() => useUi.getState().openFile({ path: 'app.py', taskId: null }))
    expect(await screen.findByLabelText('README.md has unsaved changes')).toBeInTheDocument()
    await waitFor(() => expect(screen.queryByLabelText('Unsaved changes')).not.toBeInTheDocument())
    act(() => useUi.getState().openFile({ path: 'README.md', taskId: null }))
    await waitFor(() => expect(screen.getByRole('textbox', { name: 'Editor' })).toHaveValue('draft text\n'))
    expect(screen.getByLabelText('Unsaved changes')).toBeInTheDocument()
  })

  it('keeps unsaved edits when the panel is closed and reopened', async () => {
    mockFetch({ 'GET /api/repos/r_1/tree': [], 'GET /api/repos/r_1/file': () => json(fileContent()) })
    const first = renderWithProviders(<FilesTab />)
    fireEvent.change(await screen.findByRole('textbox', { name: 'Editor' }), { target: { value: 'kept\n' } })
    first.unmount()
    renderWithProviders(<FilesTab />)
    await waitFor(() => expect(screen.getByRole('textbox', { name: 'Editor' })).toHaveValue('kept\n'))
    expect(screen.getByLabelText('Unsaved changes')).toBeInTheDocument()
  })

  it('keeps binary and very large files read-only', async () => {
    mockFetch({ 'GET /api/repos/r_1/tree': [], 'GET /api/repos/r_1/file': fileContent({ truncated: true, total_lines: 90000 }) })
    renderWithProviders(<FilesTab />)
    expect(await screen.findByRole('textbox', { name: 'Editor' })).toHaveAttribute('readonly')
    expect(screen.queryByRole('button', { name: /save file/i })).not.toBeInTheDocument()
    expect(screen.getByText(/read-only/i)).toBeInTheDocument()
  })
})
