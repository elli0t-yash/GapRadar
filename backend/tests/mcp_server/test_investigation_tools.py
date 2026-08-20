"""The Phase 1 MCP surface is a typed, provider-free database adapter."""

import socket
import uuid
from datetime import UTC, date, datetime

import pytest
from mcp import Client
from mcp.server import MCPServer
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from app.db.models import (
    Investigation,
    InvestigationCompetitor,
    InvestigationDemandEvidence,
    InvestigationResearchMatch,
    InvestigationRun,
    InvestigationWebSearchHit,
    InvestigationWebSearchRun,
    ResearchPaper,
    ResearchSearchResult,
    ResearchSearchRun,
)
from app.domain.enums import (
    CompetitorClassification,
    DemandEvidenceClassification,
    InvestigationRunStatus,
    InvestigationStatus,
    ResearchSource,
    WebSearchStatus,
)
from app.investigations.runs import set_run_status, start_run
from app.mcp_server.server import create_mcp_server

EXPECTED_READ_TOOLS = {
    "list_investigations",
    "get_investigation",
    "get_investigation_status",
    "get_investigation_research",
    "get_investigation_demand_evidence",
    "get_investigation_competitors",
    "list_opportunities",
    "get_opportunity",
    "get_opportunity_research",
    "get_reliability_overview",
    "list_reliability_incidents",
    "get_reliability_incident",
    "get_recallguard_demo",
    "get_live_brightdata_evidence",
    "get_gapradar_overview",
}
EXPECTED_WRITE_TOOLS = {"create_investigation", "run_investigation"}
EXPECTED_TOOLS = EXPECTED_READ_TOOLS | EXPECTED_WRITE_TOOLS


@pytest.fixture
def mcp_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(
        bind=engine,
        autoflush=False,
        autocommit=False,
        expire_on_commit=False,
    )


@pytest.fixture
def mcp_server(
    mcp_session_factory: sessionmaker[Session], db_session: Session
) -> MCPServer:
    # StaticPool gives this SQLite fixture one physical connection. Avoid a
    # post-commit attribute refresh opening a transaction on the seeding
    # session while the tool's separate session is trying to use it.
    db_session.expire_on_commit = False
    return create_mcp_server(session_factory=mcp_session_factory)


def seed_investigation(
    session: Session,
    *,
    query: str = "AI demand forecasting for independent restaurants",
) -> Investigation:
    investigation = Investigation(
        query=query,
        industry="Restaurants",
        status=InvestigationStatus.DRAFT,
    )
    session.add(investigation)
    session.commit()
    return investigation


def seed_run(
    session: Session,
    investigation: Investigation,
    status: InvestigationRunStatus,
) -> InvestigationRun:
    run, already_running = start_run(session, investigation=investigation)
    assert already_running is False
    if status is not InvestigationRunStatus.QUEUED:
        set_run_status(session, run, status)
        if status is InvestigationRunStatus.RUNNING:
            run.started_at = datetime.now(UTC)
        else:
            run.started_at = datetime.now(UTC)
            run.completed_at = datetime.now(UTC)
        session.commit()
    else:
        # start_run refreshes after its commit; close that read transaction
        # before the MCP tool opens its own session on StaticPool's connection.
        session.commit()
    return run


def seed_research(session: Session, investigation: Investigation) -> None:
    paper = ResearchPaper(
        arxiv_id="2608.13083",
        source=ResearchSource.ARXIV,
        title="Dynamic forecasting for perishable restaurant inventory",
        abstract="A persisted abstract about demand and perishable inventory.",
        authors=["Ada Researcher"],
        categories=[{"code": "cs.AI", "label": "Artificial Intelligence"}],
        primary_category_code="cs.AI",
        published_at=date(2026, 8, 13),
        paper_url="https://arxiv.org/abs/2608.13083",
        pdf_url="https://arxiv.org/pdf/2608.13083",
    )
    search_run = ResearchSearchRun(
        investigation_id=investigation.id,
        source=ResearchSource.ARXIV,
        query="restaurant demand forecasting",
        searched_at=datetime.now(UTC),
        provider_job_id="persisted-job-id",
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
            InvestigationResearchMatch(
                investigation_id=investigation.id,
                research_paper_id=paper.id,
                relevance_score=88.0,
                technical_readiness_score=73.0,
                matched_concepts=["demand forecasting", "perishables"],
                match_reason="The paper directly studies the stated problem.",
            ),
        ]
    )
    session.commit()


def seed_web_evidence(session: Session, investigation: Investigation) -> None:
    shared_url = "https://industry.test/restaurant-waste"
    for query, position in (
        ("restaurant inventory waste problems", 4),
        ("independent restaurant forecasting challenges", 2),
    ):
        search_run = InvestigationWebSearchRun(
            investigation_id=investigation.id,
            family="demand",
            query=query,
            provider="brightdata",
            product="serp",
            locale_country="us",
            locale_language="en",
            status=WebSearchStatus.SUCCEEDED,
            records_returned=1,
            latency_ms=50,
        )
        session.add(search_run)
        session.flush()
        session.add(
            InvestigationWebSearchHit(
                investigation_web_search_run_id=search_run.id,
                url=shared_url,
                domain="industry.test",
                title="Restaurant waste is measurable",
                snippet="Operators report persistent overstock and spoilage.",
                position=position,
            )
        )

    session.add_all(
        [
            InvestigationDemandEvidence(
                investigation_id=investigation.id,
                url=shared_url,
                domain="industry.test",
                title="Restaurant waste is measurable",
                snippet="Operators report persistent overstock and spoilage.",
                classification=DemandEvidenceClassification.STRONG_SUPPORT,
                relevance_score=91.0,
                reason="Reports the exact inventory-waste problem.",
            ),
            InvestigationDemandEvidence(
                investigation_id=investigation.id,
                url="https://contrary.test/forecasting-is-solved",
                domain="contrary.test",
                title="Forecasting is already accessible",
                snippet="Small operators already use effective forecasting tools.",
                classification=DemandEvidenceClassification.CONTRADICTS,
                relevance_score=84.0,
                reason="Directly disputes that the problem remains unsolved.",
            ),
        ]
    )

    competitor_run = InvestigationWebSearchRun(
        investigation_id=investigation.id,
        family="competitor",
        query="restaurant demand forecasting software",
        provider="brightdata",
        product="serp",
        locale_country="us",
        locale_language="en",
        status=WebSearchStatus.SUCCEEDED,
        records_returned=1,
        latency_ms=60,
    )
    session.add(competitor_run)
    session.flush()
    competitor_url = "https://forecast.test/product"
    session.add_all(
        [
            InvestigationWebSearchHit(
                investigation_web_search_run_id=competitor_run.id,
                url=competitor_url,
                domain="forecast.test",
                title="ForecastCo",
                snippet="Inventory forecasting for restaurant operators.",
                position=1,
            ),
            InvestigationCompetitor(
                investigation_id=investigation.id,
                url=competitor_url,
                domain="forecast.test",
                name="ForecastCo",
                snippet="Inventory forecasting for restaurant operators.",
                classification=CompetitorClassification.DIRECT,
                relevance_score=93.0,
                reason="Offers the same solution to the same buyer.",
            ),
        ]
    )
    session.commit()


async def call(server: MCPServer, name: str, arguments: dict[str, object]) -> object:
    async with Client(server) as client:
        return await client.call_tool(name, arguments)


@pytest.mark.asyncio
async def test_tools_list_exposes_exactly_the_safe_product_surface(
    mcp_server: MCPServer,
) -> None:
    async with Client(mcp_server) as client:
        result = await client.list_tools()

    assert {tool.name for tool in result.tools} == EXPECTED_TOOLS
    for tool in result.tools:
        assert tool.annotations is not None
        assert tool.annotations.destructive_hint is False
        assert tool.annotations.open_world_hint is False
        if tool.name in EXPECTED_READ_TOOLS:
            assert tool.annotations.read_only_hint is True
            assert tool.annotations.idempotent_hint is True
        else:
            assert tool.annotations.read_only_hint is False
            assert tool.annotations.idempotent_hint is False


@pytest.mark.asyncio
async def test_list_investigations_returns_persisted_rows_newest_first(
    mcp_server: MCPServer, db_session: Session
) -> None:
    older = seed_investigation(db_session, query="Older hypothesis")
    newer = seed_investigation(db_session, query="Newer hypothesis")
    older.created_at = datetime(2026, 8, 19, tzinfo=UTC)
    newer.created_at = datetime(2026, 8, 20, tzinfo=UTC)
    db_session.commit()

    result = await call(mcp_server, "list_investigations", {"limit": 20})

    assert result.is_error is False
    assert result.structured_content["count"] == 2
    ids = [row["id"] for row in result.structured_content["investigations"]]
    assert ids == [str(newer.id), str(older.id)]


@pytest.mark.asyncio
async def test_get_existing_investigation(
    mcp_server: MCPServer, db_session: Session
) -> None:
    investigation = seed_investigation(db_session)

    result = await call(
        mcp_server,
        "get_investigation",
        {"investigation_id": str(investigation.id)},
    )

    assert result.is_error is False
    assert result.structured_content["id"] == str(investigation.id)
    assert result.structured_content["query"] == investigation.query


@pytest.mark.asyncio
async def test_unknown_investigation_is_a_clean_tool_error(
    mcp_server: MCPServer,
) -> None:
    unknown = uuid.uuid4()

    result = await call(
        mcp_server,
        "get_investigation",
        {"investigation_id": str(unknown)},
    )

    assert result.is_error is True
    message = " ".join(item.text for item in result.content if hasattr(item, "text"))
    assert str(unknown) in message
    assert "SELECT" not in message
    assert "Traceback" not in message


@pytest.mark.asyncio
async def test_unexpected_database_error_is_sanitized_for_the_client(
    caplog: pytest.LogCaptureFixture,
) -> None:
    def fail_to_open_session() -> Session:
        raise RuntimeError("postgresql://user:secret@internal/database")

    server = create_mcp_server(session_factory=fail_to_open_session)

    result = await call(server, "list_investigations", {})

    assert result.is_error is True
    message = " ".join(item.text for item in result.content if hasattr(item, "text"))
    assert "GapRadar could not read persisted investigation data." in message
    assert "secret" not in message
    assert "postgresql" not in message
    assert "mcp_investigation_read_failed" in caplog.text
    assert "secret" not in caplog.text


@pytest.mark.asyncio
async def test_status_for_never_run_investigation_has_null_latest_run(
    mcp_server: MCPServer, db_session: Session
) -> None:
    investigation = seed_investigation(db_session)

    result = await call(
        mcp_server,
        "get_investigation_status",
        {"investigation_id": str(investigation.id)},
    )

    assert result.is_error is False
    assert result.structured_content["latest_run"] is None
    assert result.structured_content["investigation"]["status"] == "draft"


@pytest.mark.asyncio
async def test_status_returns_an_active_run_and_measured_phases(
    mcp_server: MCPServer, db_session: Session
) -> None:
    investigation = seed_investigation(db_session)
    run = seed_run(db_session, investigation, InvestigationRunStatus.RUNNING)
    run.phases = {
        "planning": {
            "state": "complete",
            "research_queries": 2,
            "demand_queries": 3,
            "competitor_queries": 2,
        },
        "research": {"state": "running", "queries_total": 2},
    }
    db_session.commit()

    result = await call(
        mcp_server,
        "get_investigation_status",
        {"investigation_id": str(investigation.id)},
    )

    latest = result.structured_content["latest_run"]
    assert latest["status"] == "running"
    assert latest["is_terminal"] is False
    assert latest["phases"]["planning"]["demand_queries"] == 3


@pytest.mark.asyncio
async def test_status_returns_terminal_run_flags(
    mcp_server: MCPServer, db_session: Session
) -> None:
    investigation = seed_investigation(db_session)
    seed_run(db_session, investigation, InvestigationRunStatus.SUCCEEDED)

    result = await call(
        mcp_server,
        "get_investigation_status",
        {"investigation_id": str(investigation.id)},
    )

    latest = result.structured_content["latest_run"]
    assert latest["status"] == "succeeded"
    assert latest["is_terminal"] is True
    assert latest["is_retryable"] is False


@pytest.mark.asyncio
async def test_research_returns_persisted_results(
    mcp_server: MCPServer, db_session: Session
) -> None:
    investigation = seed_investigation(db_session)
    seed_research(db_session, investigation)

    result = await call(
        mcp_server,
        "get_investigation_research",
        {"investigation_id": str(investigation.id)},
    )

    body = result.structured_content
    assert body["paper_count"] == 1
    assert body["matched_paper_count"] == 1
    assert body["top_papers"][0]["arxiv_id"] == "2608.13083"
    assert body["top_papers"][0]["relevance_score"] == 88.0


@pytest.mark.asyncio
async def test_research_zero_result_is_empty_and_valid(
    mcp_server: MCPServer, db_session: Session
) -> None:
    investigation = seed_investigation(db_session)

    result = await call(
        mcp_server,
        "get_investigation_research",
        {"investigation_id": str(investigation.id)},
    )

    body = result.structured_content
    assert body["paper_count"] == 0
    assert body["top_papers"] == []
    assert body["generated_queries"] == []


@pytest.mark.asyncio
async def test_demand_evidence_preserves_persisted_classifications(
    mcp_server: MCPServer, db_session: Session
) -> None:
    investigation = seed_investigation(db_session)
    seed_web_evidence(db_session, investigation)

    result = await call(
        mcp_server,
        "get_investigation_demand_evidence",
        {"investigation_id": str(investigation.id)},
    )

    assert result.structured_content["counts"] == {
        "strong_support": 1,
        "contradicts": 1,
    }


@pytest.mark.asyncio
async def test_contradictory_demand_evidence_remains_present(
    mcp_server: MCPServer, db_session: Session
) -> None:
    investigation = seed_investigation(db_session)
    seed_web_evidence(db_session, investigation)

    result = await call(
        mcp_server,
        "get_investigation_demand_evidence",
        {"investigation_id": str(investigation.id)},
    )

    classifications = {
        row["classification"] for row in result.structured_content["evidence"]
    }
    assert "contradicts" in classifications


@pytest.mark.asyncio
async def test_competitor_evidence_is_returned(
    mcp_server: MCPServer, db_session: Session
) -> None:
    investigation = seed_investigation(db_session)
    seed_web_evidence(db_session, investigation)

    result = await call(
        mcp_server,
        "get_investigation_competitors",
        {"investigation_id": str(investigation.id)},
    )

    competitor = result.structured_content["competitors"][0]
    assert competitor["name"] == "ForecastCo"
    assert competitor["classification"] == "direct"


@pytest.mark.asyncio
async def test_search_query_provenance_is_preserved(
    mcp_server: MCPServer, db_session: Session
) -> None:
    investigation = seed_investigation(db_session)
    seed_web_evidence(db_session, investigation)

    result = await call(
        mcp_server,
        "get_investigation_demand_evidence",
        {"investigation_id": str(investigation.id)},
    )

    evidence = next(
        row
        for row in result.structured_content["evidence"]
        if row["classification"] == "strong_support"
    )
    assert set(evidence["provenance"]["found_by_queries"]) == {
        "restaurant inventory waste problems",
        "independent restaurant forecasting challenges",
    }
    assert evidence["provenance"]["best_position"] == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool_name", "arguments"),
    [
        ("list_investigations", {"limit": 0}),
        ("list_investigations", {"limit": 201}),
        (
            "get_investigation_demand_evidence",
            {"investigation_id": str(uuid.uuid4()), "limit": 0},
        ),
        (
            "get_investigation_competitors",
            {"investigation_id": str(uuid.uuid4()), "limit": 201},
        ),
    ],
)
async def test_limits_are_validated_by_the_tool_contract(
    mcp_server: MCPServer, tool_name: str, arguments: dict[str, object]
) -> None:
    result = await call(mcp_server, tool_name, arguments)

    assert result.is_error is True


@pytest.mark.asyncio
async def test_each_invocation_closes_its_database_session(engine: Engine) -> None:
    closed = 0

    class TrackingSession(Session):
        def close(self) -> None:
            nonlocal closed
            closed += 1
            super().close()

    factory = sessionmaker(
        bind=engine,
        class_=TrackingSession,
        autoflush=False,
        autocommit=False,
    )
    server = create_mcp_server(session_factory=factory)

    result = await call(server, "list_investigations", {})

    assert result.is_error is False
    assert closed == 1


@pytest.mark.asyncio
async def test_all_tools_are_network_free_and_do_not_create_runs(
    mcp_server: MCPServer,
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    investigation = seed_investigation(db_session)
    seed_research(db_session, investigation)
    seed_web_evidence(db_session, investigation)

    def refuse_network(*args: object, **kwargs: object) -> None:
        raise AssertionError("an MCP read attempted a network connection")

    monkeypatch.setattr(socket.socket, "connect", refuse_network)
    monkeypatch.setattr(socket, "create_connection", refuse_network)

    calls = (
        ("list_investigations", {}),
        ("get_investigation", {"investigation_id": str(investigation.id)}),
        (
            "get_investigation_status",
            {"investigation_id": str(investigation.id)},
        ),
        (
            "get_investigation_research",
            {"investigation_id": str(investigation.id)},
        ),
        (
            "get_investigation_demand_evidence",
            {"investigation_id": str(investigation.id)},
        ),
        (
            "get_investigation_competitors",
            {"investigation_id": str(investigation.id)},
        ),
    )
    for name, arguments in calls:
        result = await call(mcp_server, name, arguments)
        assert result.is_error is False, name

    assert (
        db_session.execute(
            select(func.count()).select_from(InvestigationRun)
        ).scalar_one()
        == 0
    )
