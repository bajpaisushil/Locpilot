// Types mirroring docs/api.md (the backend <-> UI contract). Keep in sync.

export type Mode = 'ask' | 'code'

export interface ApiErrorBody {
  error: string
  detail: string | null
  hint: string | null
}

// ---------------------------------------------------------------- health / setup

export interface HealthResponse {
  status: 'ok'
  version: string
}

export type CheckStatus = 'ok' | 'missing' | 'error' | 'warning'

export interface SetupCheck {
  id: 'ollama' | 'model' | 'git' | 'sandbox'
  label: string
  status: CheckStatus
  required: boolean
  detail: string
  hint: string | null
}

export interface InstallStep {
  title: string
  command: string | null
  url: string | null
  description: string | null
}

export interface SetupStatus {
  ready: boolean
  platform: 'macos' | 'windows' | 'linux' | 'other'
  ollama_url: string
  model: string
  installed_models: string[]
  checks: SetupCheck[]
  ollama_install: InstallStep[]
  model_install: InstallStep[]
  cloud_fallback: false
  checked_at: string
}

export interface PullStatus {
  model: string
  state: 'idle' | 'pulling' | 'success' | 'error'
  status_text: string
  completed: number | null
  total: number | null
  error: string | null
}

// ---------------------------------------------------------------- model

export interface ModelInfo {
  name: string
  size_bytes: number | null
  parameter_size: string | null
  quantization: string | null
  family: string | null
}

export interface ModelStatus {
  ollama_reachable: boolean
  ollama_version: string | null
  ollama_url: string
  configured_model: string
  model_available: boolean
  models: ModelInfo[]
  num_ctx: number
  temperature: number
  error: string | null
  hint: string | null
}

export interface ModelConfigUpdate {
  model?: string
  num_ctx?: number
  temperature?: number
}

// ---------------------------------------------------------------- repositories

export interface RepoSummary {
  id: string
  name: string
  source_type: 'github' | 'local'
  source_url: string | null
  path: string
  managed: boolean
  created_at: string
  last_opened_at: string
}

export interface GitStatusFile {
  path: string
  status: string
}

export interface RepoStatus {
  branch: string | null
  head: string | null
  head_summary: string | null
  detached: boolean
  upstream: string | null
  ahead: number
  behind: number
  dirty: boolean
  staged: number
  modified: number
  untracked: number
  conflicted: number
  files: GitStatusFile[]
}

export interface RepoDetail extends RepoSummary {
  status: RepoStatus
  languages: { name: string; files: number }[]
  manifests: string[]
  test_commands: string[][]
  file_count: number
}

export type RepoCreate = { source: 'github'; url: string } | { source: 'local'; path: string }

export interface TreeEntry {
  name: string
  path: string
  type: 'file' | 'dir'
  size: number | null
}

export interface FileContent {
  path: string
  content: string
  start_line: number
  end_line: number
  total_lines: number
  language: string
  truncated: boolean
  binary: boolean
  sha: string
}

export interface SearchHit {
  path: string
  line: number
  text: string
}

// ---------------------------------------------------------------- tasks

export type TaskStatus =
  | 'queued'
  | 'running'
  | 'awaiting_approval'
  | 'needs_input'
  | 'completed'
  | 'failed'
  | 'cancelled'
  | 'interrupted'

export interface TaskSummary {
  id: string
  repo_id: string
  title: string
  mode: Mode
  status: TaskStatus
  phase: string | null
  created_at: string
  updated_at: string
  model: string | null
}

export interface MessageOut {
  id: number
  turn: number
  role: 'user' | 'assistant' | 'system'
  content: string
  mode: Mode | null
  created_at: string
}

export interface TestSummary {
  framework: string
  outcome: 'passed' | 'failed' | 'error' | 'no_tests' | 'unknown'
  passed: number | null
  failed: number | null
  errors: number | null
  skipped: number | null
  total: number | null
  failing_tests: string[]
  /** Execution notes, e.g. a timeout or a sandbox block. */
  hints?: string[]
}

export type CommandStatus = 'running' | 'completed' | 'timed_out' | 'cancelled' | 'error' | 'interrupted'

export interface CommandOut {
  id: string
  turn: number
  stage: string
  argv: string[]
  cwd: string
  status: CommandStatus
  exit_code: number | null
  duration_ms: number | null
  timed_out: boolean
  truncated: boolean
  sandbox: string
  summary: TestSummary | null
  stdout: string | null
  stderr: string | null
  started_at: string
  finished_at: string | null
}

export interface PatchFile {
  path: string
  change: 'modify' | 'create'
  kind: 'fix' | 'test'
  additions: number
  deletions: number
}

export type PatchStatus = 'proposed' | 'approved' | 'rejected' | 'applied' | 'failed' | 'rolled_back' | 'superseded'

export interface PatchOut {
  id: string
  turn: number
  attempt: number
  status: PatchStatus
  summary: string
  diff: string
  files: PatchFile[]
  warnings: string[]
  created_at: string
  decided_at: string | null
  applied_at: string | null
}

export interface ProposedCommand {
  argv: string[]
  display: string
  reason: string
  trusted: boolean
}

export interface ApprovalPayload {
  title: string
  plan_summary: string
  diff: string
  files: PatchFile[]
  commands: ProposedCommand[]
  warnings: string[]
  sandbox: string
  attempt: number
}

export interface ApprovalOut {
  id: string
  kind: 'patch'
  patch_id: string
  status: 'pending' | 'approved' | 'rejected' | 'expired'
  payload: ApprovalPayload
  created_at: string
  decided_at: string | null
}

export interface ApprovalDecision {
  decision: 'approve' | 'reject'
  note?: string
  commands?: string[][]
}

export interface TaskDetail extends TaskSummary {
  /** Number of the latest conversation turn. */
  turn: number
  repo: RepoSummary
  workspace_path: string | null
  worktree_path: string | null
  base_commit: string | null
  error: string | null
  messages: MessageOut[]
  reports: FinalReport[]
  patches: PatchOut[]
  commands: CommandOut[]
  pending_approval: ApprovalOut | null
  stats: Record<string, number>
}

/** An uncommitted change in a task worktree (LocPilot's own edits, or anything else, e.g. from a terminal). */
export interface WorktreeChangeFile {
  path: string
  status: 'A' | 'M' | 'D'
  locpilot: boolean
}

/** The branch a task's changes were committed to, and whether it was pushed. */
export interface PublishState {
  task_id: string
  branch: string
  head: string
  pushed_head: string | null
  committed_at: string
  pushed_at: string | null
  remote_url: string | null
  pull_request_url: string | null
}

export interface WorktreeChanges {
  base_commit: string | null
  worktree_path: string
  files: WorktreeChangeFile[]
  suggested_branch: string
  suggested_message: string
  publish: PublishState | null
}

export interface CommitResult {
  commit: string
  branch: string
  files: string[]
  publish: PublishState | null
}

export interface ApplyResult {
  applied: string[]
  repo_path: string
}

/** An interactive shell: in a task's worktree (LocPilot's version of the code) or the repository folder. */
export interface TerminalOut {
  id: string
  title: string
  location: 'worktree' | 'repository'
  cwd: string
  shell: string
  repo_id: string
  task_id: string | null
  running: boolean
  exit_code: number | null
  created_at: string
}

export interface DiffFile {
  path: string
  change: 'modify' | 'create' | 'delete'
  original: string | null
  modified: string | null
  unified: string
  binary: boolean
}

export interface TaskDiff {
  base_commit: string | null
  worktree_path: string | null
  files: DiffFile[]
  unified: string
}

export interface RollbackResult {
  restored: string[]
  skipped: { path: string; reason: string }[]
}

// ---------------------------------------------------------------- reports

export interface Citation {
  path: string
  start_line: number
  end_line: number
  evidence_id: string | null
}

export interface FlowStep {
  description: string
  citations: Citation[]
}

export interface Hypothesis {
  title: string
  explanation: string
  confidence: 'high' | 'medium' | 'low'
  supporting: Citation[]
  contradicting: Citation[]
  status: 'supported' | 'weak' | 'unsupported' | 'contradicted'
}

export interface ExaminedFile {
  path: string
  ranges: string[]
  symbols: string[]
  reason: string
}

export interface EvidenceSummary {
  id: string
  path: string
  start_line: number
  end_line: number
  kind: string
  title: string
}

export interface InvestigationReport {
  kind: 'explanation' | 'rca'
  intent: string
  answer_markdown: string
  problem_summary: string
  observed_symptoms: string[]
  execution_flow: FlowStep[]
  files_examined: ExaminedFile[]
  hypotheses: Hypothesis[]
  most_likely_root_cause: string | null
  root_cause_citations: Citation[]
  evidence_sufficient: boolean
  minimal_fix: string
  side_effects: string[]
  verification_steps: string[]
  uncertainties: string[]
  analysis_basis: 'static'
  removed_citations: string[]
  evidence: EvidenceSummary[]
}

export interface PlanOut {
  summary: string
  approach: string
  files_to_modify: { path: string; change: string }[]
  test_file: string | null
  test_description: string
  risks: string[]
  commands: ProposedCommand[]
}

export interface VerificationRun {
  command_id: string
  stage: string
  argv: string[]
  exit_code: number | null
  summary: TestSummary | null
}

export interface Verification {
  tests_run: boolean
  verdict: 'passed' | 'failed' | 'not_run' | 'partial'
  runs: VerificationRun[]
  regression_test: {
    status: 'detected_bug' | 'did_not_fail_before_fix' | 'not_created' | 'unknown'
    detail: string
  }
  preexisting_failures: string[]
  new_failures: string[]
}

export interface ChangeReview {
  answer_markdown: string
  file_notes: { path: string; note: string }[]
  side_effects: string[]
  risks: string[]
}

export type FinalStatus =
  | 'answered'
  | 'needs_input'
  | 'changes_verified'
  | 'changes_unverified'
  | 'changes_failed'
  | 'changes_rejected'
  | 'no_changes'
  | 'failed'
  | 'cancelled'

export interface FinalReport {
  task_id: string
  turn: number
  mode: Mode
  intent: string
  status: FinalStatus
  headline: string
  investigation: InvestigationReport | null
  plan: PlanOut | null
  patches: { id: string; attempt: number; status: string; files: PatchFile[] }[]
  verification: Verification | null
  change_review: ChangeReview | null
  clarifying_question: string | null
  retries: number
  unresolved_issues: string[]
  next_steps: string[]
  stats: {
    llm_calls: number
    prompt_tokens: number
    completion_tokens: number
    tool_calls: number
    duration_ms: number
  }
}

// ---------------------------------------------------------------- events

export type PersistedEventType =
  | 'task_status'
  | 'message'
  | 'phase'
  | 'log'
  | 'tool_call'
  | 'evidence'
  | 'llm'
  | 'report'
  | 'plan'
  | 'patch'
  | 'approval_required'
  | 'approval_resolved'
  | 'command_started'
  | 'command_finished'
  | 'final'
  | 'error'

export type EphemeralEventType = 'llm_progress' | 'command_output'

export interface PhaseData {
  phase: string
  label: string
  state: 'started' | 'completed' | 'skipped' | 'failed'
  detail: string | null
}

export interface EventDataMap {
  task_status: { status: TaskStatus; phase: string | null }
  message: MessageOut
  phase: PhaseData
  log: { level: 'info' | 'warning' | 'error'; text: string }
  tool_call: { tool: string; args: Record<string, unknown>; summary: string; evidence_ids: string[] }
  evidence: { items: EvidenceSummary[] }
  llm: {
    step: string
    state: 'started' | 'completed' | 'failed'
    model: string
    prompt_tokens?: number
    completion_tokens?: number
    duration_ms?: number
  }
  llm_progress: { step: string; tokens: number }
  report: { report: InvestigationReport }
  plan: { plan: PlanOut }
  patch: { patch: PatchOut }
  approval_required: { approval: ApprovalOut }
  approval_resolved: { approval_id: string; decision: 'approve' | 'reject'; commands: string[][] | null }
  command_started: { command: CommandOut }
  command_output: { command_id: string; stream: 'stdout' | 'stderr'; text: string }
  command_finished: { command: CommandOut }
  final: { report: FinalReport }
  error: { message: string; recoverable: boolean }
}

export type TaskEventType = keyof EventDataMap

export type TaskEvent = {
  [K in TaskEventType]: {
    id: number | null
    task_id: string
    turn: number
    type: K
    data: EventDataMap[K]
    created_at: string
  }
}[TaskEventType]

export type EventOf<K extends TaskEventType> = Extract<TaskEvent, { type: K }>
