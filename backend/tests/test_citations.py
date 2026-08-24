"""Citation validation — the mechanism behind the CBAR metric.

The product promise is an answer the user can defend. A citation that does not
resolve is worse than no citation, because it looks like a receipt and is not.
These tests pin that guarantee.
"""

from __future__ import annotations

from typing import Any

from app.agent.skills.citations import renumber, validate_and_prune


def sources(count: int) -> list[dict[str, Any]]:
    return [
        {
            'index': i,
            'chunk_id': f'chunk-{i}',
            'episode_id': f'ep-{i}',
            'episode_title': f'Episode {i}',
            'guest': f'Guest {i}',
            'snippet': f'Snippet {i}',
            'url': f'https://example.com/{i}',
        }
        for i in range(1, count + 1)
    ]


# ------------------------------------------------------------- validation


def test_valid_markers_are_preserved() -> None:
    audit = validate_and_prune('Activation matters [S1]. Retention compounds [S2].', sources(3))
    assert '[S1]' in audit.text
    assert '[S2]' in audit.text
    assert audit.used_indexes == {1, 2}


def test_dangling_marker_is_stripped() -> None:
    """A [S7] when only three sources exist is a fabricated-looking receipt."""
    audit = validate_and_prune('Claim one [S1]. Invented claim [S7].', sources(3))
    assert '[S7]' not in audit.text
    assert '[S1]' in audit.text
    assert audit.invalid_markers


def test_only_cited_sources_are_returned() -> None:
    """Returning all retrieved chunks would overstate the grounding."""
    audit = validate_and_prune('Only this one is used [S2].', sources(8))
    assert [c['index'] for c in audit.citations] == [2]


def test_grouped_markers_are_understood() -> None:
    audit = validate_and_prune('Two operators agree [S1, S3].', sources(4))
    assert audit.used_indexes == {1, 3}


def test_uncited_answer_reports_no_citations() -> None:
    """The signal AC5 depends on: an ungrounded answer must be detectable."""
    audit = validate_and_prune('A confident claim with no source at all.', sources(4))
    assert audit.has_citations is False
    assert audit.citations == []


def test_no_available_sources_strips_everything() -> None:
    audit = validate_and_prune('Fabricated [S1] and [S2].', [])
    assert '[S1]' not in audit.text
    assert '[S2]' not in audit.text
    assert audit.citations == []


def test_marker_matching_is_whitespace_and_case_tolerant() -> None:
    """Small models emit [s1] and [S 1]; those are still valid citations."""
    audit = validate_and_prune('Claim [s1] and another [S 2].', sources(3))
    assert audit.used_indexes == {1, 2}


# ------------------------------------------------------------ renumbering


def test_sparse_citations_are_renumbered_contiguously() -> None:
    """'Sources 2 and 5' invites the reader to wonder where 1, 3 and 4 went."""
    audit = validate_and_prune('First [S2]. Second [S5].', sources(6))
    text, citations = renumber(audit.text, audit.citations)

    assert '[S1]' in text
    assert '[S2]' in text
    assert '[S5]' not in text
    assert [c['index'] for c in citations] == [1, 2]


def test_renumbering_preserves_source_identity() -> None:
    """The renumbered [S1] must still point at the chunk it originally cited."""
    audit = validate_and_prune('Cited [S4].', sources(6))
    _, citations = renumber(audit.text, audit.citations)

    assert len(citations) == 1
    assert citations[0]['index'] == 1
    assert citations[0]['chunk_id'] == 'chunk-4'
    assert citations[0]['episode_title'] == 'Episode 4'


def test_renumbering_handles_grouped_markers() -> None:
    audit = validate_and_prune('Both agree [S2, S5].', sources(6))
    text, citations = renumber(audit.text, audit.citations)
    assert [c['index'] for c in citations] == [1, 2]
    assert 'S1' in text and 'S2' in text


def test_renumbering_an_uncited_answer_is_a_no_op() -> None:
    text, citations = renumber('No sources here.', [])
    assert text == 'No sources here.'
    assert citations == []


def test_full_pipeline_produces_a_defensible_answer() -> None:
    """End to end: hallucinated markers gone, real ones intact and contiguous."""
    raw = 'Grounded claim [S3]. Hallucinated claim [S99]. Another grounded one [S7].'
    audit = validate_and_prune(raw, sources(8))
    text, citations = renumber(audit.text, audit.citations)

    assert '[S99]' not in text
    assert [c['index'] for c in citations] == [1, 2]
    assert {c['chunk_id'] for c in citations} == {'chunk-3', 'chunk-7'}
    assert audit.invalid_markers
