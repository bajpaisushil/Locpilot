import { useMutation, useQueryClient } from '@tanstack/react-query'
import { CpuIcon, HardDriveIcon, MoonIcon, ShieldCheckIcon, SunIcon, WrenchIcon } from 'lucide-react'
import { useState } from 'react'
import { ErrorState, LoadingState } from '@/components/common/States'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { Separator } from '@/components/ui/separator'
import { api } from '@/lib/api'
import { memorySummary } from '@/lib/memory'
import { qk, useModelStatus, useStorage } from '@/lib/queries'
import type { ModelStatus } from '@/lib/types'
import { formatBytes } from '@/lib/utils'
import { useUi } from '@/store/ui'

function ModelForm({ status }: { status: ModelStatus }) {
  const qc = useQueryClient()
  const [model, setModel] = useState(status.configured_model)
  const [numCtx, setNumCtx] = useState(String(status.num_ctx))
  const [temperature, setTemperature] = useState(String(status.temperature))
  const save = useMutation({
    mutationFn: () =>
      api.updateModelConfig({
        model: model !== status.configured_model ? model : undefined,
        num_ctx: Number(numCtx) !== status.num_ctx ? Number(numCtx) : undefined,
        temperature: Number(temperature) !== status.temperature ? Number(temperature) : undefined,
      }),
    onSuccess: (next) => {
      qc.setQueryData(qk.model, next)
      void qc.invalidateQueries({ queryKey: qk.setup })
    },
  })
  const names = Array.from(new Set([status.configured_model, ...status.models.map((m) => m.name)]))
  const dirty =
    model !== status.configured_model || Number(numCtx) !== status.num_ctx || Number(temperature) !== status.temperature

  return (
    <div className="space-y-4">
      <div className="space-y-1.5">
        <Label htmlFor="model-select">Model</Label>
        <Select value={model} onValueChange={setModel}>
          <SelectTrigger id="model-select">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            {names.map((n) => {
              const info = status.models.find((m) => m.name === n)
              return (
                <SelectItem key={n} value={n}>
                  <span className="font-mono">{n}</span>
                  {info ? (
                    <span className="ml-2 text-xs text-muted-foreground">
                      {[info.parameter_size, info.quantization, formatBytes(info.size_bytes)].filter(Boolean).join(' · ')}
                    </span>
                  ) : (
                    <span className="ml-2 text-xs text-warning">not installed</span>
                  )}
                </SelectItem>
              )
            })}
          </SelectContent>
        </Select>
        <p className="text-[11px] text-muted-foreground">Only models installed in your local Ollama are listed.</p>
      </div>
      <div className="grid grid-cols-2 gap-3">
        <div className="space-y-1.5">
          <Label htmlFor="num-ctx">Context window (tokens)</Label>
          <Input
            id="num-ctx"
            type="number"
            min={2048}
            max={131072}
            step={1024}
            value={numCtx}
            onChange={(e) => setNumCtx(e.target.value)}
          />
          {status.num_ctx_auto && Number(numCtx) === status.num_ctx && (
            <p className="text-[11px] text-muted-foreground" data-testid="num-ctx-auto">
              Chosen automatically: this model stores context cheaply, so the larger window costs little memory.
            </p>
          )}
        </div>
        <div className="space-y-1.5">
          <Label htmlFor="temperature">Temperature</Label>
          <Input
            id="temperature"
            type="number"
            min={0}
            max={1}
            step={0.05}
            value={temperature}
            onChange={(e) => setTemperature(e.target.value)}
          />
        </div>
      </div>
      {save.isError && <ErrorState error={save.error} compact />}
      <div className="flex justify-end">
        <Button size="sm" onClick={() => save.mutate()} disabled={!dirty || save.isPending}>
          Save model settings
        </Button>
      </div>
    </div>
  )
}

function FreeRamButton() {
  const qc = useQueryClient()
  const unload = useMutation({
    mutationFn: () => api.unloadModel(),
    onSettled: () => void qc.invalidateQueries({ queryKey: qk.model }),
  })
  return (
    <span className="flex shrink-0 items-center gap-2">
      {unload.isError && <span className="text-warning">{(unload.error as Error).message}</span>}
      <Button
        size="sm"
        variant="outline"
        className="h-6 px-2 text-[11px]"
        title="Unload the model from memory now; your next message loads it again."
        disabled={unload.isPending}
        onClick={() => unload.mutate()}
      >
        Free RAM now
      </Button>
    </span>
  )
}

function StorageSection() {
  const qc = useQueryClient()
  const storage = useStorage(true)
  const archive = useMutation({
    mutationFn: () => api.archiveWorktrees(),
    onSuccess: () => void qc.invalidateQueries({ queryKey: qk.storage }),
  })
  const cleanUp = useMutation({
    mutationFn: () => api.cleanUp(),
    onSuccess: () => void qc.invalidateQueries({ queryKey: qk.storage }),
  })
  const data = storage.data
  const active = data?.worktrees.filter((w) => w.state === 'active') ?? []
  const busy = active.filter((w) => !w.can_archive)
  return (
    <section className="space-y-2" data-testid="storage-section">
      <div className="flex items-center gap-2 text-sm font-medium">
        <HardDriveIcon className="size-4 text-primary" />
        Storage
      </div>
      {storage.isLoading && <LoadingState />}
      {storage.isError && <ErrorState error={storage.error} compact onRetry={() => void storage.refetch()} />}
      {data && (
        <>
          <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 text-xs">
            <dt className="text-muted-foreground">Task worktrees</dt>
            <dd>
              {formatBytes(data.worktrees_bytes)} in {active.length} {active.length === 1 ? 'task' : 'tasks'}
              {data.archived_count > 0 && (
                <span className="text-muted-foreground">
                  {' '}
                  · {data.archived_count} archived ({formatBytes(data.archived_bytes)})
                </span>
              )}
            </dd>
            <dt className="text-muted-foreground">Saved agent states</dt>
            <dd>{formatBytes(data.checkpoints_bytes)}</dd>
            <dt className="text-muted-foreground">Task history</dt>
            <dd>{formatBytes(data.history_bytes)}</dd>
            <dt className="text-muted-foreground">Command caches · logs</dt>
            <dd>
              {formatBytes(data.caches_bytes)} · {formatBytes(data.logs_bytes)}
            </dd>
            <dt className="text-muted-foreground">Code index</dt>
            <dd>{formatBytes(data.index_bytes)}</dd>
            {data.models_bytes !== null && (
              <>
                <dt className="text-muted-foreground">Models (in Ollama)</dt>
                <dd>{formatBytes(data.models_bytes)}</dd>
              </>
            )}
            <dt className="text-muted-foreground">Folder</dt>
            <dd className="truncate font-mono" title={data.data_dir}>
              {data.data_dir}
            </dd>
          </dl>
          <div className="flex items-center justify-between gap-3">
            <p className="text-[11px] text-muted-foreground">
              Archiving keeps a finished task&apos;s changes in a small file and removes its worktree; opening the task
              again restores it. Build output and other ignored files are not kept.
              {busy.length > 0 && ` ${busy.length} in use: ${busy.map((w) => w.reason).filter(Boolean)[0]}.`}
            </p>
            <Button
              size="sm"
              variant="outline"
              className="shrink-0"
              disabled={data.archivable_bytes === 0 || archive.isPending}
              onClick={() => archive.mutate()}
            >
              Free {formatBytes(data.archivable_bytes)}
            </Button>
          </div>
          {archive.data && (
            <p className="text-[11px] text-muted-foreground" data-testid="archive-result">
              Freed {formatBytes(archive.data.bytes_freed)} from {archive.data.archived.length}{' '}
              {archive.data.archived.length === 1 ? 'task' : 'tasks'}
              {archive.data.skipped.length > 0 && `; kept ${archive.data.skipped.length} (${archive.data.skipped[0].reason})`}
              .
            </p>
          )}
          {archive.isError && <ErrorState error={archive.error} compact />}
          <div className="flex items-center justify-between gap-3">
            <p className="text-[11px] text-muted-foreground">
              Clean up removes what finished tasks no longer need — the agent&apos;s older saved states, command
              caches, logs and stale index entries — and compacts the databases. Your tasks, their history and
              follow-ups keep working.
            </p>
            <Button
              size="sm"
              variant="outline"
              className="shrink-0"
              disabled={cleanUp.isPending}
              onClick={() => cleanUp.mutate()}
            >
              Clean up
            </Button>
          </div>
          {cleanUp.data && (
            <p className="text-[11px] text-muted-foreground" data-testid="clean-up-result">
              Freed {formatBytes(cleanUp.data.bytes_freed)}.
            </p>
          )}
          {cleanUp.isError && <ErrorState error={cleanUp.error} compact />}
        </>
      )}
    </section>
  )
}

export function SettingsDialog() {
  const open = useUi((s) => s.settingsOpen)
  const setOpen = useUi((s) => s.setSettingsOpen)
  const theme = useUi((s) => s.theme)
  const setTheme = useUi((s) => s.setTheme)
  const setSetupOpen = useUi((s) => s.setSetupOpen)
  const model = useModelStatus()

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogContent className="sm:max-w-xl">
        <DialogHeader>
          <DialogTitle>Settings</DialogTitle>
          <DialogDescription>Local model, storage, appearance and dependency setup.</DialogDescription>
        </DialogHeader>

        <section className="space-y-3">
          <div className="flex items-center gap-2 text-sm font-medium">
            <CpuIcon className="size-4 text-primary" />
            Local model
            {model.data &&
              (model.data.model_available ? (
                <Badge variant="success">ready</Badge>
              ) : (
                <Badge variant="warning">{model.data.ollama_reachable ? 'model missing' : 'Ollama offline'}</Badge>
              ))}
          </div>
          {model.isLoading && <LoadingState />}
          {model.isError && <ErrorState error={model.error} compact onRetry={() => void model.refetch()} />}
          {model.data && (
            <>
              <div className="text-xs text-muted-foreground">
                Ollama {model.data.ollama_version ?? '(unreachable)'} at{' '}
                <span className="font-mono">{model.data.ollama_url}</span>
                {model.data.error && <div className="mt-1 text-warning">{model.data.error}</div>}
                {memorySummary(model.data.memory) && (
                  <div className="mt-1 flex items-center justify-between gap-2" data-testid="model-memory">
                    <span>Memory: {memorySummary(model.data.memory)}</span>
                    {!!model.data.memory?.loaded_bytes && <FreeRamButton />}
                  </div>
                )}
                {model.data.memory?.short && (
                  <div className="mt-1 text-warning">
                    Not enough free memory to load this model without swapping: close other apps, or pick a smaller
                    model or context window.
                  </div>
                )}
                {model.data.switched_from && (
                  <div className="mt-1" data-testid="model-switched-note">
                    <span className="font-mono">{model.data.switched_from}</span> is not installed, so LocPilot
                    uses <span className="font-mono">{model.data.configured_model}</span>, a model you have
                    installed.
                  </div>
                )}
              </div>
              <ModelForm key={model.data.configured_model} status={model.data} />
            </>
          )}
        </section>

        <Separator />

        <StorageSection />

        <Separator />

        <section className="space-y-2">
          <div className="text-sm font-medium">Appearance</div>
          <div className="flex gap-2">
            <Button variant={theme === 'dark' ? 'secondary' : 'outline'} size="sm" onClick={() => setTheme('dark')}>
              <MoonIcon />
              Dark
            </Button>
            <Button variant={theme === 'light' ? 'secondary' : 'outline'} size="sm" onClick={() => setTheme('light')}>
              <SunIcon />
              Light
            </Button>
          </div>
        </section>

        <Separator />

        <section className="flex items-center justify-between gap-3">
          <div className="space-y-0.5">
            <div className="flex items-center gap-2 text-sm font-medium">
              <WrenchIcon className="size-4 text-primary" />
              Dependency setup
            </div>
            <div className="text-xs text-muted-foreground">Check Ollama, the model, Git and the command sandbox.</div>
          </div>
          <Button
            variant="outline"
            size="sm"
            onClick={() => {
              setOpen(false)
              setSetupOpen(true)
            }}
          >
            Open setup
          </Button>
        </section>

        <div className="flex items-start gap-2 rounded-md bg-muted/40 px-3 py-2 text-[11px] text-muted-foreground">
          <ShieldCheckIcon className="mt-0.5 size-3.5 shrink-0" />
          All inference runs in your local Ollama. LocPilot has no cloud fallback: if the local model is unavailable, AI features are disabled.
        </div>
      </DialogContent>
    </Dialog>
  )
}
