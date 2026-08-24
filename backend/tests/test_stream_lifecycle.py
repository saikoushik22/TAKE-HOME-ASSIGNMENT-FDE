"""Streaming connection lifetime.

Regression tests for a bug that wedged the whole application.

Abandoning a turn — opening a new chat while an answer was still streaming —
leaked a pooled database connection stuck `idle in transaction`, still holding
row locks on `sessions` from the end-of-turn touch. Five of those exhausted the
pool, and every later session create, rename or delete queued behind them
forever. To a user it looked like the app had silently stopped responding.

Root cause: the endpoint took the request-scoped session dependency. A
dependency with `yield` is torn down after the response completes, and for a
StreamingResponse that teardown is not guaranteed when the client disconnects
mid-stream. The fix moved the session inside the generator, where `async with`
runs on GeneratorExit too.

These tests use a fake turn rather than a real model, so they are fast and
deterministic — the property under test is connection lifetime, not answers.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any, AsyncIterator, Iterator

import pytest
from fastapi.testclient import TestClient

from app.main import create_app


@pytest.fixture(scope="module")
def app_and_client() -> Iterator[tuple[Any, TestClient]]:
    app = create_app()
    with TestClient(app) as client:
        yield app, client


def _checked_out(app: Any) -> int:
    """Connections currently held out of the pool."""
    return app.state.database.engine.pool.checkedout()


async def _slow_turn(*args: Any, **kwargs: Any) -> AsyncIterator[dict[str, Any]]:
    """A turn that streams for long enough to be abandoned mid-flight.

    Accepts *args because it replaces an instance method and therefore receives
    the Orchestrator as its first positional argument.
    """
    yield {"type": "routing", "stage": "routing", "skill": "grounded_qa",
           "confidence": 1.0, "rule": "test", "artifact_kind": None}
    for index in range(40):
        await asyncio.sleep(0.05)
        yield {"type": "token", "text": f"token-{index} "}
    yield {"type": "done", "message_id": str(uuid.uuid4()),
           "user_message_id": str(uuid.uuid4()), "session_id": str(uuid.uuid4()),
           "skill": "grounded_qa", "provider": "fake", "model": "fake",
           "latency_ms": 1, "abstained": False, "citations": [],
           "artifact_id": None, "fallback_from": None}


@pytest.mark.integration
def test_abandoned_stream_releases_its_connection(
    app_and_client: tuple[Any, TestClient],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """THE regression. Disconnecting mid-stream must not leak a connection."""
    app, client = app_and_client
    monkeypatch.setattr("app.api.chat.Orchestrator.handle", _slow_turn)

    session_id = client.post("/api/sessions", json={"title": "abandon"}).json()["id"]
    baseline = _checked_out(app)

    try:
        # Abandon after the first frames, exactly as closing a chat mid-answer does.
        with client.stream(
            "POST", f"/api/sessions/{session_id}/stream", json={"message": "hello"}
        ) as response:
            assert response.status_code == 200
            for count, _ in enumerate(response.iter_lines()):
                if count >= 2:
                    break  # leaves the response body unread — a real disconnect

        # Give teardown a moment to run on the portal thread.
        for _ in range(50):
            if _checked_out(app) <= baseline:
                break
            import time
            time.sleep(0.1)

        assert _checked_out(app) <= baseline, (
            f"abandoned stream leaked a connection: "
            f"{_checked_out(app)} checked out vs baseline {baseline}"
        )
    finally:
        client.delete(f"/api/sessions/{session_id}")


@pytest.mark.integration
def test_completed_stream_releases_its_connection(
    app_and_client: tuple[Any, TestClient],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The happy path must not leak either."""
    app, client = app_and_client
    monkeypatch.setattr("app.api.chat.Orchestrator.handle", _slow_turn)

    session_id = client.post("/api/sessions", json={"title": "complete"}).json()["id"]
    baseline = _checked_out(app)

    try:
        with client.stream(
            "POST", f"/api/sessions/{session_id}/stream", json={"message": "hello"}
        ) as response:
            frames = list(response.iter_lines())

        assert any("done" in frame for frame in frames)
        assert _checked_out(app) <= baseline
    finally:
        client.delete(f"/api/sessions/{session_id}")


@pytest.mark.integration
def test_repeated_abandonment_does_not_exhaust_the_pool(
    app_and_client: tuple[Any, TestClient],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The failure mode was cumulative, so one abandonment is not enough to prove it.

    Six exceeds the default pool size of five — the point at which the original
    bug deadlocked every subsequent write.
    """
    app, client = app_and_client
    monkeypatch.setattr("app.api.chat.Orchestrator.handle", _slow_turn)

    session_id = client.post("/api/sessions", json={"title": "repeat"}).json()["id"]
    baseline = _checked_out(app)

    try:
        for _ in range(6):
            with client.stream(
                "POST", f"/api/sessions/{session_id}/stream", json={"message": "hi"}
            ) as response:
                for count, _line in enumerate(response.iter_lines()):
                    if count >= 1:
                        break

        # The real proof: the app still works afterwards. Under the old bug this
        # request hung forever behind the leaked row locks.
        renamed = client.patch(f"/api/sessions/{session_id}", json={"title": "still alive"})
        assert renamed.status_code == 200
        assert renamed.json()["title"] == "still alive"
        assert _checked_out(app) <= baseline
    finally:
        client.delete(f"/api/sessions/{session_id}")


def test_stream_endpoint_does_not_take_the_request_scoped_session() -> None:
    """Structural guard on the fix itself.

    Re-introducing `DbDep` here would silently restore the leak, because the
    symptom only appears once several turns have been abandoned. Cheap to
    assert, and it names the reason in the failure message.
    """
    import inspect

    from app.api.chat import stream_message

    annotations = {
        name: str(param.annotation)
        for name, param in inspect.signature(stream_message).parameters.items()
    }
    joined = " ".join(annotations.values())

    assert "DatabaseDep" in joined or "Database" in joined, (
        "the stream endpoint must own its session"
    )
    assert "AsyncSession" not in joined, (
        "stream_message must NOT take the request-scoped session dependency: its "
        "teardown is not guaranteed when a client disconnects mid-stream, which "
        "leaks a connection stuck idle in transaction"
    )
