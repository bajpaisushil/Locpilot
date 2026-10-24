import { screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it } from 'vitest'
import { ReportCard } from '@/components/chat/report/ReportCard'
import { useUi } from '@/store/ui'
import { renderWithProviders } from './utils'
import { finalReport, investigation } from './fixtures'

describe('ReportCard', () => {
  beforeEach(() => useUi.setState({ fileTarget: null }))

  it('renders all RCA sections labelled as static analysis, with hypothesis status', () => {
    renderWithProviders(<ReportCard report={finalReport()} />)
    const card = screen.getByTestId('final-report')
    for (const title of [
      'Problem summary',
      'Observed symptoms',
      'Relevant execution flow',
      'Files, functions and lines examined',
      'Candidate root causes',
      'Evidence for and against each hypothesis',
      'Most likely root cause',
      'Minimal recommended fix',
      'Potential side effects',
      'Reproduction and verification steps',
      'Remaining uncertainties',
    ]) {
      expect(within(card).getByText(title)).toBeInTheDocument()
    }
    expect(within(card).getByText('Static analysis')).toBeInTheDocument()
    const hyps = within(card).getAllByTestId('hypothesis')
    expect(hyps[0]).toHaveTextContent('Supported by evidence')
    expect(hyps[1]).toHaveTextContent('Contradicted')
    expect(within(card).queryByTestId('verification')).not.toBeInTheDocument()
  })

  it('opens the cited file and lines when a citation is clicked', async () => {
    const user = userEvent.setup()
    renderWithProviders(<ReportCard report={finalReport()} />)
    await user.click(screen.getAllByRole('button', { name: /offers\/merge\.py:12/ })[0])
    expect(useUi.getState().fileTarget).toMatchObject({ path: 'offers/merge.py', start: 12, end: 12 })
    expect(useUi.getState().rightTab).toBe('files')
  })

  it('warns about citations removed by validation and insufficient evidence', () => {
    renderWithProviders(
      <ReportCard
        report={finalReport({
          investigation: investigation({ removed_citations: ['E9', 'src/ghost.py:1-3'], evidence_sufficient: false }),
        })}
      />,
    )
    expect(screen.getByText(/could not be verified against the repository/i)).toHaveTextContent('E9')
    expect(screen.getByText('Evidence insufficient')).toBeInTheDocument()
  })

  it('distinguishes tests passed, failed and not run', () => {
    const base = finalReport({ mode: 'code', status: 'changes_verified', investigation: null })
    const run = { command_id: 'c1', stage: 'after_fix', argv: ['python', '-m', 'pytest'], exit_code: 0,
      summary: { framework: 'pytest', outcome: 'passed' as const, passed: 5, failed: 0, errors: 0, skipped: 0, total: 5, failing_tests: [] } }
    const { unmount } = renderWithProviders(
      <ReportCard
        report={{ ...base, verification: { tests_run: true, verdict: 'passed', runs: [run],
          regression_test: { status: 'detected_bug', detail: 'failed before, passes after' }, preexisting_failures: [], new_failures: [] } }}
      />,
    )
    expect(screen.getByText('Tests passed')).toBeInTheDocument()
    expect(screen.getByText('Established by execution')).toBeInTheDocument()
    expect(screen.getByText(/failed before the fix and passes after it/i)).toBeInTheDocument()
    expect(screen.getByText('5 passed')).toBeInTheDocument()
    unmount()

    const r2 = renderWithProviders(
      <ReportCard
        report={{ ...base, status: 'changes_failed', verification: { tests_run: true, verdict: 'failed',
          runs: [{ ...run, exit_code: 1, summary: { ...run.summary, outcome: 'failed', passed: 4, failed: 1 } }],
          regression_test: { status: 'unknown', detail: '' }, preexisting_failures: [], new_failures: ['tests/test_x.py::test_y'] } }}
      />,
    )
    expect(screen.getByText('Tests failed')).toBeInTheDocument()
    expect(screen.getByText('tests/test_x.py::test_y')).toBeInTheDocument()
    r2.unmount()

    renderWithProviders(
      <ReportCard
        report={{ ...base, status: 'changes_unverified', verification: { tests_run: false, verdict: 'not_run', runs: [],
          regression_test: { status: 'not_created', detail: '' }, preexisting_failures: [], new_failures: [] } }}
      />,
    )
    expect(screen.getByText('Tests not run')).toBeInTheDocument()
    expect(screen.getByText(/these changes are unverified/i)).toBeInTheDocument()
    expect(screen.queryByText('Established by execution')).not.toBeInTheDocument()
  })

  it('shows a clarifying question when LocPilot needs input', () => {
    renderWithProviders(
      <ReportCard report={finalReport({ status: 'needs_input', investigation: null, clarifying_question: 'Which endpoint returns 500?' })} />,
    )
    expect(screen.getByText(/needs more information/i)).toBeInTheDocument()
    expect(screen.getByText('Which endpoint returns 500?')).toBeInTheDocument()
  })
})
