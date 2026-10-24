import { describe, expect, it } from 'vitest'
import { actionableApprovalId, buildTurns } from '@/lib/timeline'
import type { TaskEvent } from '@/lib/types'
import { approval, finalReport, taskDetail } from './fixtures'

let nextId = 1
function ev<T extends TaskEvent['type']>(type: T, data: Extract<TaskEvent, { type: T }>['data'], turn = 1): TaskEvent {
  return { id: nextId++, task_id: 't_1', turn, type, data, created_at: new Date(Date.UTC(2026, 9, 24, 10, 0, nextId)).toISOString() } as TaskEvent
}

describe('buildTurns', () => {
  it('nests tool calls and model calls under the active phase and merges llm start/finish', () => {
    const events: TaskEvent[] = [
      ev('phase', { phase: 'evidence', label: 'Gathering evidence', state: 'started', detail: null }),
      ev('tool_call', { tool: 'search_text', args: { query: 'seen' }, summary: '3 matches', evidence_ids: ['E1'] }),
      ev('llm', { step: 'plan_evidence', state: 'started', model: 'qwen' }),
      ev('llm', { step: 'plan_evidence', state: 'completed', model: 'qwen', prompt_tokens: 100, completion_tokens: 20, duration_ms: 900 }),
      ev('phase', { phase: 'evidence', label: 'Gathering evidence', state: 'completed', detail: '4 snippets' }),
      ev('final', { report: finalReport() }),
    ]
    const [turn] = buildTurns(taskDetail(), events)
    expect(turn.userMessage?.content).toContain('duplicate hotel offers')
    expect(turn.items).toHaveLength(1)
    const step = turn.items[0]
    expect(step.kind).toBe('step')
    if (step.kind !== 'step') throw new Error('expected step')
    expect(step.state).toBe('completed')
    expect(step.detail).toBe('4 snippets')
    expect(step.children.map((c) => c.kind)).toEqual(['tool', 'llm'])
    const llm = step.children[1]
    expect(llm.kind === 'llm' && llm.state).toBe('completed')
    expect(turn.final?.headline).toContain('Root cause')
  })

  it('marks approvals resolved and merges command start/finish into one item', () => {
    const ap = approval()
    const cmd = {
      id: 'c1', turn: 1, stage: 'verification', argv: ['python', '-m', 'pytest'], cwd: '/wt', status: 'running' as const,
      exit_code: null, duration_ms: null, timed_out: false, truncated: false, sandbox: 'seatbelt', summary: null,
      stdout: null, stderr: null, started_at: '2026-10-24T10:00:00Z', finished_at: null,
    }
    const events: TaskEvent[] = [
      ev('approval_required', { approval: ap }),
      ev('approval_resolved', { approval_id: ap.id, decision: 'approve', commands: [['python', '-m', 'pytest']] }),
      ev('command_started', { command: cmd }),
      ev('command_finished', { command: { ...cmd, status: 'completed', exit_code: 0, duration_ms: 1200 } }),
    ]
    const [turn] = buildTurns(taskDetail({ mode: 'code' }), events)
    const kinds = turn.items.map((i) => i.kind)
    expect(kinds).toEqual(['approval', 'command'])
    const a = turn.items[0]
    expect(a.kind === 'approval' && a.resolved?.decision).toBe('approve')
    expect(a.kind === 'approval' && a.approval.status).toBe('approved')
    const c = turn.items[1]
    expect(c.kind === 'command' && c.command.exit_code).toBe(0)
  })

  it('keeps the user\'s own runs out of the turn timeline', () => {
    const cmd = {
      id: 'c_u', turn: 1, stage: 'user', argv: ['python main.py'], cwd: '/wt', status: 'running' as const,
      exit_code: null, duration_ms: null, timed_out: false, truncated: false, sandbox: 'seatbelt', summary: null,
      stdout: null, stderr: null, started_at: '2026-10-24T10:00:00Z', finished_at: null,
    }
    const [turn] = buildTurns(taskDetail({ mode: 'code' }), [
      ev('command_started', { command: cmd }),
      ev('command_finished', { command: { ...cmd, status: 'completed', exit_code: 1 } }),
    ])
    expect(turn.items).toEqual([])
  })

  it('separates turns and flags the newest one as live while running', () => {
    const detail = taskDetail({
      status: 'running',
      messages: [
        { id: 1, turn: 1, role: 'user', content: 'first', mode: 'ask', created_at: '2026-10-24T10:00:00Z' },
        { id: 2, turn: 2, role: 'user', content: 'second', mode: 'code', created_at: '2026-10-24T10:05:00Z' },
      ],
      reports: [finalReport({ turn: 1 })],
    })
    const turns = buildTurns(detail, [])
    expect(turns.map((t) => t.turn)).toEqual([1, 2])
    expect(turns[0].final).not.toBeNull()
    expect(turns[0].live).toBe(false)
    expect(turns[1].live).toBe(true)
  })

  it('only exposes a pending approval as actionable while awaiting approval', () => {
    expect(actionableApprovalId(taskDetail({ status: 'awaiting_approval', pending_approval: approval() }))).toBe('ap_1')
    expect(actionableApprovalId(taskDetail({ status: 'running', pending_approval: approval() }))).toBeNull()
  })
})
