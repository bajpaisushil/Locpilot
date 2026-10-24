import '@xterm/xterm/css/xterm.css'
import { FitAddon } from '@xterm/addon-fit'
import { WebLinksAddon } from '@xterm/addon-web-links'
import { Terminal } from '@xterm/xterm'
import { useQueryClient } from '@tanstack/react-query'
import { MessageSquarePlusIcon } from 'lucide-react'
import { useEffect, useRef, useState } from 'react'
import { Button } from '@/components/ui/button'
import { Hint } from '@/components/ui/tooltip'
import { api, errorMessage } from '@/lib/api'
import { openExternal } from '@/lib/desktop'
import { qk } from '@/lib/queries'
import { createInputSender, streamTerminal, tailText, TERMINAL_FONT, TERMINAL_THEMES } from '@/lib/terminal'
import type { TerminalOut } from '@/lib/types'
import { useUi } from '@/store/ui'

/** One live terminal: renders the shell's output and sends keystrokes, resizes and pastes back. */
export function XtermView({ terminal }: { terminal: TerminalOut }) {
  const theme = useUi((s) => s.theme)
  const qc = useQueryClient()
  const containerRef = useRef<HTMLDivElement>(null)
  const termRef = useRef<Terminal | null>(null)
  const [exited, setExited] = useState<{ code: number | null } | null>(
    terminal.running ? null : { code: terminal.exit_code },
  )
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    const el = containerRef.current
    if (!el) return
    const term = new Terminal({
      fontFamily: TERMINAL_FONT,
      fontSize: 12,
      lineHeight: 1.15,
      cursorBlink: true,
      scrollback: 5000,
      theme: TERMINAL_THEMES[useUi.getState().theme],
    })
    const fit = new FitAddon()
    term.loadAddon(fit)
    term.loadAddon(new WebLinksAddon((_event, uri) => void openExternal(uri)))
    term.open(el)
    termRef.current = term

    const send = createInputSender(
      (data) => api.terminalInput(terminal.id, data),
      (err) => setError(errorMessage(err)),
    )
    // Redrawing history makes xterm answer the old queries in it (cursor position, colours…) again;
    // those answers are not typing and must never reach the shell.
    let replaying = false
    const input = term.onData((data) => {
      if (!replaying) send(data)
    })

    let lastSize = ''
    let frame = 0
    const resize = () => {
      cancelAnimationFrame(frame)
      frame = requestAnimationFrame(() => {
        try {
          fit.fit()
        } catch {
          return // not laid out yet
        }
        const size = `${term.cols}x${term.rows}`
        if (size === lastSize) return
        lastSize = size
        api.resizeTerminal(terminal.id, term.cols, term.rows).catch(() => {})
      })
    }
    const observer = new ResizeObserver(resize)
    observer.observe(el)
    resize()

    const stream = streamTerminal(terminal.id, {
      onReset: (scrollback) => {
        term.reset()
        if (!scrollback) return
        replaying = true
        term.write(scrollback, () => {
          replaying = false
        })
      },
      onOutput: (data) => term.write(data),
      onExit: (code) => {
        setExited({ code })
        term.write(`\r\n\x1b[2m[process exited with code ${code ?? '?'}]\x1b[0m\r\n`)
        void qc.invalidateQueries({ queryKey: qk.terminals(terminal.repo_id) }) // the tab shows "(exited)" now
      },
      onClosed: () => setExited((prev) => prev ?? { code: null }),
    })
    term.focus()
    return () => {
      stream.close()
      observer.disconnect()
      cancelAnimationFrame(frame)
      input.dispose()
      term.dispose()
      termRef.current = null
    }
  }, [terminal.id, terminal.repo_id, qc])

  useEffect(() => {
    if (termRef.current) termRef.current.options.theme = TERMINAL_THEMES[theme]
  }, [theme])

  const shareOutput = () => {
    const term = termRef.current
    if (!term) return
    let text = term.getSelection()
    if (!text.trim()) {
      const buffer = term.buffer.active
      const lines: string[] = []
      for (let i = 0; i < buffer.length; i++) lines.push(buffer.getLine(i)?.translateToString(true) ?? '')
      text = tailText(lines)
    }
    if (!text.trim()) return
    const where = terminal.location === 'worktree' ? 'in LocPilot’s worktree' : 'in my repository'
    const { composerDraft, setComposerDraft } = useUi.getState()
    setComposerDraft(`${composerDraft ? `${composerDraft}\n\n` : ''}Terminal output (${where}):\n\`\`\`\n${text}\n\`\`\`\n`)
  }

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="flex shrink-0 items-center gap-2 border-b px-3 py-1.5 text-[11px] text-muted-foreground">
        <span className="min-w-0 flex-1 truncate">
          {terminal.location === 'worktree' ? 'LocPilot’s worktree (its changes, not your checkout)' : 'Your repository folder'}
          {' · '}
          <span className="font-mono">{terminal.cwd}</span>
        </span>
        {exited && <span>exited{exited.code !== null ? ` (${exited.code})` : ''}</span>}
        <Hint label="Put the selected text (or the latest output) into your message to LocPilot">
          <Button variant="ghost" size="xs" onClick={shareOutput}>
            <MessageSquarePlusIcon />
            Send to LocPilot
          </Button>
        </Hint>
      </div>
      {error && <div className="border-b px-3 py-1 text-[11px] text-destructive">{error}</div>}
      <div
        ref={containerRef}
        className="min-h-0 flex-1 overflow-hidden px-2 pt-1"
        style={{ background: TERMINAL_THEMES[theme].background }}
        data-testid="terminal-view"
      />
    </div>
  )
}
