"""Transcript parsing.

Upstream files are YAML frontmatter followed by a speaker-turn body:

    ---
    guest: <name>
    title: <episode title>
    youtube_url: https://www.youtube.com/watch?v=...
    publish_date: 2022-10-13
    duration_seconds: 3946.0
    keywords: [...]
    ---

    # <title>

    ## Transcript

    <Speaker> (00:00:00):
    <paragraph>

    <Speaker> (00:00:55):
    <paragraph>

Parsing is deliberately forgiving. A single malformed file must never abort a
303-episode ingest, so failures raise `TranscriptParseError`, which the ingest
loop logs and skips.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import yaml

# "Speaker Name (00:12:34):" — the speaker may contain spaces, dots, and hyphens.
# Anchored to a line start, and the timestamp is required, which keeps ordinary
# in-paragraph parentheticals from being misread as turn markers.
TURN_RE = re.compile(
    r"^(?P<speaker>[^\n(]{1,80}?)\s*\((?P<ts>\d{1,2}:\d{2}(?::\d{2})?)\):\s*$",
    re.MULTILINE,
)

FRONTMATTER_RE = re.compile(r"^---\s*\n(?P<yaml>.*?)\n---\s*\n", re.DOTALL)


class TranscriptParseError(ValueError):
    """Raised when a transcript file cannot be interpreted."""


@dataclass(slots=True)
class Turn:
    speaker: str
    start_seconds: int
    text: str

    @property
    def char_len(self) -> int:
        return len(self.text)


@dataclass(slots=True)
class Episode:
    id: str
    title: str
    guest: str | None
    channel: str | None
    youtube_url: str | None
    video_id: str | None
    publish_date: date | None
    duration_seconds: int | None
    description: str | None
    keywords: list[str]
    content_hash: str
    source_path: str
    turns: list[Turn] = field(default_factory=list)

    def to_row(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "guest": self.guest,
            "channel": self.channel,
            "youtube_url": self.youtube_url,
            "video_id": self.video_id,
            "publish_date": self.publish_date,
            "duration_seconds": self.duration_seconds,
            "description": self.description,
            "keywords": self.keywords,
            "content_hash": self.content_hash,
            "source_path": self.source_path,
            "source_updated_at": None,
        }


def parse_timestamp(raw: str) -> int:
    """'01:05:46' or '05:46' -> seconds."""
    parts = [int(p) for p in raw.split(":")]
    if len(parts) == 3:
        h, m, s = parts
    elif len(parts) == 2:
        h, m, s = 0, parts[0], parts[1]
    else:
        raise TranscriptParseError(f"Unrecognised timestamp: {raw!r}")
    return h * 3600 + m * 60 + s


def _coerce_date(value: Any) -> date | None:
    if isinstance(value, date):
        return value
    if isinstance(value, str) and value.strip():
        try:
            return date.fromisoformat(value.strip()[:10])
        except ValueError:
            return None
    return None


def _coerce_int(value: Any) -> int | None:
    try:
        if value is None or value == "":
            return None
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _coerce_keywords(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(k).strip() for k in value if str(k).strip()]
    if isinstance(value, str) and value.strip():
        return [k.strip() for k in value.split(",") if k.strip()]
    return []


def _video_id(frontmatter: dict[str, Any], url: str | None) -> str | None:
    explicit = frontmatter.get("video_id")
    if explicit:
        return str(explicit)
    if url:
        match = re.search(r"[?&]v=([\w-]+)", url)
        if match:
            return match.group(1)
    return None


def parse_turns(body: str) -> list[Turn]:
    """Split a transcript body into speaker turns."""
    matches = list(TURN_RE.finditer(body))
    turns: list[Turn] = []

    for index, match in enumerate(matches):
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(body)
        text = body[start:end].strip()
        if not text:
            continue
        try:
            seconds = parse_timestamp(match.group("ts"))
        except TranscriptParseError:
            continue
        turns.append(
            Turn(
                speaker=match.group("speaker").strip(),
                start_seconds=seconds,
                text=text,
            )
        )
    return turns


def parse_transcript(path: Path, *, episode_id: str | None = None) -> Episode:
    """Parse one transcript file into an Episode with its turns."""
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise TranscriptParseError(f"Could not read {path}: {exc}") from exc

    if not raw.strip():
        raise TranscriptParseError(f"{path} is empty")

    match = FRONTMATTER_RE.match(raw)
    if match:
        try:
            frontmatter = yaml.safe_load(match.group("yaml")) or {}
        except yaml.YAMLError as exc:
            raise TranscriptParseError(f"Invalid YAML frontmatter in {path}: {exc}") from exc
        body = raw[match.end():]
    else:
        # Tolerate a missing frontmatter block rather than dropping the episode.
        frontmatter = {}
        body = raw

    if not isinstance(frontmatter, dict):
        raise TranscriptParseError(f"Frontmatter in {path} is not a mapping")

    turns = parse_turns(body)
    if not turns:
        raise TranscriptParseError(f"No speaker turns found in {path}")

    # The slug is the episode directory name, which is stable upstream and
    # human-readable in logs and citations.
    slug = episode_id or path.parent.name or path.stem
    title = str(frontmatter.get("title") or slug.replace("-", " ").title())
    url = frontmatter.get("youtube_url")
    url = str(url) if url else None

    return Episode(
        id=slug,
        title=title,
        guest=(str(frontmatter["guest"]) if frontmatter.get("guest") else None),
        channel=(str(frontmatter["channel"]) if frontmatter.get("channel") else None),
        youtube_url=url,
        video_id=_video_id(frontmatter, url),
        publish_date=_coerce_date(frontmatter.get("publish_date")),
        duration_seconds=_coerce_int(frontmatter.get("duration_seconds")),
        description=(
            str(frontmatter["description"]).strip() if frontmatter.get("description") else None
        ),
        keywords=_coerce_keywords(frontmatter.get("keywords")),
        content_hash=hashlib.sha256(raw.encode("utf-8")).hexdigest(),
        source_path=str(path),
        turns=turns,
    )


def discover_transcripts(root: Path) -> list[Path]:
    """Find every transcript file under a corpus root, sorted for determinism."""
    if not root.exists():
        return []
    found = sorted(root.rglob("transcript.md"))
    if found:
        return found
    # Fall back to any markdown if the upstream layout ever changes.
    return sorted(
        p for p in root.rglob("*.md") if p.name.lower() not in {"readme.md", "claude.md"}
    )
