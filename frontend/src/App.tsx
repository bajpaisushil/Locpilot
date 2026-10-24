import { QueryClientProvider } from '@tanstack/react-query'
import { useEffect } from 'react'
import { Toaster } from 'sonner'
import { AppShell } from '@/components/layout/AppShell'
import { BackendGate } from '@/components/setup/BackendGate'
import { SetupGate } from '@/components/setup/SetupGate'
import { TooltipProvider } from '@/components/ui/tooltip'
import { createQueryClient } from '@/lib/query-client'
import { useUi } from '@/store/ui'

const queryClient = createQueryClient()

function useThemeClass() {
  const theme = useUi((s) => s.theme)
  useEffect(() => {
    const root = document.documentElement
    root.classList.toggle('dark', theme === 'dark')
    root.style.colorScheme = theme
  }, [theme])
  return theme
}

export default function App() {
  const theme = useThemeClass()
  return (
    <QueryClientProvider client={queryClient}>
      <TooltipProvider>
        <div className="h-full">
          <BackendGate>
            <SetupGate>
              <AppShell />
            </SetupGate>
          </BackendGate>
        </div>
        <Toaster theme={theme} position="bottom-right" closeButton richColors />
      </TooltipProvider>
    </QueryClientProvider>
  )
}
