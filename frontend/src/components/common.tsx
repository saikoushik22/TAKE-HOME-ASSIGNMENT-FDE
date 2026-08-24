import { useEffect, useRef } from 'react'
import { hardenLinks, renderMarkdown } from '../lib/markdown'
import type { ApiErrorBody } from '../lib/types'

/** Renders Markdown that has been sanitized for the app origin. */
export function Markdown({ source, className }: { source: string; className?: string }) {
  const ref = useRef<HTMLDivElement>(null)

  useEffect(() => {
    hardenLinks(ref.current)
  }, [source])

  return (
    <div
      ref={ref}
      className={className}
      dangerouslySetInnerHTML={{ __html: renderMarkdown(source) }}
    />
  )
}

/**
 * Inline error with the actionable hint from the server envelope.
 * Never a bare "Something went wrong" — see design.md P3.
 */
export function ErrorCard({
  error,
  onRetry,
}: {
  error: ApiErrorBody
  onRetry?: () => void
}) {
  const hint = error.detail?.hint as string | undefined

  return (
    <div className="error-card" role="alert">
      <span className="error-card__code">{error.code}</span>
      <span>{error.message}</span>
      {hint && <span className="error-card__hint">{hint}</span>}
      <div className="actions">
        {onRetry && (
          <button type="button" className="btn" onClick={onRetry}>
            Retry
          </button>
        )}
        {error.correlation_id && (
          <span className="error-card__hint">
            Correlation ID <code>{error.correlation_id}</code>
          </span>
        )}
      </div>
    </div>
  )
}

/** Narrated progress. A named stage beats an undifferentiated spinner. */
export function StatusLine({ label }: { label: string }) {
  return (
    <p className="status" aria-live="polite">
      <span className="spinner" aria-hidden="true" />
      {label}
    </p>
  )
}

/* ------------------------------------------------------------------ icons */
/* Inline so the app ships zero icon-font or SVG-sprite requests. All are
   aria-hidden; the accessible name always lives on the parent control. */

type IconProps = { size?: number }

function svg(path: React.ReactNode, size = 16) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.8"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      focusable="false"
    >
      {path}
    </svg>
  )
}

export const Icon = {
  Plus: ({ size }: IconProps) => svg(<><path d="M12 5v14" /><path d="M5 12h14" /></>, size),
  Menu: ({ size }: IconProps) =>
    svg(<><path d="M3 6h18" /><path d="M3 12h18" /><path d="M3 18h18" /></>, size),
  Close: ({ size }: IconProps) => svg(<><path d="M18 6 6 18" /><path d="m6 6 12 12" /></>, size),
  Send: ({ size }: IconProps) => svg(<><path d="M22 2 11 13" /><path d="M22 2 15 22l-4-9-9-4Z" /></>, size),
  Stop: ({ size }: IconProps) => svg(<rect x="6" y="6" width="12" height="12" rx="2" />, size),
  Copy: ({ size }: IconProps) =>
    svg(
      <>
        <rect x="9" y="9" width="12" height="12" rx="2" />
        <path d="M5 15V5a2 2 0 0 1 2-2h10" />
      </>,
      size,
    ),
  Download: ({ size }: IconProps) =>
    svg(<><path d="M12 3v12" /><path d="m7 10 5 5 5-5" /><path d="M5 21h14" /></>, size),
  Trash: ({ size }: IconProps) =>
    svg(<><path d="M3 6h18" /><path d="M8 6V4h8v2" /><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6" /></>, size),
  Pencil: ({ size }: IconProps) =>
    svg(<><path d="M12 20h9" /><path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4Z" /></>, size),
  Refresh: ({ size }: IconProps) =>
    svg(<><path d="M3 12a9 9 0 0 1 15-6.7L21 8" /><path d="M21 3v5h-5" /><path d="M21 12a9 9 0 0 1-15 6.7L3 16" /><path d="M3 21v-5h5" /></>, size),
  Doc: ({ size }: IconProps) =>
    svg(<><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8Z" /><path d="M14 2v6h6" /></>, size),
  Essay: ({ size }: IconProps) =>
    svg(<><path d="M4 4h16" /><path d="M4 9h16" /><path d="M4 14h10" /><path d="M4 19h7" /></>, size),
  Sun: ({ size }: IconProps) =>
    svg(<><circle cx="12" cy="12" r="4" /><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4" /></>, size),
  Moon: ({ size }: IconProps) => svg(<path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8Z" />, size),
  Chevron: ({ size }: IconProps) => svg(<path d="m6 9 6 6 6-6" />, size),
  Check: ({ size }: IconProps) => svg(<path d="M20 6 9 17l-5-5" />, size),
}
