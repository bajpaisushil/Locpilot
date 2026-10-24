import { memo, type ReactNode } from 'react'
import ReactMarkdown, { defaultUrlTransform, type Components } from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { toast } from 'sonner'
import { useCitationScope } from '@/components/chat/citation-context'
import { copyText, openExternal } from '@/lib/desktop'
import { parseLocpilotLink } from '@/lib/utils'
import { useUi } from '@/store/ui'

function urlTransform(url: string) {
  return url.startsWith('locpilot://') ? url : defaultUrlTransform(url)
}

function MarkdownLink({ href, children }: { href?: string; children?: ReactNode }) {
  const openFile = useUi((s) => s.openFile)
  const scope = useCitationScope()
  const link = parseLocpilotLink(href)
  if (link) {
    return (
      <button
        type="button"
        className="rounded bg-primary/10 px-1 font-mono text-[0.85em] text-primary hover:bg-primary/20"
        onClick={() => openFile({ path: link.path, start: link.start, end: link.end, taskId: scope.taskId })}
      >
        {children}
      </button>
    )
  }
  return (
    <a
      href={href}
      onClick={async (e) => {
        // Never navigate the app webview; open https links in the system browser.
        e.preventDefault()
        if (!href) return
        try {
          await openExternal(href)
        } catch {
          if (await copyText(href)) toast.message('Link copied to clipboard', { description: href })
        }
      }}
    >
      {children}
    </a>
  )
}

const components: Components = {
  a: ({ href, children }) => <MarkdownLink href={href}>{children}</MarkdownLink>,
}

/** Renders untrusted markdown safely: no raw HTML, only http(s)/mailto and locpilot:// links. */
export const Markdown = memo(function Markdown({ children, className }: { children: string; className?: string }) {
  return (
    <div className={className ?? 'prose-rp'}>
      <ReactMarkdown remarkPlugins={[remarkGfm]} urlTransform={urlTransform} components={components}>
        {children}
      </ReactMarkdown>
    </div>
  )
})
