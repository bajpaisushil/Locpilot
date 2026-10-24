import { FolderGit2Icon, FolderOpenIcon, GitForkIcon, InfoIcon, Loader2Icon, LockIcon } from 'lucide-react'
import { useState } from 'react'
import { ErrorState } from '@/components/common/States'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { isTauri, pickDirectory } from '@/lib/desktop'
import { useCreateRepo } from '@/lib/queries'
import type { RepoDetail } from '@/lib/types'
import { validateRepoUrl } from '@/lib/validation'
import { useUi } from '@/store/ui'

export function AddRepoDialog({ onAdded }: { onAdded?: (repo: RepoDetail) => void }) {
  const open = useUi((s) => s.addRepoOpen)
  const setOpen = useUi((s) => s.setAddRepoOpen)
  const selectRepo = useUi((s) => s.selectRepo)
  const [tab, setTab] = useState<'local' | 'github'>('local')
  const [url, setUrl] = useState('')
  const [path, setPath] = useState('')
  const [touched, setTouched] = useState(false)
  const create = useCreateRepo()
  const desktop = isTauri()

  const urlError = tab === 'github' && touched ? validateRepoUrl(url) : null

  const close = (next: boolean) => {
    if (create.isPending) return
    setOpen(next)
    if (!next) {
      create.reset()
      setTouched(false)
    }
  }

  const submit = async () => {
    setTouched(true)
    if (tab === 'github' && validateRepoUrl(url)) return
    if (tab === 'local' && !path.trim()) return
    try {
      const repo = await create.mutateAsync(
        tab === 'github' ? { source: 'github', url: url.trim() } : { source: 'local', path: path.trim() },
      )
      selectRepo(repo.id)
      onAdded?.(repo)
      setUrl('')
      setPath('')
      setTouched(false)
      setOpen(false)
    } catch {
      // shown via create.error
    }
  }

  return (
    <Dialog open={open} onOpenChange={close}>
      <DialogContent className="sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>Add a repository</DialogTitle>
          <DialogDescription>
            Open an existing local Git checkout or clone a public repository into LocPilot&apos;s workspace.
          </DialogDescription>
        </DialogHeader>
        <Tabs
          value={tab}
          onValueChange={(v) => {
            setTab(v as 'local' | 'github')
            create.reset()
          }}
        >
          <TabsList className="w-full">
            <TabsTrigger value="local">
              <FolderGit2Icon />
              Local folder
            </TabsTrigger>
            <TabsTrigger value="github">
              <GitForkIcon />
              Clone from URL
            </TabsTrigger>
          </TabsList>

          <TabsContent value="local" className="space-y-3 pt-2">
            <div className="space-y-1.5">
              <Label htmlFor="repo-path">Repository folder</Label>
              <div className="flex gap-2">
                <Input
                  id="repo-path"
                  placeholder={desktop ? 'Choose a folder…' : '/Users/you/code/my-project'}
                  value={path}
                  onChange={(e) => setPath(e.target.value)}
                  onKeyDown={(e) => e.key === 'Enter' && void submit()}
                  className="font-mono text-xs"
                  spellCheck={false}
                  autoComplete="off"
                />
                {desktop && (
                  <Button
                    variant="outline"
                    onClick={async () => {
                      const picked = await pickDirectory()
                      if (picked) setPath(picked)
                    }}
                  >
                    <FolderOpenIcon />
                    Browse…
                  </Button>
                )}
              </div>
              <p className="text-[11px] text-muted-foreground">
                {desktop
                  ? 'Pick the root of a Git work tree.'
                  : 'Enter the absolute path of a Git work tree inside your home folder.'}{' '}
                LocPilot never modifies this working tree — code changes happen in a separate Git worktree, and your
                uncommitted changes are preserved.
              </p>
            </div>
          </TabsContent>

          <TabsContent value="github" className="space-y-3 pt-2">
            <div className="space-y-1.5">
              <Label htmlFor="repo-url">Repository URL</Label>
              <Input
                id="repo-url"
                placeholder="https://github.com/owner/repo"
                value={url}
                onChange={(e) => setUrl(e.target.value)}
                onBlur={() => setTouched(true)}
                onKeyDown={(e) => e.key === 'Enter' && void submit()}
                aria-invalid={!!urlError}
                className="font-mono text-xs"
                spellCheck={false}
                autoComplete="off"
              />
              {urlError && <p className="text-xs text-destructive">{urlError}</p>}
            </div>
            <div className="flex items-start gap-2 rounded-md border bg-muted/30 px-3 py-2 text-[11px] text-muted-foreground">
              <LockIcon className="mt-0.5 size-3.5 shrink-0" />
              <span>
                Public repositories need no credentials. For private repositories, configure SSH keys
                (git@github.com:owner/repo.git) or your system Git credential helper — never paste tokens into the URL.
                LocPilot does not store or see your credentials.
              </span>
            </div>
          </TabsContent>
        </Tabs>

        {create.isError && <ErrorState error={create.error} title="Could not add the repository" />}
        {create.isPending && tab === 'github' && (
          <div className="flex items-center gap-2 text-xs text-muted-foreground">
            <InfoIcon className="size-3.5" />
            Cloning can take a while for large repositories.
          </div>
        )}

        <div className="flex justify-end gap-2">
          <Button variant="ghost" onClick={() => close(false)} disabled={create.isPending}>
            Cancel
          </Button>
          <Button
            onClick={() => void submit()}
            disabled={create.isPending || (tab === 'local' ? !path.trim() : !url.trim())}
          >
            {create.isPending && <Loader2Icon className="animate-spin" />}
            {tab === 'github' ? (create.isPending ? 'Cloning…' : 'Clone repository') : create.isPending ? 'Opening…' : 'Open repository'}
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  )
}
