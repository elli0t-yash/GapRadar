"""A full four-phase run, and what its progress honestly reports.

Every provider is a local object. What is asserted is that each phase's
outcome is derived from what happened rather than declared, and that one
phase failing never discards what another already produced.
"""

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import (
    Investigation,
    InvestigationCompetitor,
    InvestigationDemandEvidence,
    InvestigationResearchMatch,
)
from app.domain.enums import InvestigationRunStatus, PhaseState
from app.investigations.execution import execute_run
from app.investigations.planning import build_investigation_plan
from app.investigations.progress import InvestigationRunPhases
from app.investigations.runs import start_run
from app.research_intelligence.acquisition import SequenceResearchCollector
from app.web_intelligence.acquisition import (
    SequenceWebSearchProvider,
    WebSearchError,
)
from app.web_intelligence.schemas import WebIntelligenceRecord
from tests.investigations.test_execution import AlwaysMatcher, FailingCollector


def web_record(url: str, query: str) -> WebIntelligenceRecord:
    """A page whose wording actually overlaps the investigation's.

    Deliberately on-topic: the default classifiers are lexical, so a page
    about something else is correctly judged IRRELEVANT and stored
    nowhere -- which would make these tests assert the wrong thing.
    """
    return WebIntelligenceRecord(
        query=query,
        title="Booking cargo vehicles is a manual problem for drivers",
        url=url,
        domain=url.split("/")[2],
        snippet=(
            "Shippers booking cargo vehicles struggle with inflated prices "
            "from unorganized drivers; freight software platforms are manual."
        ),
        position=1,
    )


def full_run(
    session: Session,
    investigation: Investigation,
    records: list[dict[str, Any]],
    *,
    web_provider: Any = None,
    web_failures: dict[str, WebSearchError] | None = None,
    collector: Any = None,
):
    """Plan the investigation, then replay every provider it asked for."""
    plan = build_investigation_plan(investigation)

    if collector is None:
        collector = SequenceResearchCollector(
            {query: records for query in plan.research_queries}
        )
    if web_provider is None:
        web_provider = SequenceWebSearchProvider(
            {
                query: [web_record(f"https://a.test/{index}", query)]
                for index, query in enumerate(
                    plan.demand_queries + plan.competitor_queries
                )
            },
            failures=web_failures,
        )

    run, _ = start_run(session, investigation=investigation)
    return (
        execute_run(
            session,
            run_id=run.id,
            collector=collector,
            matcher=AlwaysMatcher(),
            web_provider=web_provider,
            provider_name="fake",
            provider_product="fake_serp",
        ),
        plan,
    )


def phases_of(run) -> InvestigationRunPhases:
    return InvestigationRunPhases.model_validate(run.phases)


def count(session: Session, model) -> int:
    return session.execute(select(func.count()).select_from(model)).scalar_one()


# -- the whole run ----------------------------------------------------------


def test_a_full_run_completes_every_phase(
    db_session: Session, investigation: Investigation, records: list[dict[str, Any]]
) -> None:
    run, _ = full_run(db_session, investigation, records)

    assert run.status is InvestigationRunStatus.SUCCEEDED
    phases = phases_of(run)
    assert phases.planning.state is PhaseState.COMPLETE
    assert phases.research.state is PhaseState.COMPLETE
    assert phases.demand.state is PhaseState.COMPLETE
    assert phases.competitors.state is PhaseState.COMPLETE


def test_planning_progress_reports_the_plan_it_built(
    db_session: Session, investigation: Investigation, records: list[dict[str, Any]]
) -> None:
    run, plan = full_run(db_session, investigation, records)

    planning = phases_of(run).planning
    assert planning.research_queries == len(plan.research_queries)
    assert planning.demand_queries == len(plan.demand_queries)
    assert planning.competitor_queries == len(plan.competitor_queries)


def test_research_progress_carries_the_funnel(
    db_session: Session, investigation: Investigation, records: list[dict[str, Any]]
) -> None:
    run, _ = full_run(db_session, investigation, records)

    research = phases_of(run).research
    assert research.queries_total == research.queries_completed == 3
    assert research.discovered == len(records)
    assert research.matched <= research.judged <= research.selected


def test_the_research_funnel_agrees_with_the_legacy_counters(
    db_session: Session, investigation: Investigation, records: list[dict[str, Any]]
) -> None:
    """Two representations of one fact, written from one snapshot."""
    run, _ = full_run(db_session, investigation, records)

    assert phases_of(run).research.counters.model_dump() == run.counters


def test_web_progress_counts_candidates_and_verdicts(
    db_session: Session, investigation: Investigation, records: list[dict[str, Any]]
) -> None:
    run, plan = full_run(db_session, investigation, records)

    demand = phases_of(run).demand
    assert demand.queries_total == len(plan.demand_queries)
    assert demand.queries_succeeded == demand.queries_total
    assert demand.candidates == len(plan.demand_queries)
    assert demand.judged == demand.candidates
    assert demand.by_classification


def test_a_full_run_persists_all_three_kinds_of_evidence(
    db_session: Session, investigation: Investigation, records: list[dict[str, Any]]
) -> None:
    full_run(db_session, investigation, records)

    assert count(db_session, InvestigationResearchMatch) > 0
    assert count(db_session, InvestigationDemandEvidence) > 0
    assert count(db_session, InvestigationCompetitor) > 0


def test_no_whitespace_or_verdict_phase_is_reported(
    db_session: Session, investigation: Investigation, records: list[dict[str, Any]]
) -> None:
    """A phase reporting 0/0 for work nobody wrote is indistinguishable
    from one that ran and found nothing."""
    run, _ = full_run(db_session, investigation, records)

    assert set(run.phases) == {"planning", "research", "demand", "competitors"}


# -- phase isolation --------------------------------------------------------


def test_no_web_provider_means_skipped_not_failed(
    db_session: Session, investigation: Investigation, records: list[dict[str, Any]]
) -> None:
    """No evidence and no failure -- the honest third state."""
    plan = build_investigation_plan(investigation)
    collector = SequenceResearchCollector(
        {query: records for query in plan.research_queries}
    )
    run, _ = start_run(db_session, investigation=investigation)

    finished = execute_run(
        db_session,
        run_id=run.id,
        collector=collector,
        matcher=AlwaysMatcher(),
        web_provider=None,
    )

    assert finished.status is InvestigationRunStatus.SUCCEEDED
    phases = phases_of(finished)
    assert phases.research.state is PhaseState.COMPLETE
    assert phases.demand.state is PhaseState.SKIPPED
    assert phases.competitors.state is PhaseState.SKIPPED
    assert count(db_session, InvestigationResearchMatch) > 0


def test_every_web_search_failing_does_not_discard_the_research(
    db_session: Session, investigation: Investigation, records: list[dict[str, Any]]
) -> None:
    """Research already spent budget and persisted papers. It is kept."""
    plan = build_investigation_plan(investigation)
    provider = SequenceWebSearchProvider(
        {},
        failures={
            query: WebSearchError(query, "provider refused")
            for query in plan.demand_queries + plan.competitor_queries
        },
    )

    run, _ = full_run(db_session, investigation, records, web_provider=provider)

    assert run.status is InvestigationRunStatus.SUCCEEDED
    phases = phases_of(run)
    assert phases.research.state is PhaseState.COMPLETE
    assert phases.demand.state is PhaseState.FAILED
    assert phases.competitors.state is PhaseState.FAILED
    assert count(db_session, InvestigationResearchMatch) > 0
    assert count(db_session, InvestigationDemandEvidence) == 0


def test_a_partly_failed_family_is_reported_as_partial(
    db_session: Session, investigation: Investigation, records: list[dict[str, Any]]
) -> None:
    plan = build_investigation_plan(investigation)
    failing = plan.demand_queries[0]
    provider = SequenceWebSearchProvider(
        {
            query: [web_record(f"https://a.test/{index}", query)]
            for index, query in enumerate(
                plan.demand_queries[1:] + plan.competitor_queries
            )
        },
        failures={failing: WebSearchError(failing, "provider refused")},
    )

    run, _ = full_run(db_session, investigation, records, web_provider=provider)

    phases = phases_of(run)
    assert phases.demand.state is PhaseState.PARTIAL
    assert phases.competitors.state is PhaseState.COMPLETE
    # The evidence the surviving searches found is kept.
    assert count(db_session, InvestigationDemandEvidence) > 0


def test_a_partly_failed_family_is_stated_in_the_warning(
    db_session: Session, investigation: Investigation, records: list[dict[str, Any]]
) -> None:
    """Set alongside SUCCEEDED, never instead of it."""
    plan = build_investigation_plan(investigation)
    failing = plan.competitor_queries[0]
    provider = SequenceWebSearchProvider(
        {
            query: [web_record(f"https://a.test/{index}", query)]
            for index, query in enumerate(
                plan.demand_queries + plan.competitor_queries[1:]
            )
        },
        failures={failing: WebSearchError(failing, "provider refused")},
    )

    run, _ = full_run(db_session, investigation, records, web_provider=provider)

    assert run.status is InvestigationRunStatus.SUCCEEDED
    assert run.warning and "competitors" in run.warning


def test_a_failed_research_phase_leaves_the_web_phases_pending(
    db_session: Session, investigation: Investigation
) -> None:
    """They never started, so they are not reported as failures."""
    run, _ = full_run(
        db_session, investigation, [], collector=FailingCollector()
    )

    assert run.status is InvestigationRunStatus.FAILED
    phases = phases_of(run)
    assert phases.research.state is PhaseState.FAILED
    assert phases.demand.state is PhaseState.PENDING
    assert phases.competitors.state is PhaseState.PENDING


# -- research is unchanged --------------------------------------------------


def test_research_still_uses_the_arxiv_collector_not_the_serp_provider(
    db_session: Session, investigation: Investigation, records: list[dict[str, Any]]
) -> None:
    """The two acquisition paths are separate ports and stay separate."""
    plan = build_investigation_plan(investigation)
    collector = SequenceResearchCollector(
        {query: records for query in plan.research_queries}
    )
    web_provider = SequenceWebSearchProvider(
        {
            query: [web_record(f"https://a.test/{index}", query)]
            for index, query in enumerate(
                plan.demand_queries + plan.competitor_queries
            )
        }
    )

    full_run(
        db_session,
        investigation,
        records,
        collector=collector,
        web_provider=web_provider,
    )

    # The research collector saw ONLY research queries.
    assert set(collector.searched_queries) == set(plan.research_queries)
    # The web provider saw ONLY web queries.
    assert set(web_provider.searched_queries) == set(
        plan.demand_queries + plan.competitor_queries
    )
    assert not set(web_provider.searched_queries) & set(plan.research_queries)


def test_the_planned_locale_reaches_the_provider(
    db_session: Session, investigation: Investigation, records: list[dict[str, Any]]
) -> None:
    run, plan = full_run(db_session, investigation, records)

    from app.db.models import InvestigationWebSearchRun

    rows = list(db_session.execute(select(InvestigationWebSearchRun)).scalars())
    assert rows
    assert all(row.locale_country == plan.locale.country for row in rows)
    assert all(row.investigation_run_id == run.id for row in rows)
