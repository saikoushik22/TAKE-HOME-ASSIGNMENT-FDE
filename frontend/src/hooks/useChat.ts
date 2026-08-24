import { useCallback, useRef, useState } from 'react'
import { ApiError, api, streamMessage } from '../lib/api'
import type {
  ApiErrorBody,
  Artifact,
  ChatMessage,
  Citation,
  TurnStage,
} from '../lib/types'

export interface LiveTurn {
  stage: TurnStage
  statusLabel: string
  text: string
  citations: Citation[]
  skill: string | null
  fallbackFrom: string | null
  abstained: boolean
  error: ApiErrorBody | null
}

const IDLE: LiveTurn = {
  stage: 'idle',
  statusLabel: '',
  text: '',
  citations: [],
  skill: null,
  fallbackFrom: null,
  abstained: false,
  error: null,
}

export function useChat(options: {
  onArtifact: (artifact: Artifact) => void
  onTurnComplete: () => void
}) {
  const { onArtifact, onTurnComplete } = options

  const [messages, setMessages] = useState<ChatMessage[]>([])
  const [turn, setTurn] = useState<LiveTurn>(IDLE)
  const abortRef = useRef<(() => void) | null>(null)
  // Kept in a ref so retry does not need to be re-created on every keystroke.
  const lastSentRef = useRef<{ sessionId: string; message: string; skill?: string | null } | null>(null)
  /**
   * The session currently on screen.
   *
   * A turn streams for tens of seconds on local hardware, which is easily long
   * enough for the user to open another chat mid-answer. Every stream callback
   * checks this ref against the session it was started for, so a stream that
   * outlives its session can never write into whatever is on screen now.
   */
  const activeSessionRef = useRef<string | null>(null)

  const isStreaming = turn.stage !== 'idle' && turn.stage !== 'done' &&
    turn.stage !== 'error' && turn.stage !== 'stopped'

  /** Abandon any turn still streaming. */
  const cancelInFlight = useCallback(() => {
    abortRef.current?.()
    abortRef.current = null
  }, [])

  const loadSession = useCallback(async (sessionId: string) => {
    // Abort rather than letting the previous turn finish in the background.
    // With a single local model, an abandoned generation starves the question
    // the user is actually waiting for — Ollama serialises requests, so a
    // 100-second answer nobody is reading blocks the next one entirely.
    cancelInFlight()
    activeSessionRef.current = sessionId

    const detail = await api.getSession(sessionId)
    // Guard against two session loads racing: a slow fetch for session A must
    // not overwrite the messages of session B the user has since opened.
    if (activeSessionRef.current !== sessionId) return detail

    setMessages(detail.messages)
    setTurn(IDLE)
    return detail
  }, [cancelInFlight])

  const reset = useCallback(() => {
    cancelInFlight()
    activeSessionRef.current = null
    setMessages([])
    setTurn(IDLE)
  }, [cancelInFlight])

  const stop = useCallback(() => {
    abortRef.current?.()
    abortRef.current = null
    // Partial output is preserved, not discarded — a user who stops a slow
    // local generation usually already got what they needed (design.md).
    setTurn((t) => ({ ...t, stage: 'stopped', statusLabel: '' }))
  }, [])

  const send = useCallback(
    (sessionId: string, message: string, skill?: string | null) => {
      lastSentRef.current = { sessionId, message, skill }

      // Never run two turns at once. Without this, a regenerate or a fast
      // second send leaves the first stream alive, and both write into the same
      // state — which also leaves `isStreaming` stuck true, silently disabling
      // the composer.
      cancelInFlight()
      activeSessionRef.current = sessionId

      // Optimistic user message. A pending id keeps React keys stable; the
      // authoritative row replaces it when the turn completes.
      const optimistic: ChatMessage = {
        id: `pending-${Date.now()}`,
        session_id: sessionId,
        seq: -1,
        role: 'user',
        content: message,
        created_at: new Date().toISOString(),
      }
      setMessages((prev) => [...prev, optimistic])
      setTurn({ ...IDLE, stage: 'accepted', statusLabel: 'Working…' })

      /** True while this stream still owns what is on screen. */
      const owns = () => activeSessionRef.current === sessionId

      abortRef.current = streamMessage(
        sessionId,
        { message, skill: skill ?? null },
        {
          onEvent: (event) => {
            // Aborting a fetch does not retroactively cancel events already in
            // flight, so ownership is re-checked on every one. Without this a
            // turn started in one chat streams its tokens into whichever chat
            // the user has since opened.
            if (!owns()) return

            switch (event.type) {
              case 'status':
                setTurn((t) => ({
                  ...t,
                  stage: mapStage(event.stage, t.stage),
                  statusLabel: event.detail,
                  fallbackFrom:
                    event.stage === 'fallback' ? extractFallback(event.detail) : t.fallbackFrom,
                }))
                break

              case 'routing':
                setTurn((t) => ({
                  ...t,
                  stage: 'routing',
                  skill: event.skill,
                  statusLabel: labelForSkill(event.skill),
                }))
                break

              case 'token':
                setTurn((t) => ({
                  ...t,
                  stage: t.stage === 'abstained' ? 'abstained' : 'generating',
                  statusLabel: '',
                  text: t.text + event.text,
                }))
                break

              case 'citations':
                setTurn((t) => ({ ...t, citations: event.citations }))
                break

              case 'artifact':
                onArtifact(event.artifact)
                break

              case 'done':
                setTurn((t) => ({
                  ...t,
                  stage: 'done',
                  statusLabel: '',
                  citations: event.citations?.length ? event.citations : t.citations,
                  abstained: event.abstained,
                  fallbackFrom: event.fallback_from ?? t.fallbackFrom,
                }))
                break

              case 'error':
                setTurn((t) => ({ ...t, stage: 'error', statusLabel: '', error: event.error }))
                break
            }
          },

          onDone: () => {
            // Refresh the sidebar regardless of which chat is on screen: the
            // finished turn renamed its session and bumped its ordering.
            onTurnComplete()

            // The abandoned case. Re-reading here would replace the messages of
            // the chat the user is now looking at with a different session's
            // history — the exact cross-contamination this guard exists to stop.
            if (!owns()) return

            abortRef.current = null
            // Re-read from the server so the rendered history is the persisted
            // history — ids, seq, and the citation-validated final text.
            void api
              .getSession(sessionId)
              .then((detail) => {
                if (!owns()) return
                setMessages(detail.messages)
                setTurn(IDLE)
              })
              .catch(() => {
                // Reload failed; keep the streamed text visible rather than
                // blanking the answer the user just watched arrive.
                if (!owns()) return
                setTurn((t) => (t.stage === 'error' ? t : { ...t, stage: 'done' }))
              })
          },

          onError: (error: ApiError) => {
            // An error belonging to an abandoned turn must not surface as an
            // error card on the chat the user has since opened.
            if (!owns()) return
            abortRef.current = null
            setTurn((t) => ({ ...t, stage: 'error', statusLabel: '', error: error.body }))
          },
        },
      )
    },
    [cancelInFlight, onArtifact, onTurnComplete],
  )

  const retry = useCallback(() => {
    const last = lastSentRef.current
    if (!last) return
    setMessages((prev) => prev.filter((m) => !m.id.startsWith('pending-')))
    send(last.sessionId, last.message, last.skill)
  }, [send])

  return { messages, turn, isStreaming, send, stop, retry, loadSession, reset }
}

function mapStage(stage: string, current: TurnStage): TurnStage {
  switch (stage) {
    case 'accepted': return 'accepted'
    case 'routing': return 'routing'
    case 'retrieving': return 'retrieving'
    case 'retrieved': return 'retrieved'
    case 'generating': return 'generating'
    case 'abstained': return 'abstained'
    case 'fallback': return current
    default: return current
  }
}

function labelForSkill(skill: string): string {
  switch (skill) {
    case 'ship30_essay': return 'Writing a Ship 30 for 30 essay…'
    case 'artifact': return 'Preparing an artifact…'
    default: return 'Searching transcripts…'
  }
}

function extractFallback(detail: string): string | null {
  const match = detail.match(/^(\w+) was unavailable/)
  return match?.[1] ?? null
}
