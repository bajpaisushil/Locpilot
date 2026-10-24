import { screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it } from 'vitest'
import { UnifiedDiff } from '@/components/code/UnifiedDiff'
import { renderWithProviders } from './utils'
import { SAMPLE_DIFF } from './fixtures'

describe('UnifiedDiff', () => {
  it('renders one block per file with add/delete counts', () => {
    renderWithProviders(<UnifiedDiff diff={SAMPLE_DIFF} />)
    const blocks = screen.getAllByTestId('diff-file')
    expect(blocks).toHaveLength(2)
    expect(blocks[0]).toHaveTextContent('offers/merge.py')
    expect(blocks[0]).toHaveTextContent('+2')
    expect(blocks[1]).toHaveTextContent('new')
    expect(screen.getAllByText(/seen = set\(\)/).length).toBeGreaterThanOrEqual(2)
  })

  it('collapses and expands a file', async () => {
    const user = userEvent.setup()
    renderWithProviders(<UnifiedDiff diff={SAMPLE_DIFF} />)
    expect(screen.getAllByRole('table')).toHaveLength(2)
    await user.click(screen.getByRole('button', { name: /offers\/merge\.py/ }))
    expect(screen.getAllByRole('table')).toHaveLength(1)
  })

  it('shows an empty state for an empty diff', () => {
    renderWithProviders(<UnifiedDiff diff="" />)
    expect(screen.getByText('No changes.')).toBeInTheDocument()
  })
})
