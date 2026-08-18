"""One opportunity, end to end: pain -> queries -> papers -> matches.

    Signal
      -> generate 3 research queries        (query_generation)
      -> collector.search(query) x3         (acquisition, injected)
      -> ingest_arxiv_search_results        (service)
      -> dedupe candidate papers
      -> rank_candidates                    (candidates, cheap)
      -> matcher.judge                      (matching, expensive)
      -> OpportunityResearchMatch rows      (upserted)

Every expensive or external step arrives as a parameter -- the collector,
the generator, the matcher -- so this module names no provider and no
vendor, and the whole flow runs in a unit test with no network.

Nothing here schedules anything. One opportunity, when asked.
"""

import logging
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import (
    OpportunityResearchMatch,
    ResearchPaper,
    Signal,
)
from app.research_intelligence.acquisition import (
    ResearchCollectionError,
    ResearchCollector,
)
from app.research_intelligence.candidates import RankedCandidate, rank_candidates
from app.research_intelligence.matching import (
    DEFAULT_MATCH_POLICY,
    ConceptOverlapMatcher,
    ResearchMatchPolicy,
    ResearchMatchVerdict,
    SemanticMatcher,
)
from app.research_intelligence.query_generation import (
    ConceptQueryGenerator,
    ResearchQueryGenerator,
)
from app.research_intelligence.schemas import MarketContext, ResearchQueryPlan
from app.research_intelligence.service import (
    ingest_arxiv_search_results,
    market_context_from_signal,
)

logger = logging.getLogger(__name__)


class ResearchQueryOutcome(BaseModel):
    """What one of the three searches actually did."""

    model_config = ConfigDict(frozen=True)

    query: str
    # None when the search failed before a run could be recorded.
    search_run_id: uuid.UUID | None = None
    papers_returned: int = 0
    papers_created: int = 0
    # Present only on failure. A failed search is reported, never hidden,
    # and never silently treated as a search that found nothing.
    error: str | None = None

    @property
    def succeeded(self) -> bool:
        return self.error is None


class ResearchEnrichmentResult(BaseModel):
    """The outcome of enriching one opportunity."""

    model_config = ConfigDict(frozen=True)

    signal_id: uuid.UUID
    plan: ResearchQueryPlan
    queries: list[ResearchQueryOutcome] = Field(default_factory=list)
    # Distinct papers across all three searches, after dedupe.
    candidate_paper_count: int = 0
    # Papers that survived the cheap pre-filter and were actually judged.
    judged_paper_count: int = 0
    matches_created: int = 0
    matches_updated: int = 0
    # Judged, but scored below the threshold. Counted so a run that
    # produces no matches is distinguishable from one that judged nothing.
    matches_rejected: int = 0

    @property
    def failed_queries(self) -> list[str]:
        return [outcome.query for outcome in self.queries if not outcome.succeeded]


def enrich_opportunity_with_research(
    session: Session,
    *,
    signal: Signal,
    collector: ResearchCollector,
    generator: ResearchQueryGenerator | None = None,
    matcher: SemanticMatcher | None = None,
    policy: ResearchMatchPolicy = DEFAULT_MATCH_POLICY,
    commit: bool = True,
) -> ResearchEnrichmentResult:
    """Find and judge research for ONE opportunity.

    Args:
        signal: the persisted Signal behind the Opportunity. Trust is the
            CALLER's decision -- this function enriches what it is given,
            and the API layer is where "is this opportunity visible" is
            enforced, exactly as it already is for the Discover feed.
        collector: injected. The only thing that touches a provider.
        generator / matcher: injected, defaulting to the deterministic
            implementations so a caller that has no LLM wired up still
            gets a working, honest pipeline.
        policy: relevance threshold and candidate cap.

    Partial failure is expected, not exceptional: if one of the three
    searches fails, the other two still ingest, rank, judge and persist.
    The failure is recorded on its ResearchQueryOutcome so the caller can
    see the enrichment was incomplete rather than assume it was thin.

    Re-running is safe. Papers upsert by arxiv_id, and match rows upsert
    by (signal_id, research_paper_id), so a second run updates verdicts
    instead of stacking duplicates. Each run does record new
    ResearchSearchRun rows -- searching twice really did happen twice,
    and collapsing that would destroy the provenance.
    """
    generator = generator or ConceptQueryGenerator()
    matcher = matcher or ConceptOverlapMatcher()

    context = market_context_from_signal(signal)
    plan = generator.generate(context)

    outcomes, paper_ids = _run_searches(
        session, plan=plan, signal_id=signal.id, collector=collector
    )
    papers = _load_papers(session, paper_ids)

    candidates = rank_candidates(context, plan, papers, limit=policy.candidate_limit)
    created, updated, rejected = _judge_and_persist(
        session,
        signal_id=signal.id,
        context=context,
        plan=plan,
        candidates=candidates,
        papers={paper.id: paper for paper in papers},
        matcher=matcher,
        policy=policy,
    )

    if commit:
        session.commit()

    result = ResearchEnrichmentResult(
        signal_id=signal.id,
        plan=plan,
        queries=outcomes,
        candidate_paper_count=len(papers),
        judged_paper_count=len(candidates),
        matches_created=created,
        matches_updated=updated,
        matches_rejected=rejected,
    )
    logger.info(
        "opportunity_research_enriched",
        extra={
            "signal_id": str(signal.id),
            "queries": plan.queries,
            "failed_queries": result.failed_queries,
            "candidate_papers": result.candidate_paper_count,
            "judged_papers": result.judged_paper_count,
            "matches_created": created,
            "matches_updated": updated,
            "matches_rejected": rejected,
        },
    )
    return result


def _run_searches(
    session: Session,
    *,
    plan: ResearchQueryPlan,
    signal_id: uuid.UUID,
    collector: ResearchCollector,
) -> tuple[list[ResearchQueryOutcome], list[uuid.UUID]]:
    """Run each query, ingest what came back, and keep going on failure.

    Paper ids are collected across all searches and deduped, so the same
    paper found by two queries is one candidate with two provenance
    trails -- which is the whole reason searches and papers are separate
    tables.
    """
    outcomes: list[ResearchQueryOutcome] = []
    paper_ids: list[uuid.UUID] = []
    seen: set[uuid.UUID] = set()

    for query in plan.queries:
        try:
            collected = collector.search(query)
        except ResearchCollectionError as exc:
            # Reported, never hidden, and never allowed to discard the
            # searches that worked.
            logger.warning(
                "research_search_failed",
                extra={"signal_id": str(signal_id), "query": query},
            )
            outcomes.append(ResearchQueryOutcome(query=query, error=exc.message))
            continue

        ingestion = ingest_arxiv_search_results(
            session,
            query=query,
            records=collected.records,
            signal_id=signal_id,
            searched_at=collected.searched_at or datetime.now(UTC),
            provider_job_id=collected.provider_job_id,
            # One transaction for the whole enrichment: a crash halfway
            # through must not leave three searches recorded and no
            # matches.
            commit=False,
        )
        for paper_id in ingestion.research_paper_ids:
            if paper_id not in seen:
                seen.add(paper_id)
                paper_ids.append(paper_id)

        outcomes.append(
            ResearchQueryOutcome(
                query=query,
                search_run_id=ingestion.search_run_id,
                papers_returned=len(ingestion.research_paper_ids),
                papers_created=ingestion.created,
            )
        )

    return outcomes, paper_ids


def _load_papers(
    session: Session, paper_ids: Sequence[uuid.UUID]
) -> list[ResearchPaper]:
    if not paper_ids:
        return []
    papers = list(
        session.execute(
            select(ResearchPaper).where(ResearchPaper.id.in_(paper_ids))
        ).scalars()
    )
    # Preserve first-seen order so ranking ties break the same way on
    # every run regardless of what the database returns.
    order = {paper_id: index for index, paper_id in enumerate(paper_ids)}
    papers.sort(key=lambda paper: order[paper.id])
    return papers


def _judge_and_persist(
    session: Session,
    *,
    signal_id: uuid.UUID,
    context: MarketContext,
    plan: ResearchQueryPlan,
    candidates: list[RankedCandidate],
    papers: dict[uuid.UUID, ResearchPaper],
    matcher: SemanticMatcher,
    policy: ResearchMatchPolicy,
) -> tuple[int, int, int]:
    """Judge each surviving candidate and upsert the accepted verdicts."""
    created = 0
    updated = 0
    rejected = 0

    for candidate in candidates:
        paper = papers[candidate.research_paper_id]
        verdict = matcher.judge(context=context, plan=plan, paper=paper)
        if verdict is None:
            # Declined to judge. Not evidence of irrelevance, so it is
            # not counted as a rejection and nothing is written.
            continue
        if verdict.relevance_score < policy.relevance_threshold:
            rejected += 1
            continue

        if _upsert_match(
            session, signal_id=signal_id, paper_id=paper.id, verdict=verdict
        ):
            created += 1
        else:
            updated += 1

    return created, updated, rejected


def _upsert_match(
    session: Session,
    *,
    signal_id: uuid.UUID,
    paper_id: uuid.UUID,
    verdict: ResearchMatchVerdict,
) -> bool:
    """Write one verdict. Returns True if a row was created.

    One verdict per (opportunity, paper): re-running the matcher replaces
    the previous judgement rather than stacking a second, near-identical
    claim, so "how relevant is this paper to this opportunity" always has
    exactly one answer.
    """
    existing = session.execute(
        select(OpportunityResearchMatch).where(
            OpportunityResearchMatch.signal_id == signal_id,
            OpportunityResearchMatch.research_paper_id == paper_id,
        )
    ).scalar_one_or_none()

    values: dict[str, Any] = {
        "relevance_score": verdict.relevance_score,
        "matched_concepts": list(verdict.matched_concepts),
        "match_reason": verdict.match_reason,
        "technical_readiness_score": verdict.technical_readiness_score,
    }

    if existing is not None:
        for field, value in values.items():
            setattr(existing, field, value)
        session.flush()
        return False

    session.add(
        OpportunityResearchMatch(
            signal_id=signal_id, research_paper_id=paper_id, **values
        )
    )
    session.flush()
    return True
