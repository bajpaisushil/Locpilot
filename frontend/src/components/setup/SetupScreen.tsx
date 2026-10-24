import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  AlertTriangleIcon,
  CheckCircle2Icon,
  CircleAlertIcon,
  CloudOffIcon,
  CpuIcon,
  ExternalLinkIcon,
  HardDriveDownloadIcon,
  InfoIcon,
  PackageIcon,
  RefreshCwIcon,
  XCircleIcon,
} from 'lucide-react'
import { useEffect, useRef } from 'react'
import { CommandLine } from '@/components/common/CopyButton'
import { Logo } from '@/components/common/Logo'
import { ErrorState, LoadingState } from '@/components/common/States'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Progress } from '@/components/ui/progress'
import { api, errorMessage } from '@/lib/api'
import { openExternal } from '@/lib/desktop'
import { qk, useSetupStatus } from '@/lib/queries'
import type { InstallStep, SetupCheck, SetupStatus } from '@/lib/types'
import { cn, formatBytes, formatRelativeTime } from '@/lib/utils'

const PLATFORM_LABEL: Record<SetupStatus['platform'], string> = {
  macos: 'macOS',
  windows: 'Windows',
  linux: 'Linux',
  other: 'your platform',
}

function CheckRow({ check }: { check: SetupCheck }) {
  const icon =
    check.status === 'ok' ? (
      <CheckCircle2Icon className="size-4 text-success" />
    ) : check.status === 'warning' ? (
      <AlertTriangleIcon className="size-4 text-warning" />
    ) : check.status === 'error' ? (
      <CircleAlertIcon className="size-4 text-destructive" />
    ) : (
      <XCircleIcon className="size-4 text-destructive" />
    )
  return (
    <li className="flex items-start gap-3 px-4 py-3" data-testid={`check-${check.id}`} data-status={check.status}>
      <span className="mt-0.5">{icon}</span>
      <div className="min-w-0 flex-1 space-y-0.5">
        <div className="flex flex-wrap items-center gap-2">
          <span className="text-sm font-medium">{check.label}</span>
          {check.required ? <Badge variant="outline">required</Badge> : <Badge variant="muted">informational</Badge>}
          <Badge
            variant={
              check.status === 'ok'
                ? 'success'
                : check.status === 'warning'
                  ? 'warning'
                  : 'destructive'
            }
          >
            {check.status === 'ok' ? 'ready' : check.status}
          </Badge>
        </div>
        <div className="text-xs break-words text-muted-foreground">{check.detail}</div>
        {check.hint && check.status !== 'ok' && <div className="text-xs text-foreground/80">{check.hint}</div>}
      </div>
    </li>
  )
}

function Steps({ steps }: { steps: InstallStep[] }) {
  return (
    <ol className="space-y-3">
      {steps.map((s, i) => (
        <li key={`${s.title}-${i}`} className="flex gap-3">
          <span className="flex size-5 shrink-0 items-center justify-center rounded-full border bg-muted font-mono text-[10px]">
            {i + 1}
          </span>
          <div className="min-w-0 flex-1 space-y-1.5">
            <div className="text-sm font-medium">{s.title}</div>
            {s.description && <div className="text-xs text-muted-foreground">{s.description}</div>}
            {s.command && <CommandLine command={s.command} />}
            {s.url && (
              <Button variant="outline" size="xs" onClick={() => void openExternal(s.url as string)}>
                <ExternalLinkIcon />
                Open {new URL(s.url).host}
              </Button>
            )}
          </div>
        </li>
      ))}
    </ol>
  )
}

function ModelDownload({ setup }: { setup: SetupStatus }) {
  const qc = useQueryClient()
  const pull = useQuery({
    queryKey: qk.pull,
    queryFn: api.pullStatus,
    refetchInterval: (q) => (q.state.data?.state === 'pulling' ? 1000 : false),
  })
  const start = useMutation({
    mutationFn: () => api.pullModel(setup.model),
    onSuccess: (s) => qc.setQueryData(qk.pull, s),
  })
  const prevState = useRef(pull.data?.state)
  useEffect(() => {
    const state = pull.data?.state
    if (prevState.current === 'pulling' && state === 'success') {
      void qc.invalidateQueries({ queryKey: qk.setup })
      void qc.invalidateQueries({ queryKey: qk.model })
    }
    prevState.current = state
  }, [pull.data?.state, qc])

  const status = pull.data && pull.data.model === setup.model ? pull.data : null
  const pulling = status?.state === 'pulling' || start.isPending
  const pct =
    status?.total && status.completed !== null ? Math.min(100, Math.round((status.completed / status.total) * 100)) : null

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-2">
        <Button onClick={() => start.mutate()} disabled={pulling}>
          <HardDriveDownloadIcon />
          {pulling ? 'Downloading…' : `Download ${setup.model} with Ollama`}
        </Button>
        <span className="text-xs text-muted-foreground">or run the command below in a terminal.</span>
      </div>
      {(pulling || status?.state === 'success' || status?.state === 'error') && (
        <div className="space-y-1.5 rounded-md border bg-muted/30 p-3" data-testid="pull-progress">
          <div className="flex items-center justify-between gap-2 text-xs">
            <span className="truncate">{status?.status_text || 'Contacting Ollama…'}</span>
            {status?.total ? (
              <span className="shrink-0 font-mono text-muted-foreground tabular-nums">
                {formatBytes(status.completed)} / {formatBytes(status.total)}
                {pct !== null && ` · ${pct}%`}
              </span>
            ) : null}
          </div>
          {pulling && <Progress value={pct ?? 0} indeterminate={pct === null} />}
          {status?.state === 'success' && (
            <div className="flex items-center gap-1.5 text-xs text-success">
              <CheckCircle2Icon className="size-3.5" /> Download complete.
            </div>
          )}
          {status?.state === 'error' && (
            <div className="text-xs text-destructive">{status.error ?? 'Download failed.'}</div>
          )}
        </div>
      )}
      {start.isError && <ErrorState error={start.error} title="Could not start the download" compact />}
      <p className="text-[11px] text-muted-foreground">
        The download is performed by Ollama on your machine and stored in Ollama&apos;s model folder. It is separate
        from the LocPilot installer and only needs to happen once.
      </p>
    </div>
  )
}

function UseInstalledModel({ setup }: { setup: SetupStatus }) {
  const qc = useQueryClient()
  const choose = useMutation({
    mutationFn: (model: string) => api.updateModelConfig({ model }),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: qk.setup })
      void qc.invalidateQueries({ queryKey: qk.model })
    },
  })
  const others = setup.installed_models.filter((m) => m !== setup.model)
  if (others.length === 0) return null
  return (
    <div className="space-y-2">
      <div className="text-xs text-muted-foreground">Or use a model you already have installed:</div>
      <div className="flex flex-wrap gap-1.5">
        {others.map((m) => (
          <Button key={m} variant="outline" size="xs" disabled={choose.isPending} onClick={() => choose.mutate(m)}>
            <CpuIcon />
            {m}
          </Button>
        ))}
      </div>
      {choose.isError && <div className="text-xs text-destructive">{errorMessage(choose.error)}</div>}
    </div>
  )
}

export function SetupScreen({
  onContinue,
  onBrowseWithoutAi,
  embedded = false,
}: {
  onContinue: () => void
  onBrowseWithoutAi?: () => void
  embedded?: boolean
}) {
  const setup = useSetupStatus()
  const data = setup.data

  const body = (() => {
    if (setup.isLoading) return <LoadingState label="Checking local dependencies…" />
    if (setup.isError || !data)
      return (
        <ErrorState
          error={setup.error}
          title="Could not check dependencies"
          onRetry={() => void setup.refetch()}
        />
      )
    const byId = Object.fromEntries(data.checks.map((c) => [c.id, c])) as Partial<Record<SetupCheck['id'], SetupCheck>>
    const ollamaOk = byId.ollama?.status === 'ok'
    const modelOk = byId.model?.status === 'ok'
    const missingRequired = data.checks.filter((c) => c.required && c.status !== 'ok').length
    return (
      <div className="space-y-5">
        <div
          role="status"
          className={cn(
            'flex flex-wrap items-center gap-3 rounded-lg border px-4 py-3',
            data.ready ? 'border-success/30 bg-success/8' : 'border-warning/30 bg-warning/8',
          )}
        >
          {data.ready ? (
            <CheckCircle2Icon className="size-5 text-success" />
          ) : (
            <AlertTriangleIcon className="size-5 text-warning" />
          )}
          <div className="min-w-0 flex-1">
            <div className="text-sm font-semibold" data-testid="readiness">
              {data.ready
                ? 'Ready — all required dependencies are available'
                : `${missingRequired} required item${missingRequired === 1 ? '' : 's'} need${missingRequired === 1 ? 's' : ''} attention`}
            </div>
            <div className="text-xs text-muted-foreground">
              {data.ready ? 'Status is checked live.' : 'Checking again automatically every few seconds.'} Last checked{' '}
              {formatRelativeTime(data.checked_at)} · {PLATFORM_LABEL[data.platform]} · Ollama at{' '}
              <span className="font-mono">{data.ollama_url}</span>
            </div>
          </div>
          <Button variant="outline" size="sm" onClick={() => void setup.refetch()} disabled={setup.isFetching}>
            <RefreshCwIcon className={cn(setup.isFetching && 'animate-spin')} />
            Check Again
          </Button>
        </div>

        <ul className="divide-y overflow-hidden rounded-lg border bg-card">
          {data.checks.map((c) => (
            <CheckRow key={c.id} check={c} />
          ))}
        </ul>

        {!ollamaOk && data.ollama_install.length > 0 && (
          <section className="space-y-3 rounded-lg border bg-card p-4" data-testid="ollama-guide">
            <div className="flex items-center gap-2">
              <PackageIcon className="size-4 text-primary" />
              <h3 className="text-sm font-semibold">Install and start Ollama on {PLATFORM_LABEL[data.platform]}</h3>
            </div>
            <Steps steps={data.ollama_install} />
          </section>
        )}

        {ollamaOk && !modelOk && (
          <section className="space-y-4 rounded-lg border bg-card p-4" data-testid="model-guide">
            <div className="flex items-center gap-2">
              <HardDriveDownloadIcon className="size-4 text-primary" />
              <h3 className="text-sm font-semibold">
                Download the model <span className="font-mono">{data.model}</span>
              </h3>
            </div>
            <ModelDownload setup={data} />
            {data.model_install.length > 0 && <Steps steps={data.model_install} />}
            <UseInstalledModel setup={data} />
          </section>
        )}
      </div>
    )
  })()

  return (
    <div className={cn(embedded ? '' : 'flex min-h-full justify-center overflow-y-auto px-6 py-10')}>
      <div className={cn('w-full space-y-6', !embedded && 'max-w-2xl')}>
        {!embedded && (
          <div className="flex items-start gap-3">
            <Logo className="size-10" />
            <div>
              <h1 className="text-xl font-semibold tracking-tight">Set up local AI</h1>
              <p className="text-sm text-muted-foreground">
                LocPilot runs entirely on your computer. Ollama provides local inference; LocPilot checks that it and
                the configured model are available.
              </p>
            </div>
          </div>
        )}

        {body}

        <div className="flex items-start gap-2.5 rounded-lg border bg-muted/30 px-4 py-3 text-xs text-muted-foreground">
          <CloudOffIcon className="mt-0.5 size-4 shrink-0" />
          <div className="space-y-1">
            <p>
              <span className="font-medium text-foreground">LocPilot never falls back to cloud AI APIs.</span> If no
              local model is ready, AI features stay disabled — your code is never sent to a hosted service.
            </p>
            <p className="flex items-center gap-1">
              <InfoIcon className="size-3" />
              Ollama and model downloads are separate from the LocPilot installer.
            </p>
          </div>
        </div>

        <div className="flex flex-wrap justify-end gap-2">
          {onBrowseWithoutAi && !data?.ready && (
            <Button variant="ghost" onClick={onBrowseWithoutAi}>
              Continue without AI (browse only)
            </Button>
          )}
          <Button onClick={onContinue} disabled={!data?.ready}>
            {data?.ready ? 'Continue' : 'Waiting for dependencies…'}
          </Button>
        </div>
      </div>
    </div>
  )
}
