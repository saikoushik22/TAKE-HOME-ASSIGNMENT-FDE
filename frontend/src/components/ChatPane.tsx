import { useEffect, useLayoutEffect, useRef, useState } from 'react'
import { ErrorCard, Icon, Markdown, StatusLine } from './common'
import { MessageItem, SourceChips, SourcePanel } from './MessageItem'
import type { LiveTurn } from '../hooks/useChat'
import type { ChatMessage } from '../lib/types'

const EXAMPLES = [
  'How should we choose an activation metric?',
  'What do operators say about finding product-market fit?',
  'How do great PMs work with engineering teams?',
  'Turn that into a Ship 30 for 30 essay',
]

export function ChatPane({
  messages,
  turn,
  isStreaming,
  corpusReady,
  onSend,
  onStop,
  onRetry,
}: {
  messages: ChatMessage[]
  turn: LiveTurn
  isStreaming: boolean
  corpusReady: boolean
  onSend: (message: string, skill?: string | null) => void
  onStop: () => void
  onRetry: () => void
}) {
  const scrollRef = useRef<HTMLDivElement>(null)
  const pinnedRef = useRef(true)

  // Auto-scroll only while the user is already at the bottom. Yanking someone
  // back down while they are reading earlier context is the classic chat bug.
  useLayoutEffect(() => {
    const el = scrollRef.current
    if (!el || !pinnedRef.current) return
    el.scrollTop = el.scrollHeight
  }, [messages, turn.text, turn.statusLabel])

  const onScroll = () => {
    const el = scrollRef.current
    if (!el) return
    pinnedRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < 80
  }

  const lastAssistantIndex = findLastAssistant(messages)
  const showLiveTurn = turn.stage !== 'idle'

  return (
    <>
      <div className="chat__scroll" ref={scrollRef} onScroll={onScroll}>
        {messages.length === 0 && !showLiveTurn ? (
          <EmptyState corpusReady={corpusReady} onPick={(text) => onSend(text)} />
        ) : (
          <div
            className="chat__inner"
            role="log"
            aria-live="polite"
            aria-busy={isStreaming}
            aria-label="Conversation"
          >
            {messages.map((message, index) => (
              <MessageItem
                key={message.id}
                message={message}
                isLastAssistant={index === lastAssistantIndex && !showLiveTurn}
                onWriteEssay={() =>
                  onSend('Turn that answer into a Ship 30 for 30 essay.', 'ship30_essay')
                }
                onMakeArtifact={() =>
                  onSend('Turn that into a shareable document.', 'artifact_markdown')
                }
                onRegenerate={onRetry}
              />
            ))}

            {showLiveTurn && <LiveTurnView turn={turn} onRetry={onRetry} />}
          </div>
        )}
      </div>

      <Composer
        isStreaming={isStreaming}
        disabled={!corpusReady}
        onSend={onSend}
        onStop={onStop}
      />
    </>
  )
}

function LiveTurnView({ turn, onRetry }: { turn: LiveTurn; onRetry: () => void }) {
  const [showSources, setShowSources] = useState(false)

  return (
    <article className="msg" aria-label="Assistant response in progress">
      <span className="msg__role">Assistant</span>

      {turn.fallbackFrom && (
        <p className="banner">
          {turn.fallbackFrom} was unavailable — answering with a fallback provider.
        </p>
      )}

      {turn.citations.length > 0 && (
        <SourceChips citations={turn.citations} onOpen={() => setShowSources((v) => !v)} />
      )}

      {turn.statusLabel && <StatusLine label={turn.statusLabel} />}

      {turn.text && (
        <div className={turn.abstained || turn.stage === 'abstained' ? 'notice' : undefined}>
          <Markdown
            className={`msg__body ${turn.stage === 'generating' ? 'caret' : ''}`}
            source={turn.text}
          />
        </div>
      )}

      {showSources && turn.citations.length > 0 && (
        <SourcePanel citations={turn.citations} />
      )}

      {turn.stage === 'stopped' && (
        <p className="status">Stopped. The partial answer above was kept.</p>
      )}

      {turn.error && <ErrorCard error={turn.error} onRetry={onRetry} />}
    </article>
  )
}

function EmptyState({
  corpusReady,
  onPick,
}: {
  corpusReady: boolean
  onPick: (text: string) => void
}) {
  return (
    <div className="empty">
      <h1 className="empty__title">The Lenny Growth Assistant</h1>
      <p className="empty__sub">
        Product and growth answers grounded strictly in Lenny&rsquo;s Podcast
        transcripts. Every claim carries a citation you can open and check.
      </p>

      {!corpusReady && (
        <div className="error-card" role="alert">
          <span className="error-card__code">CORPUS_EMPTY</span>
          <span>The transcript knowledge base has not been ingested yet.</span>
          <span className="error-card__hint">
            Run <code>make ingest</code> to populate it, then reload.
          </span>
        </div>
      )}

      {corpusReady && (
        <>
          <p className="empty__sub" style={{ fontSize: 13 }}>
            Try one of these — all answerable from the indexed corpus:
          </p>
          <div className="empty__grid">
            {EXAMPLES.slice(0, 3).map((example) => (
              <button
                key={example}
                type="button"
                className="example"
                onClick={() => onPick(example)}
              >
                {example}
              </button>
            ))}
          </div>
        </>
      )}
    </div>
  )
}

function Composer({
  isStreaming,
  disabled,
  onSend,
  onStop,
}: {
  isStreaming: boolean
  disabled: boolean
  onSend: (message: string) => void
  onStop: () => void
}) {
  const [value, setValue] = useState('')
  const ref = useRef<HTMLTextAreaElement>(null)

  // Grow to fit, then scroll internally. Recomputed from scratch each time so
  // deleting text shrinks the box back down.
  useEffect(() => {
    const el = ref.current
    if (!el) return
    el.style.height = 'auto'
    el.style.height = `${Math.min(el.scrollHeight, 200)}px`
  }, [value])

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'k') {
        event.preventDefault()
        ref.current?.focus()
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [])

  const submit = () => {
    const text = value.trim()
    if (!text || isStreaming || disabled) return
    onSend(text)
    setValue('')
  }

  return (
    <div className="composer">
      <form
        className="composer__inner"
        onSubmit={(event) => {
          event.preventDefault()
          submit()
        }}
      >
        <label className="sr-only" htmlFor="composer-input">
          Ask a product or growth question
        </label>

        <div className="composer__box">
          <textarea
            id="composer-input"
            ref={ref}
            className="composer__input"
            rows={1}
            value={value}
            disabled={disabled}
            placeholder={
              disabled
                ? 'Ingest the corpus to start asking questions…'
                : 'Ask about product, growth, retention, hiring…'
            }
            onChange={(event) => setValue(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === 'Enter' && !event.shiftKey) {
                event.preventDefault()
                submit()
              }
            }}
          />

          {isStreaming ? (
            <button
              type="button"
              className="btn btn--icon"
              onClick={onStop}
              aria-label="Stop generating"
              title="Stop"
            >
              <Icon.Stop />
            </button>
          ) : (
            <button
              type="submit"
              className="btn btn--primary btn--icon"
              disabled={!value.trim() || disabled}
              aria-label="Send message"
              title="Send (Enter)"
            >
              <Icon.Send />
            </button>
          )}
        </div>

        <div className="composer__hint">
          <span>
            <kbd>Enter</kbd> send · <kbd>Shift</kbd>+<kbd>Enter</kbd> newline ·{' '}
            <kbd>Ctrl</kbd>+<kbd>K</kbd> focus
          </span>
          <span>Answers are grounded in transcripts and cited.</span>
        </div>
      </form>
    </div>
  )
}

function findLastAssistant(messages: ChatMessage[]): number {
  for (let i = messages.length - 1; i >= 0; i -= 1) {
    if (messages[i]?.role === 'assistant') return i
  }
  return -1
}
