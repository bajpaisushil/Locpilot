import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, type RenderOptions } from '@testing-library/react'
import type { ReactElement } from 'react'
import { vi } from 'vitest'
import { TooltipProvider } from '@/components/ui/tooltip'
import { setConnection } from '@/lib/api'

export function createTestQueryClient() {
  return new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: Infinity, refetchOnWindowFocus: false }, mutations: { retry: false } },
  })
}

export function renderWithProviders(ui: ReactElement, opts: RenderOptions & { queryClient?: QueryClient } = {}) {
  const queryClient = opts.queryClient ?? createTestQueryClient()
  setConnection({ baseUrl: '', token: null })
  const result = render(
    <QueryClientProvider client={queryClient}>
      <TooltipProvider>{ui}</TooltipProvider>
    </QueryClientProvider>,
    opts,
  )
  return { queryClient, ...result }
}

export const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } })

export interface RecordedRequest {
  method: string
  path: string
  body: unknown
  headers: Record<string, string>
}

type Handler = (req: RecordedRequest) => Response | Promise<Response>

/**
 * Installs a fetch mock routed by "METHOD /path" (query string ignored). Unmatched requests fail
 * loudly. Returns the list of recorded requests for assertions.
 */
export function mockFetch(routes: Record<string, Handler | unknown>) {
  const calls: RecordedRequest[] = []
  const fn = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = typeof input === 'string' ? input : input instanceof URL ? input.toString() : input.url
    const method = (init?.method ?? 'GET').toUpperCase()
    const path = url.replace(/^https?:\/\/[^/]+/, '').split('?')[0]
    const headers = Object.fromEntries(Object.entries((init?.headers as Record<string, string>) ?? {}))
    const body = typeof init?.body === 'string' ? JSON.parse(init.body) : undefined
    const req = { method, path, body, headers }
    calls.push(req)
    const route = routes[`${method} ${path}`]
    if (route === undefined) return json({ error: 'not_mocked', detail: `${method} ${path}`, hint: null }, 599)
    return typeof route === 'function' ? (route as Handler)(req) : json(route)
  })
  vi.stubGlobal('fetch', fn)
  return { calls, fn }
}
