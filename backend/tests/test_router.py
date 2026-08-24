"""Intent routing.

A misroute is the most visible failure the product has: the user asks a short
question and gets a 1,250-word essay. These tests pin the deterministic layer,
which is what keeps the common cases off the unreliable 3B-model path.
"""

from __future__ import annotations

import pytest

from app.agent.router import (
    DEFAULT_SKILL,
    Router,
    classify_deterministic,
    classify_with_llm,
)
from tests.conftest import FakeProvider


# ------------------------------------------------------------ grounded QA

QA_MESSAGES = [
    'How should we choose an activation metric?',
    'What do operators say about product-market fit?',
    'Why do most onboarding flows fail?',
    'Explain the difference between activation and retention',
    'Tell me about pricing strategy',
]


@pytest.mark.parametrize('message', QA_MESSAGES)
def test_questions_route_to_grounded_qa(message: str) -> None:
    assert classify_deterministic(message).skill == 'grounded_qa'


# ------------------------------------------------------------------ essay

ESSAY_MESSAGES = [
    'Write an essay about retention',
    'Turn this into a Ship 30 for 30 essay',
    'Draft a blog post on activation metrics',
    'Turn that into a post',
    'Write a newsletter piece about pricing',
]


@pytest.mark.parametrize('message', ESSAY_MESSAGES)
def test_essay_requests_route_to_ship30(message: str) -> None:
    assert classify_deterministic(message).skill == 'ship30_essay'


# --------------------------------------------------------------- artifact

ARTIFACT_MESSAGES = [
    ('Make me a one-pager on activation', 'markdown'),
    ('Create a table comparing pricing models', 'markdown'),
    ('Build a landing page for our product', 'html'),
    ('Give me an HTML snippet with CSS for a pricing card', 'html'),
    ('Make a checklist for launch readiness', 'markdown'),
]


@pytest.mark.parametrize('message,expected_kind', ARTIFACT_MESSAGES)
def test_artifact_requests_route_with_correct_kind(message: str, expected_kind: str) -> None:
    decision = classify_deterministic(message)
    assert decision.skill == 'artifact'
    assert decision.artifact_kind == expected_kind


def test_html_requires_an_explicit_signal() -> None:
    """Markdown is the safer default: it renders predictably and has no script surface."""
    assert classify_deterministic('Make me a document about onboarding').artifact_kind == 'markdown'


# ---------------------------------------------------------------- fallback


def test_empty_message_falls_back_to_default() -> None:
    decision = classify_deterministic('')
    assert decision.skill == DEFAULT_SKILL
    assert decision.rule == 'empty-input->default'


def test_unmatched_message_falls_back_to_default() -> None:
    decision = classify_deterministic('zxcvbnm qwerty')
    assert decision.skill == DEFAULT_SKILL


def test_every_decision_is_diagnosable() -> None:
    """A misroute must be explainable, so the deciding rule is always recorded."""
    decision = classify_deterministic('Write an essay about churn')
    assert decision.rule and decision.rule != 'none'
    assert 0.0 <= decision.confidence <= 1.0
    assert set(decision.to_dict()) >= {'skill', 'confidence', 'rule', 'artifact_kind'}


# ------------------------------------------------------- LLM classifier


async def test_llm_classifier_parses_a_clean_json_reply(settings) -> None:
    provider = FakeProvider(reply='{"skill": "ship30_essay", "artifact_kind": null}')
    decision = await classify_with_llm('something ambiguous', provider, settings)
    assert decision is not None
    assert decision.skill == 'ship30_essay'


async def test_llm_classifier_tolerates_surrounding_prose(settings) -> None:
    """Small models wrap JSON in chatter. Extracting the object is expected work."""
    provider = FakeProvider(
        reply='Sure! Here you go:\n{"skill": "artifact", "artifact_kind": "html"}\nHope that helps.'
    )
    decision = await classify_with_llm('ambiguous', provider, settings)
    assert decision is not None
    assert decision.skill == 'artifact'
    assert decision.artifact_kind == 'html'


async def test_llm_classifier_returns_none_on_garbage(settings) -> None:
    provider = FakeProvider(reply='I am not going to answer that.')
    assert await classify_with_llm('ambiguous', provider, settings) is None


async def test_llm_classifier_returns_none_on_invalid_skill(settings) -> None:
    provider = FakeProvider(reply='{"skill": "delete_everything"}')
    assert await classify_with_llm('ambiguous', provider, settings) is None


async def test_llm_classifier_never_raises(settings) -> None:
    """Routing must never be the thing that breaks a request."""
    provider = FakeProvider(fail_with=RuntimeError('model exploded'))
    assert await classify_with_llm('ambiguous', provider, settings) is None


# ------------------------------------------------------------- end to end


async def test_router_prefers_deterministic_and_skips_the_model(settings) -> None:
    """A confident pattern match must not cost a model round-trip."""
    provider = FakeProvider(reply='{"skill": "artifact"}')
    decision = await Router(settings, provider).route('Write an essay about retention')

    assert decision.skill == 'ship30_essay'
    assert provider.calls == []  # the model was never consulted


async def test_router_survives_a_dead_model(settings) -> None:
    provider = FakeProvider(fail_with=RuntimeError('ollama is down'))
    decision = await Router(settings, provider).route('hmm')
    assert decision.skill == DEFAULT_SKILL


async def test_router_works_with_no_provider_at_all(settings) -> None:
    decision = await Router(settings, None).route('What is activation?')
    assert decision.skill == 'grounded_qa'
