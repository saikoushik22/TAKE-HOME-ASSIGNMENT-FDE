"""Session CRUD.

Sessions are the unit of independent context. Every conversation lives in one,
and nothing is shared between them — that isolation is a persistence property
(a foreign key), not a convention the request handlers have to remember.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Query, status

from ..core.errors import ValidationError
from ..core.logging import get_logger
from ..db.repositories import ArtifactRepository, MessageRepository, SessionRepository
from ..schemas import (
    ArtifactList,
    ArtifactOut,
    SessionCreate,
    SessionDetail,
    SessionList,
    SessionSummary,
    SessionUpdate,
)
from .deps import DbDep, RegistryDep, SettingsDep

log = get_logger(__name__)

router = APIRouter(prefix="/sessions", tags=["sessions"])


@router.post(
    "",
    response_model=SessionSummary,
    status_code=status.HTTP_201_CREATED,
    summary="Start a new chat",
)
async def create_session(
    payload: SessionCreate,
    db: DbDep,
    registry: RegistryDep,
    settings: SettingsDep,
) -> SessionSummary:
    provider = payload.provider or settings.llm_provider
    model = payload.model or settings.model_for(provider)

    row = await SessionRepository(db).create(
        provider=provider,
        model=model,
        title=payload.title or "New chat",
        user_id=payload.user_id,
        user_metadata=payload.user_metadata,
    )
    log.info("session.created", extra={"session_id": str(row["id"]),
                                       "provider": provider, "model": model})
    return SessionSummary(**row, message_count=0)


@router.get("", response_model=SessionList, summary="List sessions")
async def list_sessions(
    db: DbDep,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> SessionList:
    repo = SessionRepository(db)
    rows = await repo.list(limit=limit, offset=offset)
    return SessionList(
        sessions=[SessionSummary(**row) for row in rows],
        total=await repo.count(),
    )


@router.get("/{session_id}", response_model=SessionDetail, summary="Session with history")
async def get_session(session_id: uuid.UUID, db: DbDep) -> SessionDetail:
    session = await SessionRepository(db).get(session_id)
    messages = await MessageRepository(db).list_for_session(session_id)
    return SessionDetail(**session, messages=messages)


@router.patch("/{session_id}", response_model=SessionSummary, summary="Rename or re-model")
async def update_session(
    session_id: uuid.UUID,
    payload: SessionUpdate,
    db: DbDep,
    settings: SettingsDep,
) -> SessionSummary:
    if payload.title is None and payload.provider is None and payload.model is None:
        raise ValidationError(
            "Nothing to update.",
            hint="Provide at least one of: title, provider, model.",
        )

    # Switching provider without naming a model must not carry the old
    # provider's model name across — "llama3.2:3b" is a 404 at Anthropic.
    model = payload.model
    if payload.provider is not None and model is None:
        model = settings.model_for(payload.provider)

    row = await SessionRepository(db).update(
        session_id, title=payload.title, provider=payload.provider, model=model
    )
    log.info("session.updated", extra={"session_id": str(session_id),
                                       "provider": row["provider"], "model": row["model"]})
    return SessionSummary(**row)


@router.delete(
    "/{session_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    # Explicit: FastAPI would otherwise infer a response model from the `-> None`
    # annotation, which a 204 is not allowed to carry.
    response_model=None,
    summary="Delete a session",
)
async def delete_session(session_id: uuid.UUID, db: DbDep) -> None:
    await SessionRepository(db).soft_delete(session_id)
    log.info("session.deleted", extra={"session_id": str(session_id)})


@router.get(
    "/{session_id}/artifacts",
    response_model=ArtifactList,
    summary="Artifacts produced in a session",
)
async def list_session_artifacts(session_id: uuid.UUID, db: DbDep) -> ArtifactList:
    await SessionRepository(db).get(session_id)  # 404s rather than returning []
    rows = await ArtifactRepository(db).list_for_session(session_id)
    return ArtifactList(artifacts=[ArtifactOut(**row) for row in rows])
