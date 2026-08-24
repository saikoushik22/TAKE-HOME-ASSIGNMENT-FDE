"""Health, readiness, and configuration endpoints.

Liveness and readiness are deliberately separate. `/health` answers "is this
process running" and must never touch a dependency — an orchestrator restarting
the container because Postgres blinked would turn a recoverable dependency
outage into an application outage. `/health/ready` answers "can it actually
serve traffic" and checks everything, reporting each dependency individually so
the failing one is named rather than inferred.
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter

from .. import __version__
from ..core.logging import get_logger
from ..db.repositories import CorpusRepository
from ..llm.registry import ALL_PROVIDERS
from ..schemas import (
    ConfigResponse,
    DependencyStatus,
    HealthResponse,
    ProviderInfo,
    ReadinessResponse,
)
from ..agent.skills import skill_names
from .deps import DatabaseDep, DbDep, RegistryDep, SettingsDep

log = get_logger(__name__)

router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthResponse, summary="Liveness probe")
async def health() -> HealthResponse:
    """Liveness. No dependency calls, so it stays fast and always truthful."""
    return HealthResponse(status="ok", service="lenny-growth-assistant",
                          version=__version__)


@router.get(
    "/health/ready",
    response_model=ReadinessResponse,
    summary="Readiness probe with per-dependency detail",
)
async def readiness(
    database: DatabaseDep,
    registry: RegistryDep,
    settings: SettingsDep,
) -> ReadinessResponse:
    """Readiness. Reports each dependency separately.

    Returns 200 even when degraded: the body carries the truth, and a caller
    that wants a hard gate can read `status`. Returning 503 here would make the
    diagnostic endpoint unreadable through some proxies at exactly the moment
    you need to read it.
    """
    db_ok, provider_healths = await asyncio.gather(
        database.ping(),
        registry.health_all(),
    )

    dependencies: list[DependencyStatus] = [
        DependencyStatus(
            name="database",
            healthy=db_ok,
            reason=None if db_ok else "cannot reach PostgreSQL",
            detail={"url": _redact_dsn(settings.database_url)},
        )
    ]

    for health_item in provider_healths:
        dependencies.append(
            DependencyStatus(
                name=f"provider:{health_item.name}",
                healthy=health_item.available,
                reason=health_item.reason,
                detail={"model": health_item.model, **health_item.detail},
            )
        )

    corpus: dict[str, object] = {"ready": False, "reason": "database unavailable"}
    if db_ok:
        try:
            async for session in database.session():
                corpus = await CorpusRepository(session).stats()
                break
        except Exception as exc:  # readiness must report, never raise
            log.warning("readiness.corpus.failed", extra={"error": str(exc)})
            corpus = {"ready": False, "reason": str(exc)}

    dependencies.append(
        DependencyStatus(
            name="corpus",
            healthy=bool(corpus.get("ready")),
            reason=None if corpus.get("ready") else "no embedded chunks — run `make ingest`",
            detail=dict(corpus),
        )
    )

    # The active provider is the one that must work; the others are optional.
    active = settings.llm_provider
    active_ok = any(h.name == active and h.available for h in provider_healths)
    ready = db_ok and active_ok and bool(corpus.get("ready"))

    return ReadinessResponse(
        status="ready" if ready else "degraded",
        dependencies=dependencies,
        corpus=dict(corpus),
    )


@router.get("/config", response_model=ConfigResponse, summary="UI bootstrap config")
async def config(
    db: DbDep,
    registry: RegistryDep,
    settings: SettingsDep,
) -> ConfigResponse:
    """Everything the UI needs on first paint.

    Includes unavailable providers *with their reason*, so the client can render
    them disabled-and-explained rather than hiding them (design.md, Flow D).
    """
    healths = await registry.health_all()
    try:
        corpus = await CorpusRepository(db).stats()
    except Exception as exc:
        log.warning("config.corpus.failed", extra={"error": str(exc)})
        corpus = {"ready": False, "reason": str(exc)}

    return ConfigResponse(
        active_provider=settings.llm_provider,
        active_model=settings.model_for(settings.llm_provider),
        providers=[
            ProviderInfo(
                name=h.name,
                available=h.available,
                model=h.model,
                reason=h.reason,
                detail=h.detail,
            )
            for h in sorted(healths, key=lambda h: ALL_PROVIDERS.index(h.name))
        ],
        embedding_provider=settings.embedding_provider,
        embedding_model=settings.embedding_model,
        fallback_enabled=settings.llm_fallback_enabled,
        fallback_provider=settings.llm_fallback_provider,
        skills=skill_names(),
        corpus=dict(corpus),
    )


def _redact_dsn(dsn: str) -> str:
    """Strip credentials from a DSN before it reaches a response body."""
    if "@" not in dsn:
        return dsn
    scheme, _, rest = dsn.partition("://")
    _, _, host = rest.rpartition("@")
    return f"{scheme}://***@{host}"
