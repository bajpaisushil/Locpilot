import { useState, type ReactNode } from 'react'
import { SetupScreen } from '@/components/setup/SetupScreen'
import { Spinner } from '@/components/common/States'
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { useSetupStatus } from '@/lib/queries'
import { useUi } from '@/store/ui'

function SetupDialog() {
  const open = useUi((s) => s.setupOpen)
  const setOpen = useUi((s) => s.setSetupOpen)
  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogContent className="sm:max-w-2xl">
        <DialogHeader>
          <DialogTitle>Dependency setup</DialogTitle>
          <DialogDescription>Live status of the local dependencies LocPilot needs.</DialogDescription>
        </DialogHeader>
        {open && <SetupScreen embedded onContinue={() => setOpen(false)} />}
      </DialogContent>
    </Dialog>
  )
}

/**
 * First run (or any launch where a required dependency is missing): show the full-page setup
 * screen. The user continues once everything is ready, or explicitly opts into browse-only mode.
 */
export function SetupGate({ children }: { children: ReactNode }) {
  const setup = useSetupStatus()
  const dismissed = useUi((s) => s.setupDismissed)
  const setDismissed = useUi((s) => s.setSetupDismissed)
  const [gate, setGate] = useState<'unknown' | 'show' | 'hide'>('unknown')

  if (gate === 'unknown') {
    if (setup.data) setGate(setup.data.ready ? 'hide' : 'show')
    else if (setup.isError) setGate('show')
  }

  if (gate === 'unknown') {
    return (
      <div className="flex h-full items-center justify-center gap-2 text-sm text-muted-foreground" role="status">
        <Spinner />
        Checking local dependencies…
      </div>
    )
  }

  if (gate === 'show' && !dismissed) {
    return (
      <div className="h-full overflow-y-auto">
        <SetupScreen
          onContinue={() => setGate('hide')}
          onBrowseWithoutAi={() => {
            setDismissed(true)
            setGate('hide')
          }}
        />
      </div>
    )
  }

  return (
    <>
      {children}
      <SetupDialog />
    </>
  )
}
