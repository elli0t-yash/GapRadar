"""The investigation create contract validates SHAPE, and only shape.

Nothing here asserts that a query describes a real problem -- nothing
can. These tests pin the boundary: present, non-blank, bounded, and
otherwise passed through in the user's own words.
"""

import pytest
from pydantic import ValidationError

from app.investigations.schemas import (
    MAX_INDUSTRY_CHARS,
    MAX_QUERY_CHARS,
    InvestigationCreate,
)


def test_a_plain_query_is_accepted() -> None:
    payload = InvestigationCreate(query="Freelancers get ghosted on invoices")
    assert payload.query == "Freelancers get ghosted on invoices"
    assert payload.industry is None


@pytest.mark.parametrize("query", ["", " ", "\t", "\n", "   \t \n  "])
def test_a_blank_query_is_rejected(query: str) -> None:
    """An empty question is not a question, however it is spelled."""
    with pytest.raises(ValidationError):
        InvestigationCreate(query=query)


def test_outer_whitespace_is_trimmed() -> None:
    assert InvestigationCreate(query="  hospital rota swaps  ").query == (
        "hospital rota swaps"
    )


def test_the_users_wording_is_preserved_exactly() -> None:
    """Trimming the edges is the ONLY change made to the query.

    Case, punctuation, inner spacing and phrasing all survive: a system
    that quietly rewrites the question cannot be trusted about the
    answer, and the user has to be able to read their own sentence back.
    """
    original = "Why do  SMB clinics STILL fax referrals?! (2026)"
    assert InvestigationCreate(query=f"\n {original} \n").query == original


def test_a_query_at_the_limit_is_accepted() -> None:
    query = "x" * MAX_QUERY_CHARS
    assert len(InvestigationCreate(query=query).query) == MAX_QUERY_CHARS


def test_an_enormous_query_is_rejected() -> None:
    """The bound is real, and it is enforced on the submitted string."""
    with pytest.raises(ValidationError):
        InvestigationCreate(query="x" * (MAX_QUERY_CHARS + 1))


def test_an_optional_industry_is_accepted() -> None:
    payload = InvestigationCreate(query="rota swaps", industry="Healthcare")
    assert payload.industry == "Healthcare"


def test_a_blank_industry_becomes_none() -> None:
    """Absent and blank must not become two different stored values."""
    assert InvestigationCreate(query="rota swaps", industry="   ").industry is None


def test_industry_whitespace_is_trimmed() -> None:
    payload = InvestigationCreate(query="rota swaps", industry="  Healthcare  ")
    assert payload.industry == "Healthcare"


def test_an_enormous_industry_is_rejected() -> None:
    with pytest.raises(ValidationError):
        InvestigationCreate(query="rota swaps", industry="x" * (MAX_INDUSTRY_CHARS + 1))


@pytest.mark.parametrize("field", ["title", "description", "status"])
def test_a_client_cannot_set_derived_or_lifecycle_fields(field: str) -> None:
    """title, description and status are GapRadar's to write, not a client's.

    REFUSED, not ignored. A caller that believes it set `status` and gets
    a 201 back has been told its request succeeded when half of it was
    discarded; the mistake then surfaces much later as "GapRadar lost my
    data". The error names the offending field.
    """
    with pytest.raises(ValidationError) as caught:
        InvestigationCreate.model_validate({"query": "rota swaps", field: "anything"})

    assert field in str(caught.value)


def test_unknown_fields_are_refused_outright() -> None:
    """Not just the known-derived ones -- anything unrecognised."""
    with pytest.raises(ValidationError):
        InvestigationCreate.model_validate({"query": "rota swaps", "nonsense": 1})


def test_the_accepted_contract_is_exactly_two_fields() -> None:
    payload = InvestigationCreate(query="rota swaps")
    assert set(payload.model_dump()) == {"query", "industry"}
