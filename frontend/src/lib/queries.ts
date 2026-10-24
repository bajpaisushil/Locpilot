import { keepPreviousData, useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from './api'
import type { CommandOut, Mode, RepoCreate, RepoDetail, RepoSummary, TaskDetail, TaskStatus } from './types'

export const qk = {
  setup: ['setup'] as const,
  pull: ['setup', 'pull'] as const,
  model: ['model'] as const,
  repos: ['repos'] as const,
  repo: (id: string) => ['repos', id] as const,
  tree: (id: string, path: string, taskId: string | null = null) => ['repos', id, 'tree', path, taskId] as const,
  file: (id: string, path: string, taskId: string | null) => ['repos', id, 'file', path, taskId] as const,
  tasks: (repoId: string | null) => ['tasks', repoId] as const,
  task: (id: string) => ['task', id] as const,
  diff: (id: string) => ['task', id, 'diff'] as const,
  command: (taskId: string, commandId: string) => ['task', taskId, 'command', commandId] as const,
  worktreeChanges: (id: string) => ['task', id, 'worktree-changes'] as const,
  terminals: (repoId: string) => ['terminals', repoId] as const,
}

export const ACTIVE_STATUSES: TaskStatus[] = ['queued', 'running']

export function isActive(status: TaskStatus | null | undefined): boolean {
  return !!status && ACTIVE_STATUSES.includes(status)
}

export function useSetupStatus(enabled = true) {
  return useQuery({
    queryKey: qk.setup,
    queryFn: api.setupStatus,
    enabled,
    // Live readiness: keep polling until everything required is ready.
    refetchInterval: (query) => (query.state.data?.ready ? 30_000 : 3_000),
    refetchOnWindowFocus: true,
  })
}

export function useModelStatus() {
  return useQuery({
    queryKey: qk.model,
    queryFn: api.modelStatus,
    refetchInterval: (query) => (query.state.data?.model_available ? 30_000 : 5_000),
  })
}

export function useRepos() {
  return useQuery({ queryKey: qk.repos, queryFn: api.listRepos })
}

export function useRepo(id: string | null) {
  return useQuery({
    queryKey: qk.repo(id ?? ''),
    queryFn: () => api.getRepo(id as string),
    enabled: !!id,
    refetchInterval: 20_000,
    placeholderData: keepPreviousData,
  })
}

export function useTasks(repoId: string | null) {
  return useQuery({
    queryKey: qk.tasks(repoId),
    queryFn: () => api.listTasks(repoId ?? undefined),
    enabled: !!repoId,
    refetchInterval: (query) => (query.state.data?.some((t) => isActive(t.status)) ? 4_000 : 30_000),
  })
}

export function useTask(id: string | null) {
  return useQuery({
    queryKey: qk.task(id ?? ''),
    queryFn: () => api.getTask(id as string),
    enabled: !!id,
    refetchInterval: (query) => (isActive(query.state.data?.status) ? 5_000 : false),
  })
}

export function useTaskDiff(id: string | null, enabled = true) {
  return useQuery({
    queryKey: qk.diff(id ?? ''),
    queryFn: () => api.taskDiff(id as string),
    enabled: !!id && enabled,
  })
}

function toSummary(repo: RepoDetail): RepoSummary {
  const { id, name, source_type, source_url, path, managed, created_at, last_opened_at } = repo
  return { id, name, source_type, source_url, path, managed, created_at, last_opened_at }
}

export function useCreateRepo() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (body: RepoCreate) => api.createRepo(body),
    onSuccess: (repo) => {
      qc.setQueryData(qk.repo(repo.id), repo)
      // Make the new repo visible immediately so selection logic never sees a stale list.
      qc.setQueryData<RepoSummary[]>(qk.repos, (old) => [toSummary(repo), ...(old ?? []).filter((r) => r.id !== repo.id)])
      void qc.invalidateQueries({ queryKey: qk.repos })
    },
  })
}

function storeTask(qc: ReturnType<typeof useQueryClient>, task: TaskDetail) {
  qc.setQueryData(qk.task(task.id), task)
  void qc.invalidateQueries({ queryKey: qk.tasks(task.repo_id) })
}

export function useCreateTask() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (body: { repo_id: string; mode: Mode; message: string }) => api.createTask(body),
    onSuccess: (task) => storeTask(qc, task),
  })
}

export function useSendMessage() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (v: { taskId: string; message: string; mode?: Mode }) => api.sendMessage(v.taskId, v.message, v.mode),
    onSuccess: (task) => storeTask(qc, task),
  })
}

/** Uncommitted changes in a task worktree; polled, since terminal edits emit no events. */
export function useWorktreeChanges(id: string | null, enabled = true) {
  return useQuery({
    queryKey: qk.worktreeChanges(id ?? ''),
    queryFn: () => api.worktreeChanges(id as string),
    enabled: !!id && enabled,
    refetchInterval: 10_000,
  })
}

function invalidateChanges(qc: ReturnType<typeof useQueryClient>, taskId: string) {
  void qc.invalidateQueries({ queryKey: qk.worktreeChanges(taskId) })
  void qc.invalidateQueries({ queryKey: qk.diff(taskId) })
  void qc.invalidateQueries({ queryKey: qk.task(taskId) })
}

export function useCommit() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (v: { taskId: string; branch: string; message: string; files: string[] }) =>
      api.commit(v.taskId, { branch: v.branch, message: v.message, files: v.files }),
    onSuccess: (_r, v) => invalidateChanges(qc, v.taskId),
  })
}

export function usePush() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (taskId: string) => api.push(taskId),
    onSuccess: (_r, taskId) => invalidateChanges(qc, taskId),
  })
}

export function useApplyToRepo() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (v: { taskId: string; files: string[] }) => api.applyToRepo(v.taskId, v.files),
    onSuccess: (_r, v) => {
      invalidateChanges(qc, v.taskId)
      void qc.invalidateQueries({ queryKey: ['repos'] })
    },
  })
}

export function useTerminals(repoId: string | null) {
  return useQuery({
    queryKey: qk.terminals(repoId ?? ''),
    queryFn: () => api.listTerminals(repoId as string),
    enabled: !!repoId,
    refetchInterval: 15_000,
  })
}

export function useRunCommand() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (v: { taskId: string; command: string; allowNetwork: boolean }) =>
      api.runCommand(v.taskId, v.command, v.allowNetwork),
    onSuccess: (command: CommandOut, v) => {
      // Show the running command immediately; the event stream streams its output and final state.
      qc.setQueryData<TaskDetail>(qk.task(v.taskId), (old) =>
        old ? { ...old, commands: [...old.commands.filter((c) => c.id !== command.id), command] } : old,
      )
      void qc.invalidateQueries({ queryKey: qk.task(v.taskId) })
    },
  })
}

export function useStopCommand() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (v: { taskId: string; commandId: string }) => api.stopCommand(v.taskId, v.commandId),
    onSuccess: (_command, v) => {
      void qc.invalidateQueries({ queryKey: qk.task(v.taskId) })
      void qc.invalidateQueries({ queryKey: qk.command(v.taskId, v.commandId) })
    },
  })
}

export function useTaskAction(action: 'cancel' | 'resume') {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (taskId: string) => (action === 'cancel' ? api.cancelTask(taskId) : api.resumeTask(taskId)),
    onSuccess: (task) => storeTask(qc, task),
  })
}
