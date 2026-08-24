export type ProviderName = 'ollama' | 'anthropic' | 'openai'
export type ArtifactKind = 'markdown' | 'html'
export type SkillName = 'grounded_qa' | 'ship30_essay' | 'artifact'

export interface Citation {
  index: number
  chunk_id?: string | null
  episode_id?: string | null
  episode_title?: string | null
  guest?: string | null
  speakers?: string[]
  timestamp?: string | null
  start_seconds?: number | null
  url?: string | null
  snippet?: string | null
  score?: number | null
}

export interface Artifact {
  id: string
  session_id: string
  message_id?: string | null
  kind: ArtifactKind
  title: string
  content: string
  sanitization_report: SanitizationReport
  created_at: string
  updated_at: string
}

export interface SanitizationReport {
  removed_elements?: string[]
  removed_attributes?: string[]
  rewritten_urls?: string[]
  notes?: string[]
  changed?: boolean
}

export interface ChatMessage {
  id: string
  session_id: string
  seq: number
  role: 'user' | 'assistant' | 'system'
  content: string
  skill?: string | null
  provider?: string | null
  model?: string | null
  citations?: Citation[] | null
  retrieval_trace?: Record<string, unknown> | null
  latency_ms?: number | null
  created_at: string
}

export interface SessionSummary {
  id: string
  title: string
  provider: string
  model: string
  message_count: number
  created_at: string
  updated_at: string
}

export interface SessionDetail extends Omit<SessionSummary, 'message_count'> {
  user_id?: string | null
  user_metadata: Record<string, unknown>
  messages: ChatMessage[]
}

export interface ProviderInfo {
  name: ProviderName
  available: boolean
  model: string
  reason?: string | null
  detail: Record<string, unknown>
}

export interface CorpusStats {
  episodes?: number
  chunks?: number
  embedded_chunks?: number
  last_ingest_at?: string | null
  ready?: boolean
  reason?: string
}

export interface AppConfig {
  active_provider: ProviderName
  active_model: string
  providers: ProviderInfo[]
  embedding_provider: string
  embedding_model: string
  fallback_enabled: boolean
  fallback_provider?: string | null
  skills: string[]
  corpus: CorpusStats
}

export interface ApiErrorBody {
  code: string
  message: string
  detail: Record<string, unknown> & { hint?: string }
  correlation_id?: string | null
}

/** The turn lifecycle, as rendered by the UI. */
export type TurnStage =
  | 'idle'
  | 'accepted'
  | 'routing'
  | 'retrieving'
  | 'retrieved'
  | 'generating'
  | 'abstained'
  | 'fallback'
  | 'done'
  | 'error'
  | 'stopped'

/** Events emitted by POST /api/sessions/{id}/stream. */
export type StreamEvent =
  | { type: 'status'; stage: string; detail: string }
  | { type: 'routing'; stage: string; skill: string; confidence: number; rule: string; artifact_kind: string | null }
  | { type: 'token'; text: string }
  | { type: 'citations'; citations: Citation[] }
  | { type: 'artifact'; artifact: Artifact }
  | { type: 'done'; message_id: string; user_message_id: string; session_id: string; skill: string; provider: string; model: string; latency_ms: number; abstained: boolean; citations: Citation[]; artifact_id: string | null; fallback_from: string | null }
  | { type: 'error'; error: ApiErrorBody }
