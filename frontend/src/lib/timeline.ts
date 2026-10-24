import { isUserCommand } from './commands'
import type {
  ApprovalOut,
  CommandOut,
  EvidenceSummary,
  FinalReport,
  InvestigationReport,
  MessageOut,
  PatchOut,
  PhaseData,
  PlanOut,
  TaskDetail,
  TaskEvent,
  TaskStatus,
} from './types'

export type StepChild =
  | {
      kind: 'tool'
      key: string
      tool: string
      args: Record<string, unknown>
      summary: string
      evidenceIds: string[]
    }
  | {
      kind: 'llm'
      key: string
      step: string
      state: 'started' | 'completed' | 'failed'
      model: string
      promptTokens?: number
      completionTokens?: number
      durationMs?: number
    }
  | { kind: 'log'; key: string; level: 'info' | 'warning' | 'error'; text: string }
  | { kind: 'evidence'; key: string; items: EvidenceSummary[] }

export interface StepItem {
  kind: 'step'
  key: string
  phase: string
  label: string
  state: PhaseData['state']
  detail: string | null
  children: StepChild[]
  startedAt: string
  endedAt: string | null
}

export type TimelineItem =
  | StepItem
  | { kind: 'report'; key: string; report: InvestigationReport }
  | { kind: 'plan'; key: string; plan: PlanOut }
  | { kind: 'patch'; key: string; patch: PatchOut }
  | {
      kind: 'approval'
      key: string
      approval: ApprovalOut
      resolved: { decision: 'approve' | 'reject'; commands: string[][] | null } | null
    }
  | { kind: 'command'; key: string; command: CommandOut }
  | { kind: 'error'; key: string; message: string; recoverable: boolean }
  | { kind: 'status'; key: string; status: TaskStatus }

export interface Turn {
  turn: number
  userMessage: MessageOut | null
  assistantMessages: MessageOut[]
  items: TimelineItem[]
  final: FinalReport | null
  /** True while this is the newest turn and the task is still executing. */
  live: boolean
  /** True when this is the newest turn and the task is paused waiting for the user. */
  waiting: boolean
}

const NOTICE_STATUSES: TaskStatus[] = ['cancelled', 'interrupted', 'failed']

/**
 * Folds the persisted event log (plus the TaskDetail snapshot) into per-turn timelines.
 * Pure function — safe to memoize on (detail, events).
 */
export function buildTurns(detail: TaskDetail | null | undefined, events: TaskEvent[]): Turn[] {
  const turns = new Map<number, Turn>()
  const getTurn = (n: number): Turn => {
    let t = turns.get(n)
    if (!t) {
      t = { turn: n, userMessage: null, assistantMessages: [], items: [], final: null, live: false, waiting: false }
      turns.set(n, t)
    }
    return t
  }

  const seenMessages = new Set<number>()
  const addMessage = (m: MessageOut) => {
    if (seenMessages.has(m.id)) return
    seenMessages.add(m.id)
    const t = getTurn(m.turn)
    if (m.role === 'user') {
      if (!t.userMessage) t.userMessage = m
    } else if (m.role === 'assistant') {
      t.assistantMessages.push(m)
    }
  }
  for (const m of detail?.messages ?? []) addMessage(m)

  const commandItems = new Map<string, Extract<TimelineItem, { kind: 'command' }>>()
  const approvalItems = new Map<string, Extract<TimelineItem, { kind: 'approval' }>>()
  const patchItems = new Map<string, Extract<TimelineItem, { kind: 'patch' }>>()

  const currentStep = (t: Turn): StepItem | null => {
    for (let i = t.items.length - 1; i >= 0; i--) {
      const it = t.items[i]
      if (it.kind === 'step') return it
    }
    return null
  }

  const ensureStep = (t: Turn, ev: TaskEvent): StepItem => {
    const step = currentStep(t)
    if (step) return step
    const created: StepItem = {
      kind: 'step',
      key: `step-${ev.id ?? ev.created_at}`,
      phase: 'working',
      label: 'Working',
      state: 'started',
      detail: null,
      children: [],
      startedAt: ev.created_at,
      endedAt: null,
    }
    t.items.push(created)
    return created
  }

  for (const ev of events) {
    const t = getTurn(ev.turn)
    const key = `${ev.type}-${ev.id ?? ev.created_at}`
    switch (ev.type) {
      case 'message':
        addMessage(ev.data)
        break
      case 'phase': {
        const d = ev.data
        let existing: StepItem | null = null
        for (let i = t.items.length - 1; i >= 0; i--) {
          const it = t.items[i]
          if (it.kind === 'step' && it.phase === d.phase) {
            existing = it
            break
          }
        }
        if (d.state === 'started' && (!existing || existing.state !== 'started')) {
          t.items.push({
            kind: 'step',
            key,
            phase: d.phase,
            label: d.label || d.phase,
            state: 'started',
            detail: d.detail,
            children: [],
            startedAt: ev.created_at,
            endedAt: null,
          })
        } else if (existing) {
          existing.state = d.state
          if (d.detail) existing.detail = d.detail
          if (d.label) existing.label = d.label
          if (d.state !== 'started') existing.endedAt = ev.created_at
        } else {
          t.items.push({
            kind: 'step',
            key,
            phase: d.phase,
            label: d.label || d.phase,
            state: d.state,
            detail: d.detail,
            children: [],
            startedAt: ev.created_at,
            endedAt: ev.created_at,
          })
        }
        break
      }
      case 'tool_call':
        ensureStep(t, ev).children.push({
          kind: 'tool',
          key,
          tool: ev.data.tool,
          args: ev.data.args ?? {},
          summary: ev.data.summary,
          evidenceIds: ev.data.evidence_ids ?? [],
        })
        break
      case 'llm': {
        const step = ensureStep(t, ev)
        const d = ev.data
        let prev: Extract<StepChild, { kind: 'llm' }> | null = null
        for (let i = step.children.length - 1; i >= 0; i--) {
          const c = step.children[i]
          if (c.kind === 'llm' && c.step === d.step && c.state === 'started') {
            prev = c
            break
          }
        }
        if (prev && d.state !== 'started') {
          prev.state = d.state
          prev.promptTokens = d.prompt_tokens
          prev.completionTokens = d.completion_tokens
          prev.durationMs = d.duration_ms
          prev.model = d.model || prev.model
        } else {
          step.children.push({
            kind: 'llm',
            key,
            step: d.step,
            state: d.state,
            model: d.model,
            promptTokens: d.prompt_tokens,
            completionTokens: d.completion_tokens,
            durationMs: d.duration_ms,
          })
        }
        break
      }
      case 'log':
        ensureStep(t, ev).children.push({ kind: 'log', key, level: ev.data.level, text: ev.data.text })
        break
      case 'evidence':
        if (ev.data.items?.length) ensureStep(t, ev).children.push({ kind: 'evidence', key, items: ev.data.items })
        break
      case 'report':
        t.items.push({ kind: 'report', key, report: ev.data.report })
        break
      case 'plan':
        t.items.push({ kind: 'plan', key, plan: ev.data.plan })
        break
      case 'patch': {
        const existing = patchItems.get(ev.data.patch.id)
        if (existing) existing.patch = ev.data.patch
        else {
          const item = { kind: 'patch' as const, key, patch: ev.data.patch }
          patchItems.set(ev.data.patch.id, item)
          t.items.push(item)
        }
        break
      }
      case 'approval_required': {
        const item = { kind: 'approval' as const, key, approval: ev.data.approval, resolved: null }
        approvalItems.set(ev.data.approval.id, item)
        t.items.push(item)
        break
      }
      case 'approval_resolved': {
        const item = approvalItems.get(ev.data.approval_id)
        if (item) {
          item.resolved = { decision: ev.data.decision, commands: ev.data.commands }
          item.approval = {
            ...item.approval,
            status: ev.data.decision === 'approve' ? 'approved' : 'rejected',
            decided_at: ev.created_at,
          }
        }
        break
      }
      case 'command_started':
      case 'command_finished': {
        const cmd = ev.data.command
        if (isUserCommand(cmd)) break // the user's own runs live in the Commands tab, not in a turn
        const existing = commandItems.get(cmd.id)
        if (existing) existing.command = { ...existing.command, ...cmd }
        else {
          const item = { kind: 'command' as const, key: `command-${cmd.id}`, command: cmd }
          commandItems.set(cmd.id, item)
          t.items.push(item)
        }
        break
      }
      case 'final':
        t.final = ev.data.report
        break
      case 'error':
        t.items.push({ kind: 'error', key, message: ev.data.message, recoverable: ev.data.recoverable })
        break
      case 'task_status':
        if (NOTICE_STATUSES.includes(ev.data.status)) t.items.push({ kind: 'status', key, status: ev.data.status })
        break
      default:
        break
    }
  }

  // Reports recorded on the task are authoritative for completed turns.
  for (const r of detail?.reports ?? []) {
    const t = getTurn(r.turn)
    if (!t.final) t.final = r
  }

  const ordered = [...turns.values()].sort((a, b) => a.turn - b.turn)
  const status = detail?.status
  if (ordered.length > 0 && (status === 'running' || status === 'queued')) {
    ordered[ordered.length - 1].live = true
  }
  if (ordered.length > 0 && (status === 'awaiting_approval' || status === 'needs_input')) {
    ordered[ordered.length - 1].waiting = true
  }
  return ordered
}

/** Pending approval that the user can act on right now, if any. */
export function actionableApprovalId(detail: TaskDetail | null | undefined): string | null {
  if (!detail || detail.status !== 'awaiting_approval') return null
  return detail.pending_approval?.status === 'pending' ? detail.pending_approval.id : null
}
