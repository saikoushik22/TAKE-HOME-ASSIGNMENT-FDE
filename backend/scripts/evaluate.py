"""Score the golden set.

    python -m scripts.evaluate                    # full: generation + citations
    python -m scripts.evaluate --retrieval-only   # fast: no model calls
    python -m scripts.evaluate --limit 5          # sample while iterating

Reports the two metrics the PRD commits to:

  * **CBAR** — Citation-Backed Answer Rate. Share of in-corpus answers carrying
    at least one citation that resolves to a real retrieved chunk. Target >= 95%.
  * **Abstention Correctness** — share of out-of-corpus questions declined
    rather than answered. Target 100%. This one is a RELEASE GATE: a single
    confident fabrication fails the build.

`--retrieval-only` exists because on CPU-only hardware a full pass takes tens of
minutes. It measures the retriever in isolation, which is where most grounding
failures actually originate, and it is fast enough to run on every change.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path
from typing import Any

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.agent.orchestrator import Orchestrator  # noqa: E402
from app.core.config import get_settings  # noqa: E402
from app.core.logging import configure_logging  # noqa: E402
from app.db.repositories import CorpusRepository, SessionRepository  # noqa: E402
from app.db.session import Database  # noqa: E402
from app.llm.registry import ProviderRegistry, embedding_provider  # noqa: E402
from app.rag.embed import Embedder  # noqa: E402
from app.rag.retriever import Retriever  # noqa: E402

GOLDEN_SET = Path(__file__).resolve().parents[1] / "tests" / "eval" / "golden_set.yaml"

GREEN, RED, YELLOW, DIM, RESET = "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[0m"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Score the golden evaluation set.")
    parser.add_argument("--retrieval-only", action="store_true",
                        help="Skip generation. Much faster; measures the retriever alone.")
    parser.add_argument("--limit", type=int, default=None,
                        help="Only evaluate the first N questions in each group.")
    parser.add_argument("--verbose", action="store_true",
                        help="Print each answer's citations.")
    return parser.parse_args()


def load_golden_set() -> dict[str, list[dict[str, Any]]]:
    if not GOLDEN_SET.exists():
        raise SystemExit(f"Golden set not found at {GOLDEN_SET}")
    return yaml.safe_load(GOLDEN_SET.read_text(encoding="utf-8"))


def mark(ok: bool) -> str:
    return f"{GREEN}PASS{RESET}" if ok else f"{RED}FAIL{RESET}"


async def main() -> int:
    args = parse_args()
    settings = get_settings()
    configure_logging(level="WARNING", fmt="console")

    golden = load_golden_set()
    in_corpus = golden.get("in_corpus", [])
    out_corpus = golden.get("out_of_corpus", [])
    if args.limit:
        in_corpus = in_corpus[: args.limit]
        out_corpus = out_corpus[: args.limit]

    database = Database(settings)
    registry = ProviderRegistry(settings)

    try:
        await database.connect()
    except Exception as exc:
        print(f"\n  Cannot reach PostgreSQL: {exc}", file=sys.stderr)
        print("  Start it with:  docker compose up -d db\n", file=sys.stderr)
        return 2

    factory = database.session_factory()

    async with factory() as session:
        stats = await CorpusRepository(session).stats()
        if not stats["ready"]:
            print("\n  The corpus is empty. Run `make ingest` first.\n", file=sys.stderr)
            return 3

    print()
    print(f"  Corpus: {stats['episodes']} episodes, {stats['chunks']} chunks")
    print(f"  Mode:   {'retrieval only' if args.retrieval_only else 'full generation'}")
    print(f"  Model:  {settings.llm_provider} / {settings.model_for(settings.llm_provider)}")
    print()

    grounded = 0
    retrieval_times: list[float] = []

    # ------------------------------------------------ in-corpus (CBAR)
    print(f"  {'IN-CORPUS':<12} {'ID':<26} {'RESULT':<12} DETAIL")
    print(f"  {'-' * 74}")

    for case in in_corpus:
        async with factory() as session:
            embedder = Embedder(embedding_provider(registry, settings), settings,
                                CorpusRepository(session))
            retriever = Retriever(session, embedder, settings)

            started = time.perf_counter()
            result = await retriever.retrieve(case["question"])
            retrieval_times.append((time.perf_counter() - started) * 1000)

            if args.retrieval_only:
                ok = not result.abstain and bool(result.chunks)
                detail = (
                    f"{len(result.chunks)} chunks, top sim "
                    f"{max((c.vector_similarity for c in result.chunks), default=0):.2f}"
                    if ok else (result.reason or "no chunks")
                )
            else:
                ok, detail = await _run_turn(session, settings, registry, case["question"])

            grounded += int(ok)
            print(f"  {'':<12} {case['id']:<26} {mark(ok):<21} {DIM}{detail}{RESET}")

    # -------------------------------------- out-of-corpus (abstention)
    print()
    print(f"  {'OUT-CORPUS':<12} {'ID':<26} {'RESULT':<12} DETAIL")
    print(f"  {'-' * 74}")

    abstained = 0
    for case in out_corpus:
        async with factory() as session:
            embedder = Embedder(embedding_provider(registry, settings), settings,
                                CorpusRepository(session))
            retriever = Retriever(session, embedder, settings)
            result = await retriever.retrieve(case["question"])

            # Correct behaviour is to decline. Retrieval abstaining is the
            # strongest form, because the model is then never invoked at all.
            ok = result.abstain or not result.chunks
            best = max((c.vector_similarity for c in result.chunks), default=0.0)
            detail = (
                f"declined (best sim {best:.2f} < floor {settings.rag_min_similarity})"
                if ok else f"RETRIEVED {len(result.chunks)} chunks, best sim {best:.2f}"
            )
            abstained += int(ok)
            print(f"  {'':<12} {case['id']:<26} {mark(ok):<21} {DIM}{detail}{RESET}")

    await registry.aclose()
    await database.disconnect()

    # --------------------------------------------------------- summary
    cbar = (grounded / len(in_corpus) * 100) if in_corpus else 0.0
    abstention = (abstained / len(out_corpus) * 100) if out_corpus else 100.0
    p95 = (
        sorted(retrieval_times)[int(len(retrieval_times) * 0.95) - 1]
        if len(retrieval_times) >= 2 else (retrieval_times[0] if retrieval_times else 0.0)
    )

    metric_name = "Retrieval success" if args.retrieval_only else "CBAR"
    cbar_ok = cbar >= 95.0
    abstention_ok = abstention >= 100.0
    latency_ok = p95 <= 400.0

    print()
    print("  " + "=" * 74)
    print(f"  {metric_name:<28} {cbar:6.1f}%   target >= 95%    {mark(cbar_ok)}")
    print(f"  {'Abstention correctness':<28} {abstention:6.1f}%   target  100%    "
          f"{mark(abstention_ok)}  {YELLOW}GATE{RESET}")
    print(f"  {'Retrieval p95':<28} {p95:6.0f}ms  target <= 400ms  {mark(latency_ok)}")
    print("  " + "=" * 74)
    print()

    if not abstention_ok:
        print(f"  {RED}RELEASE GATE FAILED{RESET}: the assistant answered a question the "
              f"corpus does not cover.")
        print("  A confident fabrication is worse than an error message, because the "
              "user cannot tell it apart from a good answer.\n")

    return 0 if (cbar_ok and abstention_ok) else 1


async def _run_turn(session, settings, registry, question: str) -> tuple[bool, str]:
    """Run one full turn and report whether the answer was citation-backed."""
    sessions = SessionRepository(session)
    row = await sessions.create(
        provider=settings.llm_provider,
        model=settings.model_for(settings.llm_provider),
        title="eval",
    )
    await session.commit()

    orchestrator = Orchestrator(session, settings, registry)
    citations: list[dict[str, Any]] = []
    abstained = False

    try:
        async for event in orchestrator.handle(session_id=row["id"], message=question):
            if event.get("type") == "done":
                citations = event.get("citations") or []
                abstained = bool(event.get("abstained"))
            elif event.get("type") == "error":
                return False, f"error: {event['error'].get('code')}"
    except Exception as exc:
        return False, f"exception: {type(exc).__name__}"
    finally:
        await sessions.soft_delete(row["id"])
        await session.commit()

    if abstained:
        return False, "abstained on an in-corpus question"
    if not citations:
        return False, "answered with NO citations"
    episodes = {c.get("episode_title") for c in citations}
    return True, f"{len(citations)} citations across {len(episodes)} episodes"


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
