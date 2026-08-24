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


# ------------------------------------------------------------- small talk

SMALLTALK = [
    'hi', 'Hi', 'hii', 'hey', 'Hey there', 'hello', 'Hello!', 'yo', 'howdy',
    'good morning', 'Good evening', 'thanks', 'Thank you', 'thx', 'ty',
    'cheers', 'ok', 'okay', 'got it', 'makes sense', 'bye', 'goodbye',
    'see you', 'who are you', 'What are you?', 'what can you do', 'help',
    'what is this',
]


@pytest.mark.parametrize('message', SMALLTALK)
def test_small_talk_is_answered_without_retrieval(message: str) -> None:
    """A greeting must never reach retrieval or the model.

    Measured before this existed: 'hi' cost an embedding, a hybrid search, and
    ~3,000 tokens of transcript in the prompt — over 50 seconds on CPU to
    produce a one-word reply.
    """
    assert classify_deterministic(message).skill == 'smalltalk'


# The dangerous direction. A false positive answers a real question with
# "Hello", which is far worse than a slightly slow greeting — so anything
# carrying a real request must fall through.
NOT_SMALLTALK = [
    'hi, how should we choose an activation metric?',
    'hello, what do operators say about retention?',
    'thanks — now write me an essay about churn',
    'ok but what about B2B pricing?',
    'help me understand product-market fit',
    'who are the guests that discuss onboarding?',
    'what can you do to improve activation rates?',
]


@pytest.mark.parametrize('message', NOT_SMALLTALK)
def test_a_greeting_prefix_does_not_swallow_a_real_question(message: str) -> None:
    assert classify_deterministic(message).skill != 'smalltalk'


def test_small_talk_never_calls_the_model_to_route(settings) -> None:
    """The whole point is avoiding a round-trip, so routing must not make one."""
    provider = FakeProvider(reply='{"skill": "grounded_qa"}')
    decision = classify_deterministic('hi')
    assert decision.skill == 'smalltalk'
    assert provider.calls == []


async def test_short_unmatched_input_skips_the_llm_classifier(settings) -> None:
    """Too short to classify is not the same as ambiguous.

    A model round-trip to choose between three intents for a two-word message
    costs over a second on CPU and cannot beat the default.
    """
    provider = FakeProvider(reply='{"skill": "artifact"}')
    decision = await Router(settings, provider).route('zzz qqq')

    assert decision.skill == DEFAULT_SKILL
    assert provider.calls == [], 'the router paid for a model call on a 2-word message'


async def test_longer_ambiguous_input_still_uses_the_classifier(settings) -> None:
    """The short-circuit must not disable the classifier entirely."""
    provider = FakeProvider(reply='{"skill": "ship30_essay", "artifact_kind": null}')
    decision = await Router(settings, provider).route(
        'something genuinely ambiguous that no pattern will match at all'
    )
    assert provider.calls, 'the classifier should still run for a long ambiguous message'
    assert decision.skill == 'ship30_essay'


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
