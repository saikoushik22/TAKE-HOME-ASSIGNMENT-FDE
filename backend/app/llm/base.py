"""Provider protocol.

Every provider implements this one interface, so no caller ever knows which
model it is talking to. Adding a provider means one new file plus a registry
entry — no changes anywhere else. See architecture.md section 7.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import AsyncIterator, Literal

Role = Literal["system", "user", "assistant"]


@dataclass(slots=True)
class Message:
    role: Role
    content: str


@dataclass(slots=True)
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    def to_dict(self) -> dict[str, int]:
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
        }


@dataclass(slots=True)
class Completion:
    text: str
    model: str
    provider: str
    usage: Usage = field(default_factory=Usage)
    finish_reason: str | None = None


@dataclass(slots=True)
class ProviderHealth:
    """Availability of a provider, and — when unavailable — why.

    ``reason`` exists so the UI can show a provider disabled *with its
    precondition attached* ("no API key configured") rather than hiding it.
    A hidden option looks like a missing feature and generates a support
    question; a disabled one that states its own reason answers it.
    """

    name: str
    available: bool
    model: str
    reason: str | None = None
    detail: dict[str, object] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "available": self.available,
            "model": self.model,
            "reason": self.reason,
            "detail": self.detail,
        }


class LLMProvider(abc.ABC):
    """Abstract model provider."""

    name: str

    @abc.abstractmethod
    async def complete(
        self,
        messages: list[Message],
        *,
        model: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> Completion:
        """Return a full completion."""

    @abc.abstractmethod
    def stream(
        self,
        messages: list[Message],
        *,
        model: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> AsyncIterator[str]:
        """Yield text deltas as they arrive.

        Implemented as an async generator, so it is declared (not decorated)
        abstract here and must not be awaited before iteration.
        """

    async def embed(self, texts: list[str], *, model: str | None = None) -> list[list[float]]:
        """Embed texts. Providers that cannot embed raise NotImplementedError."""
        raise NotImplementedError(f"{self.name} does not support embeddings")

    @abc.abstractmethod
    async def health(self) -> ProviderHealth:
        """Report availability without raising."""

    async def aclose(self) -> None:
        """Release any held connections."""
        return None
