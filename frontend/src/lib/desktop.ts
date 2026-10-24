// Integration points with the Tauri shell. Every function degrades gracefully in a plain browser.

export interface BackendInfo {
  status: 'starting' | 'ready' | 'failed'
  base_url: string | null
  token: string | null
  error: string | null
  logs_tail: string | null
}

export function isTauri(): boolean {
  return typeof window !== 'undefined' && '__TAURI_INTERNALS__' in window
}

export async function getBackendInfo(): Promise<BackendInfo> {
  const { invoke } = await import('@tauri-apps/api/core')
  return invoke<BackendInfo>('backend_info')
}

export async function restartBackend(): Promise<BackendInfo> {
  const { invoke } = await import('@tauri-apps/api/core')
  return invoke<BackendInfo>('restart_backend')
}

export async function onBackendStatus(cb: (info: BackendInfo) => void): Promise<() => void> {
  const { listen } = await import('@tauri-apps/api/event')
  return listen<BackendInfo>('backend-status', (event) => cb(event.payload))
}

/** Native folder picker (Tauri only). Returns null when cancelled or unavailable. */
export async function pickDirectory(): Promise<string | null> {
  if (!isTauri()) return null
  const { open } = await import('@tauri-apps/plugin-dialog')
  const result = await open({ directory: true, multiple: false, title: 'Select a Git repository' })
  return typeof result === 'string' ? result : null
}

const LOCAL_HTTP = /^http:\/\/(localhost|127\.0\.0\.1|0\.0\.0\.0|\[::1\])(:\d{1,5})?(\/|$)/i

/**
 * Opens an https URL, or a local dev server such as http://localhost:5173, in the user's browser
 * (never inside the app webview).
 */
export async function openExternal(url: string): Promise<void> {
  let target = url.trim()
  if (LOCAL_HTTP.test(target)) target = target.replace(/^http:\/\/(0\.0\.0\.0|\[::1\])/i, 'http://localhost')
  else if (!/^https:\/\//i.test(target)) return
  if (isTauri()) {
    const { openUrl } = await import('@tauri-apps/plugin-opener')
    await openUrl(target)
    return
  }
  window.open(target, '_blank', 'noopener,noreferrer')
}

/** Saves text through a native save dialog (desktop) or a browser download. */
export async function saveTextFile(defaultName: string, contents: string): Promise<string | null> {
  if (isTauri()) {
    const { invoke } = await import('@tauri-apps/api/core')
    return invoke<string | null>('save_text_file', { defaultName, contents })
  }
  const blob = new Blob([contents], { type: 'text/x-diff;charset=utf-8' })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = defaultName
  document.body.appendChild(a)
  a.click()
  a.remove()
  setTimeout(() => URL.revokeObjectURL(url), 1000)
  return defaultName
}

export async function copyText(text: string): Promise<boolean> {
  try {
    await navigator.clipboard.writeText(text)
    return true
  } catch {
    try {
      const ta = document.createElement('textarea')
      ta.value = text
      ta.style.position = 'fixed'
      ta.style.opacity = '0'
      document.body.appendChild(ta)
      ta.select()
      const ok = document.execCommand('copy')
      ta.remove()
      return ok
    } catch {
      return false
    }
  }
}
