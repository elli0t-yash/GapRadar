"""Running an investigation end to end, with every provider faked.

NO NETWORK ANYWHERE IN THIS FILE. Acquisition replays the committed
arXiv records through SequenceResearchCollector; the query generator and
the semantic matcher are local objects. A test that reached Bright Data
or OpenAI would fail on the fake, not succeed quietly.

What is under test is the same engine the opportunity path runs. These
tests assert the INVESTIGATION-specific half: that the run record tells
the truth about what happened, and that the research lands under the
investigation rather than under a signal.
"""

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import (
    Investigation,
    InvestigationResearchMatch,
    OpportunityResearchMatch,
    ResearchPaper,
    ResearchSearchRun,
    Signal,
)
from app.domain.enums import (
    InvestigationRunStatus,
    InvestigationStatus,
    ResearchOutcomeReason,
    ResearchSubjectOrigin,
)
from app.investigations.execution import execute_run
from app.investigations.runs import start_run
from app.investigations.subject import research_subject_from_investigation
from app.research_intelligence.acquisition import (
    ResearchCollectionError,
    ResearchCollectionResult,
    SequenceResearchCollector,
)
from app.research_intelligence.matching import (
    ResearchMatchPolicy,
    ResearchMatchVerdict,
)
from app.research_intelligence.query_generation import (
    ConceptQueryGenerator,
    ResearchQueryGenerationError,
)
from app.research_intelligence.schemas import ResearchQueryPlan, ResearchSubject
from app.research_intelligence.service import get_subject_research_intelligence

# -- fakes ------------------------------------------------------------------


class AlwaysMatcher:
    """Judges every paper relevant, so persistence is what is measured."""

    def __init__(self, score: float = 90.0) -> None:
        self.score = score
        self.judged: list[str] = []

    def judge(
        self, *, subject: ResearchSubject, plan: ResearchQueryPlan, paper: ResearchPaper
    ) -> ResearchMatchVerdict | None:
        self.judged.append(paper.arxiv_id)
        return ResearchMatchVerdict(
            relevance_score=self.score,
            matched_concepts=list(plan.concepts[:2]),
            match_reason=f"Relevant to {subject.problem[:40]}",
        )


class NeverMatcher:
    """Judges every paper irrelevant. A verdict, not a failure."""

    def judge(
        self, *, subject: ResearchSubject, plan: ResearchQueryPlan, paper: ResearchPaper
    ) -> ResearchMatchVerdict | None:
        return ResearchMatchVerdict(
            relevance_score=1.0, match_reason="No connection to this problem."
        )


class BrokenMatcher:
    """The judge itself is down: it declines and REPORTS that it failed."""

    def __init__(self) -> None:
        self.failures = 0

    def judge(
        self, *, subject: ResearchSubject, plan: ResearchQueryPlan, paper: ResearchPaper
    ) -> ResearchMatchVerdict | None:
        self.failures += 1
        return None


class FailingCollector:
    """Every search fails before returning anything."""

    def __init__(self) -> None:
        self.searched: list[str] = []

    def search(self, query: str) -> ResearchCollectionResult:
        self.searched.append(query)
        raise ResearchCollectionError(query, "provider refused the job")


class UnplannableGenerator:
    """The deterministic stage has nothing specific enough to search for."""

    def generate(self, subject: ResearchSubject) -> ResearchQueryPlan:
        raise ResearchQueryGenerationError("no research vocabulary")


# -- helpers ----------------------------------------------------------------


def collector_for(
    session: Session, investigation: Investigation, records: list[dict[str, Any]]
) -> SequenceResearchCollector:
    """A replay collector covering exactly this investigation's plan."""
    plan = ConceptQueryGenerator().generate(
        research_subject_from_investigation(investigation)
    )
    return SequenceResearchCollector(
        {query: records for query in plan.queries},
        provider_job_id="j_fake",
        searched_at=datetime(2026, 8, 20, tzinfo=UTC),
    )


def run_investigation(
    session: Session,
    investigation: Investigation,
    *,
    collector: Any,
    matcher: Any = None,
    generator: Any = None,
    fallback_generator: Any = None,
):
    run, _ = start_run(session, investigation=investigation)
    return execute_run(
        session,
        run_id=run.id,
        collector=collector,
        matcher=matcher,
        generator=generator,
        fallback_generator=fallback_generator,
    )


# -- the happy path ---------------------------------------------------------


def test_a_successful_run_persists_research(
    db_session: Session, investigation: Investigation, records: list[dict[str, Any]]
) -> None:
    run = run_investigation(
        db_session,
        investigation,
        collector=collector_for(db_session, investigation, records),
        matcher=AlwaysMatcher(),
    )

    assert run.status is InvestigationRunStatus.SUCCEEDED
    assert run.completed_at is not None
    assert run.error is None
    assert (
        db_session.execute(
            select(func.count()).select_from(InvestigationResearchMatch)
        ).scalar_one()
        > 0
    )


def test_a_successful_run_is_readable_through_the_generic_read_model(
    db_session: Session, investigation: Investigation, records: list[dict[str, Any]]
) -> None:
    """The same shape the opportunity surface returns."""
    run_investigation(
        db_session,
        investigation,
        collector=collector_for(db_session, investigation, records),
        matcher=AlwaysMatcher(),
    )

    intelligence = get_subject_research_intelligence(
        db_session,
        subject_id=investigation.id,
        origin=research_subject_from_investigation(investigation).origin,
    )

    assert intelligence.subject_id == investigation.id
    # The shared read model names its subject and says which kind it is.
    # It carries no `signal_id` at all: no signal produced this, and the
    # opportunity endpoint's frozen contract is a separate model.
    assert intelligence.origin is ResearchSubjectOrigin.INVESTIGATION
    assert not hasattr(intelligence, "signal_id")
    assert intelligence.generated_queries
    assert intelligence.matched_paper_count > 0
    assert intelligence.average_relevance_score is not None
    assert intelligence.top_papers


def test_a_successful_run_reports_factual_counters(
    db_session: Session, investigation: Investigation, records: list[dict[str, Any]]
) -> None:
    """Every counter is research, and every one is measured."""
    run = run_investigation(
        db_session,
        investigation,
        collector=collector_for(db_session, investigation, records),
        matcher=AlwaysMatcher(),
    )

    assert set(run.counters) == {"discovered", "selected", "judged", "matched"}
    assert run.counters["discovered"] == len(records)
    assert run.counters["matched"] <= run.counters["judged"]
    assert run.counters["judged"] <= run.counters["selected"]


def test_a_successful_run_records_every_query(
    db_session: Session, investigation: Investigation, records: list[dict[str, Any]]
) -> None:
    run = run_investigation(
        db_session,
        investigation,
        collector=collector_for(db_session, investigation, records),
        matcher=AlwaysMatcher(),
    )

    assert len(run.query_states) == 3
    assert all(state["status"] == "succeeded" for state in run.query_states)


def test_searches_are_attributed_to_the_investigation_not_a_signal(
    db_session: Session, investigation: Investigation, records: list[dict[str, Any]]
) -> None:
    """Provenance, at the search level."""
    run_investigation(
        db_session,
        investigation,
        collector=collector_for(db_session, investigation, records),
        matcher=AlwaysMatcher(),
    )

    runs = list(db_session.execute(select(ResearchSearchRun)).scalars())
    assert runs
    assert all(r.investigation_id == investigation.id for r in runs)
    assert all(r.signal_id is None for r in runs)


def test_running_an_investigation_creates_no_signal(
    db_session: Session, investigation: Investigation, records: list[dict[str, Any]]
) -> None:
    """The rule the whole phase exists to keep."""
    run_investigation(
        db_session,
        investigation,
        collector=collector_for(db_session, investigation, records),
        matcher=AlwaysMatcher(),
    )

    assert (
        db_session.execute(select(func.count()).select_from(Signal)).scalar_one() == 0
    )
    assert (
        db_session.execute(
            select(func.count()).select_from(OpportunityResearchMatch)
        ).scalar_one()
        == 0
    )


def test_the_investigation_reads_succeeded_afterwards(
    db_session: Session, investigation: Investigation, records: list[dict[str, Any]]
) -> None:
    run_investigation(
        db_session,
        investigation,
        collector=collector_for(db_session, investigation, records),
        matcher=AlwaysMatcher(),
    )
    db_session.refresh(investigation)

    assert investigation.status is InvestigationStatus.SUCCEEDED


def test_re_running_updates_verdicts_rather_than_duplicating(
    db_session: Session, investigation: Investigation, records: list[dict[str, Any]]
) -> None:
    """Idempotent: a stranded run can be superseded without duplication."""
    run_investigation(
        db_session,
        investigation,
        collector=collector_for(db_session, investigation, records),
        matcher=AlwaysMatcher(score=80.0),
    )
    first = db_session.execute(
        select(func.count()).select_from(InvestigationResearchMatch)
    ).scalar_one()
    papers = db_session.execute(
        select(func.count()).select_from(ResearchPaper)
    ).scalar_one()

    run_investigation(
        db_session,
        investigation,
        collector=collector_for(db_session, investigation, records),
        matcher=AlwaysMatcher(score=95.0),
    )

    assert (
        db_session.execute(
            select(func.count()).select_from(InvestigationResearchMatch)
        ).scalar_one()
        == first
    )
    assert (
        db_session.execute(
            select(func.count()).select_from(ResearchPaper)
        ).scalar_one()
        == papers
    )
    scores = set(
        db_session.execute(
            select(InvestigationResearchMatch.relevance_score)
        ).scalars()
    )
    assert scores == {95.0}


# -- honest outcomes --------------------------------------------------------


def test_zero_matches_is_a_success_not_a_failure(
    db_session: Session, investigation: Investigation, records: list[dict[str, Any]]
) -> None:
    """We looked properly and found nothing above the bar. An answer."""
    run = run_investigation(
        db_session,
        investigation,
        collector=collector_for(db_session, investigation, records),
        matcher=NeverMatcher(),
    )

    assert run.status is InvestigationRunStatus.SUCCEEDED
    assert run.outcome_reason is ResearchOutcomeReason.NO_RELEVANT_RESEARCH
    assert run.counters["judged"] > 0
    assert run.counters["matched"] == 0
    assert not run.outcome_reason.is_retryable


def test_an_unplannable_query_fails_without_spending_anything(
    db_session: Session, investigation: Investigation
) -> None:
    """The gate runs BEFORE the collector, so nothing is bought."""
    collector = FailingCollector()

    run = run_investigation(
        db_session,
        investigation,
        collector=collector,
        generator=UnplannableGenerator(),
    )

    assert run.status is InvestigationRunStatus.FAILED
    assert run.outcome_reason is ResearchOutcomeReason.QUERY_PLAN_UNAVAILABLE
    assert not run.outcome_reason.is_retryable
    assert collector.searched == [], "the collector must never have been reached"


def test_acquisition_failure_is_reported_as_retryable(
    db_session: Session, investigation: Investigation
) -> None:
    run = run_investigation(
        db_session, investigation, collector=FailingCollector(), matcher=AlwaysMatcher()
    )

    assert run.status is InvestigationRunStatus.FAILED
    assert run.outcome_reason is ResearchOutcomeReason.ACQUISITION_FAILED
    assert run.outcome_reason.is_retryable
    assert "search" in (run.error or "")


def test_a_semantic_outage_is_a_failure_not_an_empty_result(
    db_session: Session, investigation: Investigation, records: list[dict[str, Any]]
) -> None:
    """Papers were selected and the judge never answered.

    Reporting that as "no relevant research" would state a verdict
    nobody gave.
    """
    run = run_investigation(
        db_session,
        investigation,
        collector=collector_for(db_session, investigation, records),
        matcher=BrokenMatcher(),
    )

    assert run.status is InvestigationRunStatus.FAILED
    assert run.outcome_reason is ResearchOutcomeReason.SEMANTIC_MATCHING_FAILED
    assert run.outcome_reason.is_retryable
    assert run.counters["selected"] > 0
    assert run.counters["judged"] == 0


def test_a_failed_run_leaves_the_investigation_failed(
    db_session: Session, investigation: Investigation
) -> None:
    run_investigation(db_session, investigation, collector=FailingCollector())
    db_session.refresh(investigation)

    assert investigation.status is InvestigationStatus.FAILED


def test_a_failed_run_can_be_followed_by_another(
    db_session: Session, investigation: Investigation, records: list[dict[str, Any]]
) -> None:
    run_investigation(db_session, investigation, collector=FailingCollector())

    run = run_investigation(
        db_session,
        investigation,
        collector=collector_for(db_session, investigation, records),
        matcher=AlwaysMatcher(),
    )

    assert run.status is InvestigationRunStatus.SUCCEEDED


def test_partial_acquisition_still_succeeds_and_says_so(
    db_session: Session, investigation: Investigation, records: list[dict[str, Any]]
) -> None:
    """One search returning is worth more than nothing from three."""
    plan = ConceptQueryGenerator().generate(
        research_subject_from_investigation(investigation)
    )
    partial = SequenceResearchCollector({plan.queries[0]: records})

    run = run_investigation(
        db_session, investigation, collector=partial, matcher=AlwaysMatcher()
    )

    assert run.status is InvestigationRunStatus.SUCCEEDED
    assert run.outcome_reason is ResearchOutcomeReason.ACQUISITION_PARTIAL
    assert run.warning and "1 of 3" in run.warning
    assert run.counters["matched"] > 0


# -- guards -----------------------------------------------------------------


def test_an_unknown_run_raises(db_session: Session) -> None:
    with pytest.raises(LookupError):
        execute_run(db_session, run_id=uuid.uuid4(), collector=FailingCollector())


def test_a_terminal_run_is_not_re_executed(
    db_session: Session, investigation: Investigation
) -> None:
    """Whatever picks a run up twice must not run it twice."""
    run = run_investigation(db_session, investigation, collector=FailingCollector())
    collector = FailingCollector()

    again = execute_run(db_session, run_id=run.id, collector=collector)

    assert again.status is InvestigationRunStatus.FAILED
    assert collector.searched == []


def test_the_default_policy_is_shared_with_the_opportunity_path() -> None:
    """Not a fork: the same threshold governs both kinds of subject."""
    from app.research_intelligence.matching import DEFAULT_MATCH_POLICY

    assert isinstance(DEFAULT_MATCH_POLICY, ResearchMatchPolicy)


# -- query generation: deterministic first, fallback second ------------------


class SpyFallback:
    """Stands in for the OpenAI query generator. NEVER calls OpenAI.

    Records whether it was asked, which is the only way to prove the
    ordering: the deterministic generator is free and must be tried
    first, and the model must be reached only when it has nothing.
    """

    def __init__(self, plan: ResearchQueryPlan | None = None) -> None:
        self.plan = plan
        self.calls: list[ResearchSubject] = []

    def generate(self, subject: ResearchSubject) -> ResearchQueryPlan:
        self.calls.append(subject)
        if self.plan is None:
            raise ResearchQueryGenerationError("the model had nothing either")
        return self.plan


def test_a_good_deterministic_plan_never_reaches_the_fallback(
    db_session: Session, investigation: Investigation, records: list[dict[str, Any]]
) -> None:
    """The cost control: the model is not asked when it is not needed."""
    fallback = SpyFallback()

    run = run_investigation(
        db_session,
        investigation,
        collector=collector_for(db_session, investigation, records),
        matcher=AlwaysMatcher(),
        fallback_generator=fallback,
    )

    assert run.status is InvestigationRunStatus.SUCCEEDED
    assert fallback.calls == []


def test_the_fallback_rescues_wording_the_lexicon_does_not_know(
    db_session: Session, investigation: Investigation, records: list[dict[str, Any]]
) -> None:
    """A user's phrasing is far likelier to miss the lexicon than a signal's.

    The fallback is handed the SAME ResearchSubject the deterministic
    stage was given -- one generator protocol, both stages.
    """
    rescued = ResearchQueryPlan(
        subject_id=investigation.id,
        queries=["vehicle routing", "freight matching", "dynamic pricing"],
        concepts=["vehicle routing", "matching markets"],
    )
    fallback = SpyFallback(rescued)
    collector = SequenceResearchCollector({q: records for q in rescued.queries})

    run = run_investigation(
        db_session,
        investigation,
        collector=collector,
        matcher=AlwaysMatcher(),
        generator=UnplannableGenerator(),
        fallback_generator=fallback,
    )

    assert run.status is InvestigationRunStatus.SUCCEEDED
    assert len(fallback.calls) == 1
    assert fallback.calls[0].origin is research_subject_from_investigation(
        investigation
    ).origin
    assert [state["query"] for state in run.query_states] == rescued.queries


def test_a_fallback_plan_faces_the_same_quality_gate(
    db_session: Session, investigation: Investigation
) -> None:
    """A model cannot buy provider jobs by rephrasing a worthless plan.

    The gate is deliberately IDENTICAL to the one the opportunity path
    applies -- a junk plan is junk whoever the subject is.
    """
    junk = ResearchQueryPlan(
        subject_id=investigation.id,
        queries=["logistics systems", "logistics systems optimization", "x"],
        concepts=["logistics systems"],
    )
    collector = FailingCollector()

    run = run_investigation(
        db_session,
        investigation,
        collector=collector,
        generator=UnplannableGenerator(),
        fallback_generator=SpyFallback(junk),
    )

    assert run.status is InvestigationRunStatus.FAILED
    assert run.outcome_reason is ResearchOutcomeReason.QUERY_PLAN_UNAVAILABLE
    assert collector.searched == []


def test_no_fallback_configured_is_an_honest_dead_end(
    db_session: Session, investigation: Investigation
) -> None:
    """Without a key the run fails with a truthful reason, never a crash."""
    run = run_investigation(
        db_session,
        investigation,
        collector=FailingCollector(),
        generator=UnplannableGenerator(),
        fallback_generator=None,
    )

    assert run.status is InvestigationRunStatus.FAILED
    assert run.outcome_reason is ResearchOutcomeReason.QUERY_PLAN_UNAVAILABLE
