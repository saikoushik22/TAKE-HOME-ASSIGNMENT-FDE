"""Intent routing.

Deterministic-first. A scored keyword-and-pattern prior runs before any model
call; only genuinely ambiguous input falls through to a schema-constrained LLM
classification, and if that fails or times out the router lands on the safest
default.

The reasoning is PRD R3: a 3B local model is an unreliable classifier, and a
routing mistake is maximally visible — the user asks a short question and gets
a 1,250-word essay. Deterministic rules make the common cases exact and free
(no extra model round-trip), and `grounded_qa` absorbs everything else.

Every decision is logged with the rule that fired and its confidence, so a
misroute is diagnosable rather than mysterious.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Literal

from ..core.config import Settings
from ..core.logging import get_logger
from ..llm.base import LLMProvider, Message

log = get_logger(__name__)

SkillName = Literal["grounded_qa", "ship30_essay", "artifact"]

DEFAULT_SKILL: SkillName = "grounded_qa"

# Confidence gap required for a deterministic decision to stand on its own.
DECISION_MARGIN = 0.15


@dataclass(slots=True)
class RouteDecision:
    skill: SkillName
    confidence: float
    rule: str
    artifact_kind: str | None = None  # 'markdown' | 'html' when skill == artifact

    def to_dict(self) -> dict[str, object]:
        return {
            "skill": self.skill,
            "confidence": round(self.confidence, 3),
            "rule": self.rule,
            "artifact_kind": self.artifact_kind,
        }


# Weighted patterns. Ordered strongest-first within each skill.
_ESSAY_PATTERNS: list[tuple[str, float]] = [
    (r"\bship\s*30\b", 1.0),
    (r"\bwrite\b.{0,30}\b(essay|post|article|newsletter|blog)\b", 0.9),
    (r"\b(essay|blog post|linkedin post|newsletter piece)\b", 0.7),
    (r"\bturn (this|that|it) into (an?|the) (essay|post|article)\b", 0.95),
    (r"\b1250\b|\b1,250\b", 0.6),
    (r"\bdraft\b.{0,20}\b(essay|post|article)\b", 0.8),
]

_ARTIFACT_PATTERNS: list[tuple[str, float]] = [
    (r"\b(html|css)\b", 0.85),
    (r"\bland(ing)? page\b", 0.9),
    (r"\bmake (me )?(a|an) (doc|document|one[- ]pager|onepager|table|checklist|template)\b", 0.9),
    (r"\b(one[- ]pager|onepager)\b", 0.8),
    (r"\bcreate (a|an) (table|matrix|checklist|template|framework|scorecard)\b", 0.85),
    (r"\b(markdown|md) (doc|document|file)\b", 0.8),
    (r"\bas (a|an) (artifact|document|table|checklist)\b", 0.8),
    (r"\b(build|render|generate)\b.{0,25}\b(page|component|card|layout|snippet)\b", 0.75),
    (r"\bcheat ?sheet\b", 0.8),
]

_QA_PATTERNS: list[tuple[str, float]] = [
    (r"^\s*(what|how|why|when|who|which|where)\b", 0.55),
    (r"\?\s*$", 0.4),
    (r"\b(explain|tell me about|what do .* say about|advice on)\b", 0.6),
    (r"\b(compare|difference between)\b", 0.5),
]

# HTML is chosen only on an explicit signal; Markdown is the safer default
# because it renders predictably and carries no script surface.
_HTML_PATTERNS = re.compile(
    r"\b(html|css|landing page|web ?page|styled|stylesheet|component|"
    r"card layout|snippet)\b",
    re.IGNORECASE,
)


def _score(text: str, patterns: list[tuple[str, float]]) -> tuple[float, str]:
    best = 0.0
    matched = "none"
    for pattern, weight in patterns:
        if re.search(pattern, text, re.IGNORECASE):
            if weight > best:
                best = weight
                matched = pattern
    return best, matched


def classify_deterministic(message: str) -> RouteDecision:
    """Score the message against each skill's patterns."""
    text = (message or "").strip()
    if not text:
        return RouteDecision(DEFAULT_SKILL, 1.0, "empty-input->default")

    essay_score, essay_rule = _score(text, _ESSAY_PATTERNS)
    artifact_score, artifact_rule = _score(text, _ARTIFACT_PATTERNS)
    qa_score, qa_rule = _score(text, _QA_PATTERNS)

    ranked = sorted(
        [
            ("ship30_essay", essay_score, essay_rule),
            ("artifact", artifact_score, artifact_rule),
            ("grounded_qa", qa_score, qa_rule),
        ],
        key=lambda item: item[1],
        reverse=True,
    )
    top_skill, top_score, top_rule = ranked[0]
    runner_up = ranked[1][1]

    if top_score == 0.0:
        return RouteDecision(DEFAULT_SKILL, 0.3, "no-pattern->default")

    # Ambiguous when the leader is not clearly ahead.
    if top_score - runner_up < DECISION_MARGIN:
        return RouteDecision(
            top_skill,  # type: ignore[arg-type]
            top_score - runner_up,
            f"ambiguous({top_rule})",
        )

    kind = None
    if top_skill == "artifact":
        kind = "html" if _HTML_PATTERNS.search(text) else "markdown"

    return RouteDecision(top_skill, top_score, top_rule, kind)  # type: ignore[arg-type]


_CLASSIFY_PROMPT = """You are an intent classifier for a podcast-transcript assistant.

Classify the user's message into exactly one intent:

- "grounded_qa": a question to answer from podcast transcripts.
- "ship30_essay": a request to write a long-form essay or post.
- "artifact": a request to produce a standalone document, table, checklist, \
one-pager, or HTML/CSS snippet.

Respond with ONLY a JSON object, no prose:
{"skill": "<intent>", "artifact_kind": "markdown" | "html" | null}
"""


async def classify_with_llm(
    message: str, provider: LLMProvider, settings: Settings
) -> RouteDecision | None:
    """Schema-constrained LLM classification for ambiguous input.

    Returns None on any failure. Routing must never be the thing that breaks a
    request, so every error path here falls back to the deterministic result.
    """
    try:
        completion = await provider.complete(
            [
                Message(role="system", content=_CLASSIFY_PROMPT),
                Message(role="user", content=message[:1000]),
            ],
            temperature=0.0,
            max_tokens=64,
        )
    except Exception as exc:
        log.warning("router.llm.failed", extra={"error": str(exc)})
        return None

    raw = completion.text.strip()
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if not match:
        log.warning("router.llm.unparseable", extra={"raw": raw[:200]})
        return None

    try:
        parsed = json.loads(match.group(0))
    except json.JSONDecodeError:
        log.warning("router.llm.bad_json", extra={"raw": raw[:200]})
        return None

    skill = parsed.get("skill")
    if skill not in {"grounded_qa", "ship30_essay", "artifact"}:
        return None

    kind = parsed.get("artifact_kind")
    if skill == "artifact" and kind not in {"markdown", "html"}:
        kind = "html" if _HTML_PATTERNS.search(message) else "markdown"
    elif skill != "artifact":
        kind = None

    return RouteDecision(skill, 0.7, "llm-classifier", kind)


class Router:
    def __init__(self, settings: Settings, provider: LLMProvider | None = None) -> None:
        self._settings = settings
        self._provider = provider

    async def route(self, message: str) -> RouteDecision:
        decision = classify_deterministic(message)

        needs_help = decision.rule.startswith(("ambiguous", "no-pattern"))
        if needs_help and self._settings.router_llm_fallback and self._provider:
            llm_decision = await classify_with_llm(message, self._provider, self._settings)
            if llm_decision is not None:
                decision = llm_decision

        # Never leave the caller with a sub-threshold guess at a destructive route.
        if decision.rule.startswith("ambiguous") and decision.confidence < 0.1:
            decision = RouteDecision(DEFAULT_SKILL, 0.5, "low-confidence->default")

        log.info("router.decision", extra=decision.to_dict())
        return decision
