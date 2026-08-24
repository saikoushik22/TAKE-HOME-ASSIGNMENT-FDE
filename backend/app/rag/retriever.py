"""Hybrid retrieval.

Vector search captures paraphrase ("how do I keep users coming back" finds
retention discussion that never uses those words). Lexical search captures the
exact terms embeddings blur — product names, people, acronyms like PMF or NDR.
A dense model happily rates *retention* and *activation* as near-neighbours; a
growth PM does not. So both run, and the ranked lists are fused with
Reciprocal Rank Fusion.

RRF is used rather than weighted score blending because it needs no score
normalisation between two incomparable scales (cosine distance vs ts_rank_cd)
and no per-corpus tuning constant that would quietly rot as the corpus grows.

See architecture.md section 5.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.config import Settings
from ..core.logging import get_logger, timed
from .embed import Embedder

log = get_logger(__name__)


@dataclass(slots=True)
class RetrievedChunk:
    chunk_id: str
    episode_id: str
    episode_title: str
    guest: str | None
    youtube_url: str | None
    text: str
    speakers: list[str]
    start_seconds: int
    start_label: str
    vector_similarity: float = 0.0
    lexical_rank: float = 0.0
    fused_score: float = 0.0
    sources: list[str] = field(default_factory=list)

    @property
    def citation_url(self) -> str | None:
        """Deep-link to the exact second in the episode.

        This is what makes grounding verifiable rather than rhetorical: the
        reader goes from doubt to confirmation in one click.
        """
        if not self.youtube_url:
            return None
        separator = "&" if "?" in self.youtube_url else "?"
        return f"{self.youtube_url}{separator}t={self.start_seconds}s"

    def to_citation(self, index: int) -> dict[str, Any]:
        return {
            "index": index,
            "chunk_id": self.chunk_id,
            "episode_id": self.episode_id,
            "episode_title": self.episode_title,
            "guest": self.guest,
            "speakers": self.speakers,
            "timestamp": self.start_label,
            "start_seconds": self.start_seconds,
            "url": self.citation_url,
            "snippet": _snippet(self.text),
            "score": round(self.fused_score, 5),
        }

    def to_trace(self) -> dict[str, Any]:
        return {
            "chunk_id": self.chunk_id,
            "episode_id": self.episode_id,
            "vector_similarity": round(self.vector_similarity, 5),
            "lexical_rank": round(self.lexical_rank, 5),
            "fused_score": round(self.fused_score, 5),
            "matched_by": self.sources,
        }


@dataclass(slots=True)
class RetrievalResult:
    query: str
    chunks: list[RetrievedChunk]
    abstain: bool
    reason: str | None = None
    candidates_considered: int = 0

    @property
    def is_empty(self) -> bool:
        return not self.chunks


def _snippet(body: str, limit: int = 320) -> str:
    collapsed = " ".join(body.split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[:limit].rsplit(" ", 1)[0] + "…"


class Retriever:
    def __init__(self, db: AsyncSession, embedder: Embedder, settings: Settings) -> None:
        self._db = db
        self._embedder = embedder
        self._settings = settings

    # ------------------------------------------------------------- searches
    async def _vector_search(self, vector: list[float], limit: int) -> list[dict[str, Any]]:
        literal = "[" + ",".join(f"{v:.6f}" for v in vector) + "]"
        result = await self._db.execute(
            text(
                """
                SELECT c.id::text AS chunk_id, c.episode_id, c.text, c.speakers,
                       c.start_seconds, c.start_label,
                       e.title AS episode_title, e.guest, e.youtube_url,
                       1 - (c.embedding <=> CAST(:vec AS vector)) AS similarity
                FROM chunks c
                JOIN episodes e ON e.id = c.episode_id
                WHERE c.embedding IS NOT NULL
                ORDER BY c.embedding <=> CAST(:vec AS vector)
                LIMIT :limit
                """
            ),
            {"vec": literal, "limit": limit},
        )
        return [dict(r._mapping) for r in result.all()]

    async def _lexical_search(self, query: str, limit: int) -> list[dict[str, Any]]:
        # websearch_to_tsquery tolerates natural phrasing and quoted phrases
        # without throwing on punctuation the way to_tsquery does.
        result = await self._db.execute(
            text(
                """
                SELECT c.id::text AS chunk_id, c.episode_id, c.text, c.speakers,
                       c.start_seconds, c.start_label,
                       e.title AS episode_title, e.guest, e.youtube_url,
                       ts_rank_cd(c.tsv, websearch_to_tsquery('english', :q)) AS rank
                FROM chunks c
                JOIN episodes e ON e.id = c.episode_id
                WHERE c.tsv @@ websearch_to_tsquery('english', :q)
                ORDER BY rank DESC
                LIMIT :limit
                """
            ),
            {"q": query, "limit": limit},
        )
        return [dict(r._mapping) for r in result.all()]

    # ---------------------------------------------------------------- fuse
    def _fuse(
        self,
        vector_hits: list[dict[str, Any]],
        lexical_hits: list[dict[str, Any]],
    ) -> list[RetrievedChunk]:
        k = self._settings.rag_rrf_k
        merged: dict[str, RetrievedChunk] = {}

        def upsert(row: dict[str, Any]) -> RetrievedChunk:
            existing = merged.get(row["chunk_id"])
            if existing is None:
                existing = RetrievedChunk(
                    chunk_id=row["chunk_id"],
                    episode_id=row["episode_id"],
                    episode_title=row["episode_title"],
                    guest=row.get("guest"),
                    youtube_url=row.get("youtube_url"),
                    text=row["text"],
                    speakers=list(row.get("speakers") or []),
                    start_seconds=int(row.get("start_seconds") or 0),
                    start_label=row.get("start_label") or "00:00:00",
                )
                merged[row["chunk_id"]] = existing
            return existing

        for rank, row in enumerate(vector_hits, start=1):
            chunk = upsert(row)
            chunk.vector_similarity = float(row.get("similarity") or 0.0)
            chunk.fused_score += 1.0 / (k + rank)
            chunk.sources.append("vector")

        for rank, row in enumerate(lexical_hits, start=1):
            chunk = upsert(row)
            chunk.lexical_rank = float(row.get("rank") or 0.0)
            chunk.fused_score += 1.0 / (k + rank)
            chunk.sources.append("lexical")

        return sorted(merged.values(), key=lambda c: c.fused_score, reverse=True)

    def _diversify(self, chunks: list[RetrievedChunk]) -> list[RetrievedChunk]:
        """Cap chunks per episode.

        Without this a single verbose episode monopolises the context window.
        An answer citing four different operators is more useful — and more
        defensible — than one citing the same episode eight times.
        """
        cap = self._settings.rag_max_per_episode
        seen: dict[str, int] = {}
        kept: list[RetrievedChunk] = []
        overflow: list[RetrievedChunk] = []

        for chunk in chunks:
            count = seen.get(chunk.episode_id, 0)
            if count < cap:
                seen[chunk.episode_id] = count + 1
                kept.append(chunk)
            else:
                overflow.append(chunk)

        # Backfill from overflow only if diversity left us short of top_k.
        limit = self._settings.rag_top_k
        if len(kept) < limit:
            kept.extend(overflow[: limit - len(kept)])
        return kept[:limit]

    # -------------------------------------------------------------- retrieve
    async def retrieve(self, query: str, *, top_k: int | None = None) -> RetrievalResult:
        query = (query or "").strip()
        if not query:
            return RetrievalResult(query=query, chunks=[], abstain=True,
                                   reason="empty query")

        limit = top_k or self._settings.rag_top_k
        candidates = max(self._settings.rag_candidates, limit)

        with timed(log, "rag.query", query_chars=len(query)) as span:
            vector = await self._embedder.embed_query(query)
            vector_hits = await self._vector_search(vector, candidates)
            lexical_hits = await self._lexical_search(query, candidates)

            fused = self._fuse(vector_hits, lexical_hits)
            span["vector_hits"] = len(vector_hits)
            span["lexical_hits"] = len(lexical_hits)
            span["fused"] = len(fused)

        considered = len(fused)

        if not fused:
            log.info("rag.empty", extra={"query_chars": len(query)})
            return RetrievalResult(
                query=query, chunks=[], abstain=True,
                reason="no matching transcript passages",
                candidates_considered=0,
            )

        # The relevance floor. This is R1's most important mitigation: when the
        # corpus does not cover a question, we return here and the model is
        # never invoked, so it cannot fabricate. Prompt instructions are
        # advisory; control flow is not. See architecture.md section 5.2.
        best_similarity = max(c.vector_similarity for c in fused)
        if best_similarity < self._settings.rag_min_similarity:
            log.info(
                "rag.abstain",
                extra={"best_similarity": round(best_similarity, 4),
                       "floor": self._settings.rag_min_similarity,
                       "candidates": considered},
            )
            return RetrievalResult(
                query=query, chunks=[], abstain=True,
                reason=(
                    f"best match scored {best_similarity:.2f}, below the "
                    f"{self._settings.rag_min_similarity:.2f} relevance floor"
                ),
                candidates_considered=considered,
            )

        selected = self._diversify(fused)
        log.info(
            "rag.hits",
            extra={"returned": len(selected),
                   "episodes": len({c.episode_id for c in selected}),
                   "top_score": round(selected[0].fused_score, 5),
                   "best_similarity": round(best_similarity, 4)},
        )
        return RetrievalResult(
            query=query, chunks=selected, abstain=False,
            candidates_considered=considered,
        )


def _excerpt(body: str, limit: int) -> str:
    """Trim a chunk for the prompt, cutting on a sentence boundary when possible.

    Cutting mid-sentence gives the model a fragment that reads as though the
    speaker was interrupted, which encourages it to invent the completion. A
    clean sentence break avoids that for the cost of a few characters.
    """
    if limit <= 0 or len(body) <= limit:
        return body

    window = body[:limit]
    # Prefer the last sentence end in the final third, so we neither cut mid
    # thought nor throw away most of the excerpt chasing a boundary.
    boundary = max(window.rfind(". "), window.rfind("? "), window.rfind("! "))
    if boundary > limit * 0.6:
        return window[: boundary + 1] + " […]"
    return window.rsplit(" ", 1)[0] + " […]"


def format_context(chunks: list[RetrievedChunk], *, max_chars_per_chunk: int = 0) -> str:
    """Render retrieved chunks as fenced, numbered evidence.

    Explicit delimiters and an untrusted-data label are the cheap mitigation for
    prompt injection carried inside transcript text (PRD R7): the model is told
    this region is evidence to cite, never instructions to follow.

    ``max_chars_per_chunk`` bounds what each source contributes to the prompt.
    Prefill dominates time-to-first-token on CPU — measured ~65 tokens/second —
    so context length is paid for directly in seconds before the user sees a
    word. Trimming per chunk keeps the number of distinct sources (and so the
    breadth of the citations) while cutting that cost.
    """
    blocks: list[str] = []
    for index, chunk in enumerate(chunks, start=1):
        header = f"[S{index}] {chunk.episode_title}"
        if chunk.guest:
            header += f" — guest: {chunk.guest}"
        header += f" (at {chunk.start_label})"
        body = _excerpt(chunk.text, max_chars_per_chunk)
        blocks.append(f"<<<SOURCE {index}\n{header}\n\n{body}\nSOURCE {index}>>>")
    return "\n\n".join(blocks)
