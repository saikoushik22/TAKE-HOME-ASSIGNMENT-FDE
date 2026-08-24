"""Embedding with a content-hash cache.

Re-ingesting an unchanged corpus performs zero embedding calls. On CPU-only
inference that is the difference between a 20-minute re-index and a 2-second
no-op, which is what makes "refresh the corpus" a routine operation instead of
something the team avoids.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from ..core.config import Settings
from ..core.errors import ProviderUnavailableError
from ..core.logging import get_logger
from ..db.repositories import CorpusRepository
from ..llm.base import LLMProvider

log = get_logger(__name__)


def text_hash(text: str, model: str) -> str:
    """Hash of (text, model).

    The model is part of the key because vectors from different models are not
    interchangeable — reusing a cached nomic vector for a different model would
    silently corrupt the index.
    """
    return hashlib.sha256(f"{model}\x00{text}".encode("utf-8")).hexdigest()


@dataclass(slots=True)
class EmbeddingResult:
    vectors: list[list[float] | None]
    computed: int
    cached: int


class Embedder:
    def __init__(
        self,
        provider: LLMProvider,
        settings: Settings,
        repo: CorpusRepository | None = None,
    ) -> None:
        self._provider = provider
        self._settings = settings
        self._repo = repo
        self._model = settings.embedding_model

    async def embed_texts(self, texts: list[str]) -> EmbeddingResult:
        """Embed texts, consulting the cache first."""
        if not texts:
            return EmbeddingResult(vectors=[], computed=0, cached=0)

        hashes = [text_hash(t, self._model) for t in texts]
        vectors: list[list[float] | None] = [None] * len(texts)
        cached_count = 0

        if self._repo is not None:
            found = await self._repo.cached_embeddings(hashes, self._model)
            for index, h in enumerate(hashes):
                literal = found.get(h)
                if literal:
                    vectors[index] = _parse_vector_literal(literal)
                    cached_count += 1

        pending = [i for i, v in enumerate(vectors) if v is None]
        if not pending:
            return EmbeddingResult(vectors=vectors, computed=0, cached=cached_count)

        batch_size = max(1, self._settings.embedding_batch_size)
        computed = 0
        to_cache: list[dict[str, object]] = []

        for start in range(0, len(pending), batch_size):
            group = pending[start:start + batch_size]
            batch = [texts[i] for i in group]
            embeddings = await self._provider.embed(batch, model=self._model)

            for position, index in enumerate(group):
                vector = embeddings[position]
                if len(vector) != self._settings.embedding_dim:
                    raise ProviderUnavailableError(
                        "Embedding dimension does not match configuration",
                        hint=(
                            f"Model '{self._model}' returned {len(vector)} dimensions but "
                            f"EMBEDDING_DIM is {self._settings.embedding_dim}. Update "
                            "EMBEDDING_DIM and re-run `make ingest-full`."
                        ),
                        detail={"expected": self._settings.embedding_dim,
                                "received": len(vector), "model": self._model},
                    )
                vectors[index] = vector
                computed += 1
                to_cache.append(
                    {
                        "content_hash": hashes[index],
                        "model": self._model,
                        "embedding": "[" + ",".join(f"{v:.6f}" for v in vector) + "]",
                    }
                )

        if self._repo is not None and to_cache:
            await self._repo.cache_embeddings(to_cache)

        log.info(
            "rag.embed",
            extra={"total": len(texts), "computed": computed,
                   "cached": cached_count, "model": self._model},
        )
        return EmbeddingResult(vectors=vectors, computed=computed, cached=cached_count)

    async def embed_query(self, query: str) -> list[float]:
        """Embed a single search query. Never cached — queries rarely repeat."""
        embeddings = await self._provider.embed([query], model=self._model)
        if not embeddings:
            raise ProviderUnavailableError(
                "The embedding provider returned no vector for the query",
                detail={"model": self._model},
            )
        return embeddings[0]


def _parse_vector_literal(literal: str) -> list[float]:
    """Parse a pgvector text literal '[0.1,0.2]' back into floats."""
    stripped = literal.strip().lstrip("[").rstrip("]")
    if not stripped:
        return []
    return [float(part) for part in stripped.split(",")]
