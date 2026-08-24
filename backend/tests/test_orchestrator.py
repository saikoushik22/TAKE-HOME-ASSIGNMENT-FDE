"""Orchestrator failure contract.

`handle()` deliberately converts exceptions into terminal `error` events and
then returns normally, so the SSE stream always closes cleanly. That choice has
a sharp edge: the caller never sees an exception, so any cleanup tied to
exception propagation silently does not happen.

These tests pin the edge.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.agent.orchestrator import Orchestrator, derive_title
from app.core.config import reload_settings
from app.core.errors import CorpusEmptyError
from app.llm.registry import ProviderRegistry


class FakeSession:
    """Minimal async session that records rollback/commit calls."""

    def __init__(self) -> None:
        self.rolled_back = 0
        self.committed = 0

    async def rollback(self) -> None:
        self.rolled_back += 1

    async def commit(self) -> None:
        self.committed += 1

    async def execute(self, *args: Any, **kwargs: Any) -> Any:  # pragma: no cover
        raise AssertionError("no query should run in these tests")


async def _drain(orchestrator: Orchestrator, **kwargs: Any) -> list[dict[str, Any]]:
    return [event async for event in orchestrator.handle(**kwargs)]


@pytest.fixture()
def broken_orchestrator(monkeypatch: pytest.MonkeyPatch) -> tuple[Orchestrator, FakeSession]:
    settings = reload_settings()
    session = FakeSession()
    orchestrator = Orchestrator(session, settings, ProviderRegistry(settings))
    return orchestrator, session


async def test_failed_turn_rolls_back_the_session(
    broken_orchestrator: tuple[Orchestrator, FakeSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed turn must not leave a poisoned session behind.

    Regression: without the rollback, the FastAPI session dependency takes its
    SUCCESS path (because handle() returned normally), calls commit() on an
    already-invalid transaction, and raises PendingRollbackError — replacing the
    real, actionable error with a confusing one and breaking every later
    statement on that session.
    """
    orchestrator, session = broken_orchestrator

    async def boom(*args: Any, **kwargs: Any):
        raise CorpusEmptyError("corpus is empty")
        yield  # pragma: no cover - makes this an async generator

    monkeypatch.setattr(orchestrator, "_run", boom)

    events = await _drain(
        orchestrator,
        session_id="00000000-0000-0000-0000-000000000001",
        message="anything",
    )

    assert session.rolled_back == 1, "a failed turn did not roll back"
    assert events[-1]["type"] == "error"
    assert events[-1]["error"]["code"] == "CORPUS_EMPTY"


async def test_unexpected_error_also_rolls_back_and_hides_internals(
    broken_orchestrator: tuple[Orchestrator, FakeSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    orchestrator, session = broken_orchestrator

    async def boom(*args: Any, **kwargs: Any):
        raise RuntimeError("asyncpg connection is closed")
        yield  # pragma: no cover

    monkeypatch.setattr(orchestrator, "_run", boom)

    events = await _drain(
        orchestrator,
        session_id="00000000-0000-0000-0000-000000000002",
        message="anything",
    )

    assert session.rolled_back == 1
    error = events[-1]["error"]
    assert error["code"] == "INTERNAL_ERROR"
    # The internal message must not leak to the client.
    assert "asyncpg" not in error["message"]


async def test_a_failing_rollback_does_not_mask_the_original_error(
    broken_orchestrator: tuple[Orchestrator, FakeSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the connection is gone, rollback fails too. The user still gets an error."""
    orchestrator, session = broken_orchestrator

    async def failing_rollback() -> None:
        raise RuntimeError("connection already closed")

    monkeypatch.setattr(session, "rollback", failing_rollback)

    async def boom(*args: Any, **kwargs: Any):
        raise RuntimeError("original failure")
        yield  # pragma: no cover

    monkeypatch.setattr(orchestrator, "_run", boom)

    events = await _drain(
        orchestrator,
        session_id="00000000-0000-0000-0000-000000000003",
        message="anything",
    )
    assert events[-1]["type"] == "error"


async def test_the_stream_always_terminates_with_a_single_error_event(
    broken_orchestrator: tuple[Orchestrator, FakeSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The client renders the last event; two error events would double-render."""
    orchestrator, _ = broken_orchestrator

    async def boom(*args: Any, **kwargs: Any):
        raise CorpusEmptyError("empty")
        yield  # pragma: no cover

    monkeypatch.setattr(orchestrator, "_run", boom)

    events = await _drain(
        orchestrator,
        session_id="00000000-0000-0000-0000-000000000004",
        message="anything",
    )
    assert sum(1 for e in events if e["type"] == "error") == 1


# ------------------------------------------------------------------ titles


@pytest.mark.parametrize(
    "message,expected",
    [
        ("", "New chat"),
        ("   ", "New chat"),
        ("How do we pick an activation metric?", "How do we pick an activation metric?"),
    ],
)
def test_short_messages_become_the_title_verbatim(message: str, expected: str) -> None:
    assert derive_title(message) == expected


def test_long_messages_are_truncated_on_a_word_boundary() -> None:
    title = derive_title("word " * 60)
    assert len(title) <= 61
    assert title.endswith("…")
    assert not title.replace("…", "").endswith(" ")


def test_whitespace_is_collapsed() -> None:
    assert derive_title("a\n\n  b\tc") == "a b c"
