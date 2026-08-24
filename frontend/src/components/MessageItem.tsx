import { useState } from 'react'
import { Icon, Markdown } from './common'
import type { ChatMessage, Citation } from '../lib/types'

/**
 * Source chips, rendered ABOVE the answer text.
 *
 * Placement is deliberate: grounding has to be visible while the answer is
 * still forming, which is when the reader decides whether to trust it.
 * Citations shown only after the fact get read as decoration (design.md P1).
 */
export function SourceChips({
  citations,
  onOpen,
}: {
  citations: Citation[]
  onOpen: () => void
}) {
  if (!citations.length) return null

  return (
    <div className="sources" role="list" aria-label={`${citations.length} sources`}>
      {citations.map((citation) => (
        <button
          key={`${citation.index}-${citation.chunk_id ?? citation.index}`}
          type="button"
          role="listitem"
          className="chip"
          onClick={onOpen}
          title={
            citation.guest
              ? `${citation.episode_title} — ${citation.guest}`
              : (citation.episode_title ?? 'Source')
          }
        >
          <span className="chip__n">{citation.index}</span>
          <span className="chip__label">{citation.episode_title ?? 'Source'}</span>
        </button>
      ))}
    </div>
  )
}

export function SourcePanel({ citations }: { citations: Citation[] }) {
  return (
    <div className="source-panel">
      {citations.map((citation) => (
        <div className="source" key={`${citation.index}-${citation.chunk_id ?? citation.index}`}>
          <span className="source__n">[{citation.index}]</span>
          <div style={{ minWidth: 0 }}>
            <div className="source__title">{citation.episode_title ?? 'Untitled episode'}</div>
            <div className="source__meta">
              {citation.guest && <span>{citation.guest}</span>}
              {citation.guest && citation.timestamp && <span> · </span>}
              {citation.timestamp && <span>{citation.timestamp}</span>}
              {citation.url && (
                <>
                  {' · '}
                  <a href={citation.url} target="_blank" rel="noopener noreferrer nofollow">
                    Open at this moment
                  </a>
                </>
              )}
            </div>
            {citation.snippet && <div className="source__snippet">{citation.snippet}</div>}
          </div>
        </div>
      ))}
    </div>
  )
}

export function MessageItem({
  message,
  onWriteEssay,
  onMakeArtifact,
  onRegenerate,
  isLastAssistant,
}: {
  message: ChatMessage
  onWriteEssay?: () => void
  onMakeArtifact?: () => void
  onRegenerate?: () => void
  isLastAssistant?: boolean
}) {
  const [showSources, setShowSources] = useState(false)
  const [copied, setCopied] = useState(false)
  const citations = message.citations ?? []

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(message.content)
      setCopied(true)
      setTimeout(() => setCopied(false), 1600)
    } catch {
      // Clipboard is permission-gated and can reject. Silently leaving the
      // label unchanged is better than an error toast for a copy button.
    }
  }

  if (message.role === 'user') {
    return (
      <article className="msg msg--user" aria-label="Your message">
        <div className="msg__bubble">
          <div className="msg__body">{message.content}</div>
        </div>
      </article>
    )
  }

  return (
    <article className="msg" aria-label="Assistant message">
      <span className="msg__role">
        Assistant
        {message.skill === 'ship30_essay' && ' · Ship 30 essay'}
        {message.skill === 'artifact' && ' · artifact'}
      </span>

      {citations.length > 0 && (
        <SourceChips citations={citations} onOpen={() => setShowSources((v) => !v)} />
      )}

      <Markdown className="msg__body" source={message.content} />

      {citations.length > 0 && (
        <>
          <button
            type="button"
            className="btn btn--ghost"
            style={{ alignSelf: 'flex-start' }}
            onClick={() => setShowSources((v) => !v)}
            aria-expanded={showSources}
          >
            {showSources ? 'Hide' : 'Show'} {citations.length} source
            {citations.length === 1 ? '' : 's'}
          </button>
          {showSources && <SourcePanel citations={citations} />}
        </>
      )}

      <div className="actions">
        <button type="button" className="btn btn--ghost" onClick={copy}>
          {copied ? <Icon.Check /> : <Icon.Copy />}
          {copied ? 'Copied' : 'Copy'}
        </button>
        {isLastAssistant && onWriteEssay && (
          <button type="button" className="btn btn--ghost" onClick={onWriteEssay}>
            <Icon.Essay /> Write essay
          </button>
        )}
        {isLastAssistant && onMakeArtifact && (
          <button type="button" className="btn btn--ghost" onClick={onMakeArtifact}>
            <Icon.Doc /> Make artifact
          </button>
        )}
        {isLastAssistant && onRegenerate && (
          <button type="button" className="btn btn--ghost" onClick={onRegenerate}>
            <Icon.Refresh /> Regenerate
          </button>
        )}
        {message.latency_ms != null && (
          <span
            style={{ fontSize: 11.5, color: 'var(--text-faint)', alignSelf: 'center' }}
            title={`${message.provider ?? ''} ${message.model ?? ''}`}
          >
            {(message.latency_ms / 1000).toFixed(1)}s
          </span>
        )}
      </div>
    </article>
  )
}
