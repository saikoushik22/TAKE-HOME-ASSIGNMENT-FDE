"""Artifact generation skill.

Produces a standalone Markdown document or a complete HTML/CSS snippet from the
conversation, then hands it to the sanitizer before it is ever persisted or
rendered. Generation and safety are separate steps on purpose: the skill's job
is to write something good, and the sanitizer's job is to assume it might not
be (architecture.md section 8).
"""

from __future__ import annotations

import re
from typing import Any, AsyncIterator

from ...core.logging import get_logger
from ...llm.base import Message
from ...rag.retriever import format_context
from .base import Skill, SkillContext, SkillResult, build_citations, build_trace
from .citations import renumber, validate_and_prune

log = get_logger(__name__)

MARKDOWN_PROMPT = """You produce standalone Markdown documents for a product and \
growth team.

Requirements:
- Start with a single '# ' title.
- Use headings, bullets, and tables where they genuinely help. Do not decorate.
- Be concrete and immediately usable — this is a working document, not an essay.
- Cite factual claims drawn from the sources with [S#] markers.
- Never invent statistics, quotes, or company outcomes.
- Output ONLY the Markdown document. No preamble, no explanation, no code fence."""

HTML_PROMPT = """You produce complete, self-contained HTML snippets with inline CSS.

Requirements:
- Output a complete fragment: markup plus a <style> block. No <html>, <head>, or <body>.
- All CSS inline in a <style> block. Modern, clean, readable layout.
- Use semantic elements (header, section, h1-h3, ul, table) and keep heading order sensible.
- Responsive: it must read well from 360px up. Use relative units.
- NO external resources of any kind — no remote images, fonts, stylesheets, or scripts.
  The render sandbox blocks all network access, so remote references simply fail.
- Use system font stacks and, where you want an image, a CSS gradient or an inline SVG.
- Never include tracking, analytics, forms that post anywhere, or external links you invented.
- Output ONLY the HTML. No preamble, no explanation, no markdown code fence."""


def _strip_fences(text: str) -> str:
    """Remove a wrapping code fence.

    Models wrap output in fences despite instruction. Persisting the fence would
    make an HTML artifact render as literal text in the viewer, which looks like
    a bug in the product rather than in the model.
    """
    stripped = text.strip()
    match = re.match(
        r"^```(?:html|markdown|md)?\s*\n(?P<body>.*?)\n?```\s*$",
        stripped,
        re.DOTALL | re.IGNORECASE,
    )
    return match.group("body").strip() if match else stripped


def _derive_title(content: str, kind: str, fallback: str) -> str:
    if kind == "markdown":
        match = re.search(r"^#\s+(.+)$", content, re.MULTILINE)
        if match:
            return match.group(1).strip()[:200]
    else:
        for pattern in (r"<h1[^>]*>(.*?)</h1>", r"<title[^>]*>(.*?)</title>",
                        r"<h2[^>]*>(.*?)</h2>"):
            match = re.search(pattern, content, re.IGNORECASE | re.DOTALL)
            if match:
                text = re.sub(r"<[^>]+>", "", match.group(1)).strip()
                if text:
                    return text[:200]
    cleaned = fallback.strip().rstrip("?.!")
    return (cleaned[:80] or "Untitled artifact").capitalize()


class ArtifactSkill(Skill):
    name = "artifact"

    async def run(self, ctx: SkillContext) -> AsyncIterator[dict[str, Any]]:
        assert ctx.provider is not None

        kind = ctx.artifact_kind if ctx.artifact_kind in {"markdown", "html"} else "markdown"

        yield {"type": "status", "stage": "retrieving",
               "detail": "Looking for supporting material…"}

        chunks: list[Any] = []
        available: list[dict[str, Any]] = []
        context_block = ""

        if ctx.retriever is not None:
            retrieval = await ctx.retriever.retrieve(self._search_topic(ctx))
            if not retrieval.abstain:
                chunks = retrieval.chunks
                available = build_citations(chunks)
                context_block = format_context(
                    chunks,
                    max_chars_per_chunk=(
                        ctx.settings.rag_context_chars_per_chunk if ctx.settings else 0
                    ),
                )

        # An artifact request is not always a knowledge question — "make a table
        # of what we just discussed" needs the conversation, not the corpus. So
        # empty retrieval degrades to conversation-only rather than abstaining.
        if not chunks:
            yield {"type": "status", "stage": "generating",
                   "detail": "Building from the conversation…"}
        else:
            yield {"type": "status", "stage": "generating",
                   "detail": f"Building from {len(chunks)} sources…"}

        system = HTML_PROMPT if kind == "html" else MARKDOWN_PROMPT
        messages: list[Message] = [Message(role="system", content=system)]

        history_block = self._history_block(ctx)
        user_parts: list[str] = []
        if context_block:
            user_parts.append(f"Sources:\n\n{context_block}")
        if history_block:
            user_parts.append(f"Conversation so far:\n\n{history_block}")
        user_parts.append(f"Request: {ctx.message}")
        messages.append(Message(role="user", content="\n\n---\n\n".join(user_parts)))

        collected: list[str] = []
        async for piece in ctx.provider.stream(messages, model=ctx.model,
                                               max_tokens=3000):
            collected.append(piece)
            yield {"type": "token", "text": piece}

        raw = _strip_fences("".join(collected))

        if available:
            audit = validate_and_prune(raw, available)
            content, citations = renumber(audit.text, audit.citations)
        else:
            content, citations = raw, []

        title = _derive_title(content, kind, ctx.message)
        artifact = {"kind": kind, "title": title, "content": content}

        summary = (
            f"I've created a {'HTML' if kind == 'html' else 'Markdown'} artifact: "
            f"**{title}**. It's open in the viewer beside this chat."
        )

        if citations:
            yield {"type": "citations", "citations": citations}
        yield {"type": "artifact", "artifact": artifact}
        yield {"type": "result", "result": SkillResult(
            text=summary,
            citations=citations,
            retrieval_trace=build_trace(chunks) if chunks else {},
            artifact=artifact,
            meta={"skill": self.name, "kind": kind, "title": title,
                  "grounded": bool(citations)},
        )}

    # ------------------------------------------------------------- internals
    @staticmethod
    def _history_block(ctx: SkillContext, limit: int = 4) -> str:
        lines: list[str] = []
        for turn in ctx.history[-limit:]:
            role = turn.get("role")
            content = (turn.get("content") or "").strip()
            if role in {"user", "assistant"} and content:
                lines.append(f"{role.upper()}: {content[:1500]}")
        return "\n\n".join(lines)

    @staticmethod
    def _search_topic(ctx: SkillContext) -> str:
        text = (ctx.message or "").strip()
        # Strip the instruction verbs so retrieval searches the subject, not
        # the format request: "make me a table about pricing" -> "pricing".
        topic = re.sub(
            r"\b(make|create|build|generate|render|write|draft|turn into|give me)\b|"
            r"\b(an?|the|me|a)\b|\b(html|css|markdown|md|doc|document|table|"
            r"one[- ]?pager|checklist|template|page|snippet|artifact)\b",
            " ", text, flags=re.IGNORECASE,
        )
        topic = " ".join(topic.split())
        if len(topic.split()) >= 3:
            return topic
        for turn in reversed(ctx.history):
            if turn.get("role") == "user" and turn.get("content", "").strip() != text:
                return turn["content"][:300]
        return text
