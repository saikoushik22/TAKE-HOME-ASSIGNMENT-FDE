import type {
  AppConfig,
  ApiErrorBody,
  Artifact,
  SessionDetail,
  SessionSummary,
  StreamEvent,
} from './types'

// Empty base = same origin. Vite proxies /api in dev; nginx proxies it in the
// container. One relative path works in every environment.
const BASE = import.meta.env.VITE_API_BASE_URL ?? ''

export class ApiError extends Error {
  readonly body: ApiErrorBody
  readonly status: number

  constructor(status: number, body: ApiErrorBody) {
    super(body.message)
    this.name = 'ApiError'
    this.status = status
    this.body = body
  }

  get hint(): string | undefined {
    return this.body.detail?.hint as string | undefined
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response
  try {
    response = await fetch(`${BASE}${path}`, {
      ...init,
      headers: {
        'Content-Type': 'application/json',
        ...(init?.headers ?? {}),
      },
    })
  } catch (cause) {
    // A network-level failure has no envelope, so synthesize one. Otherwise
    // every caller needs a second error shape for "the server isn't there".
    throw new ApiError(0, {
      code: 'NETWORK_ERROR',
      message: 'Could not reach the server.',
      detail: { hint: 'Is the backend running on port 8000?', cause: String(cause) },
    })
  }

  if (response.status === 204) return undefined as T

  const text = await response.text()
  const payload = text ? safeJson(text) : null

  if (!response.ok) {
    const body = (payload as { error?: ApiErrorBody } | null)?.error ?? {
      code: 'HTTP_ERROR',
      message: `Request failed with status ${response.status}.`,
      detail: {},
    }
    throw new ApiError(response.status, body)
  }

  return payload as T
}

function safeJson(text: string): unknown {
  try {
    return JSON.parse(text)
  } catch {
    return null
  }
}

export const api = {
  config: () => request<AppConfig>('/api/config'),

  listSessions: () =>
    request<{ sessions: SessionSummary[]; total: number }>('/api/sessions'),

  getSession: (id: string) => request<SessionDetail>(`/api/sessions/${id}`),

  createSession: (body: { title?: string; provider?: string; model?: string } = {}) =>
    request<SessionSummary>('/api/sessions', {
      method: 'POST',
      body: JSON.stringify(body),
    }),

  updateSession: (
    id: string,
    body: { title?: string; provider?: string; model?: string },
  ) =>
    request<SessionSummary>(`/api/sessions/${id}`, {
      method: 'PATCH',
      body: JSON.stringify(body),
    }),

  deleteSession: (id: string) =>
    request<void>(`/api/sessions/${id}`, { method: 'DELETE' }),

  listArtifacts: (sessionId: string) =>
    request<{ artifacts: Artifact[] }>(`/api/sessions/${sessionId}/artifacts`),

  getArtifact: (id: string) => request<Artifact>(`/api/artifacts/${id}`),
}

/**
 * Stream one turn.
 *
 * Uses fetch + a ReadableStream reader rather than EventSource, because
 * EventSource cannot issue a POST and cannot send a request body — and the
 * message has to go up with the request.
 *
 * Returns an abort function so the UI can offer a working Stop button.
 */
export function streamMessage(
  sessionId: string,
  body: { message: string; skill?: string | null },
  handlers: {
    onEvent: (event: StreamEvent) => void
    onDone?: () => void
    onError?: (error: ApiError) => void
  },
): () => void {
  const controller = new AbortController()

  void (async () => {
    try {
      const response = await fetch(`${BASE}/api/sessions/${sessionId}/stream`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
        signal: controller.signal,
      })

      if (!response.ok || !response.body) {
        const text = await response.text().catch(() => '')
        const parsed = (safeJson(text) as { error?: ApiErrorBody } | null)?.error
        throw new ApiError(response.status, parsed ?? {
          code: 'STREAM_FAILED',
          message: 'The server rejected the request.',
          detail: {},
        })
      }

      const reader = response.body.getReader()
      const decoder = new TextDecoder()
      let buffer = ''

      for (;;) {
        const { done, value } = await reader.read()
        if (done) break
        buffer += decoder.decode(value, { stream: true })

        // SSE frames are separated by a blank line. Anything after the last
        // separator is a partial frame and must stay in the buffer.
        const frames = buffer.split('\n\n')
        buffer = frames.pop() ?? ''

        for (const frame of frames) {
          const event = parseFrame(frame)
          if (event) handlers.onEvent(event)
        }
      }

      handlers.onDone?.()
    } catch (error) {
      if (controller.signal.aborted) {
        handlers.onDone?.()
        return
      }
      handlers.onError?.(
        error instanceof ApiError
          ? error
          : new ApiError(0, {
              code: 'NETWORK_ERROR',
              message: 'The connection to the server was lost.',
              detail: { hint: 'Check that the backend is running, then retry.' },
            }),
      )
    }
  })()

  return () => controller.abort()
}

function parseFrame(frame: string): StreamEvent | null {
  let eventType = 'message'
  const dataLines: string[] = []

  for (const line of frame.split('\n')) {
    if (line.startsWith('event:')) eventType = line.slice(6).trim()
    else if (line.startsWith('data:')) dataLines.push(line.slice(5).trim())
  }

  if (!dataLines.length) return null

  try {
    const payload = JSON.parse(dataLines.join('\n'))
    return { type: eventType, ...payload } as StreamEvent
  } catch {
    return null
  }
}
