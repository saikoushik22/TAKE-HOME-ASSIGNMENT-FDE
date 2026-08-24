"""Data access.

One repository per aggregate. All SQL lives here, so a query that needs tuning
later is findable in one place rather than scattered through request handlers.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Any, Sequence

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.errors import NotFoundError
from ..core.logging import get_logger

log = get_logger(__name__)


def _row_to_dict(row: Any) -> dict[str, Any]:
    return dict(row._mapping)


def _as_json(value: Any) -> str | None:
    """Serialize for a JSONB bind parameter.

    asyncpg will not implicitly cast a Python dict to jsonb, so every JSONB
    write goes through here and the SQL casts with ::jsonb.
    """
    if value is None:
        return None
    return json.dumps(value, default=str)


# ============================================================== sessions


class SessionRepository:
    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def create(
        self,
        *,
        provider: str,
        model: str,
        title: str = "New chat",
        user_id: str | None = None,
        user_metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        result = await self._db.execute(
            text(
                """
                INSERT INTO sessions (title, provider, model, user_id, user_metadata)
                VALUES (:title, :provider, :model, :user_id, CAST(:user_metadata AS jsonb))
                RETURNING id, title, provider, model, user_id, user_metadata,
                          created_at, updated_at
                """
            ),
            {
                "title": title,
                "provider": provider,
                "model": model,
                "user_id": user_id,
                "user_metadata": _as_json(user_metadata or {}),
            },
        )
        return _row_to_dict(result.one())

    async def get(self, session_id: uuid.UUID) -> dict[str, Any]:
        result = await self._db.execute(
            text(
                """
                SELECT id, title, provider, model, user_id, user_metadata,
                       created_at, updated_at
                FROM sessions
                WHERE id = :id AND deleted_at IS NULL
                """
            ),
            {"id": session_id},
        )
        row = result.first()
        if row is None:
            raise NotFoundError(
                f"Session {session_id} was not found",
                hint="It may have been deleted. Start a new chat.",
                detail={"session_id": str(session_id)},
            )
        return _row_to_dict(row)

    async def list(self, *, limit: int = 50, offset: int = 0) -> list[dict[str, Any]]:
        result = await self._db.execute(
            text(
                """
                SELECT s.id, s.title, s.provider, s.model, s.created_at, s.updated_at,
                       (SELECT count(*) FROM messages m WHERE m.session_id = s.id)
                           AS message_count
                FROM sessions s
                WHERE s.deleted_at IS NULL
                ORDER BY s.updated_at DESC
                LIMIT :limit OFFSET :offset
                """
            ),
            {"limit": limit, "offset": offset},
        )
        return [_row_to_dict(r) for r in result.all()]

    async def count(self) -> int:
        result = await self._db.execute(
            text("SELECT count(*) AS c FROM sessions WHERE deleted_at IS NULL")
        )
        return int(result.one().c)

    async def update(
        self,
        session_id: uuid.UUID,
        *,
        title: str | None = None,
        provider: str | None = None,
        model: str | None = None,
    ) -> dict[str, Any]:
        await self.get(session_id)  # 404s if missing or soft-deleted
        result = await self._db.execute(
            text(
                """
                UPDATE sessions
                SET title      = COALESCE(:title, title),
                    provider   = COALESCE(:provider, provider),
                    model      = COALESCE(:model, model),
                    updated_at = now()
                WHERE id = :id AND deleted_at IS NULL
                RETURNING id, title, provider, model, user_id, user_metadata,
                          created_at, updated_at
                """
            ),
            {"id": session_id, "title": title, "provider": provider, "model": model},
        )
        return _row_to_dict(result.one())

    async def touch(self, session_id: uuid.UUID) -> None:
        await self._db.execute(
            text("UPDATE sessions SET updated_at = now() WHERE id = :id"),
            {"id": session_id},
        )

    async def soft_delete(self, session_id: uuid.UUID) -> None:
        await self.get(session_id)
        await self._db.execute(
            text("UPDATE sessions SET deleted_at = now() WHERE id = :id"),
            {"id": session_id},
        )


# ============================================================== messages


class MessageRepository:
    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def next_seq(self, session_id: uuid.UUID) -> int:
        result = await self._db.execute(
            text("SELECT COALESCE(max(seq), 0) + 1 AS n FROM messages WHERE session_id = :sid"),
            {"sid": session_id},
        )
        return int(result.one().n)

    async def add(
        self,
        *,
        session_id: uuid.UUID,
        role: str,
        content: str,
        seq: int | None = None,
        skill: str | None = None,
        provider: str | None = None,
        model: str | None = None,
        citations: list[dict[str, Any]] | None = None,
        retrieval_trace: dict[str, Any] | None = None,
        token_usage: dict[str, Any] | None = None,
        latency_ms: int | None = None,
    ) -> dict[str, Any]:
        if seq is None:
            seq = await self.next_seq(session_id)
        result = await self._db.execute(
            text(
                """
                INSERT INTO messages (
                    session_id, seq, role, content, skill, provider, model,
                    citations, retrieval_trace, token_usage, latency_ms
                ) VALUES (
                    :session_id, :seq, :role, :content, :skill, :provider, :model,
                    CAST(:citations AS jsonb), CAST(:retrieval_trace AS jsonb),
                    CAST(:token_usage AS jsonb), :latency_ms
                )
                RETURNING id, session_id, seq, role, content, skill, provider, model,
                          citations, retrieval_trace, token_usage, latency_ms, created_at
                """
            ),
            {
                "session_id": session_id,
                "seq": seq,
                "role": role,
                "content": content,
                "skill": skill,
                "provider": provider,
                "model": model,
                "citations": _as_json(citations),
                "retrieval_trace": _as_json(retrieval_trace),
                "token_usage": _as_json(token_usage),
                "latency_ms": latency_ms,
            },
        )
        return _row_to_dict(result.one())

    async def list_for_session(
        self, session_id: uuid.UUID, *, limit: int | None = None
    ) -> list[dict[str, Any]]:
        sql = """
            SELECT id, session_id, seq, role, content, skill, provider, model,
                   citations, retrieval_trace, token_usage, latency_ms, created_at
            FROM messages
            WHERE session_id = :sid
            ORDER BY seq ASC
        """
        params: dict[str, Any] = {"sid": session_id}
        if limit is not None:
            sql += " LIMIT :limit"
            params["limit"] = limit
        result = await self._db.execute(text(sql), params)
        return [_row_to_dict(r) for r in result.all()]

    async def recent_turns(
        self, session_id: uuid.UUID, *, max_turns: int = 8
    ) -> list[dict[str, Any]]:
        """Most recent turns, oldest-first, for conversational context.

        Bounded because prompt size drives both latency and cost, and a 3B local
        model degrades sharply as context grows.
        """
        result = await self._db.execute(
            text(
                """
                SELECT role, content FROM (
                    SELECT role, content, seq
                    FROM messages
                    WHERE session_id = :sid AND role IN ('user', 'assistant')
                    ORDER BY seq DESC
                    LIMIT :lim
                ) recent
                ORDER BY seq ASC
                """
            ),
            {"sid": session_id, "lim": max_turns},
        )
        return [_row_to_dict(r) for r in result.all()]


# ============================================================== artifacts


class ArtifactRepository:
    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def create(
        self,
        *,
        session_id: uuid.UUID,
        kind: str,
        title: str,
        content: str,
        raw_content: str,
        message_id: uuid.UUID | None = None,
        sanitization_report: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        result = await self._db.execute(
            text(
                """
                INSERT INTO artifacts (
                    session_id, message_id, kind, title, content, raw_content,
                    sanitization_report
                ) VALUES (
                    :session_id, :message_id, :kind, :title, :content, :raw_content,
                    CAST(:report AS jsonb)
                )
                RETURNING id, session_id, message_id, kind, title, content,
                          sanitization_report, created_at, updated_at
                """
            ),
            {
                "session_id": session_id,
                "message_id": message_id,
                "kind": kind,
                "title": title,
                "content": content,
                "raw_content": raw_content,
                "report": _as_json(sanitization_report or {}),
            },
        )
        return _row_to_dict(result.one())

    async def attach_message(
        self, artifact_id: uuid.UUID, message_id: uuid.UUID
    ) -> None:
        """Link an artifact to the assistant message that produced it.

        Two-step because the artifact is persisted mid-stream — the client needs
        a real artifact id to render against before the assistant message exists.
        """
        await self._db.execute(
            text(
                """
                UPDATE artifacts
                SET message_id = :message_id, updated_at = now()
                WHERE id = :id
                """
            ),
            {"id": artifact_id, "message_id": message_id},
        )

    async def get(self, artifact_id: uuid.UUID) -> dict[str, Any]:
        result = await self._db.execute(
            text(
                """
                SELECT id, session_id, message_id, kind, title, content,
                       sanitization_report, created_at, updated_at
                FROM artifacts WHERE id = :id
                """
            ),
            {"id": artifact_id},
        )
        row = result.first()
        if row is None:
            raise NotFoundError(
                f"Artifact {artifact_id} was not found",
                detail={"artifact_id": str(artifact_id)},
            )
        return _row_to_dict(row)

    async def list_for_session(self, session_id: uuid.UUID) -> list[dict[str, Any]]:
        result = await self._db.execute(
            text(
                """
                SELECT id, session_id, message_id, kind, title, content,
                       sanitization_report, created_at, updated_at
                FROM artifacts
                WHERE session_id = :sid
                ORDER BY created_at DESC
                """
            ),
            {"sid": session_id},
        )
        return [_row_to_dict(r) for r in result.all()]


# ============================================================ knowledge base


class CorpusRepository:
    """Episodes, chunks, and the embedding cache."""

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def stats(self) -> dict[str, Any]:
        result = await self._db.execute(
            text(
                """
                SELECT
                    (SELECT count(*) FROM episodes)                          AS episodes,
                    (SELECT count(*) FROM chunks)                            AS chunks,
                    (SELECT count(*) FROM chunks WHERE embedding IS NOT NULL) AS embedded,
                    (SELECT max(ingested_at) FROM episodes)                  AS last_ingest
                """
            )
        )
        row = result.one()
        return {
            "episodes": int(row.episodes),
            "chunks": int(row.chunks),
            "embedded_chunks": int(row.embedded),
            "last_ingest_at": row.last_ingest,
            "ready": int(row.embedded) > 0,
        }

    async def episode_hashes(self) -> dict[str, str]:
        """Map episode id -> content hash, for incremental ingestion."""
        result = await self._db.execute(text("SELECT id, content_hash FROM episodes"))
        return {r.id: r.content_hash for r in result.all()}

    async def upsert_episode(self, episode: dict[str, Any]) -> None:
        await self._db.execute(
            text(
                """
                INSERT INTO episodes (
                    id, title, guest, channel, youtube_url, video_id, publish_date,
                    duration_seconds, description, keywords, content_hash,
                    source_path, source_updated_at, ingested_at
                ) VALUES (
                    :id, :title, :guest, :channel, :youtube_url, :video_id, :publish_date,
                    :duration_seconds, :description, :keywords, :content_hash,
                    :source_path, :source_updated_at, now()
                )
                ON CONFLICT (id) DO UPDATE SET
                    title = EXCLUDED.title,
                    guest = EXCLUDED.guest,
                    channel = EXCLUDED.channel,
                    youtube_url = EXCLUDED.youtube_url,
                    video_id = EXCLUDED.video_id,
                    publish_date = EXCLUDED.publish_date,
                    duration_seconds = EXCLUDED.duration_seconds,
                    description = EXCLUDED.description,
                    keywords = EXCLUDED.keywords,
                    content_hash = EXCLUDED.content_hash,
                    source_path = EXCLUDED.source_path,
                    source_updated_at = EXCLUDED.source_updated_at,
                    ingested_at = now()
                """
            ),
            episode,
        )

    async def delete_chunks(self, episode_id: str) -> None:
        await self._db.execute(
            text("DELETE FROM chunks WHERE episode_id = :eid"), {"eid": episode_id}
        )

    async def insert_chunks(self, rows: Sequence[dict[str, Any]]) -> None:
        if not rows:
            return
        await self._db.execute(
            text(
                """
                INSERT INTO chunks (
                    episode_id, chunk_index, text, speakers, start_seconds,
                    end_seconds, start_label, token_estimate, content_hash, embedding
                ) VALUES (
                    :episode_id, :chunk_index, :text, :speakers, :start_seconds,
                    :end_seconds, :start_label, :token_estimate, :content_hash,
                    CAST(:embedding AS vector)
                )
                ON CONFLICT (episode_id, chunk_index) DO UPDATE SET
                    text = EXCLUDED.text,
                    speakers = EXCLUDED.speakers,
                    start_seconds = EXCLUDED.start_seconds,
                    end_seconds = EXCLUDED.end_seconds,
                    start_label = EXCLUDED.start_label,
                    token_estimate = EXCLUDED.token_estimate,
                    content_hash = EXCLUDED.content_hash,
                    embedding = EXCLUDED.embedding
                """
            ),
            list(rows),
        )

    # --------------------------------------------------------- embed cache
    async def cached_embeddings(self, hashes: Sequence[str], model: str) -> dict[str, str]:
        if not hashes:
            return {}
        result = await self._db.execute(
            text(
                """
                SELECT content_hash, embedding::text AS embedding
                FROM embedding_cache
                WHERE model = :model AND content_hash = ANY(:hashes)
                """
            ),
            {"model": model, "hashes": list(hashes)},
        )
        return {r.content_hash: r.embedding for r in result.all()}

    async def cache_embeddings(self, rows: Sequence[dict[str, Any]]) -> None:
        if not rows:
            return
        await self._db.execute(
            text(
                """
                INSERT INTO embedding_cache (content_hash, model, embedding)
                VALUES (:content_hash, :model, CAST(:embedding AS vector))
                ON CONFLICT (content_hash) DO NOTHING
                """
            ),
            list(rows),
        )

    async def sample_topics(self, limit: int = 8) -> list[str]:
        """Episode titles used to suggest covered topics on an abstention."""
        result = await self._db.execute(
            text(
                """
                SELECT title FROM episodes
                WHERE title IS NOT NULL
                ORDER BY random()
                LIMIT :limit
                """
            ),
            {"limit": limit},
        )
        return [r.title for r in result.all()]
