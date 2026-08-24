"""Small talk — answered without retrieval and without a model call.

A grounded assistant still has to handle "hi" gracefully. Routing a greeting
through the full pipeline costs an embedding, a hybrid search, and ~3,000 tokens
of transcript stuffed into a 3B model's prompt — measured at over 50 seconds on
CPU to produce the word "Hello". That is not a model problem to tune; it is work
that should never have been started.

This skill returns a fixed response immediately. No embedding, no retrieval, no
generation, no database read beyond what the caller already did.

Deliberately NOT model-generated. A canned reply is instant, predictable, and
cannot hallucinate a capability the product does not have — and it is the one
place in the product where a fixed string is better than a generated one.
"""

from __future__ import annotations

import re
from typing import Any, AsyncIterator

from .base import Skill, SkillContext, SkillResult

# Each pattern must match the ENTIRE message (after normalisation), so
# "hi, how should we choose an activation metric?" is a real question and
# routes to grounded QA rather than being answered with a greeting.
_GREETING = re.compile(
    r"^(hi|hii+|hey+|hello+|yo|sup|howdy|hola|namaste|good\s+(morning|afternoon|evening))"
    r"[\s!.,]*(there|assistant|bot)?[\s!.,]*$",
    re.IGNORECASE,
)

_THANKS = re.compile(
    r"^(thanks?|thank\s+you|ty|thx|cheers|nice|great|awesome|perfect|cool|"
    r"got\s+it|makes\s+sense|ok(ay)?)[\s!.,]*$",
    re.IGNORECASE,
)

_FAREWELL = re.compile(
    r"^(bye+|goodbye|see\s+(ya|you)|later|good\s?night)[\s!.,]*$",
    re.IGNORECASE,
)

_IDENTITY = re.compile(
    r"^(who\s+(are|r)\s+(you|u)|what\s+(are|r)\s+(you|u)|what\s+can\s+you\s+do|"
    r"what\s+do\s+you\s+do|help|how\s+do\s+(i|you)\s+(use|work)\s*(this)?|"
    r"what\s+is\s+this)[\s!?.,]*$",
    re.IGNORECASE,
)

GREETING_REPLY = """Hello. I'm the Lenny Growth Assistant.

I answer product and growth questions using **only** Lenny's Podcast \
transcripts, and every claim I make carries a citation you can open and check. \
If the transcripts don't cover something, I'll say so rather than guess.

Ask me something like:

- How should we think about choosing an activation metric?
- What do operators say about finding product-market fit?
- How do strong teams prioritise their roadmap?

I can also turn any answer into a Ship 30 for 30 essay or a shareable document \
— just ask."""

THANKS_REPLY = "Happy to help. Ask me anything else about product or growth."

FAREWELL_REPLY = "Thanks for stopping by. Your conversations are saved in the sidebar."

IDENTITY_REPLY = """I'm the Lenny Growth Assistant — a research assistant \
grounded in Lenny's Podcast transcripts.

**What I do**

- Answer product and growth questions strictly from the transcripts, with \
inline citations that deep-link to the exact moment in the source episode.
- Write **Ship 30 for 30**-style essays (~1,250 words) from a grounded answer.
- Produce **artifacts** — Markdown documents or HTML/CSS — rendered beside \
this chat.

**What I won't do**

Answer from general knowledge. If the transcripts don't support something, \
I'll tell you the corpus doesn't cover it instead of inventing an answer."""


def match(message: str) -> str | None:
    """Return a canned reply, or None when this is not small talk.

    Returns None for anything unrecognised. A false negative just means a
    slightly slow greeting; a false positive means a real question gets answered
    with "Hello", which is far worse — so the patterns are anchored to the whole
    message and stay deliberately narrow.
    """
    text = (message or "").strip()
    if not text or len(text) > 60:
        return None

    if _GREETING.match(text):
        return GREETING_REPLY
    if _IDENTITY.match(text):
        return IDENTITY_REPLY
    if _THANKS.match(text):
        return THANKS_REPLY
    if _FAREWELL.match(text):
        return FAREWELL_REPLY
    return None


class SmallTalkSkill(Skill):
    name = "smalltalk"

    async def run(self, ctx: SkillContext) -> AsyncIterator[dict[str, Any]]:
        reply = match(ctx.message) or GREETING_REPLY

        # Emitted as one token so the client's streaming path is unchanged.
        # There is nothing to stream progressively — the whole answer is ready.
        yield {"type": "token", "text": reply}
        yield {
            "type": "result",
            "result": SkillResult(
                text=reply,
                citations=[],
                retrieval_trace={"skipped": True, "reason": "small talk"},
                meta={"skill": self.name, "grounded": False, "model_called": False},
            ),
        }
