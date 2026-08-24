"""Anthropic provider (cloud, optional).

Uses the official `anthropic` SDK. Optional at runtime: with no API key the
provider reports itself unavailable rather than raising, so the app runs fine
on local Ollama alone — which is the mandated demo configuration.

Note on Anthropic's message shape: system prompts are a top-level `system`
parameter, not a message with role="system". We split them out in `_split`.
"""

from __future__ import annotations

from typing import Any, AsyncIterator

from ..core.config import Settings
from ..core.errors import ProviderTimeoutError, ProviderUnavailableError
from ..core.logging import get_logger
from .base import Completion, LLMProvider, Message, ProviderHealth, Usage

log = get_logger(__name__)

_NO_KEY_REASON = "no API key configured"
_NO_KEY_HINT = "Set ANTHROPIC_API_KEY in your .env file to enable this provider."


class AnthropicProvider(LLMProvider):
    name = "anthropic"

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._api_key = settings.anthropic_api_key
        self._default_model = settings.anthropic_model
        self._client: Any = None
        self._sdk_error: str | None = None

        if not self._api_key:
            return
        try:
            import anthropic

            self._anthropic = anthropic
            self._client = anthropic.AsyncAnthropic(
                api_key=self._api_key,
                timeout=settings.llm_timeout_seconds,
                max_retries=2,
            )
        except ImportError as exc:  # pragma: no cover - dependency is pinned
            self._sdk_error = f"anthropic SDK not installed: {exc}"

    # ------------------------------------------------------------- internals
    @staticmethod
    def _split(messages: list[Message]) -> tuple[str | None, list[dict[str, str]]]:
        """Split system messages out of the turn list."""
        system_parts = [m.content for m in messages if m.role == "system"]
        turns = [
            {"role": m.role, "content": m.content}
            for m in messages
            if m.role != "system"
        ]
        return ("\n\n".join(system_parts) or None), turns

    def _require_client(self) -> Any:
        if self._client is None:
            raise ProviderUnavailableError(
                f"Anthropic provider is not configured ({self._sdk_error or _NO_KEY_REASON})",
                hint=_NO_KEY_HINT,
                detail={"provider": self.name},
            )
        return self._client

    def _translate(self, exc: Exception, model: str) -> Exception:
        """Map SDK exceptions onto our error taxonomy.

        Most specific first — a single broad handler would lose the distinction
        between retryable (429, 5xx, network) and terminal (401, 404) failures,
        which is exactly the distinction the operator needs.
        """
        a = self._anthropic
        if isinstance(exc, a.AuthenticationError):
            return ProviderUnavailableError(
                "Anthropic rejected the API key",
                hint="Check ANTHROPIC_API_KEY is valid and not expired.",
                detail={"provider": self.name},
            )
        if isinstance(exc, a.NotFoundError):
            return ProviderUnavailableError(
                f"Anthropic model '{model}' was not found",
                hint="Check ANTHROPIC_MODEL. Current IDs carry no date suffix, "
                     "e.g. claude-opus-5.",
                detail={"provider": self.name, "model": model},
            )
        if isinstance(exc, a.RateLimitError):
            return ProviderUnavailableError(
                "Anthropic rate limit reached",
                hint="Wait and retry, or lower request volume.",
                detail={"provider": self.name, "retryable": True},
            )
        if isinstance(exc, a.APITimeoutError):
            return ProviderTimeoutError(
                f"Anthropic timed out after {self._settings.llm_timeout_seconds:.0f}s",
                detail={"provider": self.name, "model": model},
            )
        if isinstance(exc, a.APIConnectionError):
            return ProviderUnavailableError(
                "Could not reach the Anthropic API",
                hint="Check network connectivity and any proxy settings.",
                detail={"provider": self.name, "retryable": True},
            )
        if isinstance(exc, a.APIStatusError):
            return ProviderUnavailableError(
                f"Anthropic returned HTTP {exc.status_code}",
                detail={"provider": self.name, "status": exc.status_code},
            )
        return exc

    @staticmethod
    def _check_refusal(message: Any) -> None:
        """Surface a safety refusal as a clean error.

        A refusal arrives as HTTP 200 with stop_reason="refusal" and no usable
        content, so `stop_reason` must be checked before reading content —
        otherwise this silently looks like an empty answer.

        Production upgrade: Anthropic offers server-side fallbacks
        (beta `server-side-fallback-2026-07-01` + `fallbacks="default"`) which
        reroute refusals automatically. Deliberately not enabled here — this
        codebase already has its own provider fallback chain, and pinning a beta
        header in a handoff deliverable adds a maintenance burden for a path the
        local demo never exercises.
        """
        if getattr(message, "stop_reason", None) == "refusal":
            details = getattr(message, "stop_details", None)
            raise ProviderUnavailableError(
                "Anthropic declined to answer this request",
                hint="Rephrase the question, or switch provider.",
                detail={
                    "provider": "anthropic",
                    "category": getattr(details, "category", None),
                },
            )

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
        system, turns = self._split(messages)

        kwargs: dict[str, Any] = {
            "model": target,
            "max_tokens": max_tokens or self._settings.llm_max_tokens,
            "messages": turns,
        }
        if system:
            kwargs["system"] = system

        try:
            message = await client.messages.create(**kwargs)
        except Exception as exc:
            raise self._translate(exc, target) from exc

        self._check_refusal(message)

        text = "".join(
            block.text for block in message.content if getattr(block, "type", None) == "text"
        )
        return Completion(
            text=text,
            model=target,
            provider=self.name,
            usage=Usage(
                prompt_tokens=message.usage.input_tokens,
                completion_tokens=message.usage.output_tokens,
            ),
            finish_reason=message.stop_reason,
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
        system, turns = self._split(messages)

        kwargs: dict[str, Any] = {
            "model": target,
            "max_tokens": max_tokens or self._settings.llm_max_tokens,
            "messages": turns,
        }
        if system:
            kwargs["system"] = system

        try:
            async with client.messages.stream(**kwargs) as stream:
                async for text in stream.text_stream:
                    yield text
                self._check_refusal(await stream.get_final_message())
        except Exception as exc:
            translated = self._translate(exc, target)
            if translated is exc:
                raise
            raise translated from exc

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
        # A key is present. We report configured-and-ready without spending a
        # billable call on every health poll; a bad key surfaces on first use
        # as a clear AuthenticationError translation above.
        return ProviderHealth(
            name=self.name,
            available=True,
            model=self._default_model,
            detail={"key_configured": True},
        )

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.close()
