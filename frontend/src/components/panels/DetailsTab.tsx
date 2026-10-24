import type { ReactNode } from 'react'
import { CopyButton } from '@/components/common/CopyButton'
import { ModeBadge, TaskStatusBadge } from '@/components/common/StatusBadge'
import { EmptyState, ErrorState, LoadingState } from '@/components/common/States'
import { Badge } from '@/components/ui/badge'
import { useRepo, useTask } from '@/lib/queries'
import { formatDateTime, formatNumber, shortSha } from '@/lib/utils'
import { useTaskEvents } from '@/store/events'
import { useUi } from '@/store/ui'

function Row({ label, children, copy }: { label: string; children: ReactNode; copy?: string | null }) {
  return (
    <div className="grid grid-cols-[8.5rem_1fr] items-start gap-2 py-1.5 text-xs">
      <dt className="text-muted-foreground">{label}</dt>
      <dd className="flex min-w-0 items-start gap-1 break-all">
        <span className="min-w-0 flex-1">{children}</span>
        {copy && <CopyButton text={copy} label={`Copy ${label.toLowerCase()}`} />}
      </dd>
    </div>
  )
}

function Heading({ children }: { children: ReactNode }) {
  return <h3 className="pt-3 pb-1 text-[10.5px] font-semibold tracking-wider text-muted-foreground uppercase">{children}</h3>
}

function RepoDetails() {
  const repoId = useUi((s) => s.selectedRepoId)
  const repo = useRepo(repoId)
  if (!repoId) return <EmptyState title="No repository selected" className="h-full" />
  if (repo.isLoading) return <LoadingState />
  if (repo.isError || !repo.data) return <ErrorState error={repo.error} onRetry={() => void repo.refetch()} />
  const r = repo.data
  return (
    <dl className="divide-y">
      <Row label="Name">{r.name}</Row>
      <Row label="Source">
        <Badge variant="secondary">{r.source_type === 'github' ? 'Cloned' : 'Local'}</Badge>
        {r.source_url && <span className="ml-2 font-mono">{r.source_url}</span>}
      </Row>
      <Row label="Path" copy={r.path}>
        <span className="font-mono">{r.path}</span>
      </Row>
      <Row label="Branch">{r.status.detached ? `detached at ${shortSha(r.status.head)}` : (r.status.branch ?? '—')}</Row>
      <Row label="HEAD">
        <span className="font-mono">{shortSha(r.status.head)}</span> {r.status.head_summary}
      </Row>
      <Row label="Working tree">
        {r.status.dirty
          ? `${r.status.staged} staged · ${r.status.modified} modified · ${r.status.untracked} untracked${r.status.conflicted ? ` · ${r.status.conflicted} conflicted` : ''}`
          : 'clean'}
      </Row>
      <Row label="Files indexed">{formatNumber(r.file_count)}</Row>
      <Row label="Languages">
        <div className="flex flex-wrap gap-1">
          {r.languages.slice(0, 10).map((l) => (
            <Badge key={l.name} variant="muted">
              {l.name} {l.files}
            </Badge>
          ))}
        </div>
      </Row>
      <Row label="Manifests">
        <span className="font-mono">{r.manifests.join(', ') || '—'}</span>
      </Row>
      <Row label="Test commands">
        {r.test_commands.length ? (
          <ul className="space-y-0.5 font-mono">
            {r.test_commands.map((c) => (
              <li key={c.join(' ')}>{c.join(' ')}</li>
            ))}
          </ul>
        ) : (
          'none detected'
        )}
      </Row>
    </dl>
  )
}

export function DetailsTab() {
  const taskId = useUi((s) => s.selectedTaskId)
  const task = useTask(taskId)
  const ev = useTaskEvents(taskId)
  if (!taskId)
    return (
      <div className="h-full overflow-y-auto px-3 pb-4">
        <Heading>Repository</Heading>
        <RepoDetails />
      </div>
    )
  if (task.isLoading) return <LoadingState />
  if (task.isError || !task.data) return <div className="p-3"><ErrorState error={task.error} onRetry={() => void task.refetch()} /></div>
  const t = task.data
  const stats = t.stats ?? {}
  const lastReport = t.reports[t.reports.length - 1]
  return (
    <div className="h-full overflow-y-auto px-3 pb-4">
      <Heading>Task</Heading>
      <dl className="divide-y">
        <Row label="Title">{t.title}</Row>
        <Row label="ID" copy={t.id}>
          <span className="font-mono">{t.id}</span>
        </Row>
        <Row label="Mode">
          <ModeBadge mode={t.mode} />
        </Row>
        <Row label="Status">
          <TaskStatusBadge status={t.status} />
          {t.phase && <span className="ml-2 text-muted-foreground">phase: {t.phase}</span>}
        </Row>
        <Row label="Model">
          <span className="font-mono">{t.model ?? '—'}</span>
        </Row>
        <Row label="Created">{formatDateTime(t.created_at)}</Row>
        <Row label="Updated">{formatDateTime(t.updated_at)}</Row>
        <Row label="Read from" copy={t.workspace_path}>
          <span className="font-mono">{t.workspace_path ?? '—'}</span>
        </Row>
        <Row label="Isolated worktree" copy={t.worktree_path}>
          <span className="font-mono">{t.worktree_path ?? 'none (read-only task)'}</span>
        </Row>
        <Row label="Base commit" copy={t.base_commit}>
          <span className="font-mono">{shortSha(t.base_commit)}</span>
        </Row>
        <Row label="Live updates">{ev.connection}</Row>
        {t.error && <Row label="Error">{t.error}</Row>}
      </dl>

      <Heading>Resource usage</Heading>
      <dl className="divide-y">
        {Object.keys(stats).length === 0 && <div className="py-1.5 text-xs text-muted-foreground">No statistics yet.</div>}
        {Object.entries(stats).map(([k, v]) => (
          <Row key={k} label={k.replace(/_/g, ' ')}>
            <span className="font-mono">{formatNumber(v)}</span>
          </Row>
        ))}
        {lastReport && <Row label="Repair attempts">{lastReport.retries}</Row>}
      </dl>

      {t.patches.length > 0 && (
        <>
          <Heading>Patches</Heading>
          <ul className="space-y-1">
            {t.patches.map((p) => (
              <li key={p.id} className="flex items-center gap-2 text-xs">
                <span className="font-mono text-muted-foreground">#{p.attempt}</span>
                <span className="min-w-0 flex-1 truncate">{p.summary}</span>
                <Badge variant={p.status === 'applied' ? 'success' : p.status === 'failed' ? 'destructive' : 'muted'}>
                  {p.status}
                </Badge>
              </li>
            ))}
          </ul>
        </>
      )}

      <Heading>Repository</Heading>
      <RepoDetails />
    </div>
  )
}
