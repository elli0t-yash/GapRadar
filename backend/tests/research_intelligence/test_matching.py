"""The verdict contract: whatever a matcher returns, what lands is valid.

These tests are written against the contract rather than the stand-in
implementation, because the contract is what the eventual LLM adapter has
to satisfy.
"""

import math
from typing import Any

import pytest
from pydantic import ValidationError

from app.research_intelligence.matching import (
    DEFAULT_MATCH_POLICY,
    DEFAULT_RELEVANCE_THRESHOLD,
    ConceptOverlapMatcher,
    ResearchMatchPolicy,
    ResearchMatchVerdict,
    clamp_score,
)
from tests.research_intelligence.test_candidates import (
    CONTEXT,
    PLAN,
    RELEVANT,
    UNRELATED,
)


def verdict(**overrides: Any) -> ResearchMatchVerdict:
    payload: dict[str, Any] = {
        "relevance_score": 80.0,
        "matched_concepts": ["urban freight"],
        "match_reason": "Addresses on-demand freight booking directly.",
    }
    payload.update(overrides)
    return ResearchMatchVerdict(**payload)


# -- score clamping ---------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(150, 100.0), (101.0, 100.0), (-3, 0.0), (0, 0.0), (100, 100.0), (72.5, 72.5)],
)
def test_scores_are_clamped_into_the_zero_to_one_hundred_band(
    raw: Any, expected: float
) -> None:
    """An LLM will return 150 eventually. It must not corrupt a ranking."""
    assert verdict(relevance_score=raw).relevance_score == expected


@pytest.mark.parametrize("raw", ["87", None, [], {}, True, math.nan, math.inf])
def test_a_non_numeric_relevance_score_is_refused_not_defaulted(raw: Any) -> None:
    """Required means required: a missing score is never silently zero."""
    with pytest.raises(ValidationError):
        verdict(relevance_score=raw)


def test_clamp_returns_none_for_a_non_number_rather_than_zero() -> None:
    """ "Could not say" and "said zero" are different answers."""
    assert clamp_score("87") is None
    assert clamp_score(None) is None
    assert clamp_score(True) is None
    assert clamp_score(math.nan) is None
    assert clamp_score(0) == 0.0


def test_technical_readiness_may_be_absent() -> None:
    """None means "not assessed", never "not ready"."""
    assert verdict().technical_readiness_score is None
    assert verdict(technical_readiness_score=None).technical_readiness_score is None


def test_technical_readiness_is_clamped_when_present() -> None:
    assert verdict(technical_readiness_score=250).technical_readiness_score == 100.0
    assert verdict(technical_readiness_score="junk").technical_readiness_score is None


# -- concept normalization --------------------------------------------------


def test_concepts_are_stripped_deduped_and_order_preserved() -> None:
    result = verdict(
        matched_concepts=[
            "  urban freight  ",
            "Urban Freight",
            "vehicle routing",
            "",
            "   ",
            None,
            42,
            "vehicle routing",
        ]
    )

    assert result.matched_concepts == ["urban freight", "vehicle routing"]


def test_a_non_list_concepts_value_becomes_empty() -> None:
    assert verdict(matched_concepts="urban freight").matched_concepts == []
    assert verdict(matched_concepts=None).matched_concepts == []


# -- reason -----------------------------------------------------------------


def test_a_blank_match_reason_is_refused() -> None:
    """A verdict with no explanation cannot be shown to anyone."""
    for blank in ("", "   ", "\n\t", None, 42):
        with pytest.raises(ValidationError):
            verdict(match_reason=blank)


def test_a_match_reason_is_whitespace_normalized() -> None:
    assert verdict(match_reason="  addresses   the\n problem ").match_reason == (
        "addresses the problem"
    )


# -- policy / threshold -----------------------------------------------------


def test_the_default_threshold_is_seventy() -> None:
    assert DEFAULT_RELEVANCE_THRESHOLD == 70.0
    assert DEFAULT_MATCH_POLICY.relevance_threshold == 70.0


def test_the_threshold_is_configurable_not_hardcoded() -> None:
    assert ResearchMatchPolicy(relevance_threshold=40.0).relevance_threshold == 40.0


def test_a_threshold_outside_the_score_band_is_refused() -> None:
    for bad in (-1.0, 101.0):
        with pytest.raises(ValidationError):
            ResearchMatchPolicy(relevance_threshold=bad)


# -- the deterministic stand-in --------------------------------------------


def test_the_stand_in_judges_a_relevant_paper() -> None:
    result = ConceptOverlapMatcher(scale=4.0).judge(
        context=CONTEXT, plan=PLAN, paper=RELEVANT
    )

    assert result is not None
    assert 0.0 < result.relevance_score <= 100.0
    assert result.matched_concepts
    assert CONTEXT.problem in result.match_reason


def test_the_stand_in_declines_rather_than_scoring_an_unrelated_paper() -> None:
    """Declining is not the same as judging irrelevant, and is not persisted."""
    assert (
        ConceptOverlapMatcher().judge(context=CONTEXT, plan=PLAN, paper=UNRELATED)
        is None
    )


def test_the_stand_in_never_invents_a_readiness_score() -> None:
    """Word counts are not evidence about how buildable research is."""
    result = ConceptOverlapMatcher(scale=4.0).judge(
        context=CONTEXT, plan=PLAN, paper=RELEVANT
    )

    assert result is not None
    assert result.technical_readiness_score is None


def test_the_stand_in_scale_cannot_push_a_score_out_of_band() -> None:
    result = ConceptOverlapMatcher(scale=1000.0).judge(
        context=CONTEXT, plan=PLAN, paper=RELEVANT
    )

    assert result is not None
    assert result.relevance_score == 100.0


def test_the_stand_in_reports_plan_concepts_rather_than_bare_tokens() -> None:
    result = ConceptOverlapMatcher(scale=4.0).judge(
        context=CONTEXT, plan=PLAN, paper=RELEVANT
    )

    assert result is not None
    assert "urban freight" in result.matched_concepts
