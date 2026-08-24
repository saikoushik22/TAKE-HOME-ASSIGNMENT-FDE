"""Configuration layer and the provider toggle.

The brief requires switching models without touching application code, so these
tests treat "config-only swap" as a contract rather than a convention.
"""

from __future__ import annotations

import os

import pytest

from app.core.config import Settings, reload_settings
from app.core.errors import ProviderUnavailableError
from app.llm.registry import ALL_PROVIDERS, ProviderRegistry, embedding_provider


def _settings(**overrides: str) -> Settings:
    """Build settings from an explicit environment slice."""
    previous = {k: os.environ.get(k) for k in overrides}
    os.environ.update(overrides)
    try:
        return reload_settings()
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        reload_settings()


# ------------------------------------------------- blank-env regression


@pytest.mark.parametrize(
    'variable',
    ['LLM_FALLBACK_PROVIDER', 'INGEST_MAX_EPISODES', 'OPENAI_BASE_URL', 'TRANSCRIPT_LOCAL_PATH'],
)
def test_blank_optional_variables_are_treated_as_unset(variable: str) -> None:
    """Regression: copying .env.example verbatim once crashed startup.

    `.env.example` ships optional settings as a bare `KEY=`, which is how a
    reader expects to see "leave this blank". Pydantic cannot coerce '' to
    `None` for a typed optional, so the documented setup path failed on the
    very first command.
    """
    settings = _settings(**{variable: ''})
    assert getattr(settings, variable.lower()) is None


def test_placeholder_api_keys_are_treated_as_absent() -> None:
    """A copied placeholder must read as 'not configured', not as a bad key.

    Otherwise the user gets a confusing 401 from the provider instead of a
    clear disabled state in the UI.
    """
    for placeholder in ('your-key-here', 'sk-xxx-replace-me', '<your key>', '   '):
        settings = _settings(ANTHROPIC_API_KEY=placeholder)
        assert settings.anthropic_api_key is None, placeholder


def test_a_real_key_is_preserved() -> None:
    settings = _settings(ANTHROPIC_API_KEY='sk-ant-abc123')
    assert settings.anthropic_api_key == 'sk-ant-abc123'


# ------------------------------------------------------- model resolution


def test_each_provider_resolves_its_own_default_model() -> None:
    settings = _settings(LLM_PROVIDER='ollama', LLM_MODEL='')
    assert settings.model_for('ollama') == settings.ollama_model
    assert settings.model_for('anthropic') == settings.anthropic_model
    assert settings.model_for('openai') == settings.openai_model


def test_llm_model_override_applies_only_to_the_active_provider() -> None:
    """A local model name must never leak into a cloud request.

    Without this scoping, setting LLM_MODEL for Ollama would send
    'llama3.2:3b' to Anthropic and produce a baffling 404.
    """
    settings = _settings(LLM_PROVIDER='ollama', LLM_MODEL='qwen2.5:7b')
    assert settings.model_for('ollama') == 'qwen2.5:7b'
    assert settings.model_for('anthropic') == settings.anthropic_model
    assert 'qwen' not in settings.model_for('anthropic')


def test_ollama_needs_no_api_key() -> None:
    settings = reload_settings()
    assert settings.api_key_for('ollama') is None


# ------------------------------------------------------------ validation


@pytest.mark.parametrize('value', ['-0.5', '2.5'])
def test_out_of_range_temperature_is_rejected(value: str) -> None:
    with pytest.raises(Exception):
        _settings(LLM_TEMPERATURE=value)


def test_candidates_below_top_k_is_rejected() -> None:
    """Fusing fewer candidates than you intend to return is incoherent."""
    with pytest.raises(Exception):
        _settings(RAG_TOP_K='30', RAG_CANDIDATES='5')


def test_relative_data_dir_resolves_against_the_repo_not_the_cwd() -> None:
    """Regression: the ingest CLI and the server run from different directories.

    With cwd-relative resolution the same DATA_DIR pointed at two places and
    the corpus was silently downloaded twice.
    """
    settings = _settings(DATA_DIR='./data')
    assert settings.data_path.is_absolute()
    assert settings.data_path.name == 'data'


# --------------------------------------------------------------- registry


def test_registry_exposes_every_provider() -> None:
    registry = ProviderRegistry(reload_settings())
    for name in ALL_PROVIDERS:
        assert registry.get(name) is not None


def test_registry_caches_one_instance_per_provider() -> None:
    """Connection pools must be reused rather than rebuilt per request."""
    registry = ProviderRegistry(reload_settings())
    assert registry.get('ollama') is registry.get('ollama')


def test_unknown_provider_is_a_clear_422_not_a_key_error() -> None:
    registry = ProviderRegistry(reload_settings())
    with pytest.raises(ProviderUnavailableError) as excinfo:
        registry.get('gpt5-turbo-ultra')
    assert excinfo.value.status_code == 422
    assert 'ollama' in str(excinfo.value.detail)


async def test_health_reports_every_provider_and_never_raises() -> None:
    """Health must not be able to break the config page."""
    registry = ProviderRegistry(reload_settings())
    healths = await registry.health_all()
    assert {h.name for h in healths} == set(ALL_PROVIDERS)


async def test_unconfigured_cloud_provider_reports_its_reason() -> None:
    """The UI shows disabled providers WITH the reason, so the reason must exist."""
    settings = _settings(ANTHROPIC_API_KEY='', OPENAI_API_KEY='')
    registry = ProviderRegistry(settings)
    for name in ('anthropic', 'openai'):
        health = await registry.get(name).health()
        assert health.available is False
        assert health.reason, f'{name} gave no reason for being unavailable'


def test_fallback_is_off_by_default() -> None:
    """Silently answering with a different model makes results irreproducible."""
    settings = reload_settings()
    assert settings.llm_fallback_enabled is False
    assert ProviderRegistry(settings).resolve_chain('ollama') == ['ollama']


def test_fallback_chain_is_ordered_when_enabled() -> None:
    settings = _settings(LLM_FALLBACK_ENABLED='true', LLM_FALLBACK_PROVIDER='anthropic')
    assert ProviderRegistry(settings).resolve_chain('ollama') == ['ollama', 'anthropic']


def test_embeddings_stay_local_when_chat_moves_to_the_cloud() -> None:
    """Re-embedding the corpus through a paid API is a cost surprise nobody asked for."""
    settings = _settings(LLM_PROVIDER='anthropic', EMBEDDING_PROVIDER='ollama')
    registry = ProviderRegistry(settings)
    assert embedding_provider(registry, settings).name == 'ollama'
