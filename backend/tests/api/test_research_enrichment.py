"""On-demand research enrichment: claim, dedupe, run, report.

`api_client`'s Bright Data handler raises on any provider call, and the
enrichment scheduler is replaced with a recorder, so a route that spent a
provider job inside the request would FAIL these tests rather than merely
be slow. That is the property the 202 exists to establish.
"""

import threading
import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.db.models import (
    Collector,
    OpportunityResearchMatch,
    ResearchEnrichmentRun,
    ResearchPaper,
    ResearchSearchRun,
    Signal,
    Source,
)
from app.domain.enums import ResearchEnrichmentStatus
from app.research_intelligence.acquisition import (
    ResearchCollectionError,
    ResearchCollectionResult,
    SequenceResearchCollector,
)
from app.research_intelligence.enrichment import (
    ResearchPlanRejectedError,
    active_enrichment,
    execute_enrichment,
    reconcile_stale_enrichments,
    start_enrichment,
    validate_plan,
)
from app.research_intelligence.execution import run_searches_concurrently
from app.research_intelligence.matching import ResearchMatchVerdict
from app.research_intelligence.query_generation import ConceptQueryGenerator
from app.research_intelligence.schemas import ResearchQueryPlan
from app.research_intelligence.service import market_context_from_signal
from tests.opportunity_engine.conftest import make_signal
from tests.opportunity_engine.test_service import open_incident
from tests.research_intelligence.conftest import arxiv_record_for

CARGO = "Why is booking cargo vehicles harder than passenger transport?"


def cargo_signal(db_session: Session, source: Source, run: Any) -> Signal:
    return make_signal(db_session, source, run, title=CARGO, industry="Logistics")


def papers(*arxiv_ids: str) -> list[dict[str, Any]]:
    return [
        arxiv_record_for(
            arxiv_id,
            title="Dynamic vehicle routing for urban freight allocation",
            abstract=(
                "On-demand freight vehicle routing and booking in congested "
                "urban logistics networks under time windows."
            ),
        )
        for arxiv_id in arxiv_ids
    ]


class AlwaysMatcher:
    def judge(self, **_: Any) -> ResearchMatchVerdict:
        return ResearchMatchVerdict(
            relevance_score=88.0,
            matched_concepts=["urban freight"],
            match_reason="Directly addresses on-demand freight allocation.",
            technical_readiness_score=None,
        )


def collector_for(db_session: Session, signal: Signal, *arxiv_ids: str):
    """A replay collector covering exactly this signal's generated plan."""
    plan = ConceptQueryGenerator().generate(market_context_from_signal(signal))
    return SequenceResearchCollector(
        {plan.queries[0]: papers(*arxiv_ids), plan.queries[1]: [], plan.queries[2]: []},
        provider_job_id="j_test",
    )


def count(session: Session, model: Any) -> int:
    return session.execute(select(func.count()).select_from(model)).scalar_one()


# -- the read model, before anything is enriched ---------------------------


def test_an_unenriched_opportunity_reads_empty(
    api_client: TestClient, db_session: Session, source: Source, run: Any
) -> None:
    signal = cargo_signal(db_session, source, run)

    body = api_client.get(f"/api/v1/opportunities/{signal.id}/research").json()

    assert body["generated_queries"] == []
    assert body["paper_count"] == 0
    assert body["matched_paper_count"] == 0
    assert body["average_relevance_score"] is None
    assert body["top_papers"] == []


def test_no_enrichment_job_exists_until_one_is_requested(
    api_client: TestClient, db_session: Session, source: Source, run: Any
) -> None:
    """Opening an opportunity must never create a job."""
    signal = cargo_signal(db_session, source, run)

    api_client.get(f"/api/v1/opportunities/{signal.id}/research")

    response = api_client.get(f"/api/v1/opportunities/{signal.id}/research/enrichment")
    assert response.status_code == 200
    assert response.json() is None
    assert count(db_session, ResearchEnrichmentRun) == 0


# -- POST: claim only -------------------------------------------------------


def test_enrichment_is_accepted_without_touching_a_provider(
    api_client: TestClient,
    db_session: Session,
    source: Source,
    run: Any,
    enrichment_scheduler: Any,
) -> None:
    """202 and a job id, with no Bright Data call inside the request."""
    signal = cargo_signal(db_session, source, run)

    response = api_client.post(f"/api/v1/opportunities/{signal.id}/research/enrich")

    assert response.status_code == 202
    body = response.json()
    assert uuid.UUID(body["enrichment_id"])
    assert body["signal_id"] == str(signal.id)
    assert body["status"] == "queued"
    assert body["already_running"] is False
    assert body["already_enriched"] is False
    # Claimed, and handed to the executor rather than executed inline.
    assert enrichment_scheduler.scheduled == [uuid.UUID(body["enrichment_id"])]


def test_the_claim_is_persisted_before_the_response(
    api_client: TestClient, db_session: Session, source: Source, run: Any
) -> None:
    signal = cargo_signal(db_session, source, run)

    body = api_client.post(
        f"/api/v1/opportunities/{signal.id}/research/enrich"
    ).json()

    job = db_session.get(ResearchEnrichmentRun, uuid.UUID(body["enrichment_id"]))
    assert job is not None
    assert job.status is ResearchEnrichmentStatus.QUEUED
    assert job.signal_id == signal.id
    assert job.started_at is None
    assert job.completed_at is None
    assert job.error is None


def test_a_second_request_joins_the_active_job(
    api_client: TestClient,
    db_session: Session,
    source: Source,
    run: Any,
    enrichment_scheduler: Any,
) -> None:
    """Double-clicking must not buy a second set of provider runs."""
    signal = cargo_signal(db_session, source, run)

    first = api_client.post(f"/api/v1/opportunities/{signal.id}/research/enrich").json()
    second = api_client.post(f"/api/v1/opportunities/{signal.id}/research/enrich")

    assert second.status_code == 202
    body = second.json()
    assert body["enrichment_id"] == first["enrichment_id"]
    assert body["already_running"] is True
    # One claim, one scheduled execution.
    assert enrichment_scheduler.scheduled == [uuid.UUID(first["enrichment_id"])]
    assert count(db_session, ResearchEnrichmentRun) == 1


def test_an_already_enriched_opportunity_is_not_re_enriched(
    api_client: TestClient,
    db_session: Session,
    source: Source,
    run: Any,
    enrichment_scheduler: Any,
) -> None:
    """Existing research must not be recomputed at real provider cost."""
    signal = cargo_signal(db_session, source, run)
    job, _ = start_enrichment(db_session, signal=signal)
    execute_enrichment(
        db_session,
        enrichment_id=job.id,
        collector=collector_for(db_session, signal, "2608.00001"),
        matcher=AlwaysMatcher(),
    )
    assert count(db_session, OpportunityResearchMatch) == 1
    enrichment_scheduler.scheduled.clear()

    response = api_client.post(f"/api/v1/opportunities/{signal.id}/research/enrich")

    assert response.status_code == 202
    body = response.json()
    assert body["already_enriched"] is True
    assert body["already_running"] is False
    # Nothing new scheduled, nothing new claimed.
    assert enrichment_scheduler.scheduled == []
    assert count(db_session, ResearchEnrichmentRun) == 1


# -- trust gating -----------------------------------------------------------


def test_an_untrusted_opportunity_cannot_start_enrichment(
    api_client: TestClient,
    db_session: Session,
    source: Source,
    run: Any,
    collector: Collector,
    enrichment_scheduler: Any,
) -> None:
    """Not a backdoor to spend provider calls on distrusted data."""
    signal = cargo_signal(db_session, source, run)
    open_incident(db_session, collector)

    response = api_client.post(f"/api/v1/opportunities/{signal.id}/research/enrich")

    assert response.status_code == 404
    assert enrichment_scheduler.scheduled == []
    assert count(db_session, ResearchEnrichmentRun) == 0


def test_an_untrusted_opportunity_cannot_read_enrichment_status(
    api_client: TestClient,
    db_session: Session,
    source: Source,
    run: Any,
    collector: Collector,
) -> None:
    signal = cargo_signal(db_session, source, run)
    open_incident(db_session, collector)

    assert (
        api_client.get(
            f"/api/v1/opportunities/{signal.id}/research/enrichment"
        ).status_code
        == 404
    )


def test_an_unknown_opportunity_is_a_404(api_client: TestClient) -> None:
    unknown = uuid.uuid4()

    assert (
        api_client.post(
            f"/api/v1/opportunities/{unknown}/research/enrich"
        ).status_code
        == 404
    )
    assert (
        api_client.get(
            f"/api/v1/opportunities/{unknown}/research/enrichment"
        ).status_code
        == 404
    )


# -- the persisted lifecycle ------------------------------------------------


def test_queued_becomes_running_then_succeeded(
    db_session: Session, source: Source, run: Any
) -> None:
    signal = cargo_signal(db_session, source, run)

    job, already = start_enrichment(db_session, signal=signal)
    assert already is False
    assert job.status is ResearchEnrichmentStatus.QUEUED

    finished = execute_enrichment(
        db_session,
        enrichment_id=job.id,
        collector=collector_for(db_session, signal, "2608.00001"),
        matcher=AlwaysMatcher(),
    )

    assert finished.status is ResearchEnrichmentStatus.SUCCEEDED
    assert finished.started_at is not None
    assert finished.completed_at is not None
    assert finished.error is None


def test_a_provider_failure_is_recorded_as_a_failed_job(
    db_session: Session, source: Source, run: Any
) -> None:
    """Every search failing means the job could not be carried out."""
    signal = cargo_signal(db_session, source, run)
    job, _ = start_enrichment(db_session, signal=signal)

    finished = execute_enrichment(
        db_session,
        enrichment_id=job.id,
        # No recorded results: every search raises ResearchCollectionError.
        collector=SequenceResearchCollector({}),
        matcher=AlwaysMatcher(),
    )

    # EVERY search failing is a failed job, not an empty success. The
    # orchestration still absorbs failures per query -- that is what makes
    # partial results possible -- but an acquisition that returned nothing
    # at all gives the user nothing to look at, so it must be reported as
    # a failure they can retry rather than as "we looked and found none".
    assert finished.status is ResearchEnrichmentStatus.FAILED
    assert finished.error is not None
    assert "all 3 research searches failed" in finished.error
    assert count(db_session, OpportunityResearchMatch) == 0

    # The failure is retryable: nothing active is left blocking the
    # opportunity.
    assert active_enrichment(db_session, signal_id=signal.id) is None


def test_a_matcher_that_always_fails_still_terminates_the_job(
    db_session: Session, source: Source, run: Any
) -> None:
    class ExplodingMatcher:
        def judge(self, **_: Any) -> ResearchMatchVerdict:
            raise RuntimeError("provider exploded")

    signal = cargo_signal(db_session, source, run)
    job, _ = start_enrichment(db_session, signal=signal)

    finished = execute_enrichment(
        db_session,
        enrichment_id=job.id,
        collector=collector_for(db_session, signal, "2608.00001"),
        matcher=ExplodingMatcher(),
    )

    assert finished.status is ResearchEnrichmentStatus.FAILED
    assert finished.completed_at is not None
    assert "provider exploded" in (finished.error or "")


def test_the_status_endpoint_reports_the_persisted_job(
    api_client: TestClient, db_session: Session, source: Source, run: Any
) -> None:
    """Status survives a client reload because it is persisted."""
    signal = cargo_signal(db_session, source, run)
    job, _ = start_enrichment(db_session, signal=signal)

    body = api_client.get(
        f"/api/v1/opportunities/{signal.id}/research/enrichment"
    ).json()

    assert body["enrichment_id"] == str(job.id)
    assert body["signal_id"] == str(signal.id)
    assert body["status"] == "queued"
    assert body["error"] is None


def test_a_failed_job_can_be_retried(
    db_session: Session, source: Source, run: Any
) -> None:
    """A terminal job no longer blocks the active-job constraint."""
    signal = cargo_signal(db_session, source, run)
    first, _ = start_enrichment(db_session, signal=signal)
    execute_enrichment(
        db_session,
        enrichment_id=first.id,
        collector=collector_for(db_session, signal, "2608.00001"),
        matcher=type("Boom", (), {"judge": lambda self, **_: (_ for _ in ()).throw(RuntimeError("x"))})(),
    )
    assert db_session.get(ResearchEnrichmentRun, first.id).status is (
        ResearchEnrichmentStatus.FAILED
    )

    second, already = start_enrichment(db_session, signal=signal)

    assert already is False
    assert second.id != first.id
    assert count(db_session, ResearchEnrichmentRun) == 2


# -- successful enrichment becomes visible through the read model ----------


def test_a_successful_enrichment_shows_up_in_get_research(
    api_client: TestClient, db_session: Session, source: Source, run: Any
) -> None:
    signal = cargo_signal(db_session, source, run)
    before = api_client.get(f"/api/v1/opportunities/{signal.id}/research").json()
    assert before["matched_paper_count"] == 0

    job, _ = start_enrichment(db_session, signal=signal)
    execute_enrichment(
        db_session,
        enrichment_id=job.id,
        collector=collector_for(db_session, signal, "2608.00001", "2608.00002"),
        matcher=AlwaysMatcher(),
    )

    after = api_client.get(f"/api/v1/opportunities/{signal.id}/research").json()
    assert len(after["generated_queries"]) == 3
    assert after["paper_count"] == 2
    assert after["matched_paper_count"] == 2
    assert after["average_relevance_score"] == 88.0
    assert len(after["top_papers"]) == 2


# -- the query-quality gate -------------------------------------------------


def test_a_plan_built_only_from_the_industry_name_is_refused() -> None:
    """The audit's junk shape: no provider job may be spent on it."""
    plan = ResearchQueryPlan(
        signal_id=uuid.uuid4(),
        queries=[
            "travel systems optimization",
            "travel systems demand forecasting",
            "travel systems resource allocation",
        ],
        concepts=["travel systems"],
        rationale="industry fallback only",
    )

    with pytest.raises(ResearchPlanRejectedError, match="industry name alone"):
        validate_plan(plan)


@pytest.mark.parametrize(
    ("queries", "concepts", "match"),
    [
        ([], ["urban freight"], "no research queries"),
        (["   "], ["urban freight"], "blank"),
        (["demand forecasting demand forecasting"], ["demand forecasting"], "repeats"),
        (["urban freight optimization"], [], "no research concepts"),
    ],
)
def test_malformed_plans_are_refused(
    queries: list[str], concepts: list[str], match: str
) -> None:
    plan = ResearchQueryPlan(
        signal_id=uuid.uuid4(), queries=queries, concepts=concepts, rationale=""
    )

    with pytest.raises(ResearchPlanRejectedError, match=match):
        validate_plan(plan)


def test_a_real_plan_passes_the_gate() -> None:
    plan = ResearchQueryPlan(
        signal_id=uuid.uuid4(),
        queries=[
            "on-demand allocation urban freight",
            "urban freight optimization",
            "vehicle routing demand forecasting",
        ],
        concepts=["on-demand allocation", "urban freight", "vehicle routing"],
        rationale="",
    )

    validate_plan(plan)  # does not raise


def test_a_rejected_plan_fails_the_job_without_spending_anything(
    db_session: Session, source: Source, run: Any
) -> None:
    """The gate runs BEFORE the collector is touched."""

    class ExplodingCollector:
        def search(self, query: str) -> Any:
            raise AssertionError(f"provider must not be called: {query}")

    signal = make_signal(
        db_session,
        source,
        run,
        title="How can travellers find trusted local help?",
        industry="Travel",
    )
    job, _ = start_enrichment(db_session, signal=signal)

    finished = execute_enrichment(
        db_session,
        enrichment_id=job.id,
        collector=ExplodingCollector(),
        matcher=AlwaysMatcher(),
    )

    assert finished.status is ResearchEnrichmentStatus.FAILED
    assert finished.error
    assert count(db_session, ResearchSearchRun) == 0
    assert count(db_session, ResearchPaper) == 0


# -- GET stays provider-free ------------------------------------------------


def test_reading_research_and_status_never_starts_work(
    api_client: TestClient,
    db_session: Session,
    source: Source,
    run: Any,
    enrichment_scheduler: Any,
) -> None:
    """A GET that scraped would make page loads cost money."""
    signal = cargo_signal(db_session, source, run)

    for _ in range(3):
        assert (
            api_client.get(f"/api/v1/opportunities/{signal.id}/research").status_code
            == 200
        )
        assert (
            api_client.get(
                f"/api/v1/opportunities/{signal.id}/research/enrichment"
            ).status_code
            == 200
        )

    assert enrichment_scheduler.scheduled == []
    assert count(db_session, ResearchEnrichmentRun) == 0
    assert count(db_session, ResearchSearchRun) == 0
    assert count(db_session, ResearchPaper) == 0
    assert count(db_session, OpportunityResearchMatch) == 0


# -- partial results, bounded time, and provider spend ----------------------
#
# The scenarios below are the ones a 14-minute run made urgent: one slow
# Bright Data job must never be able to discard the papers the other
# searches already returned, and must never be able to hold the UI open
# indefinitely.


class SlowCollector:
    """A collector where some queries hang past the acquisition budget.

    `hang` queries sleep well beyond the budget the test allows, standing
    in for a Scraper Studio job stuck at 1/16 pages. Nothing is mocked at
    the HTTP layer: the runner's own bounded wait is what is under test.
    """

    def __init__(
        self,
        results: dict[str, list[dict[str, Any]]],
        *,
        hang: set[str] | None = None,
        fail: set[str] | None = None,
        hang_seconds: float = 5.0,
    ) -> None:
        self._results = results
        self._hang = hang or set()
        self._fail = fail or set()
        self._hang_seconds = hang_seconds
        self.searched: list[str] = []
        self._lock = threading.Lock()

    def search(self, query: str) -> ResearchCollectionResult:
        with self._lock:
            self.searched.append(query)
        if query in self._hang:
            time.sleep(self._hang_seconds)
            raise ResearchCollectionError(
                query, f"local wait of {self._hang_seconds}s elapsed while job ran"
            )
        if query in self._fail:
            raise ResearchCollectionError(query, "provider refused the search")
        return ResearchCollectionResult(
            query=query,
            records=self._results.get(query, []),
            provider_job_id=f"j_{abs(hash(query)) % 10**8}",
        )


def plan_for(db_session: Session, signal: Signal) -> ResearchQueryPlan:
    return ConceptQueryGenerator().generate(market_context_from_signal(signal))


def test_three_successful_queries_succeed_with_no_warning(
    db_session: Session, source: Source, run: Any
) -> None:
    signal = cargo_signal(db_session, source, run)
    plan = plan_for(db_session, signal)
    job, _ = start_enrichment(db_session, signal=signal)

    finished = execute_enrichment(
        db_session,
        enrichment_id=job.id,
        collector=SlowCollector(
            {
                plan.queries[0]: papers("2608.10001"),
                plan.queries[1]: papers("2608.10002"),
                plan.queries[2]: papers("2608.10003"),
            }
        ),
        matcher=AlwaysMatcher(),
    )

    assert finished.status is ResearchEnrichmentStatus.SUCCEEDED
    assert finished.warning is None
    assert count(db_session, OpportunityResearchMatch) == 3
    assert [state["status"] for state in finished.query_states] == ["succeeded"] * 3


def test_one_timed_out_query_does_not_discard_the_other_two(
    db_session: Session, source: Source, run: Any
) -> None:
    """The whole point: 2 good searches beat 0 because the 3rd hung."""
    signal = cargo_signal(db_session, source, run)
    plan = plan_for(db_session, signal)
    job, _ = start_enrichment(db_session, signal=signal)

    started = time.monotonic()
    finished = execute_enrichment(
        db_session,
        enrichment_id=job.id,
        collector=SlowCollector(
            {
                plan.queries[0]: papers("2608.20001", "2608.20002"),
                plan.queries[1]: papers("2608.20003"),
            },
            hang={plan.queries[2]},
            hang_seconds=30.0,
        ),
        matcher=AlwaysMatcher(),
        acquisition_budget_seconds=2.0,
    )
    elapsed = time.monotonic() - started

    # Usable research persisted, and the run completed.
    assert finished.status is ResearchEnrichmentStatus.SUCCEEDED
    assert count(db_session, OpportunityResearchMatch) == 3

    # The warning is retained and names the shortfall honestly.
    assert finished.warning is not None
    assert "2 of 3" in finished.warning

    # BOUNDED: the 30s hang did not become the enrichment's duration.
    assert elapsed < 15.0

    states = {state["query"]: state["status"] for state in finished.query_states}
    assert states[plan.queries[0]] == "succeeded"
    assert states[plan.queries[1]] == "succeeded"
    assert states[plan.queries[2]] == "timed_out"


def test_one_successful_and_two_failed_still_persists_research(
    db_session: Session, source: Source, run: Any
) -> None:
    signal = cargo_signal(db_session, source, run)
    plan = plan_for(db_session, signal)
    job, _ = start_enrichment(db_session, signal=signal)

    finished = execute_enrichment(
        db_session,
        enrichment_id=job.id,
        collector=SlowCollector(
            {plan.queries[0]: papers("2608.30001")},
            fail={plan.queries[1], plan.queries[2]},
        ),
        matcher=AlwaysMatcher(),
    )

    assert finished.status is ResearchEnrichmentStatus.SUCCEEDED
    assert finished.warning is not None
    assert "1 of 3" in finished.warning
    assert count(db_session, OpportunityResearchMatch) == 1


def test_all_three_timing_out_is_a_retryable_failure(
    db_session: Session, source: Source, run: Any
) -> None:
    signal = cargo_signal(db_session, source, run)
    plan = plan_for(db_session, signal)
    job, _ = start_enrichment(db_session, signal=signal)

    finished = execute_enrichment(
        db_session,
        enrichment_id=job.id,
        collector=SlowCollector({}, hang=set(plan.queries), hang_seconds=30.0),
        matcher=AlwaysMatcher(),
        acquisition_budget_seconds=2.0,
    )

    assert finished.status is ResearchEnrichmentStatus.FAILED
    assert finished.error is not None
    assert "timed out" in finished.error
    assert count(db_session, OpportunityResearchMatch) == 0
    # Retryable: no active row is left blocking the opportunity.
    assert active_enrichment(db_session, signal_id=signal.id) is None


def test_the_acquisition_is_concurrent_not_sequential(
    db_session: Session, source: Source, run: Any
) -> None:
    """Three 1s searches must cost ~1s, not ~3s.

    This is the regression guard for the original defect: strictly
    sequential acquisition made one enrichment take 14 minutes.
    """
    signal = cargo_signal(db_session, source, run)
    plan = plan_for(db_session, signal)

    class SleepyCollector:
        def search(self, query: str) -> ResearchCollectionResult:
            time.sleep(1.0)
            return ResearchCollectionResult(query=query, records=[])

    started = time.monotonic()
    executions = run_searches_concurrently(
        plan.queries, collector=SleepyCollector(), total_budget_seconds=10.0
    )
    elapsed = time.monotonic() - started

    assert len(executions) == 3
    assert all(execution.succeeded for execution in executions)
    # Sequential would be >= 3s. Generous bound so a loaded CI box does
    # not make this flaky, while still failing a serial implementation.
    assert elapsed < 2.5


def test_query_states_preserve_plan_order_regardless_of_finish_order(
    db_session: Session, source: Source, run: Any
) -> None:
    """Concurrency must not make ingestion order nondeterministic."""
    signal = cargo_signal(db_session, source, run)
    plan = plan_for(db_session, signal)

    class StaggeredCollector:
        def search(self, query: str) -> ResearchCollectionResult:
            # The LAST query finishes first.
            time.sleep(0.3 if query != plan.queries[2] else 0.01)
            return ResearchCollectionResult(query=query, records=[])

    executions = run_searches_concurrently(
        plan.queries, collector=StaggeredCollector(), total_budget_seconds=10.0
    )

    assert [execution.query for execution in executions] == list(plan.queries)


# -- provider spend: one opportunity vs several -----------------------------


def test_one_opportunity_clicked_twice_runs_three_searches_not_six(
    api_client: TestClient,
    db_session: Session,
    source: Source,
    run: Any,
    enrichment_scheduler: Any,
) -> None:
    """The distinction that matters for the bill."""
    signal = cargo_signal(db_session, source, run)

    first = api_client.post(f"/api/v1/opportunities/{signal.id}/research/enrich").json()
    second = api_client.post(f"/api/v1/opportunities/{signal.id}/research/enrich").json()

    assert first["enrichment_id"] == second["enrichment_id"]
    assert second["already_running"] is True
    assert count(db_session, ResearchEnrichmentRun) == 1
    # ONE execution scheduled, so ONE plan of three searches.
    assert len(enrichment_scheduler.scheduled) == 1

    plan = plan_for(db_session, signal)
    collector = SlowCollector({query: [] for query in plan.queries})
    execute_enrichment(
        db_session,
        enrichment_id=uuid.UUID(first["enrichment_id"]),
        collector=collector,
        matcher=AlwaysMatcher(),
    )
    assert len(collector.searched) == 3


def test_two_different_opportunities_run_three_searches_each(
    api_client: TestClient,
    db_session: Session,
    source: Source,
    run: Any,
    enrichment_scheduler: Any,
) -> None:
    """Six provider jobs across two opportunities is CORRECT, not a bug."""
    first_signal = cargo_signal(db_session, source, run)
    second_signal = make_signal(
        db_session,
        source,
        run,
        title="Why is last-mile delivery scheduling unreliable in dense cities?",
        industry="Logistics",
    )

    for signal in (first_signal, second_signal):
        response = api_client.post(
            f"/api/v1/opportunities/{signal.id}/research/enrich"
        )
        assert response.status_code == 202
        assert response.json()["already_running"] is False

    # Two independent enrichments, two scheduled executions.
    assert count(db_session, ResearchEnrichmentRun) == 2
    assert len(enrichment_scheduler.scheduled) == 2

    total_searches = 0
    for signal in (first_signal, second_signal):
        job = active_enrichment(db_session, signal_id=signal.id)
        assert job is not None
        plan = plan_for(db_session, signal)
        collector = SlowCollector({query: [] for query in plan.queries})
        execute_enrichment(
            db_session,
            enrichment_id=job.id,
            collector=collector,
            matcher=AlwaysMatcher(),
        )
        total_searches += len(collector.searched)

    assert total_searches == 6


# -- surviving a backend restart -------------------------------------------


def test_a_stranded_running_job_is_reconciled_and_retryable(
    db_session: Session, source: Source, run: Any
) -> None:
    """A killed worker must not disable the feature for that opportunity.

    The active-job index that prevents duplicate spend is exactly what
    makes a stranded RUNNING row dangerous: without reconciliation the
    opportunity could never be enriched again.
    """
    signal = cargo_signal(db_session, source, run)
    job, _ = start_enrichment(db_session, signal=signal)
    job.status = ResearchEnrichmentStatus.RUNNING
    job.started_at = datetime.now(UTC) - timedelta(hours=2)
    # Reach past the model default to simulate a row created long ago.
    db_session.execute(
        update(ResearchEnrichmentRun)
        .where(ResearchEnrichmentRun.id == job.id)
        .values(created_at=datetime.now(UTC) - timedelta(hours=2))
    )
    db_session.commit()

    # Blocked while it looks active.
    assert active_enrichment(db_session, signal_id=signal.id) is not None

    reconciled = reconcile_stale_enrichments(db_session)

    assert reconciled == 1
    db_session.refresh(job)
    assert job.status is ResearchEnrichmentStatus.FAILED
    assert job.error is not None and "restart" in job.error
    # And the opportunity can be analysed again.
    assert active_enrichment(db_session, signal_id=signal.id) is None
    retry, already_running = start_enrichment(db_session, signal=signal)
    assert already_running is False
    assert retry.id != job.id


def test_reconciliation_never_touches_a_fresh_running_job(
    db_session: Session, source: Source, run: Any
) -> None:
    """A genuinely slow enrichment must survive a status poll."""
    signal = cargo_signal(db_session, source, run)
    job, _ = start_enrichment(db_session, signal=signal)
    job.status = ResearchEnrichmentStatus.RUNNING
    db_session.commit()

    assert reconcile_stale_enrichments(db_session) == 0

    db_session.refresh(job)
    assert job.status is ResearchEnrichmentStatus.RUNNING
