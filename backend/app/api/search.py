"""Raw retrieval endpoint.

Generation-free access to the retriever. This exists for two reasons that both
matter at handoff: the evaluation harness scores retrieval independently of
model quality, and an engineer debugging "why did it answer that?" can see the
exact ranked evidence without waiting on a slow local model.

Exposing retrieval as its own endpoint is also what makes the p95 retrieval
latency target in the PRD measurable in isolation.
"""

from __future__ import annotations

import time

from fastapi import APIRouter

from ..db.repositories import CorpusRepository
from ..llm.registry import embedding_provider
from ..rag.embed import Embedder
from ..rag.retriever import Retriever
from ..schemas import SearchHit, SearchRequest, SearchResponse
from .deps import DbDep, RegistryDep, SettingsDep

router = APIRouter(tags=["retrieval"])


@router.post("/search", response_model=SearchResponse, summary="Hybrid retrieval, no generation")
async def search(
    payload: SearchRequest,
    db: DbDep,
    registry: RegistryDep,
    settings: SettingsDep,
) -> SearchResponse:
    corpus = CorpusRepository(db)
    embedder = Embedder(embedding_provider(registry, settings), settings, corpus)
    retriever = Retriever(db, embedder, settings)

    started = time.perf_counter()
    result = await retriever.retrieve(payload.query, top_k=payload.top_k)
    took_ms = int((time.perf_counter() - started) * 1000)

    return SearchResponse(
        query=result.query,
        abstained=result.abstain,
        reason=result.reason,
        candidates_considered=result.candidates_considered,
        hits=[
            SearchHit(
                chunk_id=chunk.chunk_id,
                episode_id=chunk.episode_id,
                episode_title=chunk.episode_title,
                guest=chunk.guest,
                timestamp=chunk.start_label,
                url=chunk.citation_url,
                text=chunk.text,
                vector_similarity=round(chunk.vector_similarity, 5),
                lexical_rank=round(chunk.lexical_rank, 5),
                fused_score=round(chunk.fused_score, 5),
                matched_by=chunk.sources,
            )
            for chunk in result.chunks
        ],
        took_ms=took_ms,
    )


@router.get("/corpus/stats", summary="Knowledge base statistics")
async def corpus_stats(db: DbDep) -> dict[str, object]:
    return await CorpusRepository(db).stats()
