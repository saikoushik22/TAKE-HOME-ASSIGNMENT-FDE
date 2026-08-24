"""Citation validation.

Server-side enforcement that every `[S#]` marker in an answer points at a
source that was actually retrieved. A marker that resolves to nothing is worse
than no marker at all: it looks like evidence and is not. This is layer 3 of
the hallucination mitigation in PRD R1.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from ...core.logging import get_logger

log = get_logger(__name__)

# Matches [S1], [S2], and grouped forms: [S1, S3], [S1, 3], [S1][S2].
#
# The optional `S` on continuation numbers matters more than it looks. A model
# told to "cite with [S1] or [S2]" naturally writes [S1, S2] when two sources
# support one claim. Without that `S?` the group does not match at all, the
# marker is treated as unresolvable, and REAL grounding is stripped from the
# answer — silently lowering the citation rate the product is measured on.
MARKER_RE = re.compile(r"\[S\s*(\d+(?:\s*,\s*S?\s*\d+)*)\s*\]", re.IGNORECASE)


def _parse_index(raw: str) -> int | None:
    """Parse one marker number, tolerating an 'S' prefix and stray whitespace."""
    cleaned = raw.strip().lstrip("Ss").strip()
    return int(cleaned) if cleaned.isdigit() else None


@dataclass(slots=True)
class CitationAudit:
    text: str
    used_indexes: set[int] = field(default_factory=set)
    invalid_markers: list[str] = field(default_factory=list)
    citations: list[dict[str, Any]] = field(default_factory=list)

    @property
    def has_citations(self) -> bool:
        return bool(self.used_indexes)


def validate_and_prune(
    text: str, available: list[dict[str, Any]]
) -> CitationAudit:
    """Strip markers that do not resolve, and return only cited sources.

    Two things happen here, both deliberate:

    * **Invalid markers are removed** rather than left in place. A dangling
      "[S7]" when only five sources exist is a fabricated-looking receipt.
    * **Only cited sources are returned.** Showing all eight retrieved chunks
      when the answer used three overstates the grounding, and the source count
      is what the UI presents as the confidence signal.
    """
    valid_indexes = {c["index"] for c in available}
    audit = CitationAudit(text=text)

    def replace(match: re.Match[str]) -> str:
        raw_numbers = [n.strip() for n in match.group(1).split(",")]
        kept: list[str] = []
        for raw in raw_numbers:
            number = _parse_index(raw)
            if number is None:
                continue
            if number in valid_indexes:
                audit.used_indexes.add(number)
                kept.append(str(number))
            else:
                audit.invalid_markers.append(match.group(0))
        if not kept:
            return ""
        return "[S" + ", S".join(kept) + "]"

    cleaned = MARKER_RE.sub(replace, text)
    # Collapse whitespace left behind by a removed marker.
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    cleaned = re.sub(r"\s+([.,;:!?])", r"\1", cleaned)

    audit.text = cleaned.strip()
    audit.citations = [c for c in available if c["index"] in audit.used_indexes]

    if audit.invalid_markers:
        log.warning(
            "citation.invalid_markers",
            extra={"markers": audit.invalid_markers[:10],
                   "valid_count": len(valid_indexes)},
        )
    return audit


def renumber(text: str, citations: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
    """Renumber markers so they are contiguous from 1.

    After pruning, an answer may cite S2 and S5 only. Presenting those as
    "Sources 2 and 5" invites the reader to wonder where 1, 3 and 4 went.
    """
    ordered = sorted(citations, key=lambda c: c["index"])
    mapping = {c["index"]: new for new, c in enumerate(ordered, start=1)}

    def replace(match: re.Match[str]) -> str:
        numbers = [_parse_index(n) for n in match.group(1).split(",")]
        kept = [str(mapping[n]) for n in numbers if n is not None and n in mapping]
        return ("[S" + ", S".join(kept) + "]") if kept else ""

    renumbered_text = MARKER_RE.sub(replace, text)
    renumbered = [{**c, "index": mapping[c["index"]]} for c in ordered]
    return renumbered_text, renumbered
