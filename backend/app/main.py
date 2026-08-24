"""FastAPI application factory.

Wires configuration, logging, the database, and the provider registry, then
mounts the API. Startup is deliberately fault-tolerant: a missing database or an
unreachable Ollama degrades the service rather than crashing it, so an operator
gets a running `/api/health/ready` that *names the broken dependency* instead of
a container in a restart loop with the reason scrolling past in the logs.
"""

from __future__ import annotations

import time
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import __version__
from .api import api_router
from .core.config import Settings, get_settings
from .core.errors import AppError
from .core.logging import (
    configure_logging,
    correlation_id_var,
    get_logger,
    set_correlation_id,
    set_session_id,
)
from .db.session import Database
from .llm.registry import ProviderRegistry

log = get_logger(__name__)

CORRELATION_HEADER = "x-correlation-id"


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings: Settings = get_settings()
    configure_logging(level=settings.log_level, fmt=settings.log_format)

    log.info(
        "app.starting",
        extra={
            "version": __version__,
            "env": settings.app_env,
            "provider": settings.llm_provider,
            "model": settings.model_for(settings.llm_provider),
            "embedding_model": settings.embedding_model,
        },
    )

    database = Database(settings)
    app.state.settings = settings
    app.state.database = database
    app.state.registry = ProviderRegistry(settings)
    app.state.db_ready = False

    try:
        await database.connect()
        await database.apply_schema()
        app.state.db_ready = True
    except Exception as exc:
        # Start degraded on purpose. See the module docstring: a readable
        # readiness probe beats a crash loop.
        log.error(
            "app.database_unavailable",
            extra={"error": str(exc),
                   "hint": "Check DATABASE_URL and `docker compose ps`."},
        )

    try:
        yield
    finally:
        log.info("app.stopping")
        await app.state.registry.aclose()
        await database.disconnect()


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(level=settings.log_level, fmt=settings.log_format)

    app = FastAPI(
        title="The Lenny Growth Assistant",
        description=(
            "Grounded product and growth answers from Lenny's Podcast transcripts, "
            "with a Ship 30 for 30 essay skill and sandboxed artifact generation."
        ),
        version=__version__,
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url=None,
        openapi_url="/openapi.json",
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=False,
        allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["*"],
        expose_headers=[CORRELATION_HEADER],
    )

    # ------------------------------------------------------------ middleware
    @app.middleware("http")
    async def correlation_and_access_log(request: Request, call_next):  # type: ignore[no-untyped-def]
        """Attach a correlation id to every request and log the outcome.

        The id is honoured from the inbound header when present, so a trace
        started in the browser survives into the server logs. One grep on the id
        reconstructs a whole turn — routing, retrieval, provider call, and
        persistence (architecture.md section 10).
        """
        incoming = request.headers.get(CORRELATION_HEADER)
        correlation_id = set_correlation_id(incoming)
        set_session_id(None)
        started = time.perf_counter()

        try:
            response = await call_next(request)
        except Exception:
            log.exception(
                "http.unhandled",
                extra={"method": request.method, "path": request.url.path,
                       "elapsed_ms": int((time.perf_counter() - started) * 1000)},
            )
            raise

        elapsed_ms = int((time.perf_counter() - started) * 1000)
        response.headers[CORRELATION_HEADER] = correlation_id

        # Health probes fire constantly; logging them at INFO buries real traffic.
        level = 10 if request.url.path.startswith("/api/health") else 20
        log.log(
            level,
            "http.request",
            extra={"method": request.method, "path": request.url.path,
                   "status": response.status_code, "elapsed_ms": elapsed_ms},
        )
        return response

    # -------------------------------------------------------- error handlers
    @app.exception_handler(AppError)
    async def handle_app_error(request: Request, exc: AppError) -> JSONResponse:
        log.warning(
            "error.app",
            extra={"code": exc.code, "status": exc.status_code,
                   "path": request.url.path, "error": exc.message},
        )
        return JSONResponse(status_code=exc.status_code, content=exc.to_dict())

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        """Normalize FastAPI's validation shape into the one error envelope.

        Without this, validation failures would be the single endpoint family
        that returns a different error shape than everything else, and every
        client would need two parsers.
        """
        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "code": "VALIDATION_ERROR",
                    "message": "The request body failed validation.",
                    "detail": {
                        "fields": [
                            {"field": ".".join(str(p) for p in err.get("loc", [])),
                             "problem": err.get("msg")}
                            for err in exc.errors()
                        ],
                        "hint": "Check the field names and types against /docs.",
                    },
                    "correlation_id": correlation_id_var.get(),
                }
            },
        )

    @app.exception_handler(StarletteHTTPException)
    async def handle_http_exception(
        request: Request, exc: StarletteHTTPException
    ) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "error": {
                    "code": "HTTP_ERROR",
                    "message": str(exc.detail),
                    "detail": {},
                    "correlation_id": correlation_id_var.get(),
                }
            },
        )

    @app.exception_handler(Exception)
    async def handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
        """Last resort. The traceback goes to the logs, never to the client."""
        log.exception("error.unhandled", extra={"path": request.url.path})
        return JSONResponse(
            status_code=500,
            content={
                "error": {
                    "code": "INTERNAL_ERROR",
                    "message": "An unexpected error occurred.",
                    "detail": {"hint": "Search the logs for this correlation id."},
                    "correlation_id": correlation_id_var.get(),
                }
            },
        )

    app.include_router(api_router)

    @app.get("/", include_in_schema=False)
    async def root() -> dict[str, str]:
        return {
            "service": "The Lenny Growth Assistant",
            "version": __version__,
            "docs": "/docs",
            "health": "/api/health",
        }

    return app


app = create_app()
