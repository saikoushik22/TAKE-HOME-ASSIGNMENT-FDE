"""Ollama provider — the local, default, demo-mandatory path.

Talks to the Ollama HTTP API directly with httpx rather than via a client
library: the surface we need is three endpoints, and one fewer dependency is
one fewer thing for the inheriting team to keep current.
"""

from __future__ import annotations

import json
from typing import Any, AsyncIterator

import httpx

from ..core.config import Settings
from ..core.errors import ProviderTimeoutError, ProviderUnavailableError
from ..core.logging import get_logger
from .base import Completion, LLMProvider, Message, ProviderHealth, Usage

log = get_logger(__name__)

_START_HINT = (
    "Start Ollama, then pull the model: `ollama serve` and "
    "`ollama pull llama3.2:3b`. If the backend runs in Docker, set "
    "OLLAMA_BASE_URL=http://host.docker.internal:11434"
)


class OllamaProvider(LLMProvider):
    name = "ollama"

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._base_url = settings.ollama_base_url.rstrip("/")
        self._default_model = settings.ollama_model
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            timeout=httpx.Timeout(settings.llm_timeout_seconds, connect=10.0),
        )

    # ------------------------------------------------------------- internals
    def _payload(
        self,
        messages: list[Message],
        model: str | None,
        temperature: float | None,
        max_tokens: int | None,
        stream: bool,
    ) -> dict[str, Any]:
        return {
            "model": model or self._default_model,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "stream": stream,
            "keep_alive": self._settings.ollama_keep_alive,
            "options": {
                "temperature": (
                    self._settings.llm_temperature if temperature is None else temperature
                ),
                "num_predict": max_tokens or self._settings.llm_max_tokens,
            },
        }

    def _unavailable(self, exc: Exception) -> ProviderUnavailableError:
        return ProviderUnavailableError(
            f"Ollama is not reachable at {self._base_url}",
            hint=_START_HINT,
            detail={"provider": self.name, "base_url": self._base_url,
                    "error": str(exc)},
        )

    @staticmethod
    def _model_missing(body: str, model: str) -> bool:
        lowered = body.lower()
        return "not found" in lowered and model.split(":")[0].lower() in lowered

    # -------------------------------------------------------------- complete
    async def complete(
        self,
        messages: list[Message],
        *,
        model: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> Completion:
        payload = self._payload(messages, model, temperature, max_tokens, stream=False)
        try:
            response = await self._client.post("/api/chat", json=payload)
        except httpx.TimeoutException as exc:
            raise ProviderTimeoutError(
                f"Ollama timed out after {self._settings.llm_timeout_seconds:.0f}s",
                detail={"provider": self.name, "model": payload["model"]},
            ) from exc
        except httpx.HTTPError as exc:
            raise self._unavailable(exc) from exc

        if response.status_code == 404 and self._model_missing(response.text, payload["model"]):
            raise ProviderUnavailableError(
                f"Ollama model '{payload['model']}' is not installed",
                hint=f"Run `ollama pull {payload['model']}`",
                detail={"provider": self.name, "model": payload["model"]},
            )
        if response.status_code >= 400:
            raise ProviderUnavailableError(
                f"Ollama returned HTTP {response.status_code}",
                hint=_START_HINT,
                detail={"provider": self.name, "body": response.text[:500]},
            )

        data = response.json()
        return Completion(
            text=(data.get("message") or {}).get("content", ""),
            model=data.get("model", payload["model"]),
            provider=self.name,
            usage=Usage(
                prompt_tokens=data.get("prompt_eval_count", 0) or 0,
                completion_tokens=data.get("eval_count", 0) or 0,
            ),
            finish_reason=data.get("done_reason"),
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
        payload = self._payload(messages, model, temperature, max_tokens, stream=True)
        try:
            async with self._client.stream("POST", "/api/chat", json=payload) as response:
                if response.status_code >= 400:
                    body = (await response.aread()).decode("utf-8", "replace")
                    if response.status_code == 404 and self._model_missing(body, payload["model"]):
                        raise ProviderUnavailableError(
                            f"Ollama model '{payload['model']}' is not installed",
                            hint=f"Run `ollama pull {payload['model']}`",
                            detail={"provider": self.name, "model": payload["model"]},
                        )
                    raise ProviderUnavailableError(
                        f"Ollama returned HTTP {response.status_code}",
                        hint=_START_HINT,
                        detail={"provider": self.name, "body": body[:500]},
                    )

                async for line in response.aiter_lines():
                    if not line.strip():
                        continue
                    try:
                        chunk = json.loads(line)
                    except json.JSONDecodeError:
                        # A partial line is not worth failing a whole answer over.
                        log.warning("llm.stream.bad_json", extra={"provider": self.name})
                        continue
                    if chunk.get("error"):
                        raise ProviderUnavailableError(
                            f"Ollama error: {chunk['error']}",
                            hint=_START_HINT,
                            detail={"provider": self.name},
                        )
                    piece = (chunk.get("message") or {}).get("content", "")
                    if piece:
                        yield piece
                    if chunk.get("done"):
                        break
        except httpx.TimeoutException as exc:
            raise ProviderTimeoutError(
                f"Ollama timed out after {self._settings.llm_timeout_seconds:.0f}s",
                detail={"provider": self.name, "model": payload["model"]},
            ) from exc
        except httpx.HTTPError as exc:
            raise self._unavailable(exc) from exc

    # ----------------------------------------------------------------- embed
    async def embed(self, texts: list[str], *, model: str | None = None) -> list[list[float]]:
        if not texts:
            return []
        embed_model = model or self._settings.ollama_embed_model
        try:
            response = await self._client.post(
                "/api/embed",
                json={"model": embed_model, "input": texts,
                      "keep_alive": self._settings.ollama_keep_alive},
            )
        except httpx.TimeoutException as exc:
            raise ProviderTimeoutError(
                "Ollama embedding request timed out",
                detail={"provider": self.name, "model": embed_model},
            ) from exc
        except httpx.HTTPError as exc:
            raise self._unavailable(exc) from exc

        if response.status_code == 404 and self._model_missing(response.text, embed_model):
            raise ProviderUnavailableError(
                f"Embedding model '{embed_model}' is not installed",
                hint=f"Run `ollama pull {embed_model}`",
                detail={"provider": self.name, "model": embed_model},
            )
        if response.status_code >= 400:
            raise ProviderUnavailableError(
                f"Ollama embedding failed with HTTP {response.status_code}",
                hint=_START_HINT,
                detail={"provider": self.name, "body": response.text[:500]},
            )

        embeddings = response.json().get("embeddings") or []
        if len(embeddings) != len(texts):
            raise ProviderUnavailableError(
                "Ollama returned a different number of embeddings than inputs",
                hint="This usually means the model is not an embedding model.",
                detail={"expected": len(texts), "received": len(embeddings),
                        "model": embed_model},
            )
        return embeddings

    # ---------------------------------------------------------------- health
    async def health(self) -> ProviderHealth:
        model = self._default_model
        try:
            response = await self._client.get("/api/tags", timeout=5.0)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            return ProviderHealth(
                name=self.name,
                available=False,
                model=model,
                reason=f"Ollama not reachable at {self._base_url}",
                detail={"hint": _START_HINT, "error": str(exc)},
            )

        installed = [m.get("name", "") for m in response.json().get("models", [])]
        # Ollama reports 'llama3.2:3b'; a user may configure bare 'llama3.2'.
        if not any(tag == model or tag.split(":")[0] == model.split(":")[0] for tag in installed):
            return ProviderHealth(
                name=self.name,
                available=False,
                model=model,
                reason=f"Model '{model}' is not installed",
                detail={"hint": f"Run `ollama pull {model}`", "installed": installed},
            )

        return ProviderHealth(
            name=self.name, available=True, model=model,
            detail={"installed": installed, "base_url": self._base_url},
        )

    async def aclose(self) -> None:
        await self._client.aclose()
