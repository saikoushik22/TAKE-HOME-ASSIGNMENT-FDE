import { useMemo, useState } from 'react'
import { Icon, Markdown } from './common'
import type { Artifact } from '../lib/types'

/**
 * The Content-Security-Policy injected into every HTML artifact.
 *
 * `default-src 'none'` is the load-bearing directive: it blocks ALL network
 * egress — fetch, XHR, WebSocket, beacons, remote images, remote fonts. Even if
 * script runs, it has nowhere to send anything. That is what makes it safe to
 * allow scripts at all.
 *
 * Mirrors the server-side constant in app/security/sanitize.py. The server's
 * copy is authoritative for stored content; this one covers client-side
 * rendering so the guarantee does not depend on a single layer.
 */
const CSP = [
  "default-src 'none'",
  "style-src 'unsafe-inline'",
  "script-src 'unsafe-inline'",
  'img-src data:',
  'font-src data:',
  "form-action 'none'",
  "base-uri 'none'",
  "frame-ancestors 'none'",
].join('; ')

/**
 * The sandbox token list.
 *
 * `allow-scripts` WITHOUT `allow-same-origin` is the entire security posture.
 * Granting both together lets framed script reach back into the parent origin
 * and defeats the sandbox completely — it is the single most common way an
 * artifact viewer is built insecurely. A test asserts this string never
 * contains 'allow-same-origin'.
 */
const SANDBOX = 'allow-scripts'

function buildSrcDoc(html: string, title: string): string {
  const safeTitle = title.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
  return `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="${CSP}">
<meta name="referrer" content="no-referrer">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>${safeTitle}</title>
<style>
  :root { color-scheme: light dark; }
  body { margin: 0; padding: 24px; line-height: 1.6;
         font-family: ui-sans-serif, system-ui, -apple-system, 'Segoe UI',
                      Roboto, Helvetica, Arial, sans-serif; }
  img, table, pre { max-width: 100%; }
  pre { overflow-x: auto; }
</style>
</head>
<body>
${html}
</body>
</html>`
}

export function ArtifactViewer({
  artifact,
  generating,
  mobileHidden,
  onClose,
  onRegenerate,
}: {
  artifact: Artifact | null
  generating: boolean
  mobileHidden?: boolean
  onClose: () => void
  onRegenerate?: () => void
}) {
  const [tab, setTab] = useState<'preview' | 'source'>('preview')
  const [copied, setCopied] = useState(false)

  const srcDoc = useMemo(
    () =>
      artifact && artifact.kind === 'html'
        ? buildSrcDoc(artifact.content, artifact.title)
        : '',
    [artifact],
  )

  const report = artifact?.sanitization_report ?? {}
  const removals = [
    ...(report.removed_elements ?? []),
    ...(report.removed_attributes ?? []),
  ]
  const rewritten = report.rewritten_urls ?? []
  const wasChanged = removals.length > 0 || rewritten.length > 0

  const copy = async () => {
    if (!artifact) return
    try {
      await navigator.clipboard.writeText(artifact.content)
      setCopied(true)
      setTimeout(() => setCopied(false), 1600)
    } catch {
      /* clipboard permission can reject; not worth an error state */
    }
  }

  const download = () => {
    if (!artifact) return
    const extension = artifact.kind === 'html' ? 'html' : 'md'
    const blob = new Blob([artifact.content], {
      type: artifact.kind === 'html' ? 'text/html' : 'text/markdown',
    })
    const url = URL.createObjectURL(blob)
    const anchor = document.createElement('a')
    anchor.href = url
    anchor.download = `${slugify(artifact.title)}.${extension}`
    anchor.click()
    URL.revokeObjectURL(url)
  }

  return (
    <aside
      className="artifact"
      aria-label="Artifact viewer"
      data-mobile-hidden={mobileHidden ? 'true' : 'false'}
    >
      <div className="artifact__head">
        <Icon.Doc />
        <span className="artifact__title">
          {artifact?.title ?? (generating ? 'Generating…' : 'Artifact')}
        </span>

        {artifact && (
          <div className="tabs" role="tablist" aria-label="Artifact view">
            <button
              type="button"
              role="tab"
              className="tab"
              aria-selected={tab === 'preview'}
              onClick={() => setTab('preview')}
            >
              Preview
            </button>
            <button
              type="button"
              role="tab"
              className="tab"
              aria-selected={tab === 'source'}
              onClick={() => setTab('source')}
            >
              Source
            </button>
          </div>
        )}

        <button
          type="button"
          className="btn btn--ghost btn--icon"
          onClick={onClose}
          aria-label="Close artifact viewer"
          title="Close (Esc)"
        >
          <Icon.Close />
        </button>
      </div>

      {wasChanged && (
        <details className="report">
          <summary>
            {removals.length > 0
              ? `${removals.length} element${removals.length === 1 ? '' : 's'} removed for safety`
              : `${rewritten.length} URL${rewritten.length === 1 ? '' : 's'} rewritten for safety`}
          </summary>
          <p style={{ margin: '8px 0 0' }}>
            This artifact contained content the viewer does not permit. It was
            removed before rendering.
          </p>
          <ul>
            {removals.slice(0, 12).map((item, i) => (
              <li key={`r-${i}`}>
                <code>{item}</code>
              </li>
            ))}
            {rewritten.slice(0, 6).map((item, i) => (
              <li key={`u-${i}`}>
                URL neutralized: <code>{item}</code>
              </li>
            ))}
          </ul>
        </details>
      )}

      <div className="artifact__body">
        {!artifact && generating && <Skeleton />}

        {artifact && tab === 'preview' && artifact.kind === 'html' && (
          <iframe
            className="artifact__frame"
            title="Rendered artifact preview"
            sandbox={SANDBOX}
            srcDoc={srcDoc}
            referrerPolicy="no-referrer"
          />
        )}

        {artifact && tab === 'preview' && artifact.kind === 'markdown' && (
          <Markdown className="artifact__md msg__body" source={artifact.content} />
        )}

        {artifact && tab === 'source' && (
          <pre className="artifact__source">{artifact.content}</pre>
        )}
      </div>

      <div className="artifact__foot">
        <span>
          {artifact?.kind === 'html' ? 'HTML · sandboxed, no network' : 'Markdown'}
        </span>
        <span style={{ flex: 1 }} />
        {artifact && (
          <>
            <button type="button" className="btn btn--ghost" onClick={copy}>
              {copied ? <Icon.Check /> : <Icon.Copy />}
              {copied ? 'Copied' : 'Copy'}
            </button>
            <button type="button" className="btn btn--ghost" onClick={download}>
              <Icon.Download /> Download
            </button>
            {onRegenerate && (
              <button type="button" className="btn btn--ghost" onClick={onRegenerate}>
                <Icon.Refresh /> Regenerate
              </button>
            )}
          </>
        )}
      </div>
    </aside>
  )
}

function Skeleton() {
  return (
    <div className="skeleton" aria-hidden="true">
      <div className="skeleton__line" style={{ width: '55%', height: 20 }} />
      <div className="skeleton__line" style={{ width: '92%' }} />
      <div className="skeleton__line" style={{ width: '86%' }} />
      <div className="skeleton__line" style={{ width: '70%' }} />
      <div className="skeleton__line" style={{ width: '90%' }} />
      <div className="skeleton__line" style={{ width: '48%' }} />
    </div>
  )
}

function slugify(value: string): string {
  return (
    value
      .toLowerCase()
      .replace(/[^a-z0-9]+/g, '-')
      .replace(/^-+|-+$/g, '')
      .slice(0, 60) || 'artifact'
  )
}
