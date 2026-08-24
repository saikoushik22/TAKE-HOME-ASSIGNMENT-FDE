"""API contracts.

Split into two tiers. Contract tests (error envelope, validation, liveness) run
with no database, because a suite that only runs with the full stack up is a
suite people stop running. Persistence and chat tests are marked `integration`
and skip cleanly when PostgreSQL is absent.
"""

from __future__ import annotations

import uuid
from typing import Iterator

import pytest
from fastapi.testclient import TestClient

from app.main import create_app


@pytest.fixture(scope='module')
def client() -> Iterator[TestClient]:
    """A client with lifespan run.

    Startup degrades rather than crashing when the database is missing, which
    is exactly the behaviour the no-database tier depends on.
    """
    with TestClient(create_app()) as test_client:
        yield test_client


# =========================================================== no database


def test_liveness_never_touches_a_dependency(client: TestClient) -> None:
    """Liveness must stay green while dependencies are down.

    Otherwise an orchestrator restarts the container because Postgres blinked,
    turning a recoverable dependency outage into an application outage.
    """
    response = client.get('/api/health')
    assert response.status_code == 200

    body = response.json()
    assert body['status'] == 'ok'
    assert body['service'] == 'lenny-growth-assistant'
    assert body['version']


def test_every_response_carries_a_correlation_id(client: TestClient) -> None:
    """One grep on this id must reconstruct a whole turn."""
    response = client.get('/api/health')
    assert response.headers.get('x-correlation-id')


def test_inbound_correlation_id_is_honoured(client: TestClient) -> None:
    """A trace started in the browser should survive into the server logs."""
    supplied = 'trace-abc-123'
    response = client.get('/api/health', headers={'x-correlation-id': supplied})
    assert response.headers['x-correlation-id'] == supplied


def test_unknown_route_uses_the_standard_error_envelope(client: TestClient) -> None:
    """Every non-2xx shares one shape, so clients need exactly one parser."""
    response = client.get('/api/does-not-exist')
    assert response.status_code == 404

    error = response.json()['error']
    assert set(error) >= {'code', 'message', 'detail'}


def test_validation_failures_are_normalized_into_the_envelope(client: TestClient) -> None:
    """FastAPI's native 422 shape would otherwise be the one odd response."""
    response = client.post(
        f'/api/sessions/{uuid.uuid4()}/messages',
        json={'message': ''},  # fails min_length
    )
    assert response.status_code == 422

    error = response.json()['error']
    assert error['code'] == 'VALIDATION_ERROR'
    assert 'fields' in error['detail']
    assert error['detail']['hint']


def test_malformed_uuid_is_rejected_cleanly(client: TestClient) -> None:
    response = client.get('/api/sessions/not-a-uuid')
    assert response.status_code == 422
    assert response.json()['error']['code'] == 'VALIDATION_ERROR'


def test_message_length_is_bounded(client: TestClient) -> None:
    """Unbounded input is an availability problem, not just a validation one."""
    response = client.post(
        f'/api/sessions/{uuid.uuid4()}/messages',
        json={'message': 'x' * 9000},
    )
    assert response.status_code == 422


def test_openapi_documents_the_whole_surface(client: TestClient) -> None:
    spec = client.get('/openapi.json').json()
    paths = spec['paths']
    for expected in (
        '/api/health',
        '/api/health/ready',
        '/api/config',
        '/api/sessions',
        '/api/sessions/{session_id}',
        '/api/sessions/{session_id}/stream',
        '/api/artifacts/{artifact_id}',
        '/api/search',
    ):
        assert expected in paths, f'{expected} is missing from the OpenAPI spec'


def test_readiness_names_each_dependency_individually(client: TestClient) -> None:
    """Readiness must name the broken dependency, not just say 'degraded'.

    Returns 200 even when degraded: the body carries the truth, and a 503 here
    can be swallowed by proxies exactly when you need to read it.
    """
    response = client.get('/api/health/ready')
    assert response.status_code == 200

    body = response.json()
    assert body['status'] in {'ready', 'degraded'}

    names = {dep['name'] for dep in body['dependencies']}
    assert 'database' in names
    assert 'corpus' in names
    assert any(name.startswith('provider:') for name in names)

    # An unhealthy dependency must explain itself.
    for dep in body['dependencies']:
        if not dep['healthy']:
            assert dep['reason'], f"{dep['name']} is unhealthy but gave no reason"


# ========================================================== needs database


@pytest.mark.integration
def test_config_lists_providers_with_reasons(client: TestClient) -> None:
    """Unavailable providers are shown disabled WITH the reason, never hidden."""
    body = client.get('/api/config').json()

    assert body['active_provider'] in {'ollama', 'anthropic', 'openai'}
    assert {p['name'] for p in body['providers']} == {'ollama', 'anthropic', 'openai'}
    assert set(body['skills']) >= {'grounded_qa', 'ship30_essay', 'artifact'}

    for provider in body['providers']:
        if not provider['available']:
            assert provider['reason'], f"{provider['name']} disabled without a reason"


@pytest.mark.integration
def test_session_lifecycle(client: TestClient) -> None:
    created = client.post('/api/sessions', json={'title': 'Lifecycle test'})
    assert created.status_code == 201
    session = created.json()
    session_id = session['id']

    try:
        assert session['title'] == 'Lifecycle test'
        assert session['provider']
        assert session['model']

        fetched = client.get(f'/api/sessions/{session_id}')
        assert fetched.status_code == 200
        assert fetched.json()['messages'] == []

        renamed = client.patch(f'/api/sessions/{session_id}', json={'title': 'Renamed'})
        assert renamed.status_code == 200
        assert renamed.json()['title'] == 'Renamed'

        listed = client.get('/api/sessions').json()
        assert any(s['id'] == session_id for s in listed['sessions'])
    finally:
        assert client.delete(f'/api/sessions/{session_id}').status_code == 204

    assert client.get(f'/api/sessions/{session_id}').status_code == 404


@pytest.mark.integration
def test_missing_session_returns_an_actionable_404(client: TestClient) -> None:
    response = client.get(f'/api/sessions/{uuid.uuid4()}')
    assert response.status_code == 404

    error = response.json()['error']
    assert error['code'] == 'NOT_FOUND'
    assert error['detail'].get('hint')


@pytest.mark.integration
def test_switching_provider_also_switches_the_model(client: TestClient) -> None:
    """A local model name must not survive a switch to a cloud provider."""
    session = client.post('/api/sessions', json={'provider': 'ollama'}).json()
    session_id = session['id']

    try:
        assert 'llama' in session['model'].lower()

        updated = client.patch(
            f'/api/sessions/{session_id}', json={'provider': 'anthropic'}
        ).json()

        assert updated['provider'] == 'anthropic'
        assert 'llama' not in updated['model'].lower()
    finally:
        client.delete(f'/api/sessions/{session_id}')


@pytest.mark.integration
def test_empty_patch_is_rejected_with_a_hint(client: TestClient) -> None:
    session = client.post('/api/sessions', json={}).json()
    try:
        response = client.patch(f"/api/sessions/{session['id']}", json={})
        assert response.status_code == 422
        assert response.json()['error']['detail'].get('hint')
    finally:
        client.delete(f"/api/sessions/{session['id']}")


@pytest.mark.integration
def test_sessions_keep_independent_context(client: TestClient) -> None:
    """AC3: no cross-session bleed. This is a foreign key, not a convention."""
    first = client.post('/api/sessions', json={'title': 'First'}).json()
    second = client.post('/api/sessions', json={'title': 'Second'}).json()

    try:
        assert first['id'] != second['id']
        assert client.get(f"/api/sessions/{first['id']}").json()['messages'] == []
        assert client.get(f"/api/sessions/{second['id']}").json()['messages'] == []

        artifacts = client.get(f"/api/sessions/{first['id']}/artifacts").json()
        assert artifacts['artifacts'] == []
    finally:
        client.delete(f"/api/sessions/{first['id']}")
        client.delete(f"/api/sessions/{second['id']}")


@pytest.mark.integration
def test_search_returns_ranked_hits_or_abstains(client: TestClient) -> None:
    """Retrieval is exposed on its own so it can be judged without the model."""
    response = client.post('/api/search', json={'query': 'product market fit', 'top_k': 5})
    assert response.status_code == 200

    body = response.json()
    assert 'took_ms' in body

    if body['abstained']:
        assert body['reason']
        return

    assert body['hits']
    for hit in body['hits']:
        assert hit['episode_title']
        assert hit['matched_by']
        assert 0.0 <= hit['vector_similarity'] <= 1.0


@pytest.mark.integration
def test_nonsense_query_abstains_rather_than_inventing(client: TestClient) -> None:
    """AC6, at the retrieval layer: below the floor, nothing is returned."""
    response = client.post(
        '/api/search',
        json={'query': 'zxqw plorbnax fnordulate the quibnitz'},
    )
    body = response.json()
    assert body['abstained'] or body['hits'] == []


@pytest.mark.integration
def test_corpus_stats_are_reported(client: TestClient) -> None:
    body = client.get('/api/corpus/stats').json()
    assert set(body) >= {'episodes', 'chunks', 'embedded_chunks', 'ready'}
