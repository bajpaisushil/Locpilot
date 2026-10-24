import { cn } from '@/lib/utils'

/** LocPilot mark: the "lp" monogram drawn with strokes on the violet badge. */
export function Logo({ className }: { className?: string }) {
  return (
    <svg viewBox="0 0 64 64" className={cn('size-6', className)} aria-hidden>
      <defs>
        <linearGradient id="lp-logo" x1="0" y1="0" x2="1" y2="1">
          <stop offset="0" stopColor="#8b7cff" />
          <stop offset="1" stopColor="#4f46e5" />
        </linearGradient>
      </defs>
      <rect width="64" height="64" rx="14" fill="url(#lp-logo)" />
      <g fill="none" stroke="#fff" strokeWidth={5.5} strokeLinecap="round">
        <path d="M18 12.5V43" />
        <path d="M29 26V51.5" />
        <circle cx="37.5" cy="34.5" r="8.5" />
      </g>
    </svg>
  )
}
