"""Chat endpoints — streaming and buffered.

Streaming is the primary path because local CPU inference is slow enough that a
30-second spinner reads as a hung app while a 30-second narrated stream reads as
work in progress (design.md P2). The buffered endpoint exists for tests, scripts,
and any client that would rather have one JSON object than an event stream.

Both run the same orchestrator, so there is no second code path to keep in sync.
"""

from __future__ import annotations

import json
import uuid
from typing import Any, AsyncIterator

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from ..agent.orchestrator import Orchestrator
from ..core.errors import AppError
from ..core.logging import get_logger
from ..db.repositories import ArtifactRepository
from ..schemas import ArtifactOut, ChatRequest, ChatResponse, Citation
from .deps import DbDep, RegistryDep, SettingsDep

log = get_logger(__name__)

router = APIRouter(prefix="/sessions", tags=["chat"])


def _sse(event: dict[str, Any]) -> str:
    """Format one event as an SSE frame.

    The event type is carried in the `event:` field so a client can attach
    typed listeners rather than switching on a discriminator inside the payload.
    """
    kind = event.get("type", "message")
    payload = {k: v for k, v in event.items() if k != "type"}
    return f"event: {kind}\ndata: {json.dumps(payload, default=str)}\n\n"


@router.post(
    "/{session_id}/stream",
    summary="Send a message and stream the reply (SSE)",
    response_class=StreamingResponse,
)
async def stream_message(
    session_id: uuid.UUID,
    payload: ChatRequest,
    request: Request,
    db: DbDep,
    registry: RegistryDep,
    settings: SettingsDep,
) -> StreamingResponse:
    orchestrator = Orchestrator(db, settings, registry)

    async def generate() -> AsyncIterator[str]:
        # An immediate frame defeats proxy buffering and gives the UI something
        # to render before the first model token, which can be seconds away.
        yield _sse({"type": "status", "stage": "accepted", "detail": "Working…"})
        try:
            async for event in orchestrator.handle(
                session_id=session_id,
                message=payload.message,
                skill_override=payload.skill,
            ):
                if await request.is_disconnected():
                    log.info("stream.client_disconnected",
                             extra={"session_id": str(session_id)})
                    break
                yield _sse(event)
        except Exception as exc:  # the stream must always terminate cleanly
            log.exception("stream.failed", extra={"error": str(exc)})
            yield _sse({
                "type": "error",
                "error": {
                    "code": "STREAM_FAILED",
                    "message": "The response stream ended unexpectedly.",
                    "detail": {"hint": "Retry the message."},
                },
            })

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # nginx must not buffer an event stream
        },
    )


@router.post(
    "/{session_id}/messages",
    response_model=ChatResponse,
    summary="Send a message and wait for the full reply",
)
async def send_message(
    session_id: uuid.UUID,
    payload: ChatRequest,
    db: DbDep,
    registry: RegistryDep,
    settings: SettingsDep,
) -> ChatResponse:
    orchestrator = Orchestrator(db, settings, registry)

    chunks: list[str] = []
    done: dict[str, Any] | None = None
    artifact_id: str | None = None
    error: dict[str, Any] | None = None

    async for event in orchestrator.handle(
        session_id=session_id,
        message=payload.message,
        skill_override=payload.skill,
    ):
        kind = event.get("type")
        if kind == "token":
            chunks.append(event.get("text", ""))
        elif kind == "artifact":
            artifact_id = event["artifact"]["id"]
        elif kind == "done":
            done = event
        elif kind == "error":
            error = event.get("error")

    if error is not None:
        # Re-raise as a typed error so the exception handlers produce the
        # standard envelope, instead of hand-rolling a second error shape here.
        raise AppError(
            error.get("message", "The turn failed."),
            code=error.get("code", "INTERNAL_ERROR"),
            detail=error.get("detail", {}),
            status_code=_status_for(error.get("code")),
        )

    if done is None:
        raise AppError("The turn produced no result.", code="INTERNAL_ERROR")

    artifact: ArtifactOut | None = None
    if artifact_id:
        row = await ArtifactRepository(db).get(uuid.UUID(artifact_id))
        artifact = ArtifactOut(**row)

    return ChatResponse(
        session_id=uuid.UUID(done["session_id"]),
        message_id=uuid.UUID(done["message_id"]),
        user_message_id=uuid.UUID(done["user_message_id"]),
        content="".join(chunks).strip(),
        skill=done["skill"],
        provider=done["provider"],
        model=done["model"],
        citations=[Citation(**c) for c in done.get("citations") or []],
        artifact=artifact,
        abstained=bool(done.get("abstained")),
        latency_ms=int(done.get("latency_ms") or 0),
        fallback_from=done.get("fallback_from"),
    )


def _status_for(code: str | None) -> int:
    return {
        "CORPUS_EMPTY": 503,
        "PROVIDER_UNAVAILABLE": 503,
        "PROVIDER_TIMEOUT": 504,
        "DATABASE_UNAVAILABLE": 503,
        "NOT_FOUND": 404,
        "VALIDATION_ERROR": 422,
    }.get(code or "", 500)
