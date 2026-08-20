"""Authenticated Streamable HTTP boundary for the existing MCP tool server."""

from __future__ import annotations

import socket
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx2
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from mcp import Client as MCPClient
from mcp.client.streamable_http import streamable_http_client
from mcp.server import MCPServer
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.db.models import (
    CollectorRun,
    Investigation,
    InvestigationRun,
    ReliabilityIncident,
    ResearchSearchRun,
)
from app.domain.enums import InvestigationStatus
from app.factory import create_app
from app.mcp_server.server import create_mcp_server
from tests.mcp_server.conftest import RecordingInvestigationSubmitter

MCP_TOKEN = "mcp-test-token-with-at-least-32-characters"
AUTHORIZATION = {"Authorization": f"Bearer {MCP_TOKEN}"}
EXPECTED_TOOLS = {
    "list_investigations",
    "get_investigation",
    "get_investigation_status",
    "get_investigation_research",
    "get_investigation_demand_evidence",
    "get_investigation_competitors",
    "create_investigation",
    "run_investigation",
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


def mcp_settings(**overrides: str) -> Settings:
    values = {
        "APP_ENV": "test",
        "DATABASE_URL": "",
        "CORS_ORIGINS": "http://localhost:5173",
        "GAPRADAR_MCP_API_KEY": MCP_TOKEN,
        "GAPRADAR_MCP_ALLOWED_HOSTS": "testserver",
        "GAPRADAR_MCP_ALLOWED_ORIGINS": "",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def initialize_request(request_id: int = 1) -> dict[str, object]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-11-25",
            "capabilities": {},
            "clientInfo": {"name": "gapradar-test", "version": "1.0"},
        },
    }


def post_initialize(
    client: TestClient,
    *,
    authorization: str | None,
    request_id: int = 1,
    extra_headers: dict[str, str] | None = None,
):
    headers = {"Accept": "application/json, text/event-stream"}
    if authorization is not None:
        headers["Authorization"] = authorization
    if extra_headers:
        headers.update(extra_headers)
    return client.post(
        "/mcp",
        json=initialize_request(request_id),
        headers=headers,
    )


@asynccontextmanager
async def http_mcp_client(app: FastAPI) -> AsyncIterator[MCPClient]:
    async with app.router.lifespan_context(app), httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=app),
        base_url="http://testserver",
        headers=AUTHORIZATION,
        follow_redirects=True,
    ) as http_client:
        transport = streamable_http_client(
            "http://testserver/mcp",
            http_client=http_client,
        )
        async with MCPClient(transport, mode="legacy") as client:
            yield client


def test_mcp_fails_closed_when_secret_is_missing_or_weak() -> None:
    for api_key in ("", "too-short"):
        app = create_app(
            settings=mcp_settings(
                APP_ENV="production",
                GAPRADAR_MCP_API_KEY=api_key,
            ),
            mcp_server=create_mcp_server(),
        )

        with TestClient(app) as client:
            response = client.post("/mcp", json=initialize_request())
            health = client.get("/api/v1/health")

        assert response.status_code == 503
        assert response.json() == {"detail": "MCP service unavailable"}
        assert health.status_code == 200
        assert app.state.mcp_server is None


def test_mcp_rejects_missing_wrong_and_malformed_bearer_credentials(
    shared_mcp_server: MCPServer,
) -> None:
    app = create_app(settings=mcp_settings(), mcp_server=shared_mcp_server)

    with TestClient(app) as client:
        credentials = (
            None,
            "Basic abc",
            "Bearer",
            "Bearer ",
            "Bearer wrong-token",
            f"Bearer {MCP_TOKEN} trailing",
        )
        responses = [
            post_initialize(client, authorization=value, request_id=index)
            for index, value in enumerate(credentials, start=1)
        ]

    assert all(response.status_code == 401 for response in responses)
    assert all(response.json() == {"detail": "Unauthorized"} for response in responses)
    assert all(
        response.headers["www-authenticate"] == "Bearer" for response in responses
    )


def test_valid_bearer_reaches_mcp_and_never_appears_in_logs_or_errors(
    shared_mcp_server: MCPServer,
    capsys: pytest.CaptureFixture[str],
) -> None:
    app = create_app(settings=mcp_settings(), mcp_server=shared_mcp_server)

    with TestClient(app) as client:
        denied = post_initialize(
            client,
            authorization=f"Bearer {MCP_TOKEN}-wrong",
        )
        rejected_origin = post_initialize(
            client,
            authorization=f"Bearer {MCP_TOKEN}",
            request_id=2,
            extra_headers={"Origin": MCP_TOKEN},
        )
        accepted = post_initialize(
            client,
            authorization=f"Bearer {MCP_TOKEN}",
            request_id=3,
        )

    assert denied.status_code == 401
    assert rejected_origin.status_code == 403
    assert accepted.status_code == 200
    assert accepted.json()["result"]["serverInfo"]["name"] == "GapRadar"
    assert MCP_TOKEN not in denied.text
    assert MCP_TOKEN not in rejected_origin.text
    assert MCP_TOKEN not in accepted.text
    captured = capsys.readouterr()
    assert MCP_TOKEN not in captured.out
    assert MCP_TOKEN not in captured.err


def test_mcp_transport_enforces_host_and_origin_allowlists(
    shared_mcp_server: MCPServer,
) -> None:
    app = create_app(
        settings=mcp_settings(
            GAPRADAR_MCP_ALLOWED_ORIGINS="https://trusted-agent.example"
        ),
        mcp_server=shared_mcp_server,
    )

    with TestClient(app) as client:
        bad_host = post_initialize(
            client,
            authorization=f"Bearer {MCP_TOKEN}",
            extra_headers={"Host": "attacker.example"},
        )
        bad_origin = post_initialize(
            client,
            authorization=f"Bearer {MCP_TOKEN}",
            request_id=2,
            extra_headers={"Origin": "https://attacker.example"},
        )
        accepted = post_initialize(
            client,
            authorization=f"Bearer {MCP_TOKEN}",
            request_id=3,
            extra_headers={"Origin": "https://trusted-agent.example"},
        )

    assert bad_host.status_code == 421
    assert bad_origin.status_code == 403
    assert accepted.status_code == 200


def test_mcp_auth_middleware_does_not_change_non_mcp_routes(
    shared_mcp_server: MCPServer,
) -> None:
    app = create_app(settings=mcp_settings(), mcp_server=shared_mcp_server)

    with TestClient(app) as client:
        response = client.get("/api/v1/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service": "gapradar-api"}


@pytest.mark.asyncio
async def test_official_http_client_initializes_lists_all_tools_and_reads(
    shared_mcp_server: MCPServer,
) -> None:
    app = create_app(settings=mcp_settings(), mcp_server=shared_mcp_server)

    async with http_mcp_client(app) as client:
        tools = await client.list_tools()
        result = await client.call_tool("list_investigations", {"limit": 5})

    assert {tool.name for tool in tools.tools} == EXPECTED_TOOLS
    assert len(tools.tools) == 17
    assert result.is_error is False
    assert result.structured_content == {"investigations": [], "count": 0}


@pytest.mark.asyncio
async def test_http_run_action_uses_mocked_submitter_and_remains_idempotent(
    shared_mcp_server: MCPServer,
    investigation_submitter: RecordingInvestigationSubmitter,
    db_session: Session,
) -> None:
    investigation = Investigation(
        query="Remote MCP run idempotency",
        industry=None,
        status=InvestigationStatus.DRAFT,
    )
    db_session.add(investigation)
    db_session.commit()
    investigation_id = str(investigation.id)
    app = create_app(settings=mcp_settings(), mcp_server=shared_mcp_server)

    async with http_mcp_client(app) as client:
        first = await client.call_tool(
            "run_investigation", {"investigation_id": investigation_id}
        )
        second = await client.call_tool(
            "run_investigation", {"investigation_id": investigation_id}
        )

    assert first.is_error is False
    assert second.is_error is False
    assert second.structured_content["run_id"] == first.structured_content["run_id"]
    assert second.structured_content["already_running"] is True
    assert len(investigation_submitter.scheduled) == 1
    assert db_session.execute(select(func.count()).select_from(InvestigationRun)).scalar_one() == 1


@pytest.mark.asyncio
async def test_remote_read_is_provider_free_and_does_not_create_runs(
    shared_mcp_server: MCPServer,
    investigation_submitter: RecordingInvestigationSubmitter,
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def refuse_network(*args: object, **kwargs: object) -> None:
        raise AssertionError("remote MCP read attempted a network connection")

    # Patching the process-wide socket.socket.connect method breaks AnyIO's
    # own event-loop wakeup socket during an ASGI protocol test. Blocking the
    # public connection constructor still fails ordinary provider clients,
    # while the existing in-memory all-read-tools suite covers both methods.
    monkeypatch.setattr(socket, "create_connection", refuse_network)
    for provider_boundary in (
        "openai.OpenAI",
        "app.integrations.brightdata.arxiv.BrightDataArxivCollector.search",
        "app.integrations.brightdata.serp.BrightDataSerpWebSearchProvider.search_web",
        "app.integrations.brightdata.client.BrightDataClient.trigger_collector_run",
        "app.integrations.brightdata.client.BrightDataClient.request_healing",
    ):
        monkeypatch.setattr(provider_boundary, refuse_network)
    tracked_models = (
        InvestigationRun,
        CollectorRun,
        ReliabilityIncident,
        ResearchSearchRun,
    )
    before = {
        model: db_session.execute(select(func.count()).select_from(model)).scalar_one()
        for model in tracked_models
    }
    # The StaticPool test fixture shares one physical SQLite connection. End
    # this read transaction before the MCP tool opens its owned session.
    db_session.commit()
    app = create_app(settings=mcp_settings(), mcp_server=shared_mcp_server)

    async with http_mcp_client(app) as client:
        result = await client.call_tool("list_investigations", {"limit": 5})

    after = {
        model: db_session.execute(select(func.count()).select_from(model)).scalar_one()
        for model in tracked_models
    }
    assert result.is_error is False
    assert after == before
    assert investigation_submitter.scheduled == []


def test_parent_and_mcp_lifespans_start_and_stop_without_leaks(
    shared_mcp_session_factory,
    investigation_submitter: RecordingInvestigationSubmitter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lifecycle_events: list[str] = []

    @asynccontextmanager
    async def recording_lifespan(_app: FastAPI) -> AsyncIterator[None]:
        lifecycle_events.append("app_startup")
        try:
            yield
        finally:
            lifecycle_events.append("app_shutdown")

    monkeypatch.setattr("app.factory.lifespan", recording_lifespan)
    managers = []
    for _ in range(2):
        server = create_mcp_server(
            session_factory=shared_mcp_session_factory,
            investigation_submitter=investigation_submitter,
        )
        app = create_app(settings=mcp_settings(), mcp_server=server)
        manager = server.session_manager
        managers.append(manager)

        assert manager._has_started is False
        with TestClient(app):
            assert manager._has_started is True
            assert manager._task_group is not None
        assert manager._task_group is None
        assert manager._server_instances == {}
        assert manager._session_owners == {}

    assert lifecycle_events == [
        "app_startup",
        "app_shutdown",
        "app_startup",
        "app_shutdown",
    ]
    assert all(manager._task_group is None for manager in managers)
