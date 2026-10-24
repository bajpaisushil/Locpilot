import { useQueryClient } from '@tanstack/react-query'
import { useEffect } from 'react'
import { qk } from '@/lib/queries'
import { streamTaskEvents } from '@/lib/sse'
import type { TaskEvent, TaskEventType } from '@/lib/types'
import { useEventsStore } from '@/store/events'

// Events after which the TaskDetail snapshot (status, patches, commands, approvals) is stale.
const REFRESH_TASK: ReadonlySet<TaskEventType> = new Set([
  'task_status',
  'message',
  'plan',
  'patch',
  'approval_required',
  'approval_resolved',
  'command_started',
  'command_finished',
  'final',
  'error',
])
const REFRESH_DIFF: ReadonlySet<TaskEventType> = new Set(['patch', 'approval_resolved', 'command_finished', 'final'])

/**
 * Keeps one live SSE subscription for the selected task. Replays persisted events from the last
 * id we have (so history is restored after reloads/restarts) and batches store updates.
 */
export function useTaskStream(taskId: string | null) {
  const qc = useQueryClient()
  useEffect(() => {
    if (!taskId) return
    const after = useEventsStore.getState().byTask[taskId]?.lastId ?? 0
    let pending: TaskEvent[] = []
    let timer: ReturnType<typeof setTimeout> | null = null
    let refreshTask = false
    let refreshDiff = false

    const flush = () => {
      timer = null
      if (pending.length) {
        useEventsStore.getState().ingest(taskId, pending)
        pending = []
      }
      if (refreshTask) {
        refreshTask = false
        void qc.invalidateQueries({ queryKey: qk.task(taskId) })
        void qc.invalidateQueries({ queryKey: ['tasks'] })
      }
      if (refreshDiff) {
        refreshDiff = false
        void qc.invalidateQueries({ queryKey: qk.diff(taskId) })
        void qc.invalidateQueries({ queryKey: qk.worktreeChanges(taskId) })
      }
    }

    const handle = streamTaskEvents({
      taskId,
      after,
      onEvent: (ev) => {
        pending.push(ev)
        if (REFRESH_TASK.has(ev.type)) refreshTask = true
        if (REFRESH_DIFF.has(ev.type)) refreshDiff = true
        if (timer === null) timer = setTimeout(flush, 80)
      },
      onState: (state, info) => useEventsStore.getState().setConnection(taskId, state, info),
    })

    return () => {
      handle.close()
      if (timer !== null) clearTimeout(timer)
      flush()
    }
  }, [taskId, qc])
}
