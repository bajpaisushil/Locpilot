import { keepPreviousData, useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from './api'
import type { Mode, RepoCreate, RepoDetail, RepoSummary, TaskDetail, TaskStatus } from './types'

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

export function useTaskAction(action: 'cancel' | 'resume') {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (taskId: string) => (action === 'cancel' ? api.cancelTask(taskId) : api.resumeTask(taskId)),
    onSuccess: (task) => storeTask(qc, task),
  })
}
