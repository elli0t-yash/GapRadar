"""GET /opportunities/{signal_id}/research: read-only, and trust-gated.

`api_client`'s provider handler raises on any Bright Data call, so a GET
that triggered acquisition would fail these tests rather than merely be
slow. That is the property this surface exists to guarantee.
"""

import uuid
from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.db.models import Collector, Signal, Source
from app.research_intelligence.acquisition import SequenceResearchCollector
from app.research_intelligence.matching import ConceptOverlapMatcher
from app.research_intelligence.orchestration import enrich_opportunity_with_research
from app.research_intelligence.query_generation import ConceptQueryGenerator
from app.research_intelligence.service import market_context_from_signal
from tests.opportunity_engine.conftest import make_signal
from tests.opportunity_engine.test_service import open_incident
from tests.research_intelligence.conftest import arxiv_record_for

CARGO_PROBLEM = "Why is booking cargo vehicles harder than passenger transport?"


def cargo_papers(*arxiv_ids: str) -> list[dict[str, Any]]:
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


def enrich(db_session: Session, signal: Signal, *arxiv_ids: str) -> None:
    """Run enrichment directly -- never through the API, which cannot."""
    queries = (
        ConceptQueryGenerator().generate(market_context_from_signal(signal)).queries
    )
    collector = SequenceResearchCollector(
        {
            queries[0]: cargo_papers(*arxiv_ids),
            queries[1]: [],
            queries[2]: [],
        },
        provider_job_id="j_demo",
    )
    enrich_opportunity_with_research(
        db_session,
        signal=signal,
        collector=collector,
        matcher=ConceptOverlapMatcher(scale=6.0),
    )


def cargo_signal(db_session: Session, source: Source, run: Any) -> Signal:
    return make_signal(
        db_session, source, run, title=CARGO_PROBLEM, industry="Logistics"
    )


# -- the enriched result ----------------------------------------------------


def test_a_trusted_opportunity_returns_its_research_intelligence(
    api_client: TestClient, db_session: Session, source: Source, run: Any
) -> None:
    signal = cargo_signal(db_session, source, run)
    enrich(db_session, signal, "2608.00001", "2608.00002")

    response = api_client.get(f"/api/v1/opportunities/{signal.id}/research")

    assert response.status_code == 200
    body = response.json()
    assert body["signal_id"] == str(signal.id)
    assert len(body["generated_queries"]) == 3
    assert body["paper_count"] == 2
    assert body["matched_paper_count"] >= 1
    assert body["average_relevance_score"] is not None
    assert body["top_concepts"]
    assert body["top_papers"]


def test_a_top_paper_carries_everything_a_ui_needs(
    api_client: TestClient, db_session: Session, source: Source, run: Any
) -> None:
    signal = cargo_signal(db_session, source, run)
    enrich(db_session, signal, "2608.00001")

    paper = api_client.get(f"/api/v1/opportunities/{signal.id}/research").json()[
        "top_papers"
    ][0]

    assert paper["arxiv_id"] == "2608.00001"
    assert paper["title"]
    assert paper["abstract"]
    assert paper["abstract_preview"]
    assert paper["authors"]
    assert paper["categories"]
    assert paper["published_at"] == "2026-08-13"
    assert paper["paper_url"].startswith("https://arxiv.org/abs/")
    assert paper["pdf_url"].startswith("https://arxiv.org/pdf/")
    assert 70.0 <= paper["relevance_score"] <= 100.0
    assert paper["matched_concepts"]
    assert paper["match_reason"]
    # Never invented by a lexical matcher.
    assert paper["technical_readiness_score"] is None


def test_papers_are_returned_best_first(
    api_client: TestClient, db_session: Session, source: Source, run: Any
) -> None:
    signal = cargo_signal(db_session, source, run)
    enrich(db_session, signal, "2608.00001", "2608.00002", "2608.00003")

    scores = [
        paper["relevance_score"]
        for paper in api_client.get(
            f"/api/v1/opportunities/{signal.id}/research"
        ).json()["top_papers"]
    ]

    assert scores == sorted(scores, reverse=True)


# -- the empty case ---------------------------------------------------------


def test_an_unenriched_opportunity_returns_an_empty_but_valid_result(
    api_client: TestClient, db_session: Session, source: Source, run: Any
) -> None:
    """No enrichment is a 200 with nothing in it, not a 404 and not work."""
    signal = cargo_signal(db_session, source, run)

    response = api_client.get(f"/api/v1/opportunities/{signal.id}/research")

    assert response.status_code == 200
    body = response.json()
    assert body["generated_queries"] == []
    assert body["paper_count"] == 0
    assert body["matched_paper_count"] == 0
    # An average of nothing is not zero.
    assert body["average_relevance_score"] is None
    assert body["top_papers"] == []


def test_searched_but_unmatched_is_distinguishable_from_never_searched(
    api_client: TestClient, db_session: Session, source: Source, run: Any
) -> None:
    signal = cargo_signal(db_session, source, run)
    queries = (
        ConceptQueryGenerator().generate(market_context_from_signal(signal)).queries
    )
    enrich_opportunity_with_research(
        db_session,
        signal=signal,
        collector=SequenceResearchCollector({query: [] for query in queries}),
        matcher=ConceptOverlapMatcher(),
    )

    body = api_client.get(f"/api/v1/opportunities/{signal.id}/research").json()

    assert len(body["generated_queries"]) == 3
    assert body["paper_count"] == 0
    assert body["matched_paper_count"] == 0


# -- trust and 404 ----------------------------------------------------------


def test_an_untrusted_opportunity_cannot_reach_its_research(
    api_client: TestClient,
    db_session: Session,
    source: Source,
    run: Any,
    collector: Collector,
) -> None:
    """No backdoor: the trust gate is the same one the feed applies.

    The signal is enriched FIRST, so this proves the 404 comes from the
    trust check and not merely from an absence of data.
    """
    signal = cargo_signal(db_session, source, run)
    enrich(db_session, signal, "2608.00001")
    assert (
        api_client.get(f"/api/v1/opportunities/{signal.id}/research").status_code == 200
    )

    open_incident(db_session, collector)

    response = api_client.get(f"/api/v1/opportunities/{signal.id}/research")

    assert response.status_code == 404
    assert "not found" in response.json()["detail"]


def test_an_unknown_signal_is_a_404(api_client: TestClient) -> None:
    response = api_client.get(f"/api/v1/opportunities/{uuid.uuid4()}/research")

    assert response.status_code == 404
    assert "not found" in response.json()["detail"]


def test_a_malformed_signal_id_is_rejected(api_client: TestClient) -> None:
    assert api_client.get("/api/v1/opportunities/nope/research").status_code == 422


# -- the read-only guarantee ------------------------------------------------


def test_the_get_never_acquires_or_matches(
    api_client: TestClient, db_session: Session, source: Source, run: Any
) -> None:
    """A GET that scraped would make page loads cost money.

    api_client's provider handler raises on any Bright Data request, and
    nothing may be written: the counts before and after must be identical.
    """
    from sqlalchemy import func, select

    from app.db.models import (
        OpportunityResearchMatch,
        ResearchPaper,
        ResearchSearchRun,
    )

    signal = cargo_signal(db_session, source, run)
    enrich(db_session, signal, "2608.00001")

    def counts() -> tuple[int, int, int]:
        return tuple(  # type: ignore[return-value]
            db_session.execute(select(func.count()).select_from(model)).scalar_one()
            for model in (ResearchSearchRun, ResearchPaper, OpportunityResearchMatch)
        )

    before = counts()
    for _ in range(3):
        assert (
            api_client.get(f"/api/v1/opportunities/{signal.id}/research").status_code
            == 200
        )

    assert counts() == before
