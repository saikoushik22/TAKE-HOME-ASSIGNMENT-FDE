"""Corpus ingestion CLI.

    python -m scripts.ingest              # incremental: only changed episodes
    python -m scripts.ingest --limit 25   # fast smoke test
    python -m scripts.ingest --force      # re-embed everything
    python -m scripts.ingest --stats      # report and exit

Incremental by default because a full CPU-only embed of the corpus takes a long
time, while a no-op refresh takes seconds. Content hashing decides what changed,
so re-running is cheap and idempotent.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

# Allow `python scripts/ingest.py` as well as `python -m scripts.ingest`.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import get_settings  # noqa: E402
from app.core.logging import configure_logging, get_logger  # noqa: E402
from app.db.repositories import CorpusRepository  # noqa: E402
from app.db.session import Database  # noqa: E402
from app.llm.registry import ProviderRegistry, embedding_provider  # noqa: E402
from app.rag.ingest import Ingestor  # noqa: E402

log = get_logger("ingest")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Ingest Lenny's Podcast transcripts into the knowledge base."
    )
    parser.add_argument("--force", action="store_true",
                        help="Re-ingest and re-embed every episode, ignoring content hashes.")
    parser.add_argument("--limit", type=int, default=None,
                        help="Only ingest the first N episodes. Useful for a smoke test.")
    parser.add_argument("--path", type=str, default=None,
                        help="Use an already-downloaded corpus directory instead of fetching.")
    parser.add_argument("--stats", action="store_true",
                        help="Print corpus statistics and exit without ingesting.")
    return parser.parse_args()


async def main() -> int:
    args = parse_args()
    settings = get_settings()
    configure_logging(level=settings.log_level, fmt="console")

    database = Database(settings)
    registry = ProviderRegistry(settings)

    try:
        await database.connect()
        await database.apply_schema()
    except Exception as exc:
        print(f"\n  Cannot reach PostgreSQL: {exc}", file=sys.stderr)
        print("  Start it with:  docker compose up -d db\n", file=sys.stderr)
        return 2

    try:
        factory = database.session_factory()

        if args.stats:
            async with factory() as session:
                stats = await CorpusRepository(session).stats()
            print("\n  Corpus statistics")
            print("  -----------------")
            for key, value in stats.items():
                print(f"  {key:<20} {value}")
            print()
            return 0

        # Verify the embedding model is actually reachable before spending time
        # walking the corpus. Failing here costs seconds; failing after parsing
        # every transcript costs minutes and is far more annoying.
        provider = embedding_provider(registry, settings)
        health = await provider.health()
        if not health.available:
            print(f"\n  Embedding provider '{provider.name}' is unavailable: "
                  f"{health.reason}", file=sys.stderr)
            print(f"  Pull the model with:  ollama pull {settings.embedding_model}\n",
                  file=sys.stderr)
            return 3

        async with factory() as session:
            repo = CorpusRepository(session)
            ingestor = Ingestor(repo, provider, settings)
            report = await ingestor.run(
                force=args.force,
                limit=args.limit if args.limit is not None else settings.ingest_max_episodes,
                corpus_path=Path(args.path) if args.path else None,
            )
            await session.commit()

        print("\n  Ingestion complete")
        print("  ------------------")
        for key, value in report.to_dict().items():
            if key == "failures":
                continue
            print(f"  {key:<22} {value}")
        if report.failures:
            print(f"\n  {len(report.failures)} episode(s) failed:")
            for failure in report.failures[:10]:
                print(f"    - {failure.get('episode', '?')}: {failure.get('error', '')}")
        print()
        return 0 if report.episodes_failed == 0 else 1

    finally:
        await registry.aclose()
        await database.disconnect()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
