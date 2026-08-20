"""One opportunity, pain to matches, with no network anywhere.

The collector is injected, so what these tests exercise is the same code
path a real Bright Data collector will drive -- only the acquisition
implementation differs.
"""

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import (
    OpportunityResearchMatch,
    ResearchPaper,
    ResearchSearchResult,
    ResearchSearchRun,
    Signal,
)
from app.research_intelligence.acquisition import (
    ResearchCollectionError,
    ResearchCollectionResult,
    SequenceResearchCollector,
)
from app.research_intelligence.matching import (
    ConceptOverlapMatcher,
    ResearchMatchPolicy,
    ResearchMatchVerdict,
)
from app.research_intelligence.orchestration import enrich_opportunity_with_research
from app.research_intelligence.query_generation import ConceptQueryGenerator
from app.research_intelligence.schemas import ResearchQueryPlan, ResearchSubject
from app.research_intelligence.service import research_subject_from_signal
from tests.research_intelligence.conftest import (
    arxiv_record_for,
    make_opportunity_signal,
)

CARGO_PROBLEM = "Why is booking cargo vehicles harder than passenger transport?"


class AlwaysMatcher:
    """Judges every candidate at a fixed score. Used to isolate persistence."""

    def __init__(self, score: float = 90.0) -> None:
        self.score = score
        self.judged: list[str] = []

    def judge(
        self, *, subject: ResearchSubject, plan: ResearchQueryPlan, paper: ResearchPaper
    ) -> ResearchMatchVerdict:
        self.judged.append(paper.arxiv_id)
        return ResearchMatchVerdict(
            relevance_score=self.score,
            matched_concepts=plan.concepts[:2],
            match_reason=f"Relevant to {subject.problem}",
            technical_readiness_score=None,
        )


class FailingCollector:
    """Fails on one nominated query and succeeds on the rest."""

    def __init__(self, records: list[dict[str, Any]], *, fail_index: int) -> None:
        self.records = records
        self.fail_index = fail_index
        self.calls = 0

    def search(self, query: str) -> ResearchCollectionResult:
        index = self.calls
        self.calls += 1
        if index == self.fail_index:
            raise ResearchCollectionError(query, "provider timed out")
        return ResearchCollectionResult(
            query=query,
            records=self.records,
            provider_job_id=f"j_{index}",
        )


def cargo_signal(db_session: Session) -> Signal:
    return make_opportunity_signal(db_session, title=CARGO_PROBLEM)


def papers(*arxiv_ids: str) -> list[dict[str, Any]]:
    """Records whose wording overlaps the cargo problem."""
    return [
        arxiv_record_for(
            arxiv_id,
            title="Dynamic vehicle routing for urban freight allocation",
            abstract=(
                "On-demand freight vehicle routing and booking in congested "
                "urban logistics networks."
            ),
        )
        for arxiv_id in arxiv_ids
    ]


def collector_for(
    plan_queries: list[str], record_sets: list[list[dict[str, Any]]]
) -> SequenceResearchCollector:
    return SequenceResearchCollector(
        dict(zip(plan_queries, record_sets, strict=True)), provider_job_id="j_test"
    )


def plan_for(db_session: Session, signal: Signal) -> list[str]:
    subject = research_subject_from_signal(signal)
    return ConceptQueryGenerator().generate(subject).queries


def count(session: Session, model: Any) -> int:
    return session.execute(select(func.count()).select_from(model)).scalar_one()


# -- the happy path ---------------------------------------------------------


def test_three_queries_are_searched_and_recorded(db_session: Session) -> None:
    signal = cargo_signal(db_session)
    queries = plan_for(db_session, signal)
    collector = collector_for(
        queries, [papers("2608.00001"), papers("2608.00002"), papers("2608.00003")]
    )

    result = enrich_opportunity_with_research(
        db_session, signal=signal, collector=collector, matcher=AlwaysMatcher()
    )

    assert len(result.plan.queries) == 3
    assert collector.searched_queries == queries
    assert count(db_session, ResearchSearchRun) == 3
    assert result.failed_queries == []
    assert result.candidate_paper_count == 3


def test_matches_are_persisted_for_accepted_candidates(db_session: Session) -> None:
    signal = cargo_signal(db_session)
    queries = plan_for(db_session, signal)
    collector = collector_for(queries, [papers("2608.00001"), [], []])

    result = enrich_opportunity_with_research(
        db_session, signal=signal, collector=collector, matcher=AlwaysMatcher(90.0)
    )

    assert result.matches_created == 1
    match = db_session.execute(select(OpportunityResearchMatch)).scalar_one()
    assert match.signal_id == signal.id
    assert match.relevance_score == 90.0
    assert match.matched_concepts
    assert match.match_reason


# -- deduplication ----------------------------------------------------------


def test_the_same_paper_across_two_searches_is_one_paper(db_session: Session) -> None:
    signal = cargo_signal(db_session)
    queries = plan_for(db_session, signal)
    shared = papers("2608.00001")
    collector = collector_for(queries, [shared, shared, papers("2608.00002")])

    result = enrich_opportunity_with_research(
        db_session, signal=signal, collector=collector, matcher=AlwaysMatcher()
    )

    assert count(db_session, ResearchPaper) == 2
    assert result.candidate_paper_count == 2
    # Both searches keep their own provenance for the shared paper.
    assert count(db_session, ResearchSearchResult) == 3


def test_a_paper_matched_once_is_not_matched_twice(db_session: Session) -> None:
    signal = cargo_signal(db_session)
    queries = plan_for(db_session, signal)
    shared = papers("2608.00001")
    matcher = AlwaysMatcher()

    enrich_opportunity_with_research(
        db_session,
        signal=signal,
        collector=collector_for(queries, [shared, shared, shared]),
        matcher=matcher,
    )

    assert matcher.judged == ["2608.00001"]
    assert count(db_session, OpportunityResearchMatch) == 1


# -- partial failure --------------------------------------------------------


def test_one_failed_query_does_not_discard_the_others(db_session: Session) -> None:
    """A provider timeout on one search must not cost the other two."""
    signal = cargo_signal(db_session)
    collector = FailingCollector(papers("2608.00001"), fail_index=1)

    result = enrich_opportunity_with_research(
        db_session, signal=signal, collector=collector, matcher=AlwaysMatcher()
    )

    assert len(result.failed_queries) == 1
    assert len(result.queries) == 3
    succeeded = [outcome for outcome in result.queries if outcome.succeeded]
    assert len(succeeded) == 2
    # The failed search is reported, not silently recorded as empty.
    failed = next(outcome for outcome in result.queries if not outcome.succeeded)
    assert failed.search_run_id is None
    assert "timed out" in (failed.error or "")
    assert count(db_session, ResearchSearchRun) == 2
    assert count(db_session, OpportunityResearchMatch) == 1


def test_every_query_failing_still_returns_a_result(db_session: Session) -> None:
    signal = cargo_signal(db_session)
    collector = SequenceResearchCollector({})

    result = enrich_opportunity_with_research(
        db_session, signal=signal, collector=collector, matcher=AlwaysMatcher()
    )

    assert len(result.failed_queries) == 3
    assert result.candidate_paper_count == 0
    assert count(db_session, ResearchSearchRun) == 0
    assert count(db_session, OpportunityResearchMatch) == 0


# -- idempotency ------------------------------------------------------------


def test_re_enriching_updates_verdicts_rather_than_duplicating(
    db_session: Session,
) -> None:
    signal = cargo_signal(db_session)
    queries = plan_for(db_session, signal)
    records = [papers("2608.00001"), papers("2608.00002"), []]

    first = enrich_opportunity_with_research(
        db_session,
        signal=signal,
        collector=collector_for(queries, records),
        matcher=AlwaysMatcher(80.0),
    )
    second = enrich_opportunity_with_research(
        db_session,
        signal=signal,
        collector=collector_for(queries, records),
        matcher=AlwaysMatcher(95.0),
    )

    assert first.matches_created == 2
    assert second.matches_created == 0
    assert second.matches_updated == 2
    assert count(db_session, OpportunityResearchMatch) == 2
    assert count(db_session, ResearchPaper) == 2
    # The verdict was replaced, not stacked.
    scores = {
        match.relevance_score
        for match in db_session.execute(select(OpportunityResearchMatch)).scalars()
    }
    assert scores == {95.0}
    # Searching twice really did happen twice.
    assert count(db_session, ResearchSearchRun) == 6


# -- threshold --------------------------------------------------------------


def test_a_candidate_below_the_threshold_is_not_persisted(
    db_session: Session,
) -> None:
    signal = cargo_signal(db_session)
    queries = plan_for(db_session, signal)

    result = enrich_opportunity_with_research(
        db_session,
        signal=signal,
        collector=collector_for(queries, [papers("2608.00001"), [], []]),
        matcher=AlwaysMatcher(69.9),
    )

    assert result.matches_created == 0
    assert result.matches_rejected == 1
    assert count(db_session, OpportunityResearchMatch) == 0


def test_the_threshold_is_configurable_per_run(db_session: Session) -> None:
    signal = cargo_signal(db_session)
    queries = plan_for(db_session, signal)

    result = enrich_opportunity_with_research(
        db_session,
        signal=signal,
        collector=collector_for(queries, [papers("2608.00001"), [], []]),
        matcher=AlwaysMatcher(50.0),
        policy=ResearchMatchPolicy(relevance_threshold=40.0),
    )

    assert result.matches_created == 1


def test_a_declined_judgement_is_not_a_rejection(db_session: Session) -> None:
    """Declining to judge is not evidence the paper is irrelevant."""

    class DecliningMatcher:
        def judge(self, **_: Any) -> ResearchMatchVerdict | None:
            return None

    signal = cargo_signal(db_session)
    queries = plan_for(db_session, signal)

    result = enrich_opportunity_with_research(
        db_session,
        signal=signal,
        collector=collector_for(queries, [papers("2608.00001"), [], []]),
        matcher=DecliningMatcher(),
    )

    assert result.judged_paper_count == 1
    assert result.matches_created == 0
    assert result.matches_rejected == 0


# -- the candidate cap ------------------------------------------------------


def test_only_capped_candidates_reach_the_matcher(db_session: Session) -> None:
    """The expensive stage must not see all ~45 search results."""
    signal = cargo_signal(db_session)
    queries = plan_for(db_session, signal)
    matcher = AlwaysMatcher()
    batches = [
        papers(*[f"2608.{index:05d}" for index in range(start, start + 15)])
        for start in (0, 15, 30)
    ]

    result = enrich_opportunity_with_research(
        db_session,
        signal=signal,
        collector=collector_for(queries, batches),
        matcher=matcher,
        policy=ResearchMatchPolicy(candidate_limit=12),
    )

    assert result.candidate_paper_count == 45
    assert result.judged_paper_count == 12
    assert len(matcher.judged) == 12


# -- provenance -------------------------------------------------------------


def test_search_provenance_is_preserved(db_session: Session) -> None:
    signal = cargo_signal(db_session)
    queries = plan_for(db_session, signal)

    enrich_opportunity_with_research(
        db_session,
        signal=signal,
        collector=collector_for(queries, [papers("2608.00001"), [], []]),
        matcher=AlwaysMatcher(),
    )

    runs = list(db_session.execute(select(ResearchSearchRun)).scalars())
    assert {run.query for run in runs} == set(queries)
    assert all(run.signal_id == signal.id for run in runs)
    assert all(run.provider_job_id == "j_test" for run in runs)


def test_the_transaction_is_a_single_unit(db_session: Session) -> None:
    """A crash mid-enrichment must not leave searches with no matches."""
    signal = cargo_signal(db_session)
    queries = plan_for(db_session, signal)

    enrich_opportunity_with_research(
        db_session,
        signal=signal,
        collector=collector_for(queries, [papers("2608.00001"), [], []]),
        matcher=AlwaysMatcher(),
        commit=False,
    )
    assert count(db_session, ResearchSearchRun) == 3

    db_session.rollback()

    assert count(db_session, ResearchSearchRun) == 0
    assert count(db_session, ResearchPaper) == 0
    assert count(db_session, OpportunityResearchMatch) == 0


# -- the default stack ------------------------------------------------------


def test_the_default_generator_and_matcher_run_end_to_end(
    db_session: Session,
) -> None:
    """No LLM, no provider, no configuration -- and still a real match."""
    signal = cargo_signal(db_session)
    queries = plan_for(db_session, signal)

    result = enrich_opportunity_with_research(
        db_session,
        signal=signal,
        collector=collector_for(queries, [papers("2608.00001"), [], []]),
        matcher=ConceptOverlapMatcher(),
        # The lexical matcher scores well below 70 by design (it is not
        # semantic); the threshold is lowered so this exercises the
        # default stack rather than the matcher's honesty.
        policy=ResearchMatchPolicy(relevance_threshold=5.0),
    )

    assert result.matches_created >= 1
    match = db_session.execute(select(OpportunityResearchMatch)).scalars().first()
    assert match is not None
    # Lexical overlap, reported as measured -- low by design and well
    # under 70. It must still be on the 0-100 band and above the
    # threshold this run deliberately lowered.
    assert 5.0 <= match.relevance_score < 70.0
    assert match.technical_readiness_score is None


# -- semantic matcher integration -------------------------------------------
# The orchestration must behave identically whichever matcher it is given;
# these pin the parts that only matter once a matcher can FAIL per paper.


class FlakyMatcher:
    """Judges some papers and declines others, as a real provider does."""

    def __init__(self, scores: dict[str, float | None]) -> None:
        self.scores = scores
        self.seen: list[str] = []

    def judge(
        self, *, subject: ResearchSubject, plan: ResearchQueryPlan, paper: ResearchPaper
    ) -> ResearchMatchVerdict | None:
        self.seen.append(paper.arxiv_id)
        score = self.scores.get(paper.arxiv_id)
        if score is None:
            return None
        return ResearchMatchVerdict(
            relevance_score=score,
            matched_concepts=["urban freight"],
            match_reason=f"Semantic judgement for {paper.arxiv_id}.",
            technical_readiness_score=None,
        )


def test_one_paper_declining_does_not_discard_the_other_verdicts(
    db_session: Session,
) -> None:
    """A provider failure on one paper must not cost the papers that worked."""
    signal = cargo_signal(db_session)
    queries = plan_for(db_session, signal)
    matcher = FlakyMatcher({"2608.00001": 88.0, "2608.00002": None, "2608.00003": 76.0})

    result = enrich_opportunity_with_research(
        db_session,
        signal=signal,
        collector=collector_for(
            queries, [papers("2608.00001"), papers("2608.00002"), papers("2608.00003")]
        ),
        matcher=matcher,
    )

    assert len(matcher.seen) == 3
    assert result.judged_paper_count == 3
    # The declined paper is neither persisted nor counted as rejected.
    assert result.matches_created == 2
    assert result.matches_rejected == 0
    assert count(db_session, OpportunityResearchMatch) == 2


def test_a_declined_paper_leaves_an_earlier_verdict_untouched(
    db_session: Session,
) -> None:
    """Re-running with a broken provider must not delete what was learned."""
    signal = cargo_signal(db_session)
    queries = plan_for(db_session, signal)
    datasets = [papers("2608.00001"), [], []]

    enrich_opportunity_with_research(
        db_session,
        signal=signal,
        collector=collector_for(queries, datasets),
        matcher=FlakyMatcher({"2608.00001": 91.0}),
    )
    enrich_opportunity_with_research(
        db_session,
        signal=signal,
        collector=collector_for(queries, datasets),
        matcher=FlakyMatcher({"2608.00001": None}),
    )

    match = db_session.execute(select(OpportunityResearchMatch)).scalar_one()
    assert match.relevance_score == 91.0


def test_a_semantic_verdict_replaces_an_earlier_one_for_the_same_pair(
    db_session: Session,
) -> None:
    signal = cargo_signal(db_session)
    queries = plan_for(db_session, signal)
    datasets = [papers("2608.00001"), [], []]

    enrich_opportunity_with_research(
        db_session,
        signal=signal,
        collector=collector_for(queries, datasets),
        matcher=FlakyMatcher({"2608.00001": 74.0}),
    )
    second = enrich_opportunity_with_research(
        db_session,
        signal=signal,
        collector=collector_for(queries, datasets),
        matcher=FlakyMatcher({"2608.00001": 93.0}),
    )

    assert second.matches_created == 0
    assert second.matches_updated == 1
    match = db_session.execute(select(OpportunityResearchMatch)).scalar_one()
    assert match.relevance_score == 93.0
    assert count(db_session, OpportunityResearchMatch) == 1


def test_the_threshold_is_meaningful_with_differentiated_semantic_scores(
    db_session: Session,
) -> None:
    """What the saturated lexical matcher could not demonstrate."""
    signal = cargo_signal(db_session)
    queries = plan_for(db_session, signal)
    matcher = FlakyMatcher({"2608.00001": 92.0, "2608.00002": 71.0, "2608.00003": 44.0})

    result = enrich_opportunity_with_research(
        db_session,
        signal=signal,
        collector=collector_for(
            queries, [papers("2608.00001"), papers("2608.00002"), papers("2608.00003")]
        ),
        matcher=matcher,
    )

    assert result.matches_created == 2
    assert result.matches_rejected == 1
    scores = {
        m.relevance_score
        for m in db_session.execute(select(OpportunityResearchMatch)).scalars()
    }
    assert scores == {92.0, 71.0}
