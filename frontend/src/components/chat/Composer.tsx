import { ArrowUpIcon, MessageSquareTextIcon, SquareIcon, TerminalIcon, WrenchIcon } from 'lucide-react'
import { useEffect, useRef } from 'react'
import { ErrorState } from '@/components/common/States'
import { Button } from '@/components/ui/button'
import { Kbd } from '@/components/ui/kbd'
import { Hint } from '@/components/ui/tooltip'
import { commandLine, runningUserCommand, sharedUserRuns } from '@/lib/commands'
import { isActive, useCreateTask, useModelStatus, useSendMessage, useTaskAction } from '@/lib/queries'
import type { Mode, TaskDetail } from '@/lib/types'
import { cn } from '@/lib/utils'
import { useUi } from '@/store/ui'

const MODES: { id: Mode; label: string; icon: typeof WrenchIcon; hint: string }[] = [
  {
    id: 'ask',
    label: 'Ask',
    icon: MessageSquareTextIcon,
    hint: 'Read-only investigation. LocPilot reads and searches code but never edits files or runs project code.',
  },
  {
    id: 'code',
    label: 'Code',
    icon: WrenchIcon,
    hint: 'Proposes patches in an isolated Git worktree. Nothing is applied or run without your approval.',
  },
]

export function ModeToggle({ value, onChange, disabled }: { value: Mode; onChange: (m: Mode) => void; disabled?: boolean }) {
  return (
    <div role="radiogroup" aria-label="Execution mode" className="inline-flex h-7 items-center rounded-md border bg-muted/50 p-0.5">
      {MODES.map((m) => {
        const Icon = m.icon
        const active = value === m.id
        return (
          <Hint key={m.id} label={m.hint}>
            <button
              type="button"
              role="radio"
              aria-checked={active}
              disabled={disabled}
              onClick={() => onChange(m.id)}
              className={cn(
                'inline-flex h-full items-center gap-1.5 rounded-[5px] px-2.5 text-xs font-medium transition-colors disabled:opacity-50',
                active
                  ? m.id === 'code'
                    ? 'bg-primary text-primary-foreground shadow-xs'
                    : 'bg-background text-foreground shadow-xs'
                  : 'text-muted-foreground hover:text-foreground',
              )}
            >
              <Icon className="size-3.5" />
              {m.label}
            </button>
          </Hint>
        )
      })}
    </div>
  )
}

export function Composer({ repoId, task }: { repoId: string; task: TaskDetail | null }) {
  const mode = useUi((s) => s.composerMode)
  const setMode = useUi((s) => s.setComposerMode)
  const draft = useUi((s) => s.composerDraft)
  const setDraft = useUi((s) => s.setComposerDraft)
  const selectTask = useUi((s) => s.selectTask)
  const setSetupOpen = useUi((s) => s.setSetupOpen)
  const model = useModelStatus()
  const createTask = useCreateTask()
  const sendMessage = useSendMessage()
  const cancel = useTaskAction('cancel')
  const ref = useRef<HTMLTextAreaElement>(null)

  const aiReady = !!model.data?.ollama_reachable && !!model.data?.model_available
  const running = isActive(task?.status)
  const awaiting = task?.status === 'awaiting_approval'
  const submitting = createTask.isPending || sendMessage.isPending
  const error = createTask.error ?? sendMessage.error

  let blockedReason: string | null = null
  if (!model.data && model.isLoading) blockedReason = 'Checking the local model…'
  else if (!aiReady) blockedReason = 'The local model is not ready.'
  else if (running) blockedReason = 'LocPilot is working on this task…'
  else if (awaiting) blockedReason = 'Approve or reject the proposed changes above to continue.'
  else if (runningUserCommand(task)) blockedReason = 'A command you started is still running — stop it in the Commands tab to continue.'
  const disabled = !!blockedReason || submitting
  const shared = blockedReason ? [] : sharedUserRuns(task)

  useEffect(() => {
    const el = ref.current
    if (!el) return
    el.style.height = '0px'
    el.style.height = `${Math.min(220, Math.max(44, el.scrollHeight))}px`
  }, [draft])

  const submit = async () => {
    const message = draft.trim()
    if (!message || disabled) return
    try {
      if (task) {
        await sendMessage.mutateAsync({ taskId: task.id, message, mode })
      } else {
        const created = await createTask.mutateAsync({ repo_id: repoId, mode, message })
        selectTask(created.id)
      }
      setDraft('')
    } catch {
      // surfaced below via mutation error
    }
  }

  const placeholder = task
    ? task.status === 'needs_input'
      ? 'Answer the clarifying question…'
      : 'Ask a follow-up, e.g. “The tests failed — investigate and try again”'
    : mode === 'ask'
      ? 'Ask about the code — e.g. “Explain how authentication works”'
      : 'Describe a bug or change — e.g. “Fix the duplicate hotel offers with the smallest possible change”'

  return (
    <div className="border-t bg-background/80 px-4 pt-3 pb-4 backdrop-blur">
      <div className="mx-auto max-w-3xl space-y-2">
        {error && <ErrorState error={error} compact />}
        {shared.length > 0 && (
          <div className="flex min-w-0 items-center gap-1.5 text-[11px] text-muted-foreground" data-testid="shared-runs">
            <TerminalIcon className="size-3 shrink-0" />
            <span className="shrink-0">LocPilot will see the output of</span>
            {shared.map((c) => (
              <code key={c.id} className="min-w-0 truncate rounded bg-muted px-1 font-mono">
                {commandLine(c)}
              </code>
            ))}
          </div>
        )}
        {!aiReady && model.data && (
          <div className="flex items-center justify-between gap-3 rounded-md border border-warning/30 bg-warning/8 px-3 py-2 text-xs">
            <span>
              {model.data.ollama_reachable
                ? `Model “${model.data.configured_model}” is not installed in Ollama.`
                : 'Ollama is not running.'}{' '}
              AI features stay disabled until a local model is ready — LocPilot never falls back to cloud APIs.
            </span>
            <Button size="xs" variant="outline" onClick={() => setSetupOpen(true)}>
              Open setup
            </Button>
          </div>
        )}
        <div
          className={cn(
            'rounded-xl border bg-card shadow-xs transition-shadow focus-within:border-ring focus-within:ring-[3px] focus-within:ring-ring/25',
            mode === 'code' && 'border-primary/35',
          )}
        >
          <textarea
            ref={ref}
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) {
                e.preventDefault()
                void submit()
              }
            }}
            disabled={disabled && !submitting}
            placeholder={blockedReason ?? placeholder}
            aria-label="Message LocPilot"
            rows={1}
            className="block max-h-56 min-h-11 w-full resize-none bg-transparent px-3.5 pt-3 pb-1 text-sm outline-none placeholder:text-muted-foreground disabled:cursor-not-allowed"
          />
          <div className="flex items-center gap-2 px-2.5 pb-2.5">
            <ModeToggle value={mode} onChange={setMode} disabled={running} />
            <span className="hidden text-[11px] text-muted-foreground sm:inline">
              {mode === 'ask' ? 'Read-only' : 'Isolated worktree · approval required'}
            </span>
            <div className="ml-auto flex items-center gap-2">
              <span className="hidden items-center gap-1 text-[11px] text-muted-foreground md:flex">
                <Kbd>↵</Kbd> send <Kbd>⇧↵</Kbd> newline
              </span>
              {running && task ? (
                <Button
                  size="sm"
                  variant="outline"
                  onClick={() => cancel.mutate(task.id)}
                  disabled={cancel.isPending}
                  aria-label="Cancel task"
                >
                  <SquareIcon className="size-3 fill-current" />
                  Cancel
                </Button>
              ) : (
                <Button
                  size="icon-sm"
                  onClick={() => void submit()}
                  disabled={disabled || !draft.trim()}
                  aria-label="Send"
                >
                  <ArrowUpIcon />
                </Button>
              )}
            </div>
          </div>
        </div>
      </div>
    </div>
  )
}
