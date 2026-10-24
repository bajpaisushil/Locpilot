import { authHeaders, getConnection } from './api'
import { SseParser } from './sse'

/** Same monospace stack as the code viewers (kept here so terminals don't pull in Monaco). */
export const TERMINAL_FONT =
  'ui-monospace, "SF Mono", SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", monospace'

/** Terminal colours matching the editor themes (xterm needs concrete colours, not CSS variables). */
export const TERMINAL_THEMES = {
  dark: { background: '#121216', foreground: '#e8e8ec', cursor: '#a5a8ff', selectionBackground: '#3a3d5c' },
  light: { background: '#fbfbfc', foreground: '#26262c', cursor: '#4f46e5', selectionBackground: '#c7cbf5' },
} as const

export interface TerminalStreamHandlers {
  /** The full scrollback so far: sent first on every (re)connect, so the view can redraw from it. */
  onReset: (scrollback: string) => void
  onOutput: (data: string) => void
  onExit: (code: number | null) => void
  onClosed?: () => void
}

function sleep(ms: number, signal: AbortSignal) {
  return new Promise<void>((resolve) => {
    const timer = setTimeout(resolve, ms)
    signal.addEventListener('abort', () => {
      clearTimeout(timer)
      resolve()
    })
  })
}

/** Follows a terminal's output, reconnecting (with a scrollback reset) until its shell exits or it is closed. */
export function streamTerminal(terminalId: string, handlers: TerminalStreamHandlers): { close: () => void } {
  const controller = new AbortController()
  let closed = false

  const run = async () => {
    let failures = 0
    while (!closed) {
      let exited = false
      try {
        const { baseUrl } = getConnection()
        const res = await fetch(`${baseUrl}/api/terminals/${encodeURIComponent(terminalId)}/stream`, {
          headers: authHeaders({ Accept: 'text/event-stream' }),
          signal: controller.signal,
          cache: 'no-store',
        })
        if (res.status === 404) {
          handlers.onClosed?.()
          return
        }
        if (!res.ok || !res.body) throw new Error(`HTTP ${res.status}`)
        failures = 0
        const reader = res.body.getReader()
        const decoder = new TextDecoder()
        const parser = new SseParser()
        for (;;) {
          const { value, done } = await reader.read()
          if (done) break
          for (const frame of parser.push(decoder.decode(value, { stream: true }))) {
            const payload = JSON.parse(frame.data) as { data?: string; code?: number | null }
            if (frame.event === 'reset') handlers.onReset(payload.data ?? '')
            else if (frame.event === 'output') handlers.onOutput(payload.data ?? '')
            else if (frame.event === 'exit') {
              exited = true
              handlers.onExit(payload.code ?? null)
            }
          }
        }
      } catch (err) {
        if (closed || (err instanceof DOMException && err.name === 'AbortError')) return
        failures++
      }
      if (exited) return
      await sleep(Math.min(250 * 2 ** failures, 5000), controller.signal)
    }
  }
  void run()
  return {
    close: () => {
      closed = true
      controller.abort()
    },
  }
}

/** Batches keystrokes (one request per burst) and sends them strictly in order. */
export function createInputSender(send: (data: string) => Promise<void>, onError: (err: unknown) => void) {
  let pending = ''
  let inflight: Promise<void> | null = null
  const flush = async () => {
    while (pending) {
      const chunk = pending
      pending = ''
      try {
        await send(chunk)
      } catch (err) {
        onError(err)
      }
    }
    inflight = null
  }
  return (data: string) => {
    pending += data
    if (!inflight) inflight = new Promise((resolve) => setTimeout(resolve, 4)).then(flush)
  }
}

/** The selection, or else the last lines of output, for sharing with LocPilot. */
export function tailText(lines: string[], max = 60): string {
  const trimmed = [...lines]
  while (trimmed.length && !trimmed[trimmed.length - 1].trim()) trimmed.pop()
  return trimmed.slice(-max).join('\n')
}
