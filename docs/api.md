# LocPilot Local API Contract

The LocPilot desktop app is a Tauri shell (React UI) plus a bundled Python sidecar
(`locpilot-backend`, FastAPI + LangGraph). The UI talks to the sidecar over HTTP on
`127.0.0.1` only. This document is the contract between the two. FastAPI also serves
interactive OpenAPI docs at `/docs` while the backend is running.

All bodies are JSON. Timestamps are ISO-8601 UTC strings. IDs are opaque strings
unless noted. Errors use one shape:

```json
{ "error": "repo_not_found", "detail": "Human readable message", "hint": "Optional remediation" }
```

## 1. Sidecar process contract

```
locpilot-backend [--host 127.0.0.1] [--port 0] [--data-dir DIR] [--parent-pid PID]
```

| Input | Meaning |
|---|---|
| `--port 0` | Bind an ephemeral port (desktop). Dev default is `8420`. |
| `--data-dir` | Where SQLite DBs, clones, worktrees, logs live. Desktop passes the Tauri app-data dir. Default `~/.locpilot`. |
| `--parent-pid` | Backend exits when this process disappears (prevents orphaned servers). |
| env `LOCPILOT_AUTH_TOKEN` | If set, every `/api/*` request except `GET /api/health` must send `Authorization: Bearer <token>`. Desktop always sets a random per-launch token. |

When the server is listening, it prints exactly one line to **stdout**:

```
LOCPILOT_READY {"port": 51234, "pid": 4242, "version": "0.1.0"}
```

All other logging goes to stderr and `<data-dir>/logs/backend.log`.

## 2. Request security rules (enforced by the backend)

1. Server binds `127.0.0.1` only.
2. `Host` header must be `127.0.0.1`, `localhost` or `[::1]` (any port) — blocks DNS rebinding.
3. If an `Origin` header is present it must be an allowed origin: `http://127.0.0.1:5420`,
   `http://localhost:5420`, `tauri://localhost`, `http://tauri.localhost`, `https://tauri.localhost`
   (configurable via `LOCPILOT_ALLOWED_ORIGINS`).
4. Every non-GET request must send header `X-LocPilot-Client: 1` (forces CORS preflight; blocks form-CSRF).
5. Bearer token as described above when configured.

Frontend rule of thumb: send `X-LocPilot-Client: 1` on every request and `Authorization`
when a token is known. In browser dev mode the Vite dev server proxies `/api` to
`http://127.0.0.1:8420` and injects the token from env `LOCPILOT_DEV_TOKEN` (never exposed
to browser JS). In Tauri, the UI calls the Tauri command `backend_info` to get
`{ status, base_url, token, error }`.

## 3. Endpoints

### Health & setup (first-run dependency screen)

| Method | Path | Response |
|---|---|---|
| GET | `/api/health` | `{ "status": "ok", "version": "0.1.0" }` (no auth required) |
| GET | `/api/setup/status` | `SetupStatus` |
| POST | `/api/setup/pull-model` | body `{ "model": "qwen2.5-coder:7b" }` (optional, defaults to configured) → `PullStatus` |
| GET | `/api/setup/pull-status` | `PullStatus` |

```ts
type CheckStatus = "ok" | "missing" | "error" | "warning";
interface SetupCheck { id: "ollama" | "model" | "git" | "sandbox"; label: string; status: CheckStatus;
  required: boolean; detail: string; hint: string | null }
interface InstallStep { title: string; command: string | null; url: string | null; description: string | null }
interface SetupStatus {
  ready: boolean;                       // all required checks are "ok"
  platform: "macos" | "windows" | "linux" | "other";
  ollama_url: string; model: string; installed_models: string[];
  checks: SetupCheck[];
  ollama_install: InstallStep[];        // OS-specific instructions to install & start Ollama
  model_install: InstallStep[];         // e.g. `ollama pull qwen2.5-coder:7b`
  cloud_fallback: false;                // LocPilot never falls back to cloud APIs
  checked_at: string;
}
interface PullStatus { model: string; state: "idle" | "pulling" | "success" | "error";
  status_text: string; completed: number | null; total: number | null; error: string | null }
```

Pulling a model is always an explicit user action. It asks the **local Ollama daemon** to download
the model; LocPilot itself never downloads models and the installer never bundles them.

### Model

| Method | Path | Response |
|---|---|---|
| GET | `/api/model/status` | `ModelStatus` |
| PUT | `/api/model/config` | body `{ model?: string, num_ctx?: number, temperature?: number }` → `ModelStatus` |

```ts
interface ModelInfo { name: string; size_bytes: number | null; parameter_size: string | null;
  quantization: string | null; family: string | null }
interface ModelStatus { ollama_reachable: boolean; ollama_version: string | null; ollama_url: string;
  configured_model: string; model_available: boolean; models: ModelInfo[]; num_ctx: number;
  temperature: number; error: string | null; hint: string | null }
```

### Repositories

| Method | Path | Notes |
|---|---|---|
| GET | `/api/repos` | `RepoSummary[]`, most recently opened first |
| POST | `/api/repos` | body `{ source: "github", url }` or `{ source: "local", path }` → `RepoDetail` (201). Clone may take a while. |
| GET | `/api/repos/{repo_id}` | `RepoDetail` (live git status) |
| DELETE | `/api/repos/{repo_id}?delete_clone=false` | Unregisters. Only LocPilot-managed clones can be deleted from disk. → `{ "ok": true }` |
| GET | `/api/repos/{repo_id}/tree?path=&task_id=` | `TreeEntry[]` children of a directory (respects .gitignore + exclusions). With `task_id`, lists that task's worktree (includes files LocPilot created). |
| GET | `/api/repos/{repo_id}/file?path=&start=&end=&task_id=` | `FileContent`. With `task_id`, reads from that task's worktree. |
| GET | `/api/repos/{repo_id}/search?q=&regex=false&task_id=` | `SearchHit[]` (max 200); `task_id` searches the task's worktree |

```ts
interface RepoSummary { id: string; name: string; source_type: "github" | "local"; source_url: string | null;
  path: string; managed: boolean; created_at: string; last_opened_at: string }
interface GitStatusFile { path: string; status: string }   // "M", "A", "D", "R", "??", "U"
interface RepoStatus { branch: string | null; head: string | null; head_summary: string | null; detached: boolean;
  upstream: string | null; ahead: number; behind: number; dirty: boolean; staged: number; modified: number;
  untracked: number; conflicted: number; files: GitStatusFile[] }
interface RepoDetail extends RepoSummary { status: RepoStatus; languages: { name: string; files: number }[];
  manifests: string[]; test_commands: string[][]; file_count: number }
interface TreeEntry { name: string; path: string; type: "file" | "dir"; size: number | null }
interface FileContent { path: string; content: string; start_line: number; end_line: number; total_lines: number;
  language: string; truncated: boolean; binary: boolean; sha: string }
interface SearchHit { path: string; line: number; text: string }
```

Local repository rules: absolute path, must exist, must be a Git work tree, must be inside an
allowed root (default: the user's home directory; `LOCPILOT_ALLOWED_REPO_ROOTS`), never the
home directory itself. GitHub URLs: `https://github.com/<owner>/<repo>[.git]` (also gitlab.com,
bitbucket.org) or `git@github.com:<owner>/<repo>.git`. URLs containing credentials are rejected.

### Tasks

| Method | Path | Notes |
|---|---|---|
| GET | `/api/tasks?repo_id=` | `TaskSummary[]` newest first |
| POST | `/api/tasks` | body `{ repo_id, mode: "ask" | "code", message }` → `TaskDetail` (201). Fails with 503 `model_unavailable` if Ollama/model not ready. |
| GET | `/api/tasks/{task_id}` | `TaskDetail` |
| DELETE | `/api/tasks/{task_id}` | Deletes task history and removes LocPilot's worktree for it → `{ ok: true }` |
| POST | `/api/tasks/{task_id}/messages` | body `{ message, mode?: "ask" | "code" }` follow-up turn → `TaskDetail`. 409 if a turn is running. |
| POST | `/api/tasks/{task_id}/cancel` | → `TaskDetail` |
| POST | `/api/tasks/{task_id}/resume` | Resume an `interrupted` task from its last checkpoint → `TaskDetail` |
| POST | `/api/tasks/{task_id}/approvals/{approval_id}` | body `ApprovalDecision` → `TaskDetail` |
| GET | `/api/tasks/{task_id}/events?after=<event_id>` | **SSE** stream (see §4) |
| GET | `/api/tasks/{task_id}/diff` | `TaskDiff` (live worktree vs base commit) |
| GET | `/api/tasks/{task_id}/patch` | `text/x-diff` download of LocPilot's changes |
| GET | `/api/tasks/{task_id}/commands/{command_id}` | `CommandOut` with full stdout/stderr |
| POST | `/api/tasks/{task_id}/rollback` | Restores files LocPilot changed (skips files edited by someone else since) → `RollbackResult` |
| POST | `/api/tasks/{task_id}/commands` | body `{ command, allow_network?: false }` runs **your** command (via `/bin/sh`) in the task worktree, sandboxed → `CommandOut` (202, `stage: "user"`, still running). Output streams as `command_output` events; the finished run is shared with the model on the next message. 400 `no_worktree`, 409 if a turn or another of your commands is running. |
| POST | `/api/tasks/{task_id}/commands/{command_id}/stop` | Stops your running command (process tree killed, output kept) → `CommandOut` (`status: "cancelled"`). 409 if not running. |
| GET | `/api/tasks/{task_id}/worktree-changes` | Uncommitted changes in the worktree `{ files: [{ path, status: "A" \| "M" \| "D", locpilot }], suggested_branch, suggested_message, publish }` |
| POST | `/api/tasks/{task_id}/apply-to-repo` | body `{ files }` writes those changes into your working tree (plain edits; refuses on conflicts: 409 `apply_failed`) → `{ applied, repo_path }` |
| POST | `/api/tasks/{task_id}/commit` | body `{ branch, message, files }` commits the files to `branch` (created, or advanced from LocPilot's last commit; 409 `commit_failed` otherwise) → `{ commit, branch, files, publish }` |
| POST | `/api/tasks/{task_id}/push` | Pushes the task's branch to `origin` with your Git credentials → `{ publish }` (409 `push_failed` with a hint) |

### Terminals (POSIX only)

| Method | Path | Notes |
|---|---|---|
| GET | `/api/terminals?repo_id=&task_id=` | `TerminalOut[]` `{ id, title, location: "worktree" \| "repository", cwd, shell, repo_id, task_id, running, exit_code, created_at }` |
| POST | `/api/terminals` | body `{ repo_id, task_id?, cols?, rows? }` opens your login shell in the task worktree (Code-mode task) or the repository folder → `TerminalOut` (201). 409 `terminal_unavailable` at the limit (`LOCPILOT_MAX_TERMINALS`, 8). |
| GET | `/api/terminals/{id}/stream` | **SSE**: `reset` `{ data: <scrollback> }` first on every connect, then `output` `{ data }`, then `exit` `{ code }` |
| POST | `/api/terminals/{id}/input` | body `{ data }` keystrokes / paste (≤ 64 KB) → 204 |
| POST | `/api/terminals/{id}/resize` | body `{ cols, rows }` → 204 |
| DELETE | `/api/terminals/{id}` | Hangs up the shell and everything attached to it → `{ ok: true }` |

```ts
type Mode = "ask" | "code";
type TaskStatus = "queued" | "running" | "awaiting_approval" | "needs_input" | "completed"
  | "failed" | "cancelled" | "interrupted";
interface TaskSummary { id: string; repo_id: string; title: string; mode: Mode; status: TaskStatus;
  phase: string | null; created_at: string; updated_at: string; model: string | null }
interface MessageOut { id: number; turn: number; role: "user" | "assistant" | "system"; content: string;
  mode: Mode | null; created_at: string }
interface TestSummary { framework: string; outcome: "passed" | "failed" | "error" | "no_tests" | "unknown";
  passed: number | null; failed: number | null; errors: number | null; skipped: number | null;
  total: number | null; failing_tests: string[] }
interface CommandOut { id: string; turn: number; stage: string; argv: string[]; cwd: string;
  status: "running" | "completed" | "timed_out" | "cancelled" | "error" | "interrupted";
  exit_code: number | null; duration_ms: number | null; timed_out: boolean; truncated: boolean;
  sandbox: string; summary: TestSummary | null; stdout: string | null; stderr: string | null;
  started_at: string; finished_at: string | null }
interface PatchFile { path: string; change: "modify" | "create"; kind: "fix" | "test"; additions: number; deletions: number }
interface PatchOut { id: string; turn: number; attempt: number;
  status: "proposed" | "approved" | "rejected" | "applied" | "failed" | "rolled_back" | "superseded";
  summary: string; diff: string; files: PatchFile[]; warnings: string[]; created_at: string;
  decided_at: string | null; applied_at: string | null }
interface ApprovalOut { id: string; kind: "patch"; patch_id: string; status: "pending" | "approved" | "rejected" | "expired";
  payload: ApprovalPayload; created_at: string; decided_at: string | null }
interface ApprovalPayload { title: string; plan_summary: string; diff: string; files: PatchFile[];
  commands: ProposedCommand[]; warnings: string[]; sandbox: string; attempt: number }
interface ProposedCommand { argv: string[]; display: string; reason: string; trusted: boolean }
interface ApprovalDecision { decision: "approve" | "reject"; note?: string;
  commands?: string[][] }  // approve: subset of proposed commands to run ([] = run none)
interface TaskDetail extends TaskSummary { repo: RepoSummary; workspace_path: string | null;
  worktree_path: string | null; base_commit: string | null; error: string | null;
  messages: MessageOut[]; reports: FinalReport[];  // one per completed turn, oldest first
  patches: PatchOut[]; commands: CommandOut[];      // commands without stdout/stderr
  pending_approval: ApprovalOut | null; stats: Record<string, number> }
interface DiffFile { path: string; change: "modify" | "create" | "delete"; original: string | null;
  modified: string | null; unified: string; binary: boolean }
interface TaskDiff { base_commit: string | null; worktree_path: string | null; files: DiffFile[]; unified: string }
interface RollbackResult { restored: string[]; skipped: { path: string; reason: string }[] }
```

### Reports

```ts
interface Citation { path: string; start_line: number; end_line: number; evidence_id: string | null }
interface FlowStep { description: string; citations: Citation[] }
interface Hypothesis { title: string; explanation: string; confidence: "high" | "medium" | "low";
  supporting: Citation[]; contradicting: Citation[];
  status: "supported" | "weak" | "unsupported" | "contradicted" }
interface ExaminedFile { path: string; ranges: string[]; symbols: string[]; reason: string }
interface EvidenceSummary { id: string; path: string; start_line: number; end_line: number; kind: string; title: string }
interface InvestigationReport {
  kind: "explanation" | "rca"; intent: string;
  answer_markdown: string;              // may contain links: [label](locpilot://open?path=..&start=..&end=..)
  problem_summary: string; observed_symptoms: string[];
  execution_flow: FlowStep[]; files_examined: ExaminedFile[];
  hypotheses: Hypothesis[]; most_likely_root_cause: string | null; root_cause_citations: Citation[];
  evidence_sufficient: boolean; minimal_fix: string; side_effects: string[];
  verification_steps: string[]; uncertainties: string[];
  analysis_basis: "static";             // static analysis only; execution facts live in `verification`
  removed_citations: string[];          // model citations that failed validation and were dropped
  evidence: EvidenceSummary[] }
interface PlanOut { summary: string; approach: string; files_to_modify: { path: string; change: string }[];
  test_file: string | null; test_description: string; risks: string[]; commands: ProposedCommand[] }
interface Verification { tests_run: boolean; verdict: "passed" | "failed" | "not_run" | "partial";
  runs: { command_id: string; stage: string; argv: string[]; exit_code: number | null; summary: TestSummary | null }[];
  regression_test: { status: "detected_bug" | "did_not_fail_before_fix" | "not_created" | "unknown"; detail: string };
  preexisting_failures: string[]; new_failures: string[] }
interface ChangeReview { answer_markdown: string; file_notes: { path: string; note: string }[];
  side_effects: string[]; risks: string[] }
interface FinalReport {
  task_id: string; turn: number; mode: Mode; intent: string;
  status: "answered" | "needs_input" | "changes_verified" | "changes_unverified" | "changes_failed"
    | "changes_rejected" | "no_changes" | "failed" | "cancelled";
  headline: string;
  investigation: InvestigationReport | null; plan: PlanOut | null;
  patches: { id: string; attempt: number; status: string; files: PatchFile[] }[];
  verification: Verification | null; change_review: ChangeReview | null;
  clarifying_question: string | null; retries: number;
  unresolved_issues: string[]; next_steps: string[];
  stats: { llm_calls: number; prompt_tokens: number; completion_tokens: number; tool_calls: number; duration_ms: number } }
```

`locpilot://open?path=<repo-relative>&start=<n>&end=<n>` links inside markdown must be
intercepted by the UI and open the file viewer. Every citation has been verified against real
repository content by the backend before it reaches the UI.

## 4. Task event stream (SSE)

`GET /api/tasks/{task_id}/events?after=<last_seen_event_id>` returns `text/event-stream`.
Persisted events carry an `id:` line; the client must remember the last id and reconnect with
`after=` (or `Last-Event-ID`). A comment line `: ping` is sent every 15 s. The stream stays
open across turns until the client disconnects. Because the desktop API needs an
`Authorization` header, clients should read SSE with `fetch()` + a stream reader (not `EventSource`).

```
id: 42
event: phase
data: {"id":42,"task_id":"t_…","turn":1,"type":"phase","data":{…},"created_at":"…"}
```

| type | persisted | data |
|---|---|---|
| `task_status` | yes | `{ status, phase }` |
| `message` | yes | `MessageOut` |
| `phase` | yes | `{ phase, label, state: "started" \| "completed" \| "skipped" \| "failed", detail }` |
| `log` | yes | `{ level: "info" \| "warning" \| "error", text }` |
| `tool_call` | yes | `{ tool, args, summary, evidence_ids: string[] }` |
| `evidence` | yes | `{ items: EvidenceSummary[] }` |
| `llm` | yes | `{ step, state: "started" \| "completed" \| "failed", model, prompt_tokens?, completion_tokens?, duration_ms? }` |
| `llm_progress` | **no** | `{ step, tokens }` |
| `report` | yes | `{ report: InvestigationReport }` |
| `plan` | yes | `{ plan: PlanOut }` |
| `patch` | yes | `{ patch: PatchOut }` |
| `approval_required` | yes | `{ approval: ApprovalOut }` |
| `approval_resolved` | yes | `{ approval_id, decision, commands }` |
| `command_started` | yes | `{ command: CommandOut }` |
| `command_output` | **no** | `{ command_id, stream: "stdout" \| "stderr", text }` |
| `command_finished` | yes | `{ command: CommandOut }` (stdout/stderr tails only, ≤ 4 KB each) |
| `final` | yes | `{ report: FinalReport }` |
| `error` | yes | `{ message, recoverable: boolean }` |

## 5. Phases (for timeline labels)

`understand`, `inspect`, `evidence`, `analyze`, `validate`, `clarify`, `plan`, `patch`,
`approval`, `apply`, `verify`, `diagnose`, `review`, `report`.
