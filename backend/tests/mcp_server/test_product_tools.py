"""Cross-product MCP read-safety and overview tests."""

import socket

import pytest
from mcp import Client
from mcp.server import MCPServer
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from app.db.models import (
    CollectorRun,
    Investigation,
    InvestigationResearchMatch,
    InvestigationRun,
    OpportunityResearchMatch,
    ReliabilityIncident,
    ResearchPaper,
    ResearchSearchRun,
    Signal,
)
from app.domain.enums import InvestigationStatus
from app.mcp_server.server import create_mcp_server
from tests.opportunity_engine.conftest import (
    make_collector,
    make_run,
    make_signal,
    make_source,
)
from tests.opportunity_engine.test_service import open_incident


async def call(server: MCPServer, name: str, arguments: dict[str, object]) -> object:
    async with Client(server) as client:
        return await client.call_tool(name, arguments)


@pytest.mark.asyncio
async def test_gapradar_overview_reuses_persisted_dashboard_semantics(
    shared_mcp_server: MCPServer,
    db_session: Session,
) -> None:
    source = make_source(db_session)
    collector = make_collector(db_session, source)
    run = make_run(db_session, collector, record_count=12)
    signal = make_signal(db_session, source, run, title="Persisted opportunity")
    db_session.commit()

    result = await call(shared_mcp_server, "get_gapradar_overview", {"top": 5})

    assert result.is_error is False
    body = result.structured_content
    assert body["pipeline"]["last_run_id"] == str(run.id)
    assert body["signals"] == {"total": 1, "trusted": 1}
    assert body["top_opportunities"][0]["id"] == str(signal.id)


@pytest.mark.asyncio
@pytest.mark.parametrize("top", [0, 51])
async def test_gapradar_overview_limit_is_validated(
    shared_mcp_server: MCPServer,
    top: int,
) -> None:
    result = await call(shared_mcp_server, "get_gapradar_overview", {"top": top})

    assert result.is_error is True


@pytest.mark.asyncio
async def test_new_read_adapters_close_one_session_per_invocation(engine: Engine) -> None:
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

    for name in (
        "list_opportunities",
        "get_reliability_overview",
        "get_gapradar_overview",
    ):
        result = await call(server, name, {})
        assert result.is_error is False

    assert closed == 3


@pytest.mark.asyncio
async def test_new_read_error_is_sanitized(
    caplog: pytest.LogCaptureFixture,
) -> None:
    def fail_to_open_session() -> Session:
        raise RuntimeError("postgresql://user:secret@internal/database")

    server = create_mcp_server(session_factory=fail_to_open_session)

    result = await call(server, "list_opportunities", {})

    assert result.is_error is True
    message = " ".join(item.text for item in result.content if hasattr(item, "text"))
    assert "GapRadar could not read persisted data." in message
    assert "secret" not in message
    assert "postgresql" not in message
    assert "mcp_persisted_read_failed" in caplog.text
    assert "secret" not in caplog.text


@pytest.mark.asyncio
async def test_every_mcp_read_is_network_free_and_creates_no_execution(
    shared_mcp_server: MCPServer,
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    investigation = Investigation(
        query="Persisted hypothesis",
        industry="Restaurants",
        status=InvestigationStatus.DRAFT,
    )
    db_session.add(investigation)
    db_session.commit()

    source = make_source(db_session)
    opportunity_collector = make_collector(db_session, source)
    opportunity_run = make_run(db_session, opportunity_collector)
    opportunity = make_signal(db_session, source, opportunity_run)
    incident_collector = make_collector(
        db_session,
        source,
        name="Reliability-only collector",
        external_collector_id="c_reliability_only",
    )
    incident = open_incident(db_session, incident_collector)
    db_session.commit()

    tracked_models = (
        InvestigationRun,
        InvestigationResearchMatch,
        OpportunityResearchMatch,
        ResearchSearchRun,
        ResearchPaper,
        CollectorRun,
        ReliabilityIncident,
        Signal,
    )
    before = {
        model: db_session.execute(select(func.count()).select_from(model)).scalar_one()
        for model in tracked_models
    }
    incident_snapshot = {
        "status": incident.status,
        "evidence": incident.evidence,
        "recovery_proof": incident.recovery_proof,
        "repair_attempts": incident.repair_attempts,
    }
    investigation_status = investigation.status
    db_session.commit()

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
        ("list_opportunities", {}),
        ("get_opportunity", {"opportunity_id": str(opportunity.id)}),
        (
            "get_opportunity_research",
            {"opportunity_id": str(opportunity.id)},
        ),
        ("get_reliability_overview", {}),
        ("list_reliability_incidents", {}),
        (
            "get_reliability_incident",
            {"incident_id": str(incident.id)},
        ),
        ("get_recallguard_demo", {}),
        ("get_live_brightdata_evidence", {}),
        ("get_gapradar_overview", {}),
    )
    for name, arguments in calls:
        result = await call(shared_mcp_server, name, arguments)
        assert result.is_error is False, name

    after = {
        model: db_session.execute(select(func.count()).select_from(model)).scalar_one()
        for model in tracked_models
    }
    assert after == before
    db_session.refresh(incident)
    db_session.refresh(investigation)
    assert {
        "status": incident.status,
        "evidence": incident.evidence,
        "recovery_proof": incident.recovery_proof,
        "repair_attempts": incident.repair_attempts,
    } == incident_snapshot
    assert investigation.status is investigation_status
