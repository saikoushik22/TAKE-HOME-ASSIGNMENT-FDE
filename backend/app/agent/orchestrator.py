"""Turn orchestration.

Owns the lifecycle of a single conversational turn: load session state, persist
the user message, pick a skill, run it, sanitize anything it produced, persist
the result, and emit a typed event stream along the way.

This is the only module that knows about *all* of routing, retrieval, providers,
sanitization, and persistence. Keeping that composition in one place is what lets
every layer underneath stay independently testable — a skill can be exercised
with a fake provider and no database, because it never reaches for either itself.
"""

from __future__ import annotations

import time
import uuid
from typing import Any, AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession

from ..core.config import Settings
from ..core.errors import AppError, CorpusEmptyError
from ..core.logging import get_logger, set_session_id
from ..db.repositories import (
    ArtifactRepository,
    CorpusRepository,
    MessageRepository,
    SessionRepository,
)
from ..llm.registry import ProviderRegistry, embedding_provider
from ..rag.embed import Embedder
from ..rag.retriever import Retriever
from ..security.sanitize import sanitize
from .router import Router
from .skills import SkillContext, SkillResult, get_skill

log = get_logger(__name__)

# A session title is derived from its first user message. Long enough to be
# recognisable in the sidebar, short enough not to wrap.
_TITLE_MAX_CHARS = 60


def derive_title(message: str) -> str:
    collapsed = " ".join((message or "").split())
    if not collapsed:
        return "New chat"
    if len(collapsed) <= _TITLE_MAX_CHARS:
        return collapsed
    return collapsed[:_TITLE_MAX_CHARS].rsplit(" ", 1)[0] + "…"


class Orchestrator:
    """Runs one turn end to end."""

    def __init__(
        self,
        db: AsyncSession,
        settings: Settings,
        registry: ProviderRegistry,
    ) -> None:
        self._db = db
        self._settings = settings
        self._registry = registry
        self._sessions = SessionRepository(db)
        self._messages = MessageRepository(db)
        self._artifacts = ArtifactRepository(db)
        self._corpus = CorpusRepository(db)

    # ------------------------------------------------------------------ run
    async def handle(
        self,
        *,
        session_id: uuid.UUID,
        message: str,
        skill_override: str | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """Execute a turn, yielding SSE-shaped events.

        Never raises for an expected failure. Anything derived from AppError is
        converted into a terminal ``error`` event so the stream always closes
        cleanly and the client can render an actionable message inline rather
        than seeing a socket drop.
        """
        set_session_id(str(session_id))
        started = time.perf_counter()

        try:
            async for event in self._run(session_id, message, skill_override):
                yield event
        except AppError as exc:
            await self._abort()
            log.warning(
                "turn.failed",
                extra={"code": exc.code, "error": exc.message,
                       "elapsed_ms": int((time.perf_counter() - started) * 1000)},
            )
            yield {"type": "error", **exc.to_dict()}
        except Exception as exc:  # unexpected: log the trace, tell the user nothing internal
            await self._abort()
            log.exception("turn.crashed", extra={"error": str(exc)})
            yield {
                "type": "error",
                "error": {
                    "code": "INTERNAL_ERROR",
                    "message": "Something went wrong while answering.",
                    "detail": {"hint": "Check the backend logs for the correlation id."},
                },
            }

    async def _abort(self) -> None:
        """Roll back after a failed turn.

        This method exists because `handle()` deliberately converts an exception
        into a terminal `error` event and then returns NORMALLY. That makes the
        stream close cleanly for the client, but it also means the caller never
        sees an exception — so the FastAPI session dependency takes its success
        path and calls `commit()` on a session whose transaction is already
        invalid. The result is `PendingRollbackError`: the real, actionable
        error is replaced by a confusing one, and every later statement on that
        session fails too.

        Rolling back here keeps the "errors are events, not exceptions" contract
        without leaving a poisoned session behind it.
        """
        try:
            await self._db.rollback()
        except Exception as exc:  # a failed rollback must not mask the original error
            log.warning("turn.rollback_failed", extra={"error": str(exc)})

    # ------------------------------------------------------------- internals
    async def _run(
        self,
        session_id: uuid.UUID,
        message: str,
        skill_override: str | None,
    ) -> AsyncIterator[dict[str, Any]]:
        started = time.perf_counter()
        session = await self._sessions.get(session_id)

        # An empty corpus is an operator problem with a known fix, not a product
        # state. Surfacing it as an error with the fix attached stops the user
        # chasing a "the assistant doesn't know anything" ghost.
        stats = await self._corpus.stats()
        if not stats["ready"]:
            raise CorpusEmptyError(
                "The transcript knowledge base has not been ingested yet.",
                detail=stats,
            )

        history = await self._messages.recent_turns(session_id, max_turns=8)

        user_row = await self._messages.add(
            session_id=session_id, role="user", content=message
        )

        # First user message names the session.
        if not history:
            await self._sessions.update(session_id, title=derive_title(message))

        provider, fell_back_from = await self._registry.acquire(session["provider"])
        model = session["model"] or self._registry.default_model(provider.name)  # type: ignore[arg-type]

        if fell_back_from:
            yield {
                "type": "status",
                "stage": "fallback",
                "detail": (
                    f"{fell_back_from} was unavailable — answering with "
                    f"{provider.name} instead."
                ),
            }

        embedder = Embedder(
            embedding_provider(self._registry, self._settings),
            self._settings,
            self._corpus,
        )
        retriever = Retriever(self._db, embedder, self._settings)

        # ---- route -------------------------------------------------------
        if skill_override:
            skill_name = skill_override
            artifact_kind = "html" if skill_override == "artifact_html" else None
            if skill_override in {"artifact_html", "artifact_markdown"}:
                skill_name = "artifact"
                artifact_kind = skill_override.split("_", 1)[1]
            decision_meta = {"skill": skill_name, "rule": "explicit-override",
                             "confidence": 1.0, "artifact_kind": artifact_kind}
        else:
            router = Router(self._settings, provider)
            decision = await router.route(message)
            skill_name = decision.skill
            artifact_kind = decision.artifact_kind
            decision_meta = decision.to_dict()

        yield {"type": "routing", "stage": "routing", **decision_meta}

        skill = get_skill(skill_name)
        ctx = SkillContext(
            message=message,
            history=history,
            retriever=retriever,
            provider=provider,
            settings=self._settings,
            artifact_kind=artifact_kind,
            model=model,
        )

        # ---- execute -----------------------------------------------------
        result: SkillResult | None = None
        artifact_row: dict[str, Any] | None = None

        async for event in skill.run(ctx):
            kind = event.get("type")

            if kind == "result":
                result = event["result"]
                continue

            if kind == "artifact":
                artifact_row = await self._persist_artifact(
                    session_id, event["artifact"]
                )
                yield {
                    "type": "artifact",
                    "artifact": {
                        "id": str(artifact_row["id"]),
                        "kind": artifact_row["kind"],
                        "title": artifact_row["title"],
                        "content": artifact_row["content"],
                        "sanitization_report": artifact_row["sanitization_report"],
                    },
                }
                continue

            yield event

        if result is None:  # a skill that yields no result is a bug in that skill
            raise AppError(
                "The skill produced no result.",
                detail={"skill": skill_name},
            )

        # ---- persist -----------------------------------------------------
        latency_ms = int((time.perf_counter() - started) * 1000)
        assistant_row = await self._messages.add(
            session_id=session_id,
            role="assistant",
            content=result.text,
            skill=skill_name,
            provider=provider.name,
            model=model,
            citations=result.citations or None,
            retrieval_trace=result.retrieval_trace or None,
            latency_ms=latency_ms,
        )

        if artifact_row is not None:
            await self._artifacts.attach_message(
                artifact_row["id"], assistant_row["id"]
            )

        await self._sessions.touch(session_id)

        log.info(
            "turn.completed",
            extra={
                "skill": skill_name,
                "provider": provider.name,
                "model": model,
                "latency_ms": latency_ms,
                "abstained": result.abstained,
                "citations": len(result.citations),
                "artifact": bool(artifact_row),
                "fallback_from": fell_back_from,
            },
        )

        yield {
            "type": "done",
            "message_id": str(assistant_row["id"]),
            "user_message_id": str(user_row["id"]),
            "session_id": str(session_id),
            "skill": skill_name,
            "provider": provider.name,
            "model": model,
            "latency_ms": latency_ms,
            "abstained": result.abstained,
            "citations": result.citations,
            "artifact_id": str(artifact_row["id"]) if artifact_row else None,
            "fallback_from": fell_back_from,
        }

    async def _persist_artifact(
        self, session_id: uuid.UUID, artifact: dict[str, Any]
    ) -> dict[str, Any]:
        """Sanitize before storing.

        Sanitization happens on the way *in*, not on the way out, so the stored
        `content` column is safe by construction and every read path inherits
        that guarantee without having to remember to sanitize. `raw_content`
        keeps the original for audit (architecture.md section 8).
        """
        kind = artifact.get("kind", "markdown")
        raw = artifact.get("content", "")
        title = artifact.get("title") or "Untitled artifact"

        if len(raw.encode("utf-8")) > self._settings.artifact_max_bytes:
            raw = raw[: self._settings.artifact_max_bytes]
            log.warning("artifact.truncated", extra={"limit": self._settings.artifact_max_bytes})

        cleaned = sanitize(raw, kind)
        row = await self._artifacts.create(
            session_id=session_id,
            kind=kind,
            title=title,
            content=cleaned.content,
            raw_content=raw,
            sanitization_report=cleaned.report.to_dict(),
        )
        log.info(
            "artifact.created",
            extra={"artifact_id": str(row["id"]), "kind": kind,
                   "changed": cleaned.report.changed},
        )
        return row
