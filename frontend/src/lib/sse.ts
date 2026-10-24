import { authHeaders, getConnection } from './api'
import type { TaskEvent } from './types'

export interface SseFrame {
  id: string | null
  event: string | null
  data: string
}

/**
 * Incremental parser for the text/event-stream format (WHATWG HTML §9.2).
 * Feed it decoded text chunks; it returns every complete frame.
 */
export class SseParser {
  private buffer = ''
  private dataLines: string[] = []
  private eventName: string | null = null
  private frameId: string | null = null

  push(chunk: string): SseFrame[] {
    this.buffer += chunk
    const frames: SseFrame[] = []
    for (;;) {
      const match = /\r\n|\n|\r/.exec(this.buffer)
      if (!match) break
      // A trailing CR may be the first half of a CRLF split across chunks.
      if (match[0] === '\r' && match.index === this.buffer.length - 1) break
      const line = this.buffer.slice(0, match.index)
      this.buffer = this.buffer.slice(match.index + match[0].length)
      const frame = this.processLine(line)
      if (frame) frames.push(frame)
    }
    return frames
  }

  private processLine(line: string): SseFrame | null {
    if (line === '') {
      if (this.dataLines.length === 0) {
        this.eventName = null
        this.frameId = null
        return null
      }
      const frame: SseFrame = { id: this.frameId, event: this.eventName, data: this.dataLines.join('\n') }
      this.dataLines = []
      this.eventName = null
      this.frameId = null
      return frame
    }
    if (line.startsWith(':')) return null // comment / heartbeat
    const colon = line.indexOf(':')
    const field = colon === -1 ? line : line.slice(0, colon)
    let value = colon === -1 ? '' : line.slice(colon + 1)
    if (value.startsWith(' ')) value = value.slice(1)
    if (field === 'data') this.dataLines.push(value)
    else if (field === 'event') this.eventName = value
    else if (field === 'id' && !value.includes('\0')) this.frameId = value
    return null
  }
}

export type StreamState = 'connecting' | 'open' | 'reconnecting' | 'closed' | 'error'

export interface TaskStreamOptions {
  taskId: string
  /** Last persisted event id already seen; the server replays everything after it. */
  after: number
  onEvent: (event: TaskEvent) => void
  onState?: (state: StreamState, info?: string) => void
}

export interface StreamHandle {
  close: () => void
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

/**
 * Subscribes to a task's SSE stream using fetch (so the Authorization header can be sent),
 * resuming from the last seen event id after disconnects with exponential backoff.
 */
export function streamTaskEvents(opts: TaskStreamOptions): StreamHandle {
  const controller = new AbortController()
  let lastId = opts.after
  let failures = 0
  let closed = false

  const run = async () => {
    while (!closed) {
      opts.onState?.(failures === 0 ? 'connecting' : 'reconnecting')
      try {
        const { baseUrl } = getConnection()
        const res = await fetch(
          `${baseUrl}/api/tasks/${encodeURIComponent(opts.taskId)}/events?after=${lastId}`,
          {
            headers: authHeaders({ Accept: 'text/event-stream' }),
            signal: controller.signal,
            cache: 'no-store',
          },
        )
        if (res.status === 404 || res.status === 401 || res.status === 403) {
          opts.onState?.('error', `Event stream unavailable (HTTP ${res.status})`)
          return
        }
        if (!res.ok || !res.body) throw new Error(`HTTP ${res.status}`)
        failures = 0
        opts.onState?.('open')
        const reader = res.body.getReader()
        const decoder = new TextDecoder()
        const parser = new SseParser()
        for (;;) {
          const { value, done } = await reader.read()
          if (done) break
          for (const frame of parser.push(decoder.decode(value, { stream: true }))) {
            if (!frame.data) continue
            let parsed: TaskEvent
            try {
              parsed = JSON.parse(frame.data) as TaskEvent
            } catch {
              continue
            }
            if (frame.id !== null) {
              const n = Number(frame.id)
              if (Number.isFinite(n) && n > lastId) lastId = n
            }
            opts.onEvent(parsed)
          }
        }
      } catch {
        if (controller.signal.aborted) return
      }
      if (closed) return
      failures += 1
      opts.onState?.('reconnecting')
      await sleep(Math.min(10_000, 500 * 2 ** Math.min(failures, 5)), controller.signal)
    }
  }

  void run()
  return {
    close() {
      if (closed) return
      closed = true
      controller.abort()
      opts.onState?.('closed')
    },
  }
}
