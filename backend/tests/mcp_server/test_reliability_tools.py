"""Official-client tests for persisted RecallGuard MCP reads."""

import uuid

import pytest
from mcp import Client
from mcp.server import MCPServer
from sqlalchemy.orm import Session

from app.domain.enums import IncidentStatus
from tests.opportunity_engine.conftest import make_collector, make_run, make_source
from tests.opportunity_engine.test_service import open_incident


async def call(server: MCPServer, name: str, arguments: dict[str, object]) -> object:
    async with Client(server) as client:
        return await client.call_tool(name, arguments)


@pytest.mark.asyncio
async def test_reliability_overview_reads_persisted_collector_health(
    shared_mcp_server: MCPServer,
    db_session: Session,
) -> None:
    source = make_source(db_session)
    collector = make_collector(db_session, source)
    run = make_run(db_session, collector, record_count=17)
    db_session.commit()

    result = await call(shared_mcp_server, "get_reliability_overview", {})

    assert result.is_error is False
    body = result.structured_content
    assert body["state"] == "healthy"
    assert body["collectors"][0]["collector_id"] == str(collector.id)
    assert body["collectors"][0]["last_run_id"] == str(run.id)
    assert body["collectors"][0]["last_record_count"] == 17


@pytest.mark.asyncio
async def test_incident_list_and_detail_preserve_persisted_proof(
    shared_mcp_server: MCPServer,
    db_session: Session,
) -> None:
    source = make_source(db_session)
    collector = make_collector(db_session, source)
    incident = open_incident(db_session, collector)
    incident.status = IncidentStatus.RECOVERED
    incident.repair_attempts = 2
    incident.evidence = {
        "severity": "high",
        "confidence": 0.838,
        "occurrences": [],
        "events": [],
    }
    incident.recovery_proof = {
        "decision": "approve",
        "schema_fidelity": "pass",
        "semantic_fidelity": "pass",
        "source_fidelity": "pass",
    }
    db_session.commit()

    listed = await call(
        shared_mcp_server,
        "list_reliability_incidents",
        {"status": "recovered", "limit": 20},
    )
    detail = await call(
        shared_mcp_server,
        "get_reliability_incident",
        {"incident_id": str(incident.id)},
    )

    assert listed.structured_content["count"] == 1
    assert listed.structured_content["incidents"][0]["classification"] == (
        "extraction_drift"
    )
    assert listed.structured_content["incidents"][0]["recommended_action"] == (
        "request_heal"
    )
    assert detail.structured_content["evidence"]["severity"] == "high"
    assert detail.structured_content["evidence"]["confidence"] == 0.838
    assert detail.structured_content["recovery_proof"]["decision"] == "approve"


@pytest.mark.asyncio
async def test_incident_without_recovery_has_no_proof(
    shared_mcp_server: MCPServer,
    db_session: Session,
) -> None:
    source = make_source(db_session)
    collector = make_collector(db_session, source)
    incident = open_incident(db_session, collector)
    db_session.commit()

    result = await call(
        shared_mcp_server,
        "get_reliability_incident",
        {"incident_id": str(incident.id)},
    )

    assert result.structured_content["status"] == "degraded"
    assert result.structured_content["recovery_proof"] is None


@pytest.mark.asyncio
async def test_unknown_incident_is_a_clean_tool_error(
    shared_mcp_server: MCPServer,
) -> None:
    unknown = uuid.uuid4()

    result = await call(
        shared_mcp_server,
        "get_reliability_incident",
        {"incident_id": str(unknown)},
    )

    assert result.is_error is True
    message = " ".join(item.text for item in result.content if hasattr(item, "text"))
    assert str(unknown) in message
    assert "SELECT" not in message
    assert "Traceback" not in message


@pytest.mark.asyncio
async def test_demo_read_is_explicitly_fixture_replay_and_does_not_start_it(
    shared_mcp_server: MCPServer,
    db_session: Session,
) -> None:
    result = await call(shared_mcp_server, "get_recallguard_demo", {})

    assert result.is_error is False
    assert result.structured_content["mode"] == "fixture_replay"
    assert result.structured_content["session_id"] is None
    assert result.structured_content["status"] == "healthy"


@pytest.mark.asyncio
async def test_live_evidence_read_does_not_fabricate_missing_history(
    shared_mcp_server: MCPServer,
) -> None:
    result = await call(shared_mcp_server, "get_live_brightdata_evidence", {})

    assert result.is_error is False
    assert result.structured_content["available"] is False
    assert result.structured_content["mode"] == "persisted_real_brightdata_run"
    assert result.structured_content["live_trigger_safe"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", [0, 201])
async def test_incident_limit_is_validated(
    shared_mcp_server: MCPServer,
    limit: int,
) -> None:
    result = await call(
        shared_mcp_server,
        "list_reliability_incidents",
        {"limit": limit},
    )

    assert result.is_error is True
