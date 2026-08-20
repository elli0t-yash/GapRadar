"""Official-client tests for persisted Opportunity MCP reads."""

import uuid
from datetime import UTC, date, datetime

import pytest
from mcp import Client
from mcp.server import MCPServer
from sqlalchemy.orm import Session

from app.db.models import (
    OpportunityResearchMatch,
    ResearchPaper,
    ResearchSearchResult,
    ResearchSearchRun,
    Signal,
)
from app.domain.enums import ResearchSource
from tests.opportunity_engine.conftest import (
    make_collector,
    make_run,
    make_signal,
    make_source,
)


async def call(server: MCPServer, name: str, arguments: dict[str, object]) -> object:
    async with Client(server) as client:
        return await client.call_tool(name, arguments)


def seed_opportunity(
    session: Session, *, title: str = "Strong opportunity"
) -> Signal:
    source = make_source(session)
    collector = make_collector(session, source)
    run = make_run(session, collector)
    signal = make_signal(session, source, run, title=title, itch_score=95)
    session.commit()
    return signal


def seed_opportunity_research(session: Session, signal_id: uuid.UUID) -> None:
    paper = ResearchPaper(
        arxiv_id="2608.13083",
        source=ResearchSource.ARXIV,
        title="Research for a persisted market problem",
        abstract="A full persisted abstract supporting the opportunity.",
        authors=["Ada Researcher"],
        categories=[{"code": "cs.AI", "label": "Artificial Intelligence"}],
        primary_category_code="cs.AI",
        published_at=date(2026, 8, 13),
        paper_url="https://arxiv.org/abs/2608.13083",
        pdf_url="https://arxiv.org/pdf/2608.13083",
    )
    search_run = ResearchSearchRun(
        signal_id=signal_id,
        source=ResearchSource.ARXIV,
        query="persisted opportunity research",
        searched_at=datetime.now(UTC),
        provider_job_id="historical-job",
    )
    session.add_all([paper, search_run])
    session.flush()
    session.add_all(
        [
            ResearchSearchResult(
                research_search_run_id=search_run.id,
                research_paper_id=paper.id,
                position=0,
            ),
            OpportunityResearchMatch(
                signal_id=signal_id,
                research_paper_id=paper.id,
                relevance_score=91.0,
                technical_readiness_score=77.0,
                matched_concepts=["forecasting", "operations"],
                match_reason="Directly addresses the persisted opportunity.",
            ),
        ]
    )
    session.commit()


@pytest.mark.asyncio
async def test_list_opportunities_returns_ranked_persisted_rows(
    shared_mcp_server: MCPServer,
    db_session: Session,
) -> None:
    low = seed_opportunity(db_session, title="Low")
    # Reuse the same collector graph for the second signal.
    high = make_signal(
        db_session,
        low.source,
        low.collector_run,
        title="High",
        itch_score=100,
    )
    db_session.commit()

    result = await call(shared_mcp_server, "list_opportunities", {"limit": 20})

    assert result.is_error is False
    assert result.structured_content["count"] == 2
    assert [row["id"] for row in result.structured_content["opportunities"]] == [
        str(high.id),
        str(low.id),
    ]


@pytest.mark.asyncio
async def test_get_opportunity_preserves_source_provenance(
    shared_mcp_server: MCPServer,
    db_session: Session,
) -> None:
    signal = seed_opportunity(db_session)

    result = await call(
        shared_mcp_server,
        "get_opportunity",
        {"opportunity_id": str(signal.id)},
    )

    body = result.structured_content
    assert body["id"] == str(signal.id)
    assert body["source_id"] == str(signal.source_id)
    assert body["collector_run_id"] == str(signal.collector_run_id)
    assert body["source"] == "fix_my_itch"
    assert body["opportunity_score"] is not None


@pytest.mark.asyncio
async def test_unknown_opportunity_is_a_clean_tool_error(
    shared_mcp_server: MCPServer,
) -> None:
    unknown = uuid.uuid4()

    result = await call(
        shared_mcp_server,
        "get_opportunity",
        {"opportunity_id": str(unknown)},
    )

    assert result.is_error is True
    message = " ".join(item.text for item in result.content if hasattr(item, "text"))
    assert str(unknown) in message
    assert "SELECT" not in message
    assert "Traceback" not in message


@pytest.mark.asyncio
async def test_opportunity_research_returns_only_persisted_results(
    shared_mcp_server: MCPServer,
    db_session: Session,
) -> None:
    signal = seed_opportunity(db_session)
    seed_opportunity_research(db_session, signal.id)

    result = await call(
        shared_mcp_server,
        "get_opportunity_research",
        {"opportunity_id": str(signal.id)},
    )

    body = result.structured_content
    assert body["signal_id"] == str(signal.id)
    assert body["paper_count"] == 1
    assert body["matched_paper_count"] == 1
    assert body["top_papers"][0]["arxiv_id"] == "2608.13083"


@pytest.mark.asyncio
async def test_opportunity_research_zero_state_is_structured(
    shared_mcp_server: MCPServer,
    db_session: Session,
) -> None:
    signal = seed_opportunity(db_session)

    result = await call(
        shared_mcp_server,
        "get_opportunity_research",
        {"opportunity_id": str(signal.id)},
    )

    assert result.structured_content["paper_count"] == 0
    assert result.structured_content["generated_queries"] == []
    assert result.structured_content["top_papers"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", [0, 201])
async def test_opportunity_limit_is_validated(
    shared_mcp_server: MCPServer,
    limit: int,
) -> None:
    result = await call(shared_mcp_server, "list_opportunities", {"limit": limit})

    assert result.is_error is True
