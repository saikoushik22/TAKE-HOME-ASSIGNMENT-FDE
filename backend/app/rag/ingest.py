"""Ingestion pipeline: fetch -> parse -> chunk -> embed -> index.

Incremental by default. Each episode's content hash is compared against the
stored one; unchanged episodes are skipped entirely, so a routine refresh costs
seconds rather than a full re-embed.

Failure isolation is deliberate: one malformed transcript must never abort a
303-episode run. Per-episode failures are logged with a reason and counted in
the report, and the remaining episodes still ingest.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from ..core.config import Settings
from ..core.logging import get_logger
from ..db.repositories import CorpusRepository
from ..llm.base import LLMProvider
from .chunk import chunk_episode, embedding_text, to_vector_literal
from .embed import Embedder
from .fetch import fetch_corpus
from .parse import TranscriptParseError, discover_transcripts, parse_transcript

log = get_logger(__name__)


@dataclass(slots=True)
class IngestReport:
    episodes_seen: int = 0
    episodes_ingested: int = 0
    episodes_skipped: int = 0
    episodes_failed: int = 0
    chunks_written: int = 0
    embeddings_computed: int = 0
    embeddings_cached: int = 0
    failures: list[dict[str, str]] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return {
            "episodes_seen": self.episodes_seen,
            "episodes_ingested": self.episodes_ingested,
            "episodes_skipped": self.episodes_skipped,
            "episodes_failed": self.episodes_failed,
            "chunks_written": self.chunks_written,
            "embeddings_computed": self.embeddings_computed,
            "embeddings_cached": self.embeddings_cached,
            "failures": self.failures[:20],
        }


class Ingestor:
    def __init__(
        self,
        repo: CorpusRepository,
        provider: LLMProvider,
        settings: Settings,
    ) -> None:
        self._repo = repo
        self._settings = settings
        self._embedder = Embedder(provider, settings, repo)

    async def run(
        self,
        *,
        force: bool = False,
        limit: int | None = None,
        corpus_path: Path | None = None,
    ) -> IngestReport:
        report = IngestReport()
        root = corpus_path or self._resolve_corpus()
        files = discover_transcripts(root)

        if not files:
            log.warning("ingest.no_files", extra={"path": str(root)})
            return report

        max_episodes = limit if limit is not None else self._settings.ingest_max_episodes
        if max_episodes:
            files = files[:max_episodes]

        known_hashes = await self._repo.episode_hashes()
        report.episodes_seen = len(files)

        log.info(
            "ingest.start",
            extra={"files": len(files), "force": force, "path": str(root)},
        )

        for path in files:
            try:
                await self._ingest_one(path, known_hashes, force, report)
                # Commit per episode, not once at the end. Two reasons, both
                # learned the hard way:
                #   1. A single transaction spanning the whole run holds
                #      relation locks on `episodes`/`chunks` for hours, which
                #      blocks the `CREATE ... IF NOT EXISTS` that every backend
                #      startup runs — one ingest wedges the whole service.
                #   2. Work becomes durable as it completes, so a crash at
                #      episode 200 does not discard the first 199.
                await self._repo.commit()
            except Exception as exc:
                # Isolation: never let one bad file end the run.
                await self._repo.rollback()
                report.episodes_failed += 1
                report.failures.append({"path": str(path), "error": str(exc)})
                log.warning(
                    "ingest.episode.failed",
                    extra={"path": str(path), "error": str(exc),
                           "error_type": type(exc).__name__},
                )

        log.info("ingest.complete", extra=report.to_dict())
        return report

    # ------------------------------------------------------------- internals
    def _resolve_corpus(self) -> Path:
        if self._settings.transcript_local_path:
            return Path(self._settings.transcript_local_path)
        return fetch_corpus(
            repo=self._settings.transcript_repo,
            ref=self._settings.transcript_ref,
            destination=self._settings.data_path,
        )

    async def _ingest_one(
        self,
        path: Path,
        known_hashes: dict[str, str],
        force: bool,
        report: IngestReport,
    ) -> None:
        try:
            episode = parse_transcript(path)
        except TranscriptParseError as exc:
            report.episodes_failed += 1
            report.failures.append({"path": str(path), "error": str(exc)})
            log.warning("ingest.parse.failed", extra={"path": str(path),
                                                      "error": str(exc)})
            return

        if not force and known_hashes.get(episode.id) == episode.content_hash:
            report.episodes_skipped += 1
            return

        chunks = chunk_episode(
            episode,
            target_chars=self._settings.chunk_target_chars,
            max_chars=self._settings.chunk_max_chars,
            overlap_turns=self._settings.chunk_overlap_turns,
        )
        if not chunks:
            report.episodes_failed += 1
            report.failures.append({"path": str(path), "error": "produced no chunks"})
            return

        texts = [embedding_text(episode, c) for c in chunks]
        embedded = await self._embedder.embed_texts(texts)
        report.embeddings_computed += embedded.computed
        report.embeddings_cached += embedded.cached

        rows = [
            chunk.to_row(to_vector_literal(vector))
            for chunk, vector in zip(chunks, embedded.vectors)
        ]

        # Per-episode transaction: if the run dies midway, episodes already
        # committed stay queryable and only the incomplete one is retried.
        await self._repo.upsert_episode(episode.to_row())
        await self._repo.delete_chunks(episode.id)
        await self._repo.insert_chunks(rows)

        report.episodes_ingested += 1
        report.chunks_written += len(rows)
        log.info(
            "ingest.episode",
            extra={"episode_id": episode.id, "chunks": len(rows),
                   "computed": embedded.computed, "cached": embedded.cached},
        )
