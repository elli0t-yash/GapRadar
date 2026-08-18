"""Pain wording in, research wording out -- exactly three times.

The generator is a lexicon, not a language model, and these tests pin
what that buys and what it does not.
"""

import uuid

import pytest

from app.research_intelligence.query_generation import (
    QUERIES_PER_OPPORTUNITY,
    ConceptQueryGenerator,
    ResearchQueryGenerationError,
    _repeats_a_token,
    normalize_query,
    select_queries,
    tokenize,
)
from app.research_intelligence.schemas import MarketContext

CARGO = MarketContext(
    signal_id=uuid.uuid4(),
    problem="Why is booking cargo vehicles harder than passenger transport?",
    description=(
        "Consumers needing to transport single pieces of furniture cannot easily "
        "book small cargo tempos or pickup trucks on-demand, forcing them to "
        "negotiate with unorganized tempo drivers at inflated prices with no fare "
        "transparency."
    ),
    industry="Logistics",
)


def generate(context: MarketContext) -> list[str]:
    return ConceptQueryGenerator().generate(context).queries


# -- the shape of a plan ----------------------------------------------------


def test_exactly_three_queries_are_generated() -> None:
    assert len(generate(CARGO)) == QUERIES_PER_OPPORTUNITY == 3


def test_no_query_is_blank_or_whitespace() -> None:
    assert all(query.strip() for query in generate(CARGO))


def test_queries_are_distinct() -> None:
    queries = generate(CARGO)

    assert len(set(queries)) == 3


def test_the_plan_reports_its_concepts_and_rationale() -> None:
    plan = ConceptQueryGenerator().generate(CARGO)

    assert plan.signal_id == CARGO.signal_id
    assert plan.concepts, "a plan with no concepts is not auditable"
    assert "Logistics" in plan.rationale


# -- translation, not repetition --------------------------------------------


def test_the_problem_statement_is_not_used_as_a_query() -> None:
    """Searching arXiv with consumer phrasing finds nothing."""
    queries = generate(CARGO)

    assert CARGO.problem.lower() not in queries
    for query in queries:
        assert "why is" not in query
        assert "harder than" not in query


def test_queries_use_research_terminology(monkeypatch: pytest.MonkeyPatch) -> None:
    plan = ConceptQueryGenerator().generate(CARGO)
    joined = " ".join(plan.queries)

    # Terms a paper would actually be published under.
    assert "vehicle routing" in joined or "urban freight" in joined
    assert any(
        method in joined
        for method in ("optimization", "forecasting", "allocation", "modelling")
    )


def test_the_problem_context_is_represented() -> None:
    """Freight/booking wording must reach the queries, not just the industry."""
    plan = ConceptQueryGenerator().generate(CARGO)

    assert "urban freight" in plan.concepts
    assert "on-demand allocation" in plan.concepts


def test_the_industry_is_represented() -> None:
    plan = ConceptQueryGenerator().generate(CARGO)

    assert any(
        "logistics" in concept or "freight" in concept for concept in plan.concepts
    )


def test_two_different_problems_produce_different_queries() -> None:
    fintech = MarketContext(
        signal_id=uuid.uuid4(),
        problem="Why do small merchants wait weeks for payment settlement?",
        description="Merchants cannot predict when card payments will settle.",
        industry="Fintech",
    )

    assert set(generate(CARGO)) != set(generate(fintech))


def test_a_context_without_an_industry_still_generates_three() -> None:
    context = MarketContext(
        signal_id=uuid.uuid4(),
        problem="Scheduling delivery drivers is chaotic",
        description="Dispatch is manual and routes are planned on paper.",
        industry=None,
    )

    plan = ConceptQueryGenerator().generate(context)
    assert len(plan.queries) == 3
    assert "no industry supplied" in plan.rationale


def test_an_unmapped_industry_still_contributes_a_concept() -> None:
    context = MarketContext(
        signal_id=uuid.uuid4(),
        problem="Booking equipment is slow",
        description="Operators cannot see availability.",
        industry="Aquaculture",
    )

    plan = ConceptQueryGenerator().generate(context)
    assert any("aquaculture" in concept for concept in plan.concepts)


def test_a_context_with_no_usable_wording_is_refused() -> None:
    """Better to fail loudly than to pad the plan with a repeat."""
    context = MarketContext(
        signal_id=uuid.uuid4(), problem="the and of", description="is it", industry=None
    )

    with pytest.raises(ResearchQueryGenerationError, match="no research vocabulary"):
        ConceptQueryGenerator().generate(context)


# -- normalization and dedupe -----------------------------------------------


def test_queries_are_normalized() -> None:
    assert normalize_query("  Urban   FREIGHT  Routing ") == "urban freight routing"


def test_selection_drops_blanks() -> None:
    assert select_queries(["", "   ", "vehicle routing"], limit=3) == [
        "vehicle routing"
    ]


def test_selection_drops_exact_duplicates() -> None:
    selected = select_queries(
        ["vehicle routing", "Vehicle Routing", "urban freight"], limit=3
    )

    assert selected == ["vehicle routing", "urban freight"]


def test_selection_drops_reordered_near_duplicates() -> None:
    """Word order must not disguise the same search."""
    selected = select_queries(
        ["urban freight routing", "routing urban freight"], limit=3
    )

    assert selected == ["urban freight routing"]


def test_selection_keeps_a_genuinely_different_query() -> None:
    selected = select_queries(
        ["urban freight routing", "credit risk modelling"], limit=3
    )

    assert len(selected) == 2


def test_selection_respects_the_limit() -> None:
    assert (
        len(select_queries([f"topic{n} optimization" for n in range(9)], limit=3)) == 3
    )


def test_selection_preserves_order() -> None:
    """Candidates arrive best-angle-first; a later one never displaces them."""
    selected = select_queries(
        ["alpha routing", "beta pricing", "gamma forecasting"], limit=3
    )

    assert selected == ["alpha routing", "beta pricing", "gamma forecasting"]


# -- tokenization -----------------------------------------------------------


def test_tokenize_drops_stopwords_and_short_tokens() -> None:
    assert tokenize("Why is the booking of a cargo van hard?") == [
        "booking",
        "cargo",
        "van",
    ]


def test_tokenize_is_case_and_punctuation_insensitive() -> None:
    assert tokenize("Cargo-vehicles, BOOKING!") == ["cargo", "vehicles", "booking"]


def test_a_single_recognised_term_still_yields_three_distinct_queries() -> None:
    """A thin but usable context is padded with angles, never with repeats."""
    context = MarketContext(
        signal_id=uuid.uuid4(),
        problem="Warehouse work is slow",
        description="Nothing else is known.",
        industry=None,
    )

    plan = ConceptQueryGenerator().generate(context)

    assert plan.concepts == ["warehouse operations"]
    assert len(plan.queries) == 3
    assert len(set(plan.queries)) == 3
    assert all("warehouse operations" in query for query in plan.queries)


# -- degenerate queries -----------------------------------------------------
# A domain term can collide with the method term appended to it, producing a
# query that says the same thing twice. Found by auditing the real 133-item
# trusted corpus, where it affected 27 opportunities (20%).


def test_a_query_repeating_a_token_is_rejected() -> None:
    """ "demand forecasting demand forecasting" is malformed, not narrower."""
    selected = select_queries(
        ["demand forecasting demand forecasting", "urban freight optimization"],
        limit=3,
    )

    assert selected == ["urban freight optimization"]


@pytest.mark.parametrize(
    "query",
    [
        "demand forecasting demand forecasting",
        "process optimization optimization",
        "food systems beverage systems",
        "payment systems issues systems",
    ],
)
def test_known_degenerate_shapes_are_detected(query: str) -> None:
    assert _repeats_a_token(query) is True


@pytest.mark.parametrize(
    "query",
    [
        "urban freight optimization",
        "vehicle routing demand forecasting",
        "clinical workflow",
    ],
)
def test_a_well_formed_query_is_not_treated_as_degenerate(query: str) -> None:
    assert _repeats_a_token(query) is False


def test_no_generated_plan_contains_a_degenerate_query() -> None:
    """The end-to-end guarantee, over contexts that used to trigger it."""
    contexts = [
        MarketContext(
            signal_id=uuid.uuid4(),
            problem="Why can't small restaurants access wholesale ingredient pricing?",
            description="Demand is unpredictable and capacity is wasted.",
            industry="B2B Services",
        ),
        MarketContext(
            signal_id=uuid.uuid4(),
            problem="Why do payment apps have confusing cashback terms?",
            description="Users cannot tell what they earned.",
            industry="Payment Issues",
        ),
        MarketContext(
            signal_id=uuid.uuid4(),
            problem="Why can't hosts prepare restaurant-quality meals quickly?",
            description="Home cooking takes too long.",
            industry="Food & Beverage",
        ),
    ]

    for context in contexts:
        plan = ConceptQueryGenerator().generate(context)
        assert len(plan.queries) == 3
        for query in plan.queries:
            tokens = query.split()
            assert len(tokens) == len(set(tokens)), f"degenerate query: {query!r}"


def test_a_domain_term_colliding_with_a_method_term_still_yields_three() -> None:
    """Rejecting the degenerate pairing must not cost a query."""
    context = MarketContext(
        signal_id=uuid.uuid4(),
        problem="Demand planning for warehouse capacity is manual",
        description="Stock levels and demand are guessed.",
        industry=None,
    )

    plan = ConceptQueryGenerator().generate(context)

    assert "demand forecasting" in plan.concepts
    assert len(plan.queries) == 3
    assert len(set(plan.queries)) == 3
