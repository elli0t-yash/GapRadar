"""MarketContext -> ResearchSubject, proven lossless.

ResearchSubject is the abstraction the research engine will eventually
take instead of MarketContext. Nothing consumes it yet, ON PURPOSE:
migrating query generation, candidate selection and semantic matching is
a separate change, and this phase must not alter live opportunity
enrichment. What these tests pin is the property that makes that later
migration safe -- the conversion loses nothing.
"""

import uuid

import pytest
from pydantic import ValidationError

from app.db.models import Signal
from app.domain.enums import ResearchSubjectOrigin
from app.research_intelligence.schemas import MarketContext, ResearchSubject
from app.research_intelligence.service import market_context_from_signal

CONTEXT = MarketContext(
    signal_id=uuid.UUID("11111111-1111-1111-1111-111111111111"),
    problem="Freelancers get ghosted on invoices",
    description="Small studios chase payment for months with no leverage.",
    industry="B2B Services",
)


def test_conversion_preserves_the_problem() -> None:
    assert CONTEXT.as_research_subject().problem == CONTEXT.problem


def test_conversion_preserves_the_description() -> None:
    assert CONTEXT.as_research_subject().description == CONTEXT.description


def test_conversion_preserves_the_industry() -> None:
    assert CONTEXT.as_research_subject().industry == CONTEXT.industry


def test_conversion_preserves_a_missing_industry() -> None:
    """None must survive as None, never become "" or an invented value."""
    context = CONTEXT.model_copy(update={"industry": None})
    assert context.as_research_subject().industry is None


def test_the_signal_id_becomes_the_subject_id() -> None:
    """Renamed, not reinterpreted: the identity is carried across intact."""
    assert CONTEXT.as_research_subject().subject_id == CONTEXT.signal_id


def test_a_converted_market_context_is_labelled_as_a_signal() -> None:
    """Provenance survives the generalisation.

    A MarketContext is only ever built from a persisted Signal, so the
    subject it becomes says so. Without the label a validated market
    signal and a user's typed hypothesis reach the research engine as
    the same anonymous triple of strings.
    """
    assert CONTEXT.as_research_subject().origin is ResearchSubjectOrigin.SIGNAL


def test_the_conversion_is_total() -> None:
    """Every MarketContext field has a counterpart on the subject.

    This is the test that fails if someone adds a field to MarketContext
    and forgets the conversion, which is exactly how a "lossless"
    conversion quietly stops being one.
    """
    carried = {"problem", "description", "industry"}
    market_fields = set(MarketContext.model_fields) - {"signal_id"}
    subject_fields = set(ResearchSubject.model_fields) - {"subject_id", "origin"}

    assert market_fields == carried
    assert subject_fields == carried


def test_a_research_subject_is_immutable() -> None:
    """Frozen like every other contract on this side."""
    subject = CONTEXT.as_research_subject()
    with pytest.raises(ValidationError):
        subject.problem = "something else"  # type: ignore[misc]


def test_a_subject_built_from_a_real_signal_carries_the_shown_wording(
    opportunity_signal: Signal,
) -> None:
    """End to end: Signal -> MarketContext -> ResearchSubject.

    Asserts against the Signal columns the product surface renders, so a
    divergence between what a user reads and what the research engine is
    given would fail here.
    """
    subject = market_context_from_signal(opportunity_signal).as_research_subject()

    assert subject.subject_id == opportunity_signal.id
    assert subject.problem == opportunity_signal.title
    assert subject.description == opportunity_signal.body
    assert subject.industry == "B2B Services"
    assert subject.origin is ResearchSubjectOrigin.SIGNAL
