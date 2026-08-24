"""OpenAI provider (cloud, optional).

Uses the official `openai` SDK. Because `openai_base_url` is configurable, this
adapter also covers any OpenAI-compatible endpoint (vLLM, LM Studio, Together,
OpenRouter) without additional code — a cheap way to widen provider coverage
for the inheriting team.
"""

from __future__ import annotations

from typing import Any, AsyncIterator

from ..core.config import Settings
from ..core.errors import ProviderTimeoutError, ProviderUnavailableError
from ..core.logging import get_logger
from .base import Completion, LLMProvider, Message, ProviderHealth, Usage

log = get_logger(__name__)

_NO_KEY_REASON = "no API key configured"
_NO_KEY_HINT = "Set OPENAI_API_KEY in your .env file to enable this provider."


class OpenAIProvider(LLMProvider):
    name = "openai"

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._default_model = settings.openai_model
        self._client: Any = None
        self._sdk_error: str | None = None

        if not settings.openai_api_key:
            return
        try:
            import openai

            self._openai = openai
            kwargs: dict[str, Any] = {
                "api_key": settings.openai_api_key,
                "timeout": settings.llm_timeout_seconds,
                "max_retries": 2,
            }
            if settings.openai_base_url:
                kwargs["base_url"] = settings.openai_base_url
            self._client = openai.AsyncOpenAI(**kwargs)
        except ImportError as exc:  # pragma: no cover - dependency is pinned
            self._sdk_error = f"openai SDK not installed: {exc}"

    # ------------------------------------------------------------- internals
    def _require_client(self) -> Any:
        if self._client is None:
            raise ProviderUnavailableError(
                f"OpenAI provider is not configured ({self._sdk_error or _NO_KEY_REASON})",
                hint=_NO_KEY_HINT,
                detail={"provider": self.name},
            )
        return self._client

    def _translate(self, exc: Exception, model: str) -> Exception:
        o = self._openai
        if isinstance(exc, o.AuthenticationError):
            return ProviderUnavailableError(
                "OpenAI rejected the API key",
                hint="Check OPENAI_API_KEY is valid.",
                detail={"provider": self.name},
            )
        if isinstance(exc, o.NotFoundError):
            return ProviderUnavailableError(
                f"OpenAI model '{model}' was not found",
                hint="Check OPENAI_MODEL is a model your key can access.",
                detail={"provider": self.name, "model": model},
            )
        if isinstance(exc, o.RateLimitError):
            return ProviderUnavailableError(
                "OpenAI rate limit reached",
                hint="Wait and retry, or lower request volume.",
                detail={"provider": self.name, "retryable": True},
            )
        if isinstance(exc, o.APITimeoutError):
            return ProviderTimeoutError(
                f"OpenAI timed out after {self._settings.llm_timeout_seconds:.0f}s",
                detail={"provider": self.name, "model": model},
            )
        if isinstance(exc, o.APIConnectionError):
            return ProviderUnavailableError(
                "Could not reach the OpenAI API",
                hint="Check network connectivity and OPENAI_BASE_URL if set.",
                detail={"provider": self.name, "retryable": True},
            )
        if isinstance(exc, o.APIStatusError):
            return ProviderUnavailableError(
                f"OpenAI returned HTTP {exc.status_code}",
                detail={"provider": self.name, "status": exc.status_code},
            )
        return exc

    # -------------------------------------------------------------- complete
    async def complete(
        self,
        messages: list[Message],
        *,
        model: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> Completion:
        client = self._require_client()
        target = model or self._default_model
        try:
            response = await client.chat.completions.create(
                model=target,
                messages=[{"role": m.role, "content": m.content} for m in messages],
                temperature=(
                    self._settings.llm_temperature if temperature is None else temperature
                ),
                max_tokens=max_tokens or self._settings.llm_max_tokens,
            )
        except Exception as exc:
            raise self._translate(exc, target) from exc

        choice = response.choices[0]
        usage = response.usage
        return Completion(
            text=choice.message.content or "",
            model=target,
            provider=self.name,
            usage=Usage(
                prompt_tokens=getattr(usage, "prompt_tokens", 0) or 0,
                completion_tokens=getattr(usage, "completion_tokens", 0) or 0,
            ),
            finish_reason=choice.finish_reason,
        )

    # ---------------------------------------------------------------- stream
    async def stream(
        self,
        messages: list[Message],
        *,
        model: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> AsyncIterator[str]:
        client = self._require_client()
        target = model or self._default_model
        try:
            stream = await client.chat.completions.create(
                model=target,
                messages=[{"role": m.role, "content": m.content} for m in messages],
                temperature=(
                    self._settings.llm_temperature if temperature is None else temperature
                ),
                max_tokens=max_tokens or self._settings.llm_max_tokens,
                stream=True,
            )
            async for chunk in stream:
                if not chunk.choices:
                    continue
                piece = chunk.choices[0].delta.content
                if piece:
                    yield piece
        except Exception as exc:
            translated = self._translate(exc, target)
            if translated is exc:
                raise
            raise translated from exc

    # ----------------------------------------------------------------- embed
    async def embed(self, texts: list[str], *, model: str | None = None) -> list[list[float]]:
        if not texts:
            return []
        client = self._require_client()
        target = model or "text-embedding-3-small"
        try:
            response = await client.embeddings.create(model=target, input=texts)
        except Exception as exc:
            raise self._translate(exc, target) from exc
        return [item.embedding for item in response.data]

    # ---------------------------------------------------------------- health
    async def health(self) -> ProviderHealth:
        if self._client is None:
            return ProviderHealth(
                name=self.name,
                available=False,
                model=self._default_model,
                reason=self._sdk_error or _NO_KEY_REASON,
                detail={"hint": _NO_KEY_HINT},
            )
        return ProviderHealth(
            name=self.name,
            available=True,
            model=self._default_model,
            detail={"key_configured": True,
                    "base_url": self._settings.openai_base_url or "default"},
        )

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.close()
