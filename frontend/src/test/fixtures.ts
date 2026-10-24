// Test-only data factories. Never imported by application code.
import type {
  ApprovalOut,
  CommandOut,
  FinalReport,
  InvestigationReport,
  ModelStatus,
  RepoDetail,
  SetupStatus,
  TaskDetail,
} from '@/lib/types'

export function setupStatus(overrides: Partial<SetupStatus> = {}): SetupStatus {
  return {
    ready: true,
    platform: 'macos',
    ollama_url: 'http://127.0.0.1:11434',
    model: 'qwen2.5-coder:7b',
    installed_models: ['qwen2.5-coder:7b'],
    checks: [
      { id: 'ollama', label: 'Ollama', status: 'ok', required: true, detail: 'Ollama 0.40.6 is running', hint: null },
      { id: 'model', label: 'Model qwen2.5-coder:7b', status: 'ok', required: true, detail: 'installed (4.7 GB)', hint: null },
      { id: 'git', label: 'Git', status: 'ok', required: true, detail: 'git version 2.51.0', hint: null },
      { id: 'sandbox', label: 'Test command sandbox', status: 'ok', required: false, detail: 'macOS Seatbelt', hint: null },
    ],
    ollama_install: [
      { title: 'Download and install Ollama for macOS', command: null, url: 'https://ollama.com/download/mac', description: null },
      { title: 'Or install with Homebrew', command: 'brew install ollama', url: null, description: null },
      { title: 'Start Ollama', command: 'ollama serve', url: null, description: null },
    ],
    model_install: [{ title: 'Download the model', command: 'ollama pull qwen2.5-coder:7b', url: null, description: null }],
    cloud_fallback: false,
    checked_at: new Date().toISOString(),
    ...overrides,
  }
}

export function setupOllamaMissing(): SetupStatus {
  const base = setupStatus()
  return {
    ...base,
    ready: false,
    installed_models: [],
    checks: base.checks.map((c) =>
      c.id === 'ollama'
        ? { ...c, status: 'missing', detail: 'Ollama is not reachable at http://127.0.0.1:11434', hint: 'Install and start Ollama' }
        : c.id === 'model'
          ? { ...c, status: 'missing', detail: 'Cannot check models while Ollama is offline' }
          : c,
    ),
  }
}

export function setupModelMissing(): SetupStatus {
  const base = setupStatus()
  return {
    ...base,
    ready: false,
    installed_models: ['llama3.2:3b'],
    checks: base.checks.map((c) =>
      c.id === 'model' ? { ...c, status: 'missing', detail: 'qwen2.5-coder:7b is not installed', hint: 'Run ollama pull' } : c,
    ),
  }
}

export function modelStatus(overrides: Partial<ModelStatus> = {}): ModelStatus {
  return {
    ollama_reachable: true,
    ollama_version: '0.40.6',
    ollama_url: 'http://127.0.0.1:11434',
    configured_model: 'qwen2.5-coder:7b',
    model_available: true,
    models: [{ name: 'qwen2.5-coder:7b', size_bytes: 4_700_000_000, parameter_size: '7.6B', quantization: 'Q4_K_M', family: 'qwen2' }],
    num_ctx: 16384,
    temperature: 0.1,
    error: null,
    hint: null,
    ...overrides,
  }
}

export function repoDetail(overrides: Partial<RepoDetail> = {}): RepoDetail {
  return {
    id: 'r_1',
    name: 'hotel-api',
    source_type: 'local',
    source_url: null,
    path: '/Users/dev/code/hotel-api',
    managed: false,
    created_at: '2026-10-24T10:00:00Z',
    last_opened_at: '2026-10-24T10:00:00Z',
    status: {
      branch: 'main',
      head: 'abcdef1234567890',
      head_summary: 'Initial commit',
      detached: false,
      upstream: null,
      ahead: 0,
      behind: 0,
      dirty: true,
      staged: 0,
      modified: 1,
      untracked: 1,
      conflicted: 0,
      files: [{ path: 'notes.txt', status: '??' }],
    },
    languages: [{ name: 'Python', files: 12 }],
    manifests: ['pyproject.toml'],
    test_commands: [['python', '-m', 'pytest', '-q']],
    file_count: 20,
    ...overrides,
  }
}

export function investigation(overrides: Partial<InvestigationReport> = {}): InvestigationReport {
  const cite = (path: string, s: number, e: number, id: string) => ({ path, start_line: s, end_line: e, evidence_id: id })
  return {
    kind: 'rca',
    intent: 'root_cause',
    answer_markdown: 'Offers are deduplicated with a set that is **re-created on every iteration**.',
    problem_summary: 'Duplicate hotel offers are returned when merging supplier results.',
    observed_symptoms: ['The same offer appears twice in search results'],
    execution_flow: [{ description: 'search() calls merge_offers()', citations: [cite('offers/merge.py', 10, 18, 'E1')] }],
    files_examined: [{ path: 'offers/merge.py', ranges: ['1-40'], symbols: ['merge_offers'], reason: 'definition' }],
    hypotheses: [
      {
        title: 'seen set reset inside the loop',
        explanation: 'seen = set() is inside the for loop',
        confidence: 'high',
        supporting: [cite('offers/merge.py', 12, 12, 'E1')],
        contradicting: [],
        status: 'supported',
      },
      {
        title: 'Supplier returns duplicates',
        explanation: 'Could also be upstream',
        confidence: 'low',
        supporting: [],
        contradicting: [cite('offers/supplier.py', 5, 9, 'E2')],
        status: 'contradicted',
      },
    ],
    most_likely_root_cause: 'The `seen` set is re-initialised inside the loop in merge_offers.',
    root_cause_citations: [cite('offers/merge.py', 12, 12, 'E1')],
    evidence_sufficient: true,
    minimal_fix: 'Move `seen = set()` above the loop.',
    side_effects: ['None expected'],
    verification_steps: ['Run python -m pytest -q'],
    uncertainties: ['Supplier ordering not verified at runtime'],
    analysis_basis: 'static',
    removed_citations: [],
    evidence: [{ id: 'E1', path: 'offers/merge.py', start_line: 1, end_line: 40, kind: 'definition', title: 'def merge_offers' }],
    ...overrides,
  }
}

export function finalReport(overrides: Partial<FinalReport> = {}): FinalReport {
  return {
    task_id: 't_1',
    turn: 1,
    mode: 'ask',
    intent: 'root_cause',
    status: 'answered',
    headline: 'Root cause identified in offers/merge.py',
    investigation: investigation(),
    plan: null,
    patches: [],
    verification: null,
    change_review: null,
    clarifying_question: null,
    retries: 0,
    unresolved_issues: [],
    next_steps: [],
    stats: { llm_calls: 3, prompt_tokens: 9000, completion_tokens: 800, tool_calls: 6, duration_ms: 42000 },
    ...overrides,
  }
}

export const SAMPLE_DIFF = `--- a/offers/merge.py
+++ b/offers/merge.py
@@ -9,6 +9,6 @@ def merge_offers(results):
     merged = []
-    for supplier in results:
-        seen = set()
+    seen = set()
+    for supplier in results:
         for offer in supplier:
             key = offer.key()
--- /dev/null
+++ b/tests/test_merge_regression.py
@@ -0,0 +1,3 @@
+def test_no_duplicates():
+    assert len(merge_offers([[A], [A]])) == 1
+
`

export function approval(overrides: Partial<ApprovalOut> = {}): ApprovalOut {
  return {
    id: 'ap_1',
    kind: 'patch',
    patch_id: 'p_1',
    status: 'pending',
    created_at: new Date().toISOString(),
    decided_at: null,
    payload: {
      title: 'Hoist the seen set out of the loop',
      plan_summary: 'Move `seen = set()` before the supplier loop and add a regression test.',
      diff: SAMPLE_DIFF,
      files: [
        { path: 'offers/merge.py', change: 'modify', kind: 'fix', additions: 2, deletions: 2 },
        { path: 'tests/test_merge_regression.py', change: 'create', kind: 'test', additions: 2, deletions: 0 },
      ],
      commands: [
        { argv: ['python', '-m', 'pytest', '-q'], display: 'python -m pytest -q', reason: 'Run the test suite', trusted: true },
        { argv: ['make', 'test'], display: 'make test', reason: 'Project test target', trusted: false },
      ],
      warnings: ['tests/test_merge_regression.py is a new file'],
      sandbox: 'macOS Seatbelt: no network, writes limited to the task worktree',
      attempt: 1,
    },
    ...overrides,
  }
}

/** A command the user ran themselves (stage "user"), finished with exit code 0 unless overridden. */
export function userCommand(overrides: Partial<CommandOut> = {}): CommandOut {
  return {
    id: 'c_1', turn: 1, stage: 'user', argv: ['python main.py'], cwd: '/data/worktrees/t_1', status: 'completed',
    exit_code: 0, duration_ms: 420, timed_out: false, truncated: false, sandbox: 'seatbelt',
    summary: { framework: 'unknown', outcome: 'passed', passed: null, failed: null, errors: null, skipped: null,
               total: null, failing_tests: [], hints: [] },
    stdout: null, stderr: null, started_at: '2026-10-24T10:02:00Z', finished_at: '2026-10-24T10:02:01Z',
    ...overrides,
  }
}

export function taskDetail(overrides: Partial<TaskDetail> = {}): TaskDetail {
  const repo = repoDetail()
  return {
    id: 't_1',
    repo_id: repo.id,
    title: 'Find the root cause of duplicate hotel offers',
    mode: 'ask',
    status: 'completed',
    phase: null,
    created_at: '2026-10-24T10:00:00Z',
    updated_at: '2026-10-24T10:01:00Z',
    model: 'qwen2.5-coder:7b',
    repo: {
      id: repo.id,
      name: repo.name,
      source_type: repo.source_type,
      source_url: null,
      path: repo.path,
      managed: false,
      created_at: repo.created_at,
      last_opened_at: repo.last_opened_at,
    },
    turn: 1,
    workspace_path: repo.path,
    worktree_path: null,
    base_commit: null,
    error: null,
    messages: [
      { id: 1, turn: 1, role: 'user', content: 'Find the root cause of duplicate hotel offers', mode: 'ask', created_at: '2026-10-24T10:00:00Z' },
    ],
    reports: [],
    patches: [],
    commands: [],
    pending_approval: null,
    stats: { llm_calls: 3 },
    ...overrides,
  }
}
