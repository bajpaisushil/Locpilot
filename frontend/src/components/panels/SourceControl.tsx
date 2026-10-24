import {
  CheckIcon,
  CloudUploadIcon,
  ExternalLinkIcon,
  FolderInputIcon,
  GitBranchIcon,
  GitCommitHorizontalIcon,
  RefreshCwIcon,
} from 'lucide-react'
import { useState } from 'react'
import { toast } from 'sonner'
import { ErrorState } from '@/components/common/States'
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from '@/components/ui/alert-dialog'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Checkbox } from '@/components/ui/checkbox'
import { Input } from '@/components/ui/input'
import { Textarea } from '@/components/ui/textarea'
import { Hint } from '@/components/ui/tooltip'
import { openExternal } from '@/lib/desktop'
import { isActive, useApplyToRepo, useCommit, usePush, useWorktreeChanges } from '@/lib/queries'
import type { TaskDetail, WorktreeChangeFile } from '@/lib/types'
import { cn, shortSha } from '@/lib/utils'

const STATUS_CLASS: Record<WorktreeChangeFile['status'], string> = {
  A: 'text-success',
  M: 'text-warning',
  D: 'text-destructive',
}
const MOD_KEY = typeof navigator !== 'undefined' && /mac/i.test(navigator.platform) ? '⌘' : 'Ctrl'

/**
 * VS Code-style source control for a task worktree: type a message, pick files, Commit / Commit & Push,
 * or apply the changes to the user's own repository folder.
 */
export function SourceControl({ task }: { task: TaskDetail }) {
  const changes = useWorktreeChanges(task.id)
  const commit = useCommit()
  const push = usePush()
  const apply = useApplyToRepo()
  const [message, setMessage] = useState('')
  const [branch, setBranch] = useState<string | null>(null)
  const [checked, setChecked] = useState<Record<string, boolean>>({})
  const [confirmApply, setConfirmApply] = useState(false)

  const data = changes.data
  const files = data?.files ?? []
  const isChecked = (f: WorktreeChangeFile) => checked[f.path] ?? f.locpilot
  const selected = files.filter(isChecked).map((f) => f.path)
  const branchName = (branch ?? data?.suggested_branch ?? '').trim()
  const busy = commit.isPending || push.isPending || apply.isPending || isActive(task.status)
  const publish = data?.publish ?? null
  const unpushed = !!publish && publish.head !== publish.pushed_head
  const canCommit = !!data && selected.length > 0 && !!branchName && !busy
  const error = commit.error ?? push.error ?? apply.error

  const resetErrors = () => {
    commit.reset()
    push.reset()
    apply.reset()
  }

  const doPush = async () => {
    resetErrors()
    try {
      const res = await push.mutateAsync(task.id)
      toast.success(`Pushed ${res.publish.branch}`, { description: res.publish.remote_url ?? 'origin' })
    } catch {
      // shown below
    }
  }

  const doCommit = async (andPush: boolean) => {
    if (!canCommit || !data) return
    resetErrors()
    try {
      const res = await commit.mutateAsync({
        taskId: task.id,
        branch: branchName,
        message: message.trim() || data.suggested_message,
        files: selected,
      })
      setMessage('')
      setChecked({})
      toast.success(`Committed to ${res.branch}`, {
        description: `${shortSha(res.commit)} · ${res.files.length} file${res.files.length === 1 ? '' : 's'}`,
      })
    } catch {
      return
    }
    if (andPush) await doPush()
  }

  const doApply = async () => {
    resetErrors()
    try {
      const res = await apply.mutateAsync({ taskId: task.id, files: selected })
      toast.success(`Applied ${res.applied.length} file${res.applied.length === 1 ? '' : 's'} to your repository`, {
        description: res.repo_path,
      })
    } catch {
      // shown below
    } finally {
      setConfirmApply(false)
    }
  }

  return (
    <section className="shrink-0 space-y-2 border-b p-3" aria-label="Source control">
      <div className="flex items-center gap-2 text-[11px] text-muted-foreground">
        <GitCommitHorizontalIcon className="size-3.5" />
        <span className="font-medium tracking-wide uppercase">Source control</span>
        <span className="ml-auto">
          {files.length} uncommitted change{files.length === 1 ? '' : 's'}
        </span>
        <Hint label="Refresh (picks up edits made from a terminal)">
          <Button variant="ghost" size="icon-xs" onClick={() => void changes.refetch()} aria-label="Refresh changes">
            <RefreshCwIcon className={cn(changes.isFetching && 'animate-spin')} />
          </Button>
        </Hint>
      </div>

      <Textarea
        value={message}
        onChange={(e) => setMessage(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) {
            e.preventDefault()
            void doCommit(false)
          }
        }}
        rows={2}
        placeholder={`Message (${MOD_KEY}↵ to commit)${data?.suggested_message ? ` — default: ${data.suggested_message}` : ''}`}
        aria-label="Commit message"
        className="min-h-14 resize-y px-2.5 py-1.5 text-xs"
      />
      <div className="flex items-center gap-2">
        <GitBranchIcon className="size-3.5 shrink-0 text-muted-foreground" />
        <Input
          value={branchName}
          onChange={(e) => setBranch(e.target.value)}
          aria-label="Branch"
          spellCheck={false}
          autoComplete="off"
          className="h-7 font-mono text-xs"
        />
      </div>
      <div className="flex flex-wrap items-center gap-2">
        <Button size="xs" onClick={() => void doCommit(false)} disabled={!canCommit}>
          <CheckIcon />
          Commit
        </Button>
        <Hint label="Commit, then push the branch to origin with your own Git credentials">
          <Button size="xs" variant="outline" onClick={() => void doCommit(true)} disabled={!canCommit}>
            <CloudUploadIcon />
            Commit &amp; Push
          </Button>
        </Hint>
        {unpushed && (
          <Button size="xs" variant="outline" onClick={() => void doPush()} disabled={busy}>
            <CloudUploadIcon />
            Push
          </Button>
        )}
        <Hint label="Write the selected files into your own repository folder as plain edits (no commit; your branch and staging area are untouched)">
          <Button
            size="xs"
            variant="ghost"
            className="ml-auto"
            onClick={() => setConfirmApply(true)}
            disabled={!selected.length || busy}
          >
            <FolderInputIcon />
            Apply to my repo
          </Button>
        </Hint>
      </div>

      {files.length > 0 ? (
        <ul className="max-h-32 overflow-y-auto rounded-md border py-0.5" aria-label="Uncommitted changes">
          {files.map((f) => (
            <li key={f.path}>
              <label className="flex cursor-pointer items-center gap-2 px-2 py-0.5 text-xs hover:bg-accent/50">
                <Checkbox
                  checked={isChecked(f)}
                  onCheckedChange={(v) => setChecked((c) => ({ ...c, [f.path]: v === true }))}
                  aria-label={`Include ${f.path}`}
                />
                <span className={cn('w-3 shrink-0 font-mono font-semibold', STATUS_CLASS[f.status])}>{f.status}</span>
                <span className="min-w-0 flex-1 truncate font-mono">{f.path}</span>
                {f.locpilot && <Badge variant="outline">LocPilot</Badge>}
              </label>
            </li>
          ))}
        </ul>
      ) : (
        data && <p className="text-xs text-muted-foreground">No uncommitted changes in LocPilot’s worktree.</p>
      )}

      {publish && (
        <div className="flex flex-wrap items-center gap-x-1.5 gap-y-1 text-[11px] text-muted-foreground" data-testid="publish-state">
          <CheckIcon className="size-3 text-success" />
          <span>
            Committed <code className="font-mono">{shortSha(publish.head)}</code> to{' '}
            <code className="font-mono">{publish.branch}</code>
          </span>
          <span>·</span>
          <span className={cn(!unpushed && 'text-success')}>{unpushed ? 'not pushed yet' : 'pushed to origin'}</span>
          {!unpushed && publish.pull_request_url && (
            <Button variant="link" size="xs" className="h-auto px-1" onClick={() => void openExternal(publish.pull_request_url as string)}>
              Create pull request
              <ExternalLinkIcon />
            </Button>
          )}
        </div>
      )}
      {changes.isError && <ErrorState error={changes.error} compact />}
      {error && <ErrorState error={error} compact />}

      <AlertDialog open={confirmApply} onOpenChange={setConfirmApply}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>
              Apply {selected.length} file{selected.length === 1 ? '' : 's'} to your repository?
            </AlertDialogTitle>
            <AlertDialogDescription>
              The changes are written into <span className="font-mono">{task.repo.path}</span> as uncommitted edits, so
              your editor and anything running from that folder see them right away. Your branch, staging area and
              commits are not touched, and nothing is written if your folder has conflicting edits.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <ul className="max-h-40 overflow-y-auto font-mono text-xs">
            {selected.map((p) => (
              <li key={p}>{p}</li>
            ))}
          </ul>
          <AlertDialogFooter>
            <AlertDialogCancel>Cancel</AlertDialogCancel>
            <AlertDialogAction
              onClick={(e) => {
                e.preventDefault()
                void doApply()
              }}
              disabled={apply.isPending}
            >
              Apply to my repo
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </section>
  )
}
