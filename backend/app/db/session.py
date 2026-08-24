"""Database engine, startup schema application, and session dependency.

Uses SQLAlchemy Core (async) with explicit SQL rather than the ORM. The schema
leans on Postgres-specific types — `vector`, `tsvector`, text arrays — that the
ORM abstracts poorly, and explicit SQL keeps the retrieval queries readable,
which matters because they are the queries most likely to be tuned later.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import AsyncIterator

from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncEngine,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy import text

from ..core.config import Settings
from ..core.errors import DatabaseUnavailableError
from ..core.logging import get_logger

log = get_logger(__name__)

SCHEMA_PATH = Path(__file__).with_name("schema.sql")

# Arbitrary but stable: two workers applying the schema concurrently must not race.
_SCHEMA_LOCK_ID = 0x1E77_9A55


class Database:
    """Owns the engine lifecycle."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._engine: AsyncEngine | None = None
        self._sessionmaker: async_sessionmaker | None = None

    @property
    def engine(self) -> AsyncEngine:
        if self._engine is None:
            raise DatabaseUnavailableError("Database engine is not initialised")
        return self._engine

    # ------------------------------------------------------------- lifecycle
    async def connect(self) -> None:
        """Create the engine and wait for Postgres, with bounded backoff.

        Compose starts the database and the backend together; without a retry
        loop the backend would crash on a cold start purely because Postgres
        needed another second. `depends_on: service_healthy` covers the Compose
        path, but this also covers running the backend natively.
        """
        self._engine = create_async_engine(
            self._settings.database_url,
            pool_size=self._settings.db_pool_size,
            max_overflow=self._settings.db_max_overflow,
            pool_pre_ping=True,  # a recycled-but-dead connection must not surface as a 500
            echo=False,
        )
        self._sessionmaker = async_sessionmaker(bind=self._engine, expire_on_commit=False)

        attempts = self._settings.db_connect_retries
        delay = self._settings.db_connect_backoff_seconds
        last: Exception | None = None

        for attempt in range(1, attempts + 1):
            try:
                async with self._engine.connect() as conn:
                    await conn.execute(text("SELECT 1"))
                log.info("db.connect", extra={"attempt": attempt, "outcome": "ok"})
                return
            except Exception as exc:
                last = exc
                log.warning(
                    "db.connect.retry",
                    extra={"attempt": attempt, "of": attempts,
                           "retry_in_s": delay, "error": str(exc)},
                )
                if attempt < attempts:
                    await asyncio.sleep(delay)
                    delay *= 2

        raise DatabaseUnavailableError(
            "Could not connect to PostgreSQL",
            detail={"attempts": attempts, "error": str(last)},
        )

    async def apply_schema(self) -> None:
        """Apply schema.sql idempotently under an advisory lock."""
        ddl = SCHEMA_PATH.read_text(encoding="utf-8").replace(
            "{EMBEDDING_DIM}", str(self._settings.embedding_dim)
        )
        async with self.engine.begin() as conn:
            # Bound every lock wait in this transaction.
            #
            # `CREATE TABLE/INDEX IF NOT EXISTS` still needs a relation lock even
            # when it ends up doing nothing, so a long-running write transaction
            # (an ingest, say) blocks it. Without a timeout the startup hangs
            # forever, holds the advisory lock below, and every *subsequent*
            # startup queues behind it — one slow writer silently wedges the
            # whole service, with a container that simply never turns healthy.
            #
            # Failing in seconds with an actionable message is strictly better
            # than an unbounded wait nobody can diagnose from the outside.
            await conn.execute(text("SET LOCAL lock_timeout = '10s'"))
            await conn.execute(text("SET LOCAL statement_timeout = '60s'"))

            try:
                await conn.execute(text("SELECT pg_advisory_xact_lock(:k)"),
                                   {"k": _SCHEMA_LOCK_ID})
            except Exception as exc:
                raise DatabaseUnavailableError(
                    "Timed out waiting to apply the database schema",
                    hint=(
                        "Another process is holding the schema lock — usually an "
                        "ingest still running, or a previous startup stuck behind "
                        "one. Wait for it to finish, or inspect with: "
                        "SELECT pid, state, query FROM pg_stat_activity "
                        "WHERE state <> 'idle';"
                    ),
                    detail={"lock_id": _SCHEMA_LOCK_ID, "error": str(exc)},
                ) from exc

            # asyncpg prepares every statement, and a prepared statement may
            # contain exactly one command — so the multi-statement schema file
            # cannot go through the normal execute path. Dropping to the raw
            # driver connection uses the simple query protocol, which accepts
            # the whole script. Still inside the transaction and the advisory
            # lock, so the operation stays atomic.
            raw = await conn.get_raw_connection()
            await raw.driver_connection.execute(ddl)

            await self._verify_embedding_dim(conn)
        log.info("db.schema.applied", extra={"embedding_dim": self._settings.embedding_dim})

    async def _verify_embedding_dim(self, conn: AsyncConnection) -> None:
        """Fail loudly if the stored vectors do not match EMBEDDING_DIM.

        A silent mismatch is the worst outcome: queries still run and return
        neighbours, they are simply meaningless. Better to refuse to start and
        say exactly which command fixes it.
        """
        result = await conn.execute(
            text(
                """
                SELECT a.atttypmod AS dim
                FROM pg_attribute a
                JOIN pg_class c ON c.oid = a.attrelid
                WHERE c.relname = 'chunks' AND a.attname = 'embedding'
                """
            )
        )
        row = result.first()
        if row is None or row.dim in (None, -1):
            return
        if int(row.dim) != int(self._settings.embedding_dim):
            raise DatabaseUnavailableError(
                "Embedding dimension mismatch between the database and configuration",
                hint=(
                    f"The chunks table stores {row.dim}-dim vectors but "
                    f"EMBEDDING_DIM is {self._settings.embedding_dim}. Either restore "
                    "the previous embedding model, or re-index with `make reset-index` "
                    "followed by `make ingest-full`."
                ),
                detail={"db_dim": int(row.dim),
                        "configured_dim": self._settings.embedding_dim},
            )

    async def ping(self) -> bool:
        try:
            async with self.engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
            return True
        except Exception as exc:
            log.warning("db.ping.failed", extra={"error": str(exc)})
            return False

    async def disconnect(self) -> None:
        if self._engine is not None:
            await self._engine.dispose()
            self._engine = None
            self._sessionmaker = None

    # -------------------------------------------------------------- sessions
    def session_factory(self) -> async_sessionmaker:
        if self._sessionmaker is None:
            raise DatabaseUnavailableError("Database is not initialised")
        return self._sessionmaker

    async def session(self) -> AsyncIterator:
        """FastAPI dependency: yields a session, commits, rolls back on error."""
        factory = self.session_factory()
        async with factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise
