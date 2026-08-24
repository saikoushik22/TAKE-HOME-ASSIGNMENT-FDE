"""Ship 30 for 30 essay skill.

Drafts section-wise rather than single-shot. Asking a 3B local model for 1,250
coherent words in one pass produces drift and repetition; here each call stays
inside the model's reliable working range (~250 words), and only the assembly
step sees the whole essay. See SKILL.md section 5 and PRD R3.
"""

from __future__ import annotations

import json
import re
from typing import Any, AsyncIterator

from ....core.logging import get_logger
from ....llm.base import Message
from ....rag.retriever import format_context
from ..base import Skill, SkillContext, SkillResult, build_citations, build_trace, load_skill_file
from ..citations import renumber, validate_and_prune
from .validators import repair_instruction, validate_essay

log = get_logger(__name__)

MAX_REPAIRS = 1
TARGET_SECTIONS = 5


def _skill_text() -> str:
    return load_skill_file("ship30/SKILL.md")


PLAN_PROMPT = """You are planning a Ship 30 for 30 style essay.

Return ONLY a JSON object, no prose:
{{
  "title": "<a specific, claim-shaped title (not a topic label)>",
  "thesis": "<the single argument in one sentence, with no 'and'>",
  "sections": [
    {{"heading": "<a claim, not a label>", "focus": "<what this section proves>"}}
  ]
}}

Rules:
- Exactly {n} sections, forming a progression where each earns the next.
- The final section must be the takeaway and must name a concrete action.
- Headings are claims ("Retention is a leading indicator"), never labels ("Retention").
- Ground the plan in the supplied sources only.
"""

SECTION_PROMPT = """Write ONE section of a Ship 30 for 30 style essay.

Essay title: {title}
Thesis: {thesis}
This section's heading: {heading}
What it must establish: {focus}
{position_note}

Requirements:
- Roughly {words} words. Start with the '## {heading}' line, then the prose.
- Paragraphs of 1-3 sentences.
- Bold the single most important sentence.
- Cite claims with [S#] markers drawn from the sources below.
- Second person, present tense, no filler, no preamble.
- Use ONLY the sources for factual claims. Never invent numbers, quotes, or companies.

Write only this section. Do not write a title, an introduction, or other sections."""

HOOK_PROMPT = """Write the opening of a Ship 30 for 30 style essay.

Title: {title}
Thesis: {thesis}

Requirements:
- Start with '# {title}' on the first line.
- Then roughly 120 words of opening prose.
- The first prose line must create a curiosity gap: a counterintuitive claim, a
  specific stake, a named tension, or a sharp question.
- NEVER open with a definition, "In today's...", or "Have you ever...".
- Bold one sentence. Cite with [S#] where you make a factual claim.
- Do not write any '## ' section headings.

Sources are below. Use only them for factual claims."""


def _strip_fences(text: str) -> str:
    return re.sub(r"^\s*```(?:markdown|md)?\s*|\s*```\s*$", "", text.strip())


def _parse_plan(raw: str, fallback_topic: str) -> dict[str, Any]:
    """Parse the plan, falling back to a sane default structure.

    A local model will occasionally return prose instead of JSON. A failed plan
    must degrade to a usable essay rather than an error, so the fallback is a
    generic-but-valid progression.
    """
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if match:
        try:
            parsed = json.loads(match.group(0))
            sections = parsed.get("sections") or []
            if isinstance(sections, list) and len(sections) >= 3:
                return {
                    "title": str(parsed.get("title") or fallback_topic).strip("#").strip(),
                    "thesis": str(parsed.get("thesis") or "").strip(),
                    "sections": [
                        {
                            "heading": str(s.get("heading", "")).strip("#").strip(),
                            "focus": str(s.get("focus", "")).strip(),
                        }
                        for s in sections[:6]
                        if str(s.get("heading", "")).strip()
                    ],
                }
        except (json.JSONDecodeError, AttributeError, TypeError):
            pass

    log.warning("ship30.plan.fallback", extra={"topic": fallback_topic[:80]})
    return {
        "title": fallback_topic,
        "thesis": f"What operators actually do about {fallback_topic.lower()}.",
        "sections": [
            {"heading": "The problem is not what you think",
             "focus": "reframe the common assumption"},
            {"heading": "What the operators actually did",
             "focus": "concrete evidence from the sources"},
            {"heading": "How to apply it this week",
             "focus": "a specific, actionable takeaway"},
        ],
    }


class Ship30Skill(Skill):
    name = "ship30_essay"

    async def run(self, ctx: SkillContext) -> AsyncIterator[dict[str, Any]]:
        assert ctx.retriever is not None and ctx.provider is not None

        topic = self._resolve_topic(ctx)

        yield {"type": "status", "stage": "retrieving",
               "detail": "Gathering evidence from transcripts…"}
        retrieval = await ctx.retriever.retrieve(topic, top_k=10)

        if retrieval.abstain or retrieval.is_empty:
            text = (
                "I can't write a grounded essay on this — Lenny's transcripts don't "
                "cover it well enough to support the claims.\n\n"
                "Ask me about product-market fit, activation, retention, pricing, or "
                "building product teams, and I'll write you a full essay with sources."
            )
            for line in text.split("\n"):
                yield {"type": "token", "text": line + "\n"}
            yield {"type": "result", "result": SkillResult(
                text=text, abstained=True,
                retrieval_trace={"abstained": True, "reason": retrieval.reason},
                meta={"skill": self.name, "abstained": True},
            )}
            return

        chunks = retrieval.chunks
        available = build_citations(chunks)
        context_block = format_context(
            chunks,
            max_chars_per_chunk=(
                ctx.settings.rag_context_chars_per_chunk if ctx.settings else 0
            ),
        )
        skill_spec = _skill_text()

        yield {"type": "status", "stage": "planning",
               "detail": f"Planning the essay from {len(chunks)} sources…"}

        plan = await self._plan(ctx, topic, context_block, skill_spec)
        sections = plan["sections"]

        yield {"type": "status", "stage": "drafting",
               "detail": f"Drafting {len(sections) + 1} sections…"}

        parts: list[str] = []

        # ---- hook ----------------------------------------------------------
        hook = await self._generate(
            ctx, skill_spec, context_block,
            HOOK_PROMPT.format(title=plan["title"], thesis=plan["thesis"]),
            max_tokens=400,
        )
        parts.append(_strip_fences(hook))
        yield {"type": "token", "text": parts[-1] + "\n\n"}

        # ---- body ----------------------------------------------------------
        body_words = max(180, (1250 - 240) // max(1, len(sections)))
        for index, section in enumerate(sections):
            is_last = index == len(sections) - 1
            note = (
                "This is the FINAL section: it is the takeaway. Close the loop the "
                "opening created and name a concrete action the reader can take this week."
                if is_last else
                f"This is section {index + 1} of {len(sections)}."
            )
            yield {"type": "status", "stage": "drafting",
                   "detail": f"Section {index + 1}/{len(sections)}: {section['heading']}"}

            text = await self._generate(
                ctx, skill_spec, context_block,
                SECTION_PROMPT.format(
                    title=plan["title"], thesis=plan["thesis"],
                    heading=section["heading"], focus=section["focus"],
                    words=body_words, position_note=note,
                ),
                max_tokens=700,
            )
            cleaned = _strip_fences(text)
            parts.append(cleaned)
            yield {"type": "token", "text": cleaned + "\n\n"}

        essay = "\n\n".join(p.strip() for p in parts if p.strip())

        # ---- validate and repair -------------------------------------------
        validation = validate_essay(essay)
        repairs = 0
        while not validation.ok and repairs < MAX_REPAIRS:
            repairs += 1
            yield {"type": "status", "stage": "revising",
                   "detail": f"Revising: {validation.errors[0].message}"}
            log.info("ship30.repair", extra={"attempt": repairs,
                                             "issues": [i.code for i in validation.errors]})
            essay = _strip_fences(await self._repair(ctx, skill_spec, context_block,
                                                     essay, validation))
            validation = validate_essay(essay)

        audit = validate_and_prune(essay, available)
        final_text, final_citations = renumber(audit.text, audit.citations)

        artifact = {
            "kind": "markdown",
            "title": plan["title"][:200],
            "content": final_text,
        }

        yield {"type": "citations", "citations": final_citations}
        yield {"type": "artifact", "artifact": artifact}
        yield {"type": "result", "result": SkillResult(
            text=final_text,
            citations=final_citations,
            retrieval_trace=build_trace(chunks, query=topic),
            artifact=artifact,
            meta={
                "skill": self.name,
                "validation": validation.to_dict(),
                "repairs": repairs,
                "sections": len(sections),
            },
        )}

    # ------------------------------------------------------------- internals
    def _resolve_topic(self, ctx: SkillContext) -> str:
        """Derive the essay topic.

        "Turn that into an essay" has no topic of its own — the substrate is the
        previous exchange, so we reach back for it.
        """
        text = (ctx.message or "").strip()
        refers_back = re.search(
            r"\b(that|this|it|the above|your answer|previous)\b", text, re.IGNORECASE
        )
        if refers_back or len(text.split()) <= 8:
            for turn in reversed(ctx.history):
                if turn.get("role") == "assistant" and turn.get("content"):
                    return turn["content"][:600]
            for turn in reversed(ctx.history):
                if turn.get("role") == "user" and turn.get("content", "").strip() != text:
                    return turn["content"][:400]
        return text

    async def _generate(
        self, ctx: SkillContext, skill_spec: str, context_block: str,
        instruction: str, *, max_tokens: int,
    ) -> str:
        completion = await ctx.provider.complete(  # type: ignore[union-attr]
            [
                Message(role="system", content=skill_spec),
                Message(
                    role="user",
                    content=f"Sources:\n\n{context_block}\n\n---\n\n{instruction}",
                ),
            ],
            model=ctx.model,
            temperature=0.6,  # a little warmth: this is prose, not classification
            max_tokens=max_tokens,
        )
        return completion.text

    async def _plan(
        self, ctx: SkillContext, topic: str, context_block: str, skill_spec: str
    ) -> dict[str, Any]:
        completion = await ctx.provider.complete(  # type: ignore[union-attr]
            [
                Message(role="system", content=skill_spec),
                Message(
                    role="user",
                    content=(
                        f"Sources:\n\n{context_block}\n\n---\n\n"
                        f"{PLAN_PROMPT.format(n=TARGET_SECTIONS)}\n\n"
                        f"Topic or source answer:\n{topic}"
                    ),
                ),
            ],
            model=ctx.model, temperature=0.3, max_tokens=600,
        )
        return _parse_plan(completion.text, topic[:120])

    async def _repair(
        self, ctx: SkillContext, skill_spec: str, context_block: str,
        essay: str, validation: Any,
    ) -> str:
        completion = await ctx.provider.complete(  # type: ignore[union-attr]
            [
                Message(role="system", content=skill_spec),
                Message(
                    role="user",
                    content=(
                        f"Sources:\n\n{context_block}\n\n---\n\n"
                        f"Revise this essay. {repair_instruction(validation)}\n\n"
                        "Keep everything that already works. Return the COMPLETE revised "
                        "essay in Markdown, nothing else.\n\n"
                        f"---\n\n{essay}"
                    ),
                ),
            ],
            model=ctx.model, temperature=0.4, max_tokens=2600,
        )
        return completion.text
