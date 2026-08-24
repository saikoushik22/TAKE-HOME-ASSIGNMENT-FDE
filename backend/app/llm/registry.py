"""Provider registry and fallback chain.

Resolves a provider name to a live adapter, caching one instance per provider
for the process lifetime so HTTP connection pools are reused.

Fallback policy (architecture.md section 7.3): OFF by default. Answering with a
different model than the caller selected makes results irreproducible, and a
cloud fallback would ship local data off the machine without consent. When it
is enabled and fires, the caller is always told which model actually answered.
"""

from __future__ import annotations

import asyncio
from typing import Iterable

from ..core.config import ProviderName, Settings
from ..core.errors import ProviderUnavailableError
from ..core.logging import get_logger
from .anthropic_provider import AnthropicProvider
from .base import LLMProvider, ProviderHealth
from .ollama_provider import OllamaProvider
from .openai_provider import OpenAIProvider

log = get_logger(__name__)

ALL_PROVIDERS: tuple[ProviderName, ...] = ("ollama", "anthropic", "openai")

_BUILDERS = {
    "ollama": OllamaProvider,
    "anthropic": AnthropicProvider,
    "openai": OpenAIProvider,
}


class ProviderRegistry:
    """Lazily builds and caches providers."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._cache: dict[ProviderName, LLMProvider] = {}
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------ resolution
    def get(self, name: ProviderName | str) -> LLMProvider:
        if name not in _BUILDERS:
            raise ProviderUnavailableError(
                f"Unknown provider '{name}'",
                hint=f"Valid providers: {', '.join(ALL_PROVIDERS)}",
                detail={"requested": name, "valid": list(ALL_PROVIDERS)},
                status_code=422,
                code="UNKNOWN_PROVIDER",
            )
        cached = self._cache.get(name)  # type: ignore[arg-type]
        if cached is None:
            cached = _BUILDERS[name](self._settings)  # type: ignore[index]
            self._cache[name] = cached  # type: ignore[index]
        return cached

    @property
    def default_provider(self) -> ProviderName:
        return self._settings.llm_provider

    def default_model(self, provider: ProviderName | None = None) -> str:
        return self._settings.model_for(provider or self._settings.llm_provider)

    # --------------------------------------------------------------- health
    async def health_all(self) -> list[ProviderHealth]:
        """Health for every provider, concurrently. Never raises."""
        async def one(name: ProviderName) -> ProviderHealth:
            try:
                return await self.get(name).health()
            except Exception as exc:  # defensive: health must never break a page
                log.warning(
                    "llm.health.error",
                    extra={"provider": name, "error": str(exc)},
                )
                return ProviderHealth(
                    name=name,
                    available=False,
                    model=self._settings.model_for(name),
                    reason=f"health check failed: {exc}",
                )

        return list(await asyncio.gather(*(one(n) for n in ALL_PROVIDERS)))

    # -------------------------------------------------------------- fallback
    def resolve_chain(self, requested: ProviderName | None) -> list[ProviderName]:
        """Ordered providers to attempt for one call."""
        primary: ProviderName = requested or self._settings.llm_provider
        chain: list[ProviderName] = [primary]
        if self._settings.llm_fallback_enabled:
            fb = self._settings.llm_fallback_provider
            if fb and fb != primary:
                chain.append(fb)
        return chain

    async def acquire(
        self, requested: ProviderName | None
    ) -> tuple[LLMProvider, ProviderName | None]:
        """Return a healthy provider, plus the provider it fell back *from*.

        The second element is None on the happy path. When it is set, the
        caller is responsible for telling the user — via the `x-llm-fallback-from`
        header and a UI banner — that a different model answered.
        """
        chain = self.resolve_chain(requested)
        first_error: Exception | None = None

        for index, name in enumerate(chain):
            provider = self.get(name)
            health = await provider.health()
            if health.available:
                if index > 0:
                    log.warning(
                        "llm.fallback",
                        extra={"from": chain[0], "to": name,
                               "reason": str(first_error)},
                    )
                    return provider, chain[0]
                return provider, None
            if first_error is None:
                first_error = ProviderUnavailableError(
                    f"Provider '{name}' is unavailable: {health.reason}",
                    hint=str(health.detail.get("hint") or "") or None,
                    detail={"provider": name, **health.detail},
                )

        raise first_error or ProviderUnavailableError(
            "No model provider is available",
            hint="Start Ollama (`ollama serve`) or configure a cloud API key.",
            detail={"attempted": list(chain)},
        )

    # ------------------------------------------------------------- lifecycle
    async def aclose(self) -> None:
        for provider in self._cache.values():
            try:
                await provider.aclose()
            except Exception:  # pragma: no cover - shutdown best effort
                log.warning("llm.close.error", extra={"provider": provider.name})
        self._cache.clear()


def embedding_provider(registry: ProviderRegistry, settings: Settings) -> LLMProvider:
    """Provider used for embeddings.

    Deliberately independent of the chat provider: embeddings stay local by
    default even when chat runs in the cloud, because re-embedding the corpus
    through a paid API is a cost surprise nobody asked for.
    """
    return registry.get(settings.embedding_provider)


def summarize(healths: Iterable[ProviderHealth]) -> dict[str, object]:
    healths = list(healths)
    return {
        "providers": [h.to_dict() for h in healths],
        "any_available": any(h.available for h in healths),
    }
