import type {
  ApplyResult,
  ApprovalDecision,
  CommandOut,
  CommitResult,
  FileContent,
  HealthResponse,
  ModelConfigUpdate,
  ModelStatus,
  Mode,
  PullStatus,
  RepoCreate,
  RepoDetail,
  RepoSummary,
  RollbackResult,
  SearchHit,
  SetupStatus,
  TaskDetail,
  TaskDiff,
  TaskSummary,
  TerminalOut,
  TreeEntry,
  WorktreeChanges,
  PublishState,
} from './types'

/**
 * Where the backend lives and how to authenticate.
 * - Browser dev mode: same origin (Vite proxies /api and injects the token server-side).
 * - Desktop (Tauri): http://127.0.0.1:<ephemeral port> + per-launch bearer token from `backend_info`.
 */
export interface BackendConnection {
  baseUrl: string
  token: string | null
}

let connection: BackendConnection = { baseUrl: '', token: null }

export function setConnection(next: BackendConnection) {
  connection = { baseUrl: next.baseUrl.replace(/\/+$/, ''), token: next.token }
}

export function getConnection(): BackendConnection {
  return connection
}

export function authHeaders(extra?: Record<string, string>): Record<string, string> {
  const headers: Record<string, string> = { 'X-LocPilot-Client': '1', ...extra }
  if (connection.token) headers.Authorization = `Bearer ${connection.token}`
  return headers
}

export class ApiError extends Error {
  readonly status: number
  readonly code: string
  readonly detail: string
  readonly hint: string | null

  constructor(status: number, code: string, detail: string, hint: string | null = null) {
    super(detail)
    this.name = 'ApiError'
    this.status = status
    this.code = code
    this.detail = detail
    this.hint = hint
  }
}

/** Normalizes the documented `{error, detail, hint}` shape and FastAPI's default `{detail}` shapes. */
export async function toApiError(res: Response): Promise<ApiError> {
  let body: unknown = null
  try {
    body = await res.json()
  } catch {
    // non-JSON error body
  }
  if (body && typeof body === 'object') {
    const b = body as Record<string, unknown>
    if (typeof b.error === 'string') {
      return new ApiError(
        res.status,
        b.error,
        typeof b.detail === 'string' && b.detail ? b.detail : b.error,
        typeof b.hint === 'string' ? b.hint : null,
      )
    }
    if (typeof b.detail === 'string') return new ApiError(res.status, `http_${res.status}`, b.detail)
    if (Array.isArray(b.detail)) {
      const msg = b.detail
        .map((d) => {
          const item = d as { loc?: unknown[]; msg?: string }
          const loc = Array.isArray(item.loc) ? item.loc.filter((p) => p !== 'body').join('.') : ''
          return loc ? `${loc}: ${item.msg ?? 'invalid'}` : (item.msg ?? 'invalid')
        })
        .join('; ')
      return new ApiError(res.status, 'validation_error', msg || 'Invalid request')
    }
  }
  return new ApiError(res.status, `http_${res.status}`, res.statusText || `Request failed (${res.status})`)
}

export function errorMessage(err: unknown): string {
  if (err instanceof ApiError) return err.detail
  if (err instanceof Error) return err.message
  return String(err)
}

type Method = 'GET' | 'POST' | 'PUT' | 'DELETE'

async function request<T>(method: Method, path: string, body?: unknown, signal?: AbortSignal): Promise<T> {
  const headers = authHeaders(body !== undefined ? { 'Content-Type': 'application/json' } : undefined)
  let res: Response
  try {
    res = await fetch(`${connection.baseUrl}${path}`, {
      method,
      headers,
      body: body !== undefined ? JSON.stringify(body) : undefined,
      signal,
    })
  } catch (err) {
    if (err instanceof DOMException && err.name === 'AbortError') throw err
    throw new ApiError(
      0,
      'backend_unreachable',
      'Cannot reach the LocPilot backend.',
      'Make sure the local backend is running (desktop: restart LocPilot; dev: ./scripts/dev.sh).',
    )
  }
  if (!res.ok) throw await toApiError(res)
  if (res.status === 204) return undefined as T
  return (await res.json()) as T
}

const q = (params: Record<string, string | number | boolean | null | undefined>) => {
  const sp = new URLSearchParams()
  for (const [k, v] of Object.entries(params)) {
    if (v === undefined || v === null || v === '') continue
    sp.set(k, String(v))
  }
  const s = sp.toString()
  return s ? `?${s}` : ''
}

const enc = encodeURIComponent

export const api = {
  health: () => request<HealthResponse>('GET', '/api/health'),

  setupStatus: () => request<SetupStatus>('GET', '/api/setup/status'),
  pullModel: (model?: string) => request<PullStatus>('POST', '/api/setup/pull-model', model ? { model } : {}),
  pullStatus: () => request<PullStatus>('GET', '/api/setup/pull-status'),

  modelStatus: () => request<ModelStatus>('GET', '/api/model/status'),
  updateModelConfig: (update: ModelConfigUpdate) => request<ModelStatus>('PUT', '/api/model/config', update),

  listRepos: () => request<RepoSummary[]>('GET', '/api/repos'),
  createRepo: (body: RepoCreate) => request<RepoDetail>('POST', '/api/repos', body),
  getRepo: (id: string) => request<RepoDetail>('GET', `/api/repos/${enc(id)}`),
  deleteRepo: (id: string, deleteClone = false) =>
    request<{ ok: boolean }>('DELETE', `/api/repos/${enc(id)}${q({ delete_clone: deleteClone })}`),
  tree: (id: string, path = '', taskId: string | null = null) =>
    request<TreeEntry[]>('GET', `/api/repos/${enc(id)}/tree${q({ path, task_id: taskId })}`),
  file: (id: string, path: string, opts: { start?: number; end?: number; taskId?: string | null } = {}) =>
    request<FileContent>(
      'GET',
      `/api/repos/${enc(id)}/file${q({ path, start: opts.start, end: opts.end, task_id: opts.taskId })}`,
    ),
  /** Saves an edited file (refused with 409 `file_changed` if it changed on disk since it was loaded). */
  saveFile: (id: string, body: { path: string; content: string; expected_sha: string; task_id: string | null }) =>
    request<{ path: string; sha: string; size: number }>('PUT', `/api/repos/${enc(id)}/file`, body),
  search: (id: string, query: string, regex = false) =>
    request<SearchHit[]>('GET', `/api/repos/${enc(id)}/search${q({ q: query, regex })}`),

  listTasks: (repoId?: string) => request<TaskSummary[]>('GET', `/api/tasks${q({ repo_id: repoId })}`),
  createTask: (body: { repo_id: string; mode: Mode; message: string }) =>
    request<TaskDetail>('POST', '/api/tasks', body),
  getTask: (id: string) => request<TaskDetail>('GET', `/api/tasks/${enc(id)}`),
  deleteTask: (id: string) => request<{ ok: boolean }>('DELETE', `/api/tasks/${enc(id)}`),
  sendMessage: (id: string, message: string, mode?: Mode) =>
    request<TaskDetail>('POST', `/api/tasks/${enc(id)}/messages`, mode ? { message, mode } : { message }),
  cancelTask: (id: string) => request<TaskDetail>('POST', `/api/tasks/${enc(id)}/cancel`, {}),
  resumeTask: (id: string) => request<TaskDetail>('POST', `/api/tasks/${enc(id)}/resume`, {}),
  decideApproval: (taskId: string, approvalId: string, decision: ApprovalDecision) =>
    request<TaskDetail>('POST', `/api/tasks/${enc(taskId)}/approvals/${enc(approvalId)}`, decision),
  taskDiff: (id: string) => request<TaskDiff>('GET', `/api/tasks/${enc(id)}/diff`),
  command: (taskId: string, commandId: string) =>
    request<CommandOut>('GET', `/api/tasks/${enc(taskId)}/commands/${enc(commandId)}`),
  /** Runs a command the user typed in the task worktree; returns the running command record. */
  runCommand: (taskId: string, command: string, allowNetwork: boolean) =>
    request<CommandOut>('POST', `/api/tasks/${enc(taskId)}/commands`, { command, allow_network: allowNetwork }),
  stopCommand: (taskId: string, commandId: string) =>
    request<CommandOut>('POST', `/api/tasks/${enc(taskId)}/commands/${enc(commandId)}/stop`, {}),
  rollback: (id: string) => request<RollbackResult>('POST', `/api/tasks/${enc(id)}/rollback`, {}),

  worktreeChanges: (id: string) => request<WorktreeChanges>('GET', `/api/tasks/${enc(id)}/worktree-changes`),
  /** Writes the selected changes into the user's own working tree (plain file edits). */
  applyToRepo: (id: string, files: string[]) =>
    request<ApplyResult>('POST', `/api/tasks/${enc(id)}/apply-to-repo`, { files }),
  commit: (id: string, body: { branch: string; message: string; files: string[] }) =>
    request<CommitResult>('POST', `/api/tasks/${enc(id)}/commit`, body),
  push: (id: string) => request<{ publish: PublishState }>('POST', `/api/tasks/${enc(id)}/push`, {}),

  listTerminals: (repoId: string) => request<TerminalOut[]>('GET', `/api/terminals${q({ repo_id: repoId })}`),
  openTerminal: (body: { repo_id: string; task_id?: string | null; cols: number; rows: number }) =>
    request<TerminalOut>('POST', '/api/terminals', body),
  terminalInput: (id: string, data: string) => request<void>('POST', `/api/terminals/${enc(id)}/input`, { data }),
  resizeTerminal: (id: string, cols: number, rows: number) =>
    request<void>('POST', `/api/terminals/${enc(id)}/resize`, { cols, rows }),
  closeTerminal: (id: string) => request<{ ok: boolean }>('DELETE', `/api/terminals/${enc(id)}`),

  /** Fetches the LocPilot patch as text (auth headers can't be attached to a plain link). */
  async patchText(id: string): Promise<string> {
    let res: Response
    try {
      res = await fetch(`${connection.baseUrl}/api/tasks/${enc(id)}/patch`, { headers: authHeaders() })
    } catch {
      throw new ApiError(0, 'backend_unreachable', 'Cannot reach the LocPilot backend.')
    }
    if (!res.ok) throw await toApiError(res)
    return res.text()
  },
}

export type Api = typeof api
