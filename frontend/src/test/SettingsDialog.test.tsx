import { screen } from '@testing-library/react'
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
