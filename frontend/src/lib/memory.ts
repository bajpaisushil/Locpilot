import type { ModelMemory, TaskDetail } from './types'
import { formatBytes } from './utils'

/** One line about the model and this machine's memory, for the hint and the settings. */
export function memorySummary(mem: ModelMemory | null | undefined): string | null {
  if (!mem) return null
  const parts: string[] = []
  if (mem.loaded_bytes) parts.push(`uses ${formatBytes(mem.loaded_bytes)} of RAM`)
  else if (mem.needed_bytes) parts.push(`needs about ${formatBytes(mem.needed_bytes)} of RAM`)
  if (mem.available_bytes !== null && mem.total_bytes)
    parts.push(`${formatBytes(mem.available_bytes)} of ${formatBytes(mem.total_bytes)} free`)
  if (mem.context_bytes)
    parts.push(`context ${formatBytes(mem.context_bytes)}${mem.kv_cache_type === 'q8_0' ? ' (stored in 8 bits)' : ''}`)
  return parts.length ? parts.join(' · ') : null
}

/** Shares of the machine's RAM: the model, everything else in use, and what is free. */
export function ramShares(mem: ModelMemory | null | undefined) {
  if (!mem?.total_bytes || mem.available_bytes === null) return null
  const total = mem.total_bytes
  const model = Math.min(mem.loaded_bytes ?? 0, total)
  const free = Math.max(0, Math.min(mem.available_bytes, total - model))
  const other = Math.max(0, total - model - free)
  const pct = (n: number) => (n / total) * 100
  return { total, model, other, free, modelPct: pct(model), otherPct: pct(other), freePct: pct(free) }
}

/** How full the model's context window was at this task's last step. */
export function contextUse(task: TaskDetail | undefined) {
  const stats = task?.stats ?? {}
  const window = stats.context_window ?? 0
  if (!task || !window) return null
  const used = stats.context_tokens ?? 0
  return {
    used,
    window,
    pct: Math.min(100, (used / window) * 100),
    cleared: stats.context_cleared_turn === task.turn,
  }
}
