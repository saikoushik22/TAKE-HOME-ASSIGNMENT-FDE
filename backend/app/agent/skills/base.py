"""Skill contract.

A skill is a bounded capability with its own prompt material, its own output
shape, and its own validation. Skills never talk to the HTTP layer and never
talk to each other — the orchestrator composes them. That boundary is what
makes each one independently testable.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, AsyncIterator

from ...core.config import Settings
from ...llm.base import LLMProvider
from ...rag.retriever import RetrievedChunk, Retriever

SKILLS_DIR = Path(__file__).parent


@lru_cache(maxsize=8)
def load_skill_file(relative: str) -> str:
    """Read a SKILL.md from disk.

    Cached per process: skill files are read-only at runtime, and re-reading on
    every request would add filesystem I/O to the hot path for no benefit.
    Restarting picks up edits, which is the documented workflow.
    """
    path = SKILLS_DIR / relative
    if not path.exists():
        raise FileNotFoundError(f"Skill definition missing: {path}")
    return path.read_text(encoding="utf-8")


@dataclass(slots=True)
class SkillContext:
    """Everything a skill needs to do its job."""

    message: str
    history: list[dict[str, str]] = field(default_factory=list)
    retriever: Retriever | None = None
    provider: LLMProvider | None = None
    settings: Settings | None = None
    artifact_kind: str | None = None
    model: str | None = None


@dataclass(slots=True)
class SkillResult:
    """What a skill produces."""

    text: str
    citations: list[dict[str, Any]] = field(default_factory=list)
    retrieval_trace: dict[str, Any] = field(default_factory=dict)
    artifact: dict[str, Any] | None = None
    abstained: bool = False
    meta: dict[str, Any] = field(default_factory=dict)


class Skill(abc.ABC):
    """Base class for all skills."""

    name: str

    @abc.abstractmethod
    def run(self, ctx: SkillContext) -> AsyncIterator[dict[str, Any]]:
        """Execute, yielding SSE-shaped events.

        Event shapes:
          {"type": "status",    "stage": str, "detail": str}
          {"type": "token",     "text": str}
          {"type": "citations", "citations": [...]}
          {"type": "artifact",  "artifact": {...}}
          {"type": "result",    "result": SkillResult}
        """


def build_citations(chunks: list[RetrievedChunk]) -> list[dict[str, Any]]:
    return [chunk.to_citation(i) for i, chunk in enumerate(chunks, start=1)]


def build_trace(chunks: list[RetrievedChunk], **extra: Any) -> dict[str, Any]:
    return {
        "chunks": [chunk.to_trace() for chunk in chunks],
        "episodes": sorted({chunk.episode_id for chunk in chunks}),
        **extra,
    }
