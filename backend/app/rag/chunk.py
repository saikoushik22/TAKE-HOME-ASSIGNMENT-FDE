"""Speaker-aware chunking.

Transcripts are speech, not prose. Fixed-size character splitting severs an
answer mid-thought and strands the speaker attribution in a neighbouring chunk,
which directly corrupts citations — our headline metric. So chunk boundaries
follow turn boundaries:

* accumulate whole turns up to ``chunk_target_chars``
* never split a turn unless that single turn exceeds ``chunk_max_chars``
* carry ``chunk_overlap_turns`` trailing turns into the next chunk, so a
  question is never separated from the answer that follows it

See architecture.md section 4.1 step 3.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any, Iterable

from .parse import Episode, Turn

# Split on sentence-ending punctuation followed by whitespace and a capital or quote.
_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+(?=[\"'A-Z0-9])")

# Rough token estimate. Deliberately not a real tokenizer: this only bounds
# prompt size, and adding a tokenizer dependency for a heuristic is not worth
# the install cost for the team inheriting this.
_CHARS_PER_TOKEN = 4


def estimate_tokens(text: str) -> int:
    return max(1, len(text) // _CHARS_PER_TOKEN)


def format_timestamp(seconds: int) -> str:
    h, rem = divmod(max(0, seconds), 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


@dataclass(slots=True)
class Chunk:
    episode_id: str
    chunk_index: int
    text: str
    speakers: list[str]
    start_seconds: int
    end_seconds: int
    content_hash: str = ""
    embedding: list[float] | None = field(default=None)

    def __post_init__(self) -> None:
        if not self.content_hash:
            self.content_hash = hashlib.sha256(self.text.encode("utf-8")).hexdigest()

    @property
    def start_label(self) -> str:
        return format_timestamp(self.start_seconds)

    def to_row(self, embedding_literal: str | None) -> dict[str, Any]:
        return {
            "episode_id": self.episode_id,
            "chunk_index": self.chunk_index,
            "text": self.text,
            "speakers": self.speakers,
            "start_seconds": self.start_seconds,
            "end_seconds": self.end_seconds,
            "start_label": self.start_label,
            "token_estimate": estimate_tokens(self.text),
            "content_hash": self.content_hash,
            "embedding": embedding_literal,
        }


def _split_long_turn(turn: Turn, max_chars: int) -> list[Turn]:
    """Split a single oversized turn on sentence boundaries.

    Timestamps are interpolated proportionally across the pieces: the source
    gives one timestamp per turn, so for a 6-minute monologue an exact per-piece
    time is unavailable. A proportional estimate lands the citation link close
    enough to be useful, which beats pointing every piece at the turn's start.
    """
    sentences = _SENTENCE_RE.split(turn.text)
    pieces: list[str] = []
    buffer = ""

    for sentence in sentences:
        candidate = f"{buffer} {sentence}".strip() if buffer else sentence
        if len(candidate) > max_chars and buffer:
            pieces.append(buffer)
            buffer = sentence
        else:
            buffer = candidate
    if buffer:
        pieces.append(buffer)

    # A single sentence longer than max_chars: hard-wrap as a last resort.
    if len(pieces) == 1 and len(pieces[0]) > max_chars:
        text = pieces[0]
        pieces = [text[i:i + max_chars] for i in range(0, len(text), max_chars)]

    total = sum(len(p) for p in pieces) or 1
    out: list[Turn] = []
    offset = 0
    # Without a duration we cannot interpolate; assume ~150 spoken words/min.
    approx_duration = max(1, int(len(turn.text) / 5 / 150 * 60))

    for piece in pieces:
        out.append(
            Turn(
                speaker=turn.speaker,
                start_seconds=turn.start_seconds + int(approx_duration * offset / total),
                text=piece,
            )
        )
        offset += len(piece)
    return out


def _normalise(turns: Iterable[Turn], max_chars: int) -> list[Turn]:
    out: list[Turn] = []
    for turn in turns:
        if turn.char_len > max_chars:
            out.extend(_split_long_turn(turn, max_chars))
        else:
            out.append(turn)
    return out


def _render(turns: list[Turn]) -> str:
    """Render turns with speaker labels retained.

    The speaker label stays inside the chunk text on purpose: it is what lets a
    model attribute a claim to a person, and what makes a retrieved chunk
    readable on its own in the citation panel.
    """
    return "\n\n".join(f"{t.speaker} ({format_timestamp(t.start_seconds)}): {t.text}"
                       for t in turns)


def chunk_episode(
    episode: Episode,
    *,
    target_chars: int = 1400,
    max_chars: int = 2400,
    overlap_turns: int = 1,
) -> list[Chunk]:
    """Chunk one episode's turns."""
    turns = _normalise(episode.turns, max_chars)
    if not turns:
        return []

    chunks: list[Chunk] = []
    current: list[Turn] = []
    current_len = 0

    def flush() -> None:
        nonlocal current, current_len
        if not current:
            return
        text = _render(current)
        chunks.append(
            Chunk(
                episode_id=episode.id,
                chunk_index=len(chunks),
                text=text,
                # dict.fromkeys preserves speaking order while de-duplicating.
                speakers=list(dict.fromkeys(t.speaker for t in current)),
                start_seconds=current[0].start_seconds,
                end_seconds=current[-1].start_seconds,
            )
        )
        carry = current[-overlap_turns:] if overlap_turns > 0 else []
        current = list(carry)
        current_len = sum(t.char_len for t in current)

    for turn in turns:
        if current and current_len + turn.char_len > target_chars:
            flush()
        current.append(turn)
        current_len += turn.char_len

    # Flush the tail, but drop a final chunk that is nothing but carried-over
    # overlap — it would duplicate the previous chunk and pollute retrieval.
    if current and (len(current) > overlap_turns or not chunks):
        current_len = 0
        text = _render(current)
        chunks.append(
            Chunk(
                episode_id=episode.id,
                chunk_index=len(chunks),
                text=text,
                speakers=list(dict.fromkeys(t.speaker for t in current)),
                start_seconds=current[0].start_seconds,
                end_seconds=current[-1].start_seconds,
            )
        )

    return chunks


def embedding_text(episode: Episode, chunk: Chunk) -> str:
    """Text actually embedded for a chunk.

    Prefixed with episode title and guest so the vector carries episode context
    even when the chunk alone is ambiguous — "we tried that and it worked" is
    meaningless without knowing who is speaking and about what.
    """
    header = f"Episode: {episode.title}"
    if episode.guest:
        header += f" | Guest: {episode.guest}"
    if episode.keywords:
        header += f" | Topics: {', '.join(episode.keywords[:8])}"
    return f"{header}\n\n{chunk.text}"


def to_vector_literal(values: list[float] | None) -> str | None:
    """Render a Python list as a pgvector literal, e.g. '[0.1,0.2]'."""
    if values is None:
        return None
    return "[" + ",".join(f"{v:.6f}" for v in values) + "]"
