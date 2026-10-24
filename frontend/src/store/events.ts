import { create } from 'zustand'
import type { StreamState } from '@/lib/sse'
import type { TaskEvent } from '@/lib/types'

const MAX_LIVE_OUTPUT = 64 * 1024

export interface TaskEventState {
  /** Persisted events in id order (deduplicated). */
  events: TaskEvent[]
  lastId: number
  /** Live token counters from `llm_progress`, keyed by `${turn}:${step}`. */
  llmTokens: Record<string, number>
  /** Live command output from `command_output`, keyed by command id. */
  commandOutput: Record<string, string>
  connection: StreamState
  connectionInfo: string | null
}

const empty = (): TaskEventState => ({
  events: [],
  lastId: 0,
  llmTokens: {},
  commandOutput: {},
  connection: 'connecting',
  connectionInfo: null,
})

interface EventsStore {
  byTask: Record<string, TaskEventState>
  ingest: (taskId: string, events: TaskEvent[]) => void
  setConnection: (taskId: string, state: StreamState, info?: string) => void
  reset: (taskId: string) => void
}

export const useEventsStore = create<EventsStore>()((set) => ({
  byTask: {},
  ingest: (taskId, incoming) =>
    set((s) => {
      const prev = s.byTask[taskId] ?? empty()
      let events = prev.events
      let lastId = prev.lastId
      let llmTokens = prev.llmTokens
      let commandOutput = prev.commandOutput
      let changed = false
      for (const ev of incoming) {
        if (ev.type === 'llm_progress') {
          llmTokens = { ...llmTokens, [`${ev.turn}:${ev.data.step}`]: ev.data.tokens }
          changed = true
          continue
        }
        if (ev.type === 'command_output') {
          const before = commandOutput[ev.data.command_id] ?? ''
          let next = before + ev.data.text
          if (next.length > MAX_LIVE_OUTPUT) next = next.slice(next.length - MAX_LIVE_OUTPUT)
          commandOutput = { ...commandOutput, [ev.data.command_id]: next }
          changed = true
          continue
        }
        if (ev.id === null || ev.id === undefined) {
          // Unknown ephemeral event: keep it renderable but don't advance the cursor.
          events = [...events, ev]
          changed = true
          continue
        }
        if (ev.id <= lastId) continue
        events = events === prev.events ? [...events, ev] : (events.push(ev), events)
        lastId = ev.id
        changed = true
      }
      if (!changed) return s
      return {
        byTask: { ...s.byTask, [taskId]: { ...prev, events, lastId, llmTokens, commandOutput } },
      }
    }),
  setConnection: (taskId, state, info) =>
    set((s) => {
      const prev = s.byTask[taskId] ?? empty()
      if (prev.connection === state && prev.connectionInfo === (info ?? null)) return s
      return { byTask: { ...s.byTask, [taskId]: { ...prev, connection: state, connectionInfo: info ?? null } } }
    }),
  reset: (taskId) =>
    set((s) => {
      const next = { ...s.byTask }
      delete next[taskId]
      return { byTask: next }
    }),
}))

const EMPTY = empty()

export function useTaskEvents(taskId: string | null): TaskEventState {
  return useEventsStore((s) => (taskId ? (s.byTask[taskId] ?? EMPTY) : EMPTY))
}
