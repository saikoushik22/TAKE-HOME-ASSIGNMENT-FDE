"""Structural contract enforcement for the Ship 30 essay.

Encodes section 2 of SKILL.md as executable checks. Validation exists because
a small local model drifts on long-form structure (PRD R3), and structure is
the part of "Ship 30 style" that is objectively checkable — so we check it
rather than hoping.

A failure names the offending section so the handler can repair just that
section instead of regenerating 1,250 words.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

WORD_MIN, WORD_MAX = 1_100, 1_400
WORD_TARGET = 1_250
H2_MIN = 3
BOLD_MIN, BOLD_MAX = 3, 20
CITATION_MIN = 2

_H1_RE = re.compile(r"^#\s+\S", re.MULTILINE)
_H2_RE = re.compile(r"^##\s+\S", re.MULTILINE)
_BULLET_RE = re.compile(r"^\s*[-*+]\s+\S", re.MULTILINE)
_BOLD_RE = re.compile(r"\*\*[^*\n]{2,}?\*\*")
_CITE_RE = re.compile(r"\[S\s*\d+", re.IGNORECASE)

# Openers that signal a definition or throat-clear rather than a hook (P2).
_WEAK_HOOK_RE = re.compile(
    r"^\s*(in today'?s|in the world of|have you ever|.{0,40}\bis one of the most\b|"
    r".{0,30}\bis defined as\b|.{0,30}\brefers to\b|let'?s talk about)",
    re.IGNORECASE,
)

# Banned register from SKILL.md P6.
_BANNED_WORDS = (
    "leverage", "utilize", "delve", "myriad", "in the realm of",
    "it is important to note", "furthermore", "moreover", "in conclusion",
    "game-changer", "game changer",
)


@dataclass(slots=True)
class ValidationIssue:
    code: str
    message: str
    severity: str = "error"  # 'error' blocks; 'warning' is reported only

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message, "severity": self.severity}


@dataclass(slots=True)
class ValidationResult:
    issues: list[ValidationIssue] = field(default_factory=list)
    word_count: int = 0
    h2_count: int = 0
    bold_count: int = 0
    citation_count: int = 0
    bullet_count: int = 0

    @property
    def ok(self) -> bool:
        return not any(i.severity == "error" for i in self.issues)

    @property
    def errors(self) -> list[ValidationIssue]:
        return [i for i in self.issues if i.severity == "error"]

    def to_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "word_count": self.word_count,
            "h2_count": self.h2_count,
            "bold_count": self.bold_count,
            "citation_count": self.citation_count,
            "bullet_count": self.bullet_count,
            "issues": [i.to_dict() for i in self.issues],
        }


def count_words(markdown: str) -> int:
    """Count prose words, excluding Markdown syntax and citation markers."""
    text = re.sub(r"```.*?```", " ", markdown, flags=re.DOTALL)
    text = _CITE_RE.sub(" ", text)
    text = re.sub(r"[#*_>`\[\]()|-]", " ", text)
    return len([w for w in text.split() if any(ch.isalnum() for ch in w)])


def first_prose_line(markdown: str) -> str:
    """The first non-heading, non-blank line — the hook."""
    for line in markdown.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            return stripped
    return ""


def validate_essay(markdown: str) -> ValidationResult:
    """Check a draft against the structural contract."""
    result = ValidationResult(
        word_count=count_words(markdown),
        h2_count=len(_H2_RE.findall(markdown)),
        bold_count=len(_BOLD_RE.findall(markdown)),
        citation_count=len(_CITE_RE.findall(markdown)),
        bullet_count=len(_BULLET_RE.findall(markdown)),
    )
    add = result.issues.append

    if result.word_count < WORD_MIN:
        add(ValidationIssue(
            "word_count_low",
            f"Essay is {result.word_count} words; the contract requires at least "
            f"{WORD_MIN} (target {WORD_TARGET}).",
        ))
    elif result.word_count > WORD_MAX:
        add(ValidationIssue(
            "word_count_high",
            f"Essay is {result.word_count} words; the contract allows at most {WORD_MAX}.",
        ))

    h1_count = len(_H1_RE.findall(markdown))
    if h1_count == 0:
        add(ValidationIssue("missing_title", "Essay has no H1 title."))
    elif h1_count > 1:
        add(ValidationIssue(
            "multiple_titles", f"Essay has {h1_count} H1 headings; exactly one is required.",
        ))

    if result.h2_count < H2_MIN:
        add(ValidationIssue(
            "too_few_sections",
            f"Essay has {result.h2_count} H2 sections; at least {H2_MIN} are required.",
        ))

    if result.bullet_count < 1:
        add(ValidationIssue(
            "no_bullets", "Essay has no bullet list; skimmable formatting requires at least one.",
        ))

    if result.bold_count < BOLD_MIN:
        add(ValidationIssue(
            "too_little_emphasis",
            f"Essay has {result.bold_count} bold spans; at least {BOLD_MIN} are required.",
        ))
    elif result.bold_count > BOLD_MAX:
        add(ValidationIssue(
            "bold_soup",
            f"Essay has {result.bold_count} bold spans; more than {BOLD_MAX} means "
            "emphasis stops carrying meaning.",
            severity="warning",
        ))

    if result.citation_count < CITATION_MIN:
        add(ValidationIssue(
            "too_few_citations",
            f"Essay has {result.citation_count} citations; at least {CITATION_MIN} "
            "are required for a grounded claim set.",
        ))

    hook = first_prose_line(markdown)
    if hook and _WEAK_HOOK_RE.match(hook):
        add(ValidationIssue(
            "weak_hook",
            "The opening line reads as a definition or throat-clear rather than a hook.",
            severity="warning",
        ))

    lowered = markdown.lower()
    found_banned = [w for w in _BANNED_WORDS if w in lowered]
    if found_banned:
        add(ValidationIssue(
            "banned_register",
            f"Contains banned words: {', '.join(sorted(set(found_banned))[:6])}.",
            severity="warning",
        ))

    return result


def repair_instruction(result: ValidationResult) -> str:
    """Turn validation errors into a targeted revision instruction."""
    parts: list[str] = []
    for issue in result.errors:
        if issue.code == "word_count_low":
            deficit = WORD_TARGET - result.word_count
            parts.append(
                f"Expand by roughly {deficit} words by deepening existing sections "
                "with specific, cited detail. Do not add a new section and do not pad."
            )
        elif issue.code == "word_count_high":
            excess = result.word_count - WORD_TARGET
            parts.append(f"Cut roughly {excess} words. Remove repetition, not evidence.")
        elif issue.code == "too_few_sections":
            parts.append(f"Restructure into at least {H2_MIN} '## ' sections that build on each other.")
        elif issue.code == "no_bullets":
            parts.append("Add one bullet list of parallel items where the content is genuinely a list.")
        elif issue.code == "too_little_emphasis":
            parts.append(f"Bold the single most important sentence in each section (at least {BOLD_MIN}).")
        elif issue.code == "too_few_citations":
            parts.append(f"Add [S#] citations so at least {CITATION_MIN} claims are sourced.")
        elif issue.code == "missing_title":
            parts.append("Add a single '# ' title at the top.")
        elif issue.code == "multiple_titles":
            parts.append("Keep exactly one '# ' title; demote the others to '## '.")
    return " ".join(parts)
