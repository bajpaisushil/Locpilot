import { useEffect } from 'react'
import { ChatPanel } from '@/components/chat/ChatPanel'
import { Sidebar } from '@/components/layout/Sidebar'
import { TopBar } from '@/components/layout/TopBar'
import { RightPanel } from '@/components/panels/RightPanel'
import { AddRepoDialog } from '@/components/repos/AddRepoDialog'
import { SettingsDialog } from '@/components/settings/SettingsDialog'
import { ResizableGroup, ResizableHandle, ResizablePanel } from '@/components/ui/resizable'
import { useTaskStream } from '@/hooks/useTaskStream'
import { useRepos } from '@/lib/queries'
import { useUi } from '@/store/ui'

export function AppShell() {
  const rightOpen = useUi((s) => s.rightPanelOpen)
  const taskId = useUi((s) => s.selectedTaskId)
  const repoId = useUi((s) => s.selectedRepoId)
  const selectRepo = useUi((s) => s.selectRepo)
  const repos = useRepos()

  useTaskStream(taskId)

  // Keep the selection valid: fall back to the most recently opened repository.
  useEffect(() => {
    if (!repos.data || repos.isFetching) return
    if (repoId && repos.data.some((r) => r.id === repoId)) return
    selectRepo(repos.data[0]?.id ?? null)
  }, [repos.data, repos.isFetching, repoId, selectRepo])

  return (
    <div className="flex h-full min-h-0">
      <ResizableGroup orientation="horizontal">
        <ResizablePanel id="sidebar" defaultSize={264} minSize={210} maxSize={420}>
          <Sidebar />
        </ResizablePanel>
        <ResizableHandle />
        <ResizablePanel id="main" minSize={420}>
          <div className="flex h-full min-h-0 flex-col">
            <TopBar />
            <div className="min-h-0 flex-1">
              <ChatPanel />
            </div>
          </div>
        </ResizablePanel>
        {rightOpen && (
          <>
            <ResizableHandle />
            <ResizablePanel id="right" defaultSize="38" minSize={320}>
              <RightPanel />
            </ResizablePanel>
          </>
        )}
      </ResizableGroup>
      <AddRepoDialog />
      <SettingsDialog />
    </div>
  )
}
