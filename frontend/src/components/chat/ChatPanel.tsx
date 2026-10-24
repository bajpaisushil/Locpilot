import {
  BugIcon,
  FlaskConicalIcon,
  FolderGit2Icon,
  GitBranchPlusIcon,
  GitForkIcon,
  MessageSquareTextIcon,
  PlayIcon,
  RouteIcon,
  ShieldCheckIcon,
  WrenchIcon,
} from 'lucide-react'
import { useEffect, useMemo, useRef } from 'react'
import { CitationContext } from '@/components/chat/citation-context'
import { Composer } from '@/components/chat/Composer'
import { TurnView } from '@/components/chat/TurnView'
import { Logo } from '@/components/common/Logo'
import { ErrorState, LoadingState } from '@/components/common/States'
import { Button } from '@/components/ui/button'
import { ApiError } from '@/lib/api'
import { useRepo, useTask, useTaskAction } from '@/lib/queries'
import { actionableApprovalId, buildTurns } from '@/lib/timeline'
import type { Mode, TaskDetail } from '@/lib/types'
import { useTaskEvents } from '@/store/events'
import { useUi } from '@/store/ui'

const EXAMPLES: { mode: Mode; icon: typeof BugIcon; text: string }[] = [
  { mode: 'ask', icon: RouteIcon, text: 'Explain how authentication works in this repository.' },
  { mode: 'ask', icon: BugIcon, text: 'Find the root cause of duplicate hotel offers.' },
  { mode: 'ask', icon: MessageSquareTextIcon, text: 'Explain this function and its callers step by step: ' },
  { mode: 'ask', icon: BugIcon, text: 'Investigate why this API returns a 500 error: ' },
  { mode: 'code', icon: WrenchIcon, text: 'Fix the bug with the smallest possible change and add a regression test.' },
  { mode: 'code', icon: FlaskConicalIcon, text: 'Add regression tests for this issue: ' },
]

function NoRepoWelcome() {
  const setAddRepoOpen = useUi((s) => s.setAddRepoOpen)
  return (
    <div className="flex h-full items-center justify-center p-8">
      <div className="max-w-xl space-y-6 text-center">
        <Logo className="mx-auto size-12" />
        <div className="space-y-2">
          <h1 className="text-xl font-semibold tracking-tight">Connect a repository to get started</h1>
          <p className="text-sm text-muted-foreground">
            LocPilot investigates bugs, explains code with evidence, and proposes verified fixes — entirely on your
            machine with a local model.
          </p>
        </div>
        <div className="flex flex-wrap justify-center gap-2">
          <Button onClick={() => setAddRepoOpen(true)}>
            <FolderGit2Icon />
            Open local repository
          </Button>
          <Button variant="outline" onClick={() => setAddRepoOpen(true)}>
            <GitForkIcon />
            Clone from GitHub
          </Button>
        </div>
        <div className="grid gap-3 text-left sm:grid-cols-2">
          <div className="rounded-lg border bg-card p-3">
            <div className="mb-1 flex items-center gap-1.5 text-xs font-semibold">
              <MessageSquareTextIcon className="size-3.5 text-primary" /> Ask mode
            </div>
            <p className="text-xs text-muted-foreground">
              Read-only. Searches and reads code, traces callers, and reports evidence-backed findings with line references.
            </p>
          </div>
          <div className="rounded-lg border bg-card p-3">
            <div className="mb-1 flex items-center gap-1.5 text-xs font-semibold">
              <GitBranchPlusIcon className="size-3.5 text-primary" /> Code mode
            </div>
            <p className="text-xs text-muted-foreground">
              Works in an isolated Git worktree. You review every diff and command before anything is applied or run.
            </p>
          </div>
        </div>
      </div>
    </div>
  )
}

function NewTaskEmpty({ repoName }: { repoName: string }) {
  const setDraft = useUi((s) => s.setComposerDraft)
  const setMode = useUi((s) => s.setComposerMode)
  return (
    <div className="flex h-full items-center justify-center p-8">
      <div className="w-full max-w-2xl space-y-5">
        <div className="space-y-1.5 text-center">
          <h2 className="text-lg font-semibold tracking-tight">What should we look into in {repoName}?</h2>
          <p className="text-sm text-muted-foreground">
            Describe a bug, ask how something works, or request a change. Answers cite real files and lines.
          </p>
        </div>
        <div className="grid gap-2 sm:grid-cols-2">
          {EXAMPLES.map((ex) => {
            const Icon = ex.icon
            return (
              <button
                key={ex.text}
                type="button"
                onClick={() => {
                  setMode(ex.mode)
                  setDraft(ex.text)
                }}
                className="group flex items-start gap-2.5 rounded-lg border bg-card px-3 py-2.5 text-left text-sm transition-colors hover:border-primary/40 hover:bg-accent/40"
              >
                <Icon className="mt-0.5 size-4 shrink-0 text-muted-foreground group-hover:text-primary" />
                <span className="min-w-0 flex-1">{ex.text.trim()}</span>
                <span className="text-[10px] font-medium tracking-wide text-muted-foreground uppercase">{ex.mode}</span>
              </button>
            )
          })}
        </div>
        <div className="flex items-center justify-center gap-1.5 text-[11px] text-muted-foreground">
          <ShieldCheckIcon className="size-3.5" />
          Repository content is treated as untrusted data and never leaves this machine.
        </div>
      </div>
    </div>
  )
}

function TaskFooter({ task }: { task: TaskDetail }) {
  const resume = useTaskAction('resume')
  if (task.status === 'interrupted') {
    return (
      <div className="flex flex-wrap items-center gap-3 rounded-lg border border-warning/30 bg-warning/8 px-3 py-2.5 text-sm">
        <span className="min-w-0 flex-1">
          This task was interrupted. Resume continues from its last checkpoint without repeating completed edits.
        </span>
        <Button size="sm" onClick={() => resume.mutate(task.id)} disabled={resume.isPending}>
          <PlayIcon />
          Resume
        </Button>
        {resume.isError && <ErrorState error={resume.error} compact className="w-full" />}
      </div>
    )
  }
  if (task.status === 'failed' && task.error) {
    return <ErrorState error={new ApiError(0, 'task_failed', task.error)} title="Task failed" />
  }
  return null
}

function TaskConversation({ taskId }: { taskId: string }) {
  const task = useTask(taskId)
  const selectTask = useUi((s) => s.selectTask)
  const ev = useTaskEvents(taskId)
  const turns = useMemo(() => buildTurns(task.data, ev.events), [task.data, ev.events])
  const approvalId = actionableApprovalId(task.data)
  const scrollRef = useRef<HTMLDivElement>(null)
  const stickToBottom = useRef(true)

  useEffect(() => {
    const el = scrollRef.current
    if (el && stickToBottom.current) el.scrollTop = el.scrollHeight
  }, [ev.events.length, ev.llmTokens, ev.commandOutput, task.data?.status])

  useEffect(() => {
    if (task.error instanceof ApiError && task.error.status === 404) selectTask(null)
  }, [task.error, selectTask])

  if (task.isLoading) return <LoadingState label="Loading task…" className="h-full" />
  if (task.isError || !task.data)
    return (
      <div className="p-6">
        <ErrorState error={task.error} title="Could not load this task" onRetry={() => void task.refetch()} />
      </div>
    )

  const scope = { repoId: task.data.repo_id, taskId: task.data.worktree_path ? task.data.id : null }
  return (
    <CitationContext.Provider value={scope}>
      <div
        ref={scrollRef}
        className="h-full overflow-y-auto"
        onScroll={(e) => {
          const el = e.currentTarget
          stickToBottom.current = el.scrollHeight - el.scrollTop - el.clientHeight < 120
        }}
      >
        <div className="mx-auto max-w-3xl space-y-8 px-4 py-6">
          {(ev.connection === 'reconnecting' || ev.connection === 'error') && (
            <div className="rounded-md border border-warning/30 bg-warning/8 px-3 py-1.5 text-xs text-warning">
              {ev.connection === 'reconnecting'
                ? 'Live updates disconnected — reconnecting…'
                : (ev.connectionInfo ?? 'Live updates unavailable.')}
            </div>
          )}
          {turns.map((t) => (
            <TurnView
              key={t.turn}
              turn={t}
              taskId={taskId}
              actionableApprovalId={approvalId}
              llmTokens={ev.llmTokens}
              commandOutput={ev.commandOutput}
            />
          ))}
          <TaskFooter task={task.data} />
        </div>
      </div>
    </CitationContext.Provider>
  )
}

export function ChatPanel() {
  const repoId = useUi((s) => s.selectedRepoId)
  const taskId = useUi((s) => s.selectedTaskId)
  const repo = useRepo(repoId)
  const task = useTask(taskId)

  if (!repoId) return <NoRepoWelcome />
  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="min-h-0 flex-1">
        {taskId ? (
          <TaskConversation taskId={taskId} />
        ) : repo.isError ? (
          <div className="p-6">
            <ErrorState error={repo.error} title="Could not load this repository" onRetry={() => void repo.refetch()} />
          </div>
        ) : (
          <NewTaskEmpty repoName={repo.data?.name ?? 'this repository'} />
        )}
      </div>
      <Composer repoId={repoId} task={taskId ? (task.data ?? null) : null} />
    </div>
  )
}
