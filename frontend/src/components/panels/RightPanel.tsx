import { FileCode2Icon, FileDiffIcon, InfoIcon, SquareTerminalIcon, TerminalIcon } from 'lucide-react'
import { ChangesTab } from '@/components/panels/ChangesTab'
import { CommandsTab } from '@/components/panels/CommandsTab'
import { DetailsTab } from '@/components/panels/DetailsTab'
import { FilesTab } from '@/components/panels/FilesTab'
import { TerminalTab } from '@/components/panels/TerminalTab'
import { isMac } from '@/hooks/useShortcuts'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { useTask } from '@/lib/queries'
import { useUi, type RightTab } from '@/store/ui'

export function RightPanel() {
  const tab = useUi((s) => s.rightTab)
  const setTab = useUi((s) => s.setRightTab)
  const taskId = useUi((s) => s.selectedTaskId)
  const task = useTask(taskId)
  const commandCount = task.data?.commands.length ?? 0
  const patchCount = task.data?.patches.filter((p) => p.status === 'applied').length ?? 0

  return (
    <Tabs value={tab} onValueChange={(v) => setTab(v as RightTab)} className="flex h-full min-h-0 flex-col gap-0 bg-panel">
      <div className="flex h-12 shrink-0 items-center border-b px-2">
        <TabsList className="w-full">
          <TabsTrigger value="files">
            <FileCode2Icon />
            Files
          </TabsTrigger>
          <TabsTrigger value="changes">
            <FileDiffIcon />
            Changes
            {patchCount > 0 && <span className="rounded bg-primary/20 px-1 text-[10px] text-primary">{patchCount}</span>}
          </TabsTrigger>
          <TabsTrigger value="commands">
            <TerminalIcon />
            Commands
            {commandCount > 0 && <span className="rounded bg-muted-foreground/20 px-1 text-[10px]">{commandCount}</span>}
          </TabsTrigger>
          <TabsTrigger value="terminal" title={`Terminal (${isMac() ? '⌘J' : 'Ctrl+J'})`}>
            <SquareTerminalIcon />
            Terminal
          </TabsTrigger>
          <TabsTrigger value="details">
            <InfoIcon />
            Details
          </TabsTrigger>
        </TabsList>
      </div>
      <TabsContent value="files" className="min-h-0">
        <FilesTab />
      </TabsContent>
      <TabsContent value="changes" className="min-h-0">
        <ChangesTab />
      </TabsContent>
      <TabsContent value="commands" className="min-h-0">
        <CommandsTab />
      </TabsContent>
      <TabsContent value="terminal" className="min-h-0">
        <TerminalTab />
      </TabsContent>
      <TabsContent value="details" className="min-h-0">
        <DetailsTab />
      </TabsContent>
    </Tabs>
  )
}
