"""Official-client tests for explicit Investigation MCP actions."""

import socket
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from mcp import Client
from mcp.server import MCPServer
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from app.db.models import Investigation, InvestigationRun
from app.domain.enums import (
    InvestigationRunStatus,
    InvestigationStatus,
    ResearchOutcomeReason,
)
from app.investigations.runs import set_run_status
from app.mcp_server.server import create_mcp_server
from tests.mcp_server.conftest import RecordingInvestigationSubmitter


async def call(server: MCPServer, name: str, arguments: dict[str, object]) -> object:
    async with Client(server) as client:
        return await client.call_tool(name, arguments)


def seed_investigation(session: Session) -> Investigation:
    investigation = Investigation(
        query="AI demand forecasting for independent restaurants",
        industry="Restaurants",
        status=InvestigationStatus.DRAFT,
    )
    session.add(investigation)
    session.commit()
    return investigation


@pytest.mark.asyncio
async def test_create_persists_a_normalized_draft_and_starts_nothing(
    shared_mcp_server: MCPServer,
    investigation_submitter: RecordingInvestigationSubmitter,
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def refuse_network(*args: object, **kwargs: object) -> None:
        raise AssertionError("create_investigation contacted a provider")

    monkeypatch.setattr(socket.socket, "connect", refuse_network)
    monkeypatch.setattr(socket, "create_connection", refuse_network)
    monkeypatch.setattr("openai.OpenAI", refuse_network)

    result = await call(
        shared_mcp_server,
        "create_investigation",
        {"query": "  Restaurant forecasting gap  ", "industry": "   "},
    )

    assert result.is_error is False
    body = result.structured_content
    assert body["query"] == "Restaurant forecasting gap"
    assert body["industry"] is None
    assert body["status"] == "draft"
    assert body["title"] is None
    assert body["description"] is None
    assert investigation_submitter.scheduled == []
    assert db_session.execute(select(func.count()).select_from(Investigation)).scalar_one() == 1
    assert db_session.execute(select(func.count()).select_from(InvestigationRun)).scalar_one() == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "arguments",
    [
        {"query": ""},
        {"query": "   "},
        {"query": "x" * 2001},
        {"query": "valid", "industry": "x" * 256},
    ],
)
async def test_create_uses_the_api_input_validation_contract(
    shared_mcp_server: MCPServer,
    arguments: dict[str, object],
) -> None:
    result = await call(shared_mcp_server, "create_investigation", arguments)

    assert result.is_error is True


@pytest.mark.asyncio
async def test_create_accepts_an_absent_industry(
    shared_mcp_server: MCPServer,
) -> None:
    result = await call(
        shared_mcp_server,
        "create_investigation",
        {"query": "A bounded user hypothesis"},
    )

    assert result.is_error is False
    assert result.structured_content["industry"] is None


@pytest.mark.asyncio
async def test_run_claims_and_submits_exactly_once(
    shared_mcp_server: MCPServer,
    investigation_submitter: RecordingInvestigationSubmitter,
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    investigation = seed_investigation(db_session)

    def refuse_network(*args: object, **kwargs: object) -> None:
        raise AssertionError("run claim contacted a provider inline")

    monkeypatch.setattr(socket.socket, "connect", refuse_network)
    monkeypatch.setattr(socket, "create_connection", refuse_network)
    monkeypatch.setattr("openai.OpenAI", refuse_network)

    first = await call(
        shared_mcp_server,
        "run_investigation",
        {"investigation_id": str(investigation.id)},
    )
    second = await call(
        shared_mcp_server,
        "run_investigation",
        {"investigation_id": str(investigation.id)},
    )

    assert first.is_error is False
    assert second.is_error is False
    assert first.structured_content["status"] == "queued"
    assert first.structured_content["already_running"] is False
    assert second.structured_content["run_id"] == first.structured_content["run_id"]
    assert second.structured_content["already_running"] is True
    assert investigation_submitter.scheduled == [
        uuid.UUID(first.structured_content["run_id"])
    ]
    runs = list(db_session.execute(select(InvestigationRun)).scalars())
    assert len(runs) == 1
    assert runs[0].status is InvestigationRunStatus.QUEUED


@pytest.mark.asyncio
async def test_run_for_missing_investigation_is_a_clean_error(
    shared_mcp_server: MCPServer,
    investigation_submitter: RecordingInvestigationSubmitter,
) -> None:
    unknown = uuid.uuid4()

    result = await call(
        shared_mcp_server,
        "run_investigation",
        {"investigation_id": str(unknown)},
    )

    assert result.is_error is True
    message = " ".join(item.text for item in result.content if hasattr(item, "text"))
    assert str(unknown) in message
    assert "SELECT" not in message
    assert "Traceback" not in message
    assert investigation_submitter.scheduled == []


@pytest.mark.asyncio
async def test_run_reconciles_a_stale_claim_before_retrying(
    shared_mcp_server: MCPServer,
    investigation_submitter: RecordingInvestigationSubmitter,
    db_session: Session,
) -> None:
    investigation = seed_investigation(db_session)
    first = await call(
        shared_mcp_server,
        "run_investigation",
        {"investigation_id": str(investigation.id)},
    )
    first_id = uuid.UUID(first.structured_content["run_id"])
    stale = db_session.get(InvestigationRun, first_id)
    assert stale is not None
    set_run_status(db_session, stale, InvestigationRunStatus.RUNNING)
    stale.created_at = datetime.now(UTC) - timedelta(hours=2)
    db_session.commit()

    second = await call(
        shared_mcp_server,
        "run_investigation",
        {"investigation_id": str(investigation.id)},
    )

    db_session.refresh(stale)
    assert stale.status is InvestigationRunStatus.FAILED
    assert stale.outcome_reason is ResearchOutcomeReason.INTERRUPTED
    assert second.structured_content["already_running"] is False
    assert second.structured_content["run_id"] != str(first_id)
    assert len(investigation_submitter.scheduled) == 2


@pytest.mark.asyncio
async def test_terminal_failure_uses_backend_retry_semantics(
    shared_mcp_server: MCPServer,
    investigation_submitter: RecordingInvestigationSubmitter,
    db_session: Session,
) -> None:
    investigation = seed_investigation(db_session)
    first = await call(
        shared_mcp_server,
        "run_investigation",
        {"investigation_id": str(investigation.id)},
    )
    failed = db_session.get(
        InvestigationRun,
        uuid.UUID(first.structured_content["run_id"]),
    )
    assert failed is not None
    set_run_status(db_session, failed, InvestigationRunStatus.FAILED)
    failed.outcome_reason = ResearchOutcomeReason.ACQUISITION_FAILED
    failed.completed_at = datetime.now(UTC)
    db_session.commit()

    status = await call(
        shared_mcp_server,
        "get_investigation_status",
        {"investigation_id": str(investigation.id)},
    )
    retry = await call(
        shared_mcp_server,
        "run_investigation",
        {"investigation_id": str(investigation.id)},
    )

    assert status.structured_content["latest_run"]["is_retryable"] is True
    assert retry.structured_content["already_running"] is False
    assert retry.structured_content["run_id"] != first.structured_content["run_id"]
    assert len(investigation_submitter.scheduled) == 2


@pytest.mark.asyncio
async def test_submission_failure_is_sanitized_and_does_not_leave_active_claim(
    shared_mcp_session_factory: sessionmaker[Session],
    db_session: Session,
    caplog: pytest.LogCaptureFixture,
) -> None:
    db_session.expire_on_commit = False
    investigation = seed_investigation(db_session)

    def fail_submission(run_id: uuid.UUID) -> None:
        raise RuntimeError("provider-secret-must-not-leak")

    server = create_mcp_server(
        session_factory=shared_mcp_session_factory,
        investigation_submitter=fail_submission,
    )

    result = await call(
        server,
        "run_investigation",
        {"investigation_id": str(investigation.id)},
    )

    assert result.is_error is True
    message = " ".join(item.text for item in result.content if hasattr(item, "text"))
    assert "GapRadar could not complete the Investigation action." in message
    assert "provider-secret" not in message
    assert "provider-secret" not in caplog.text
    run = db_session.execute(select(InvestigationRun)).scalar_one()
    db_session.refresh(run)
    assert run.status is InvestigationRunStatus.FAILED
    assert run.outcome_reason is ResearchOutcomeReason.UNEXPECTED_ERROR


@pytest.mark.asyncio
async def test_create_action_closes_its_session(engine: Engine) -> None:
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
    server = create_mcp_server(
        session_factory=factory,
        investigation_submitter=RecordingInvestigationSubmitter(),
    )

    result = await call(
        server,
        "create_investigation",
        {"query": "A safe persisted draft"},
    )

    assert result.is_error is False
    assert closed == 1
