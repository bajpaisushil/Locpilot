import { useMutation, useQueryClient } from '@tanstack/react-query'
import { CpuIcon, MoonIcon, ShieldCheckIcon, SunIcon, WrenchIcon } from 'lucide-react'
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
import { qk, useModelStatus } from '@/lib/queries'
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
          <DialogDescription>Local model, appearance and dependency setup.</DialogDescription>
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
              </div>
              <ModelForm key={model.data.configured_model} status={model.data} />
            </>
          )}
        </section>

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
