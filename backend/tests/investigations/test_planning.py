"""The planner: three families, validated before anything is bought.

A model must not be able to return thirty queries and cause thirty
billable requests. Every rule below exists because a generator could
otherwise produce the shape it names.
"""

import uuid

import pytest
from sqlalchemy.orm import Session

from app.db.models import Investigation
from app.domain.enums import ResearchSubjectOrigin
from app.investigations.planning import (
    MAX_WEB_QUERIES_PER_FAMILY,
    MAX_WEB_QUERIES_PER_PLAN,
    InvestigationPlan,
    InvestigationPlanRejectedError,
    TemplateWebQueryGenerator,
    build_investigation_plan,
    locale_for,
    validate_plan,
)
from app.investigations.subject import research_subject_from_investigation
from app.research_intelligence.schemas import ResearchQueryPlan, ResearchSubject
from app.web_intelligence.schemas import (
    DEFAULT_LOCALE,
    INDIA_LOCALE,
    MAX_QUERY_CHARS,
    WebSearchFamily,
)


def subject(problem: str, industry: str | None = None) -> ResearchSubject:
    return ResearchSubject(
        subject_id=uuid.uuid4(),
        origin=ResearchSubjectOrigin.INVESTIGATION,
        problem=problem,
        description=problem,
        industry=industry,
    )


def plan_with(
    *,
    demand: list[str],
    competitors: list[str],
    research: list[str] | None = None,
) -> InvestigationPlan:
    subject_value = subject("cargo booking is broken", "Logistics")
    return InvestigationPlan(
        subject=subject_value,
        research_plan=ResearchQueryPlan(
            subject_id=subject_value.subject_id,
            queries=research or ["a routing", "b freight", "c pricing"],
            concepts=["vehicle routing"],
        ),
        demand_queries=demand,
        competitor_queries=competitors,
    )


GOOD_DEMAND = ["cargo booking problems", "freight dispatch challenges"]
GOOD_COMPETITORS = ["cargo booking software", "freight dispatch platform"]


# -- the plan contract ------------------------------------------------------


def test_a_well_formed_plan_is_accepted() -> None:
    validate_plan(plan_with(demand=GOOD_DEMAND, competitors=GOOD_COMPETITORS))


def test_the_plan_orders_demand_before_competitors() -> None:
    """If a run is cut short, "is the problem real" matters more."""
    plan = plan_with(demand=GOOD_DEMAND, competitors=GOOD_COMPETITORS)

    families = [search.family for search in plan.web_searches]

    assert families == [
        WebSearchFamily.DEMAND,
        WebSearchFamily.DEMAND,
        WebSearchFamily.COMPETITOR,
        WebSearchFamily.COMPETITOR,
    ]


# -- count bounds -----------------------------------------------------------


def test_too_few_demand_queries_is_refused() -> None:
    with pytest.raises(InvestigationPlanRejectedError):
        validate_plan(
            plan_with(demand=["cargo problems"], competitors=GOOD_COMPETITORS)
        )


def test_thirty_queries_cannot_buy_thirty_requests() -> None:
    """THE COST CONTROL, stated as a test."""
    thirty = [f"cargo booking problems {index}" for index in range(30)]

    with pytest.raises(InvestigationPlanRejectedError):
        validate_plan(plan_with(demand=thirty, competitors=GOOD_COMPETITORS))


def test_the_per_plan_ceiling_is_the_family_cap_times_the_families() -> None:
    assert MAX_WEB_QUERIES_PER_PLAN == MAX_WEB_QUERIES_PER_FAMILY * len(
        WebSearchFamily
    )


def test_a_wrong_research_query_count_is_refused() -> None:
    with pytest.raises(InvestigationPlanRejectedError):
        validate_plan(
            plan_with(
                demand=GOOD_DEMAND,
                competitors=GOOD_COMPETITORS,
                research=["only one"],
            )
        )


# -- query shape ------------------------------------------------------------


def test_a_blank_query_is_refused() -> None:
    with pytest.raises(InvestigationPlanRejectedError):
        validate_plan(
            plan_with(
                demand=["cargo booking problems", "   "],
                competitors=GOOD_COMPETITORS,
            )
        )


def test_prose_is_refused() -> None:
    """A very long string is a sign the planner emitted an essay."""
    with pytest.raises(InvestigationPlanRejectedError):
        validate_plan(
            plan_with(
                demand=["cargo booking problems", "problems " * MAX_QUERY_CHARS],
                competitors=GOOD_COMPETITORS,
            )
        )


def test_near_duplicate_queries_within_a_family_are_refused() -> None:
    """Three requests should be three angles, not one paid for three times."""
    with pytest.raises(InvestigationPlanRejectedError) as caught:
        validate_plan(
            plan_with(
                demand=["cargo booking problems", "booking cargo problems"],
                competitors=GOOD_COMPETITORS,
            )
        )

    assert "near-duplicate" in str(caught.value)


def test_identical_queries_across_families_are_allowed() -> None:
    """Different families asking a similar thing is not double-buying one."""
    validate_plan(
        plan_with(
            demand=["cargo booking problems", "freight dispatch challenges"],
            competitors=["cargo booking software", "freight dispatch platform"],
        )
    )


# -- family intent ----------------------------------------------------------


def test_a_demand_family_of_product_searches_is_refused() -> None:
    """A model must not answer three product searches and have GapRadar
    report that it looked at demand."""
    with pytest.raises(InvestigationPlanRejectedError) as caught:
        validate_plan(
            plan_with(
                demand=["cargo booking software", "freight dispatch platform"],
                competitors=GOOD_COMPETITORS,
            )
        )

    assert "intent" in str(caught.value)


def test_a_competitor_family_of_complaint_searches_is_refused() -> None:
    with pytest.raises(InvestigationPlanRejectedError):
        validate_plan(
            plan_with(
                demand=GOOD_DEMAND,
                competitors=["cargo booking problems", "freight challenges"],
            )
        )


# -- the deterministic generator --------------------------------------------


def test_the_template_generator_produces_both_families() -> None:
    demand, competitors = TemplateWebQueryGenerator().generate_web_queries(
        subject("Restaurants waste food because forecasting is manual")
    )

    assert len(demand) == len(competitors) == MAX_WEB_QUERIES_PER_FAMILY
    assert all("restaurants" in query for query in demand + competitors)


def test_the_generated_families_pass_their_own_validation() -> None:
    """The generator and the gate must agree, or nothing ever runs."""
    demand, competitors = TemplateWebQueryGenerator().generate_web_queries(
        subject("Restaurants waste food because forecasting is manual")
    )

    validate_plan(plan_with(demand=demand, competitors=competitors))


def test_wording_with_no_searchable_nouns_is_refused() -> None:
    with pytest.raises(InvestigationPlanRejectedError):
        TemplateWebQueryGenerator().generate_web_queries(subject("it is a thing"))


def test_the_industry_broadens_a_thin_problem() -> None:
    demand, _ = TemplateWebQueryGenerator().generate_web_queries(
        subject("scheduling", "Healthcare")
    )
    assert any("healthcare" in query for query in demand)


# -- locale -----------------------------------------------------------------


def test_the_default_locale_is_us_english() -> None:
    assert locale_for(subject("Restaurants waste food")) == DEFAULT_LOCALE


@pytest.mark.parametrize(
    "problem",
    [
        "GST compliance for small Indian exporters",
        "UPI reconciliation is manual",
        "India logistics booking is broken",
    ],
)
def test_an_investigation_that_names_india_is_searched_from_india(
    problem: str,
) -> None:
    """The one case the acquisition pilot earned."""
    assert locale_for(subject(problem)) == INDIA_LOCALE


def test_the_industry_can_carry_the_india_signal() -> None:
    assert locale_for(subject("compliance is manual", "Indian exports")) == (
        INDIA_LOCALE
    )


def test_the_locale_is_never_inferred_from_anything_but_the_text() -> None:
    """No IP, no browser locale, no timezone -- so two identical
    investigations cannot return different evidence."""
    one = subject("Restaurants waste food")
    two = subject("Restaurants waste food")

    assert locale_for(one) == locale_for(two) == DEFAULT_LOCALE


# -- the whole plan ---------------------------------------------------------


def test_a_plan_is_built_for_a_real_investigation(
    db_session: Session, investigation: Investigation
) -> None:
    plan = build_investigation_plan(investigation)

    assert len(plan.research_queries) == 3
    assert 2 <= len(plan.demand_queries) <= 3
    assert 2 <= len(plan.competitor_queries) <= 3
    assert plan.subject == research_subject_from_investigation(investigation)
    assert plan.rationale


def test_building_a_plan_contacts_nobody(
    db_session: Session, investigation: Investigation, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Validation happens BEFORE any provider is reachable."""

    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("unexpected provider client construction")

    monkeypatch.setattr("openai.OpenAI", refuse)

    build_investigation_plan(investigation)


def test_the_web_fallback_is_only_asked_when_the_template_fails(
    db_session: Session, investigation: Investigation
) -> None:
    class SpyFallback:
        def __init__(self) -> None:
            self.calls = 0

        def generate_web_queries(self, subject_value: ResearchSubject):
            self.calls += 1
            return list(GOOD_DEMAND), list(GOOD_COMPETITORS)

    fallback = SpyFallback()

    build_investigation_plan(investigation, web_fallback=fallback)

    assert fallback.calls == 0


def test_a_fallback_plan_faces_the_same_gate(
    db_session: Session, investigation: Investigation
) -> None:
    """A model cannot buy requests by rephrasing a worthless family."""

    class UnusableTemplate:
        def generate_web_queries(self, subject_value: ResearchSubject):
            raise InvestigationPlanRejectedError("nothing searchable")

    class JunkFallback:
        def generate_web_queries(self, subject_value: ResearchSubject):
            # Near-duplicates, and no competitor markers at all.
            return ["cargo problems", "problems cargo"], ["cargo", "freight"]

    with pytest.raises(InvestigationPlanRejectedError):
        build_investigation_plan(
            investigation,
            web_generator=UnusableTemplate(),
            web_fallback=JunkFallback(),
        )
