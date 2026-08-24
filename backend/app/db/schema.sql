-- The Lenny Growth Assistant — schema
--
-- Applied idempotently at startup under an advisory lock (see session.py), so
-- concurrent workers cannot race each other. Deliberately a single declarative
-- file rather than an Alembic chain: for a greenfield system delivered at one
-- version, a reviewable file is easier to audit than a migration chain with a
-- single revision in it. See architecture.md section 3.3 for the switch path.
--
-- {EMBEDDING_DIM} is substituted at runtime from settings.embedding_dim.

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- ============================================================ conversations

CREATE TABLE IF NOT EXISTS sessions (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    title           TEXT        NOT NULL DEFAULT 'New chat',
    provider        TEXT        NOT NULL,
    model           TEXT        NOT NULL,
    user_id         TEXT        NULL,
    user_metadata   JSONB       NOT NULL DEFAULT '{}'::jsonb,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted_at      TIMESTAMPTZ NULL
);

CREATE INDEX IF NOT EXISTS sessions_active_idx
    ON sessions (updated_at DESC) WHERE deleted_at IS NULL;

CREATE TABLE IF NOT EXISTS messages (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id      UUID        NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    seq             INTEGER     NOT NULL,
    role            TEXT        NOT NULL CHECK (role IN ('user', 'assistant', 'system')),
    content         TEXT        NOT NULL,
    skill           TEXT        NULL,
    provider        TEXT        NULL,
    model           TEXT        NULL,
    citations       JSONB       NULL,
    retrieval_trace JSONB       NULL,
    token_usage     JSONB       NULL,
    latency_ms      INTEGER     NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- Ordering is a database invariant, not a hope about timestamp resolution.
    CONSTRAINT messages_session_seq_key UNIQUE (session_id, seq)
);

CREATE INDEX IF NOT EXISTS messages_session_idx ON messages (session_id, seq);

CREATE TABLE IF NOT EXISTS artifacts (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id          UUID        NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    message_id          UUID        NULL REFERENCES messages(id) ON DELETE SET NULL,
    kind                TEXT        NOT NULL CHECK (kind IN ('markdown', 'html')),
    title               TEXT        NOT NULL,
    -- `content` is sanitized and is the ONLY field the API ever renders.
    -- `raw_content` is retained for audit: if the sanitizer strips something it
    -- should not have, you can prove it.
    content             TEXT        NOT NULL,
    raw_content         TEXT        NOT NULL,
    sanitization_report JSONB       NOT NULL DEFAULT '{}'::jsonb,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS artifacts_session_idx
    ON artifacts (session_id, created_at DESC);

-- ========================================================== knowledge base

CREATE TABLE IF NOT EXISTS episodes (
    id                TEXT PRIMARY KEY,
    title             TEXT        NOT NULL,
    guest             TEXT        NULL,
    channel           TEXT        NULL,
    youtube_url       TEXT        NULL,
    video_id          TEXT        NULL,
    publish_date      DATE        NULL,
    duration_seconds  INTEGER     NULL,
    description       TEXT        NULL,
    keywords          TEXT[]      NOT NULL DEFAULT '{}',
    content_hash      TEXT        NOT NULL,
    source_path       TEXT        NOT NULL,
    source_updated_at TIMESTAMPTZ NULL,
    ingested_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS chunks (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    episode_id     TEXT        NOT NULL REFERENCES episodes(id) ON DELETE CASCADE,
    chunk_index    INTEGER     NOT NULL,
    text           TEXT        NOT NULL,
    speakers       TEXT[]      NOT NULL DEFAULT '{}',
    -- start_seconds is what makes grounding verifiable: a citation renders as
    -- {youtube_url}&t={start_seconds}s, so the user can jump to the moment.
    start_seconds  INTEGER     NOT NULL DEFAULT 0,
    end_seconds    INTEGER     NOT NULL DEFAULT 0,
    start_label    TEXT        NOT NULL DEFAULT '00:00:00',
    token_estimate INTEGER     NOT NULL DEFAULT 0,
    content_hash   TEXT        NOT NULL,
    embedding      VECTOR({EMBEDDING_DIM}) NULL,
    tsv            TSVECTOR GENERATED ALWAYS AS (to_tsvector('english', text)) STORED,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT chunks_episode_index_key UNIQUE (episode_id, chunk_index)
);

CREATE INDEX IF NOT EXISTS chunks_episode_idx ON chunks (episode_id);
CREATE INDEX IF NOT EXISTS chunks_tsv_gin ON chunks USING gin (tsv);
CREATE INDEX IF NOT EXISTS chunks_embedding_hnsw
    ON chunks USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);

-- Embedding cache: keyed by content hash so re-ingesting an unchanged corpus
-- performs zero embedding calls. On CPU-only inference this is the difference
-- between a 20-minute re-index and a 2-second no-op.
CREATE TABLE IF NOT EXISTS embedding_cache (
    content_hash TEXT PRIMARY KEY,
    model        TEXT        NOT NULL,
    embedding    VECTOR({EMBEDDING_DIM}) NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
