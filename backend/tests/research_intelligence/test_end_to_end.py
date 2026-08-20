"""The whole research flow for one real opportunity, with no network.

    "Why is booking cargo vehicles harder than passenger transport?"
        -> 3 research queries
        -> replayed arXiv datasets (as Bright Data would deliver them)
        -> ResearchPaper persistence
        -> candidate pre-filter
        -> semantic matcher abstraction
        -> OpportunityResearchMatch rows
        -> GET /api/v1/opportunities/{id}/research

This is the demonstration the stage is judged by. Every provider
interaction is a replay of records in the collector's real output shape,
so what it proves is the backend flow -- not the scraper, which is owned
elsewhere.
"""

from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import (
    OpportunityResearchMatch,
    ResearchPaper,
    ResearchSearchResult,
    ResearchSearchRun,
)
from app.research_intelligence.acquisition import SequenceResearchCollector
from app.research_intelligence.matching import (
    ConceptOverlapMatcher,
    ResearchMatchPolicy,
)
from app.research_intelligence.orchestration import enrich_opportunity_with_research
from app.research_intelligence.query_generation import ConceptQueryGenerator
from app.research_intelligence.service import (
    get_research_intelligence,
    research_subject_from_signal,
)
from tests.research_intelligence.conftest import (
    arxiv_record_for,
    make_opportunity_signal,
)

CARGO_PROBLEM = "Why is booking cargo vehicles harder than passenger transport?"


def record(arxiv_id: str, *, title: str, abstract: str) -> dict[str, Any]:
    return arxiv_record_for(arxiv_id, title=title, abstract=abstract)


# Three plausible result sets, one per query, in the collector's shape.
# The middle paper appears in two of them -- the real case that made
# papers and searches separate tables.
SHARED = record(
    "2608.13083",
    title="Dynamic vehicle routing for on-demand urban freight",
    abstract=(
        "We study booking and allocation of freight vehicles in congested "
        "urban logistics networks, with dynamic pricing under time windows."
    ),
)
ROUTING = record(
    "2607.22582",
    title="Branch and price for the time-dependent vehicle routing problem",
    abstract="Exact optimization for urban vehicle routing with time windows.",
)
FORECASTING = record(
    "2607.10173",
    title="Freight demand forecasting for metropolitan logistics",
    abstract="Forecasting urban freight demand to support fleet allocation.",
)
IRRELEVANT = record(
    "2607.99999",
    title="Protein folding with quantum annealing",
    abstract="Nothing here concerns markets, movement, or logistics.",
)


def test_pain_to_research_intelligence_end_to_end(
    db_session: Session, api_client: TestClient
) -> None:
    # 1. A trusted Opportunity, persisted exactly as the market side writes it.
    signal = make_opportunity_signal(db_session, title=CARGO_PROBLEM)

    # 2. Pain wording is translated into research wording.
    subject = research_subject_from_signal(signal)
    plan = ConceptQueryGenerator().generate(subject)
    assert len(plan.queries) == 3
    assert CARGO_PROBLEM.lower() not in plan.queries
    assert "urban freight" in plan.concepts

    # 3. Acquisition is replayed, not performed. This is exactly the seam
    #    Codex's Bright Data collector will satisfy.
    collector = SequenceResearchCollector(
        {
            plan.queries[0]: [SHARED, IRRELEVANT],
            plan.queries[1]: [ROUTING, SHARED],
            plan.queries[2]: [FORECASTING],
        },
        provider_job_id="d2t1787082186614rfvifpqfsr7o",
    )

    result = enrich_opportunity_with_research(
        db_session,
        signal=signal,
        collector=collector,
        matcher=ConceptOverlapMatcher(),
        policy=ResearchMatchPolicy(relevance_threshold=5.0),
    )

    # 4. Papers persisted once each, despite one appearing in two searches.
    assert collector.searched_queries == plan.queries
    assert result.candidate_paper_count == 4
    assert db_session.execute(select(ResearchPaper)).scalars().all().__len__() == 4
    assert len(db_session.execute(select(ResearchSearchRun)).scalars().all()) == 3
    # Five result rows for four papers: the shared paper has two.
    assert len(db_session.execute(select(ResearchSearchResult)).scalars().all()) == 5

    # 5. The pre-filter dropped the unrelated paper before anything expensive.
    assert result.judged_paper_count == 3

    # 6. Matches persisted for what cleared the threshold.
    matches = list(db_session.execute(select(OpportunityResearchMatch)).scalars())
    assert matches
    assert all(match.signal_id == signal.id for match in matches)
    # Lexical, unscaled: on the band, differentiated, and not saturated.
    assert all(0.0 < match.relevance_score < 70.0 for match in matches)
    assert len({match.relevance_score for match in matches}) > 1
    matched_arxiv_ids = {
        db_session.get(ResearchPaper, match.research_paper_id).arxiv_id
        for match in matches
    }
    assert "2607.99999" not in matched_arxiv_ids

    # 7. The read model assembles it.
    intelligence = get_research_intelligence(db_session, signal_id=signal.id)
    # The shared read model names its subject; the endpoint below is what
    # renders that as `signal_id` for this surface's frozen contract.
    assert intelligence.subject_id == signal.id
    assert len(intelligence.generated_queries) == 3
    assert intelligence.paper_count == 4
    assert intelligence.matched_paper_count == len(matches)
    assert intelligence.average_relevance_score is not None
    assert intelligence.top_concepts

    # 8. And the API serves it, without touching a provider.
    response = api_client.get(f"/api/v1/opportunities/{signal.id}/research")
    assert response.status_code == 200
    body = response.json()
    assert body["signal_id"] == str(signal.id)
    assert body["paper_count"] == 4
    assert body["matched_paper_count"] == len(matches)
    assert len(body["top_papers"]) == len(matches)
    top = body["top_papers"][0]
    assert top["arxiv_id"] in matched_arxiv_ids
    assert top["match_reason"]
    assert top["matched_concepts"]


def test_re_running_the_whole_flow_is_idempotent(
    db_session: Session, api_client: TestClient
) -> None:
    signal = make_opportunity_signal(db_session, title=CARGO_PROBLEM)
    plan = ConceptQueryGenerator().generate(research_subject_from_signal(signal))
    datasets = {
        plan.queries[0]: [SHARED],
        plan.queries[1]: [ROUTING],
        plan.queries[2]: [FORECASTING],
    }

    for _ in range(2):
        enrich_opportunity_with_research(
            db_session,
            signal=signal,
            collector=SequenceResearchCollector(datasets),
            matcher=ConceptOverlapMatcher(),
            policy=ResearchMatchPolicy(relevance_threshold=5.0),
        )

    body = api_client.get(f"/api/v1/opportunities/{signal.id}/research").json()

    # Papers and matches did not double; the searches honestly did.
    assert body["paper_count"] == 3
    assert len(db_session.execute(select(ResearchPaper)).scalars().all()) == 3
    assert len(db_session.execute(select(ResearchSearchRun)).scalars().all()) == 6
    assert len(body["generated_queries"]) == 3
