import { useQuery } from '@tanstack/react-query'
import { RefreshCwIcon, ServerCrashIcon } from 'lucide-react'
import { useEffect, useState, type ReactNode } from 'react'
import { CommandLine } from '@/components/common/CopyButton'
import { Logo } from '@/components/common/Logo'
import { Spinner } from '@/components/common/States'
import { Button } from '@/components/ui/button'
import { api, errorMessage, setConnection } from '@/lib/api'
import { getBackendInfo, isTauri, onBackendStatus, restartBackend, type BackendInfo } from '@/lib/desktop'

function Splash({ label }: { label: string }) {
  return (
    <div className="flex h-full flex-col items-center justify-center gap-4" role="status">
      <Logo className="size-12" />
      <div className="flex items-center gap-2 text-sm text-muted-foreground">
        <Spinner />
        {label}
      </div>
    </div>
  )
}

function FailurePanel({
  title,
  message,
  logs,
  onRetry,
  retrying,
  children,
}: {
  title: string
  message: string
  logs?: string | null
  onRetry: () => void
  retrying?: boolean
  children?: ReactNode
}) {
  return (
    <div className="flex h-full items-center justify-center p-8">
      <div className="w-full max-w-2xl space-y-4">
        <div className="flex items-center gap-3">
          <ServerCrashIcon className="size-8 text-destructive" />
          <div>
            <h1 className="text-lg font-semibold">{title}</h1>
            <p className="text-sm text-muted-foreground">{message}</p>
          </div>
        </div>
        {children}
        {logs && (
          <pre className="max-h-72 overflow-auto rounded-md border bg-code p-3 font-mono text-[11px] leading-relaxed whitespace-pre-wrap">
            {logs}
          </pre>
        )}
        <Button onClick={onRetry} disabled={retrying}>
          <RefreshCwIcon className={retrying ? 'animate-spin' : undefined} />
          Retry
        </Button>
      </div>
    </div>
  )
}

/** Desktop: waits for the bundled sidecar to report its port + token. */
function DesktopGate({ children }: { children: ReactNode }) {
  const [info, setInfo] = useState<BackendInfo | null>(null)
  const [restarting, setRestarting] = useState(false)

  useEffect(() => {
    let disposed = false
    let unlisten: (() => void) | null = null
    const apply = (next: BackendInfo) => {
      if (disposed) return
      if (next.status === 'ready' && next.base_url) setConnection({ baseUrl: next.base_url, token: next.token })
      setInfo(next)
    }
    void onBackendStatus(apply).then((fn) => {
      if (disposed) fn()
      else unlisten = fn
    })
    // Poll as well, in case the ready event fired before we subscribed.
    const poll = async () => {
      while (!disposed) {
        try {
          const next = await getBackendInfo()
          apply(next)
          if (next.status !== 'starting') return
        } catch (err) {
          apply({ status: 'failed', base_url: null, token: null, error: errorMessage(err), logs_tail: null })
          return
        }
        await new Promise((r) => setTimeout(r, 400))
      }
    }
    void poll()
    return () => {
      disposed = true
      unlisten?.()
    }
  }, [])

  if (!info || info.status === 'starting') return <Splash label="Starting LocPilot's local agent…" />
  if (info.status === 'failed')
    return (
      <FailurePanel
        title="The local agent failed to start"
        message={info.error ?? 'The bundled backend exited unexpectedly.'}
        logs={info.logs_tail}
        retrying={restarting}
        onRetry={async () => {
          setRestarting(true)
          try {
            const next = await restartBackend()
            if (next.status === 'ready' && next.base_url) setConnection({ baseUrl: next.base_url, token: next.token })
            setInfo(next)
          } catch (err) {
            setInfo({ status: 'failed', base_url: null, token: null, error: errorMessage(err), logs_tail: info.logs_tail })
          } finally {
            setRestarting(false)
          }
        }}
      />
    )
  return <>{children}</>
}

/** Browser dev mode: the backend is started separately and reached through the Vite proxy. */
function BrowserGate({ children }: { children: ReactNode }) {
  const health = useQuery({ queryKey: ['health'], queryFn: api.health, retry: 1, refetchInterval: (q) => (q.state.data ? false : 3000) })
  if (health.isLoading) return <Splash label="Connecting to the LocPilot backend…" />
  if (health.isError)
    return (
      <FailurePanel
        title="Cannot reach the LocPilot backend"
        message={errorMessage(health.error)}
        onRetry={() => void health.refetch()}
        retrying={health.isFetching}
      >
        <div className="space-y-2 text-sm text-muted-foreground">
          <p>In browser development mode the backend runs separately. Start both servers with:</p>
          <CommandLine command="./scripts/dev.sh" />
        </div>
      </FailurePanel>
    )
  return <>{children}</>
}

export function BackendGate({ children }: { children: ReactNode }) {
  return isTauri() ? <DesktopGate>{children}</DesktopGate> : <BrowserGate>{children}</BrowserGate>
}
