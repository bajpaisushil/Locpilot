import { CpuIcon, GitBranchIcon, PanelRightCloseIcon, PanelRightOpenIcon } from 'lucide-react'
import { ModeBadge, TaskStatusBadge } from '@/components/common/StatusBadge'
import { Button } from '@/components/ui/button'
import { Hint } from '@/components/ui/tooltip'
import { useModelStatus, useRepo, useTask } from '@/lib/queries'
import { memorySummary } from '@/lib/memory'
import { cn, formatBytes } from '@/lib/utils'
import { useUi } from '@/store/ui'

export function ModelStatusPill() {
  const model = useModelStatus()
  const setSetupOpen = useUi((s) => s.setSetupOpen)
  let tone = 'bg-muted-foreground/50'
  let text = 'Checking model…'
  let hint = 'Checking local model status'
  let ram: string | null = null
  if (model.isError) {
    tone = 'bg-destructive'
    text = 'Backend error'
    hint = 'Could not query model status'
  } else if (model.data) {
    if (!model.data.ollama_reachable) {
      tone = 'bg-destructive'
      text = 'Ollama offline'
      hint = model.data.hint ?? 'Start Ollama to enable AI features'
    } else if (!model.data.model_available) {
      tone = 'bg-warning'
      text = `${model.data.configured_model} missing`
      hint = model.data.hint ?? 'Download the configured model'
    } else {
      const mem = model.data.memory
      tone = mem?.short ? 'bg-warning' : 'bg-success'
      text = model.data.configured_model
      ram = mem?.loaded_bytes ? formatBytes(mem.loaded_bytes) : null
      hint = `${text} — local inference via Ollama ${model.data.ollama_version ?? ''} · context ${model.data.num_ctx} tokens`
      const summary = memorySummary(mem)
      if (summary) hint += ` · ${summary}`
      if (mem?.short)
        hint += ' · Not enough free memory: close other apps, or pick a smaller model or context window in Settings.'
    }
  }
  return (
    <Hint label={hint}>
      <button
        type="button"
        onClick={() => setSetupOpen(true)}
        className="inline-flex h-7 items-center gap-1.5 rounded-full border bg-background/60 px-2.5 text-xs transition-colors hover:bg-accent"
        aria-label={text}
        data-testid="model-status-pill"
      >
        <span className={cn('size-1.5 shrink-0 rounded-full', tone)} aria-hidden />
        <CpuIcon className="size-3.5 shrink-0 text-muted-foreground" />
        {/* With the side panel open the header is narrow: then only the dot shows (the name is in the hint). */}
        <span className="hidden max-w-44 truncate font-mono text-[11px] @min-[560px]:block">{text}</span>
        {ram && (
          <span className="hidden text-[11px] text-muted-foreground @min-[720px]:block" data-testid="model-ram">
            {ram}
          </span>
        )}
      </button>
    </Hint>
  )
}

export function TopBar() {
  const repoId = useUi((s) => s.selectedRepoId)
  const taskId = useUi((s) => s.selectedTaskId)
  const rightOpen = useUi((s) => s.rightPanelOpen)
  const setRightOpen = useUi((s) => s.setRightPanelOpen)
  const repo = useRepo(repoId)
  const task = useTask(taskId)
  const st = repo.data?.status
  const changes = st ? st.staged + st.modified + st.untracked + st.conflicted : 0
  const branch = st?.detached ? `detached ${st.head?.slice(0, 7) ?? ''}` : (st?.branch ?? '—')

  return (
    <header className="@container flex h-12 shrink-0 items-center gap-3 border-b px-4" data-tauri-drag-region>
      {/* The task title shrinks first. In a very narrow header (side panel open on a small window) the branch shows
          only its icon and the repo name truncates too; overflow-hidden keeps anything from covering the model pill. */}
      <div className="flex min-w-0 flex-1 items-center gap-2 overflow-hidden">
        {repo.data ? (
          <>
            <span
              className="max-w-[18ch] shrink-0 truncate text-sm font-medium @max-[460px]:min-w-[3ch] @max-[460px]:shrink"
              title={repo.data.path}
            >
              {repo.data.name}
            </span>
            {st && (
              <Hint
                label={`${branch} — ${
                  st.dirty
                    ? `${changes} uncommitted change${changes === 1 ? '' : 's'} in your working tree (LocPilot never modifies them)`
                    : 'working tree clean'
                }`}
              >
                <span className="inline-flex shrink-0 items-center gap-1 rounded-md border px-1.5 py-0.5 font-mono text-[11px] text-muted-foreground">
                  <GitBranchIcon className="size-3" />
                  <span className="@max-[460px]:hidden">{branch}</span>
                  {st.dirty && <span className="size-1.5 rounded-full bg-warning" aria-label="uncommitted changes" />}
                  {(st.ahead > 0 || st.behind > 0) && (
                    <span className="text-[10px]">
                      {st.ahead > 0 && `↑${st.ahead}`}
                      {st.behind > 0 && `↓${st.behind}`}
                    </span>
                  )}
                </span>
              </Hint>
            )}
          </>
        ) : (
          <span className="text-sm text-muted-foreground">{repoId ? 'Loading…' : 'No repository'}</span>
        )}
        {task.data && (
          <>
            <span className="shrink-0 text-muted-foreground/50">/</span>
            <span className="min-w-0 truncate text-sm text-muted-foreground" title={task.data.title}>
              {task.data.title}
            </span>
            <ModeBadge mode={task.data.mode} className="shrink-0" />
            <TaskStatusBadge status={task.data.status} className="shrink-0" />
          </>
        )}
      </div>
      <div className="ml-auto flex shrink-0 items-center gap-2">
        <ModelStatusPill />
        <Hint label={rightOpen ? 'Hide panel' : 'Show panel'}>
          <Button
            variant="ghost"
            size="icon-sm"
            onClick={() => setRightOpen(!rightOpen)}
            aria-label={rightOpen ? 'Hide side panel' : 'Show side panel'}
          >
            {rightOpen ? <PanelRightCloseIcon /> : <PanelRightOpenIcon />}
          </Button>
        </Hint>
      </div>
    </header>
  )
}
