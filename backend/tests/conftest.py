"""Shared fixtures.

Two tiers of test run here:

* **Unit** — no database, no model, no network. A fake provider stands in for
  the LLM. These must run anywhere, including CI with nothing installed.
* **Integration** — marked `integration`, needs a live PostgreSQL. Skipped
  automatically when the database is absent rather than failing, so a
  contributor without Docker still gets a green, meaningful suite.

The split exists because a test suite that can only run with the full stack up
is a suite people stop running.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from typing import Any, AsyncIterator

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import Settings, reload_settings  # noqa: E402
from app.llm.base import Completion, LLMProvider, Message, ProviderHealth, Usage  # noqa: E402


# --------------------------------------------------------------- settings


@pytest.fixture(scope="session", autouse=True)
def _test_env() -> None:
    """Pin the environment so a developer's .env cannot change test outcomes."""
    os.environ.update(
        {
            "APP_ENV": "test",
            "LOG_LEVEL": "WARNING",
            "LOG_FORMAT": "console",
            "LLM_PROVIDER": "ollama",
            "EMBEDDING_DIM": "768",
            # Fail fast when the database is absent. The production default
            # retries with backoff for ~15s, which would make every no-database
            # test run feel broken.
            "DB_CONNECT_RETRIES": "1",
            "DB_CONNECT_BACKOFF_SECONDS": "0.1",
            # Must track the calibrated default in app/core/config.py. Pinned so
            # a developer's local .env cannot change test outcomes, but pinning
            # it to a stale value silently tests a floor the product does not use.
            "RAG_MIN_SIMILARITY": "0.50",
            "RAG_TOP_K": "5",
            "RAG_CANDIDATES": "20",
            # Blank values on purpose: these reproduce the .env.example shape
            # that once crashed startup.
            "LLM_FALLBACK_PROVIDER": "",
            "INGEST_MAX_EPISODES": "",
            "ANTHROPIC_API_KEY": "",
            "OPENAI_API_KEY": "",
        }
    )
    reload_settings()


@pytest.fixture()
def settings() -> Settings:
    return reload_settings()


# ---------------------------------------------------------- fake provider


class FakeProvider(LLMProvider):
    """A deterministic stand-in for a model.

    Records the messages it was handed, so a test can assert on the *prompt*
    a skill built — which is where grounding rules actually live — without
    involving a real model or its non-determinism.
    """

    name = "fake"

    def __init__(
        self,
        reply: str = "A grounded answer [S1].",
        *,
        available: bool = True,
        embedding: list[float] | None = None,
        fail_with: Exception | None = None,
    ) -> None:
        self.reply = reply
        self._available = available
        self._embedding = embedding or [0.1] * 768
        self._fail_with = fail_with
        self.calls: list[list[Message]] = []

    async def complete(
        self,
        messages: list[Message],
        *,
        model: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> Completion:
        if self._fail_with:
            raise self._fail_with
        self.calls.append(list(messages))
        return Completion(
            text=self.reply,
            model=model or "fake-model",
            provider=self.name,
            usage=Usage(prompt_tokens=10, completion_tokens=5),
        )

    async def stream(
        self,
        messages: list[Message],
        *,
        model: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> AsyncIterator[str]:
        if self._fail_with:
            raise self._fail_with
        self.calls.append(list(messages))
        for word in self.reply.split(" "):
            yield word + " "

    async def embed(
        self, texts: list[str], *, model: str | None = None
    ) -> list[list[float]]:
        return [list(self._embedding) for _ in texts]

    async def health(self) -> ProviderHealth:
        return ProviderHealth(
            name=self.name,
            available=self._available,
            model="fake-model",
            reason=None if self._available else "fake provider disabled for this test",
        )

    @property
    def last_prompt(self) -> str:
        """Every message of the most recent call, concatenated."""
        if not self.calls:
            return ""
        return "\n".join(m.content for m in self.calls[-1])


@pytest.fixture()
def fake_provider() -> FakeProvider:
    return FakeProvider()


# ------------------------------------------------------------- retrieval


class FakeRetriever:
    """Returns a fixed result, so skills can be tested without a database."""

    def __init__(self, result: Any) -> None:
        self.result = result
        self.queries: list[str] = []

    async def retrieve(self, query: str, *, top_k: int | None = None) -> Any:
        self.queries.append(query)
        return self.result


def make_chunk(
    index: int = 1,
    *,
    episode_id: str = "ep-1",
    title: str = "Finding product-market fit",
    text: str = "You know you have PMF when usage pulls you forward.",
    similarity: float = 0.8,
) -> Any:
    from app.rag.retriever import RetrievedChunk

    return RetrievedChunk(
        chunk_id=f"chunk-{index}",
        episode_id=episode_id,
        episode_title=title,
        guest="A Guest",
        youtube_url="https://www.youtube.com/watch?v=abc123",
        text=text,
        speakers=["Lenny", "A Guest"],
        start_seconds=120,
        start_label="00:02:00",
        vector_similarity=similarity,
        fused_score=0.9,
        sources=["vector"],
    )


# ----------------------------------------------------------- integration


def _database_url() -> str:
    return os.getenv(
        "TEST_DATABASE_URL",
        "postgresql+asyncpg://lenny:lenny@localhost:5432/lenny",
    )


async def _can_connect(url: str) -> bool:
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(url)
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False
    finally:
        await engine.dispose()


@pytest.fixture(scope="session")
def database_available() -> bool:
    try:
        return asyncio.run(_can_connect(_database_url()))
    except Exception:
        return False


@pytest.fixture(autouse=True)
def _skip_integration_without_db(request: pytest.FixtureRequest) -> None:
    """Skip, never fail, when the database an integration test needs is absent."""
    if request.node.get_closest_marker("integration"):
        if not request.getfixturevalue("database_available"):
            pytest.skip("PostgreSQL not reachable — start it with `docker compose up -d db`")
