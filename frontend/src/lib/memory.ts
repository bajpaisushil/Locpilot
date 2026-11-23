import type { ModelMemory } from './types'
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
