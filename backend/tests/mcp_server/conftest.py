"""Shared fixtures for official in-memory MCP client tests."""

import uuid

import pytest
from mcp.server import MCPServer
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.mcp_server.server import create_mcp_server


class RecordingInvestigationSubmitter:
    """Records explicit run submissions without executing provider work."""

    def __init__(self) -> None:
        self.scheduled: list[uuid.UUID] = []

    def __call__(self, run_id: uuid.UUID) -> None:
        self.scheduled.append(run_id)


@pytest.fixture
def investigation_submitter() -> RecordingInvestigationSubmitter:
    return RecordingInvestigationSubmitter()


@pytest.fixture
def shared_mcp_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(
        bind=engine,
        autoflush=False,
        autocommit=False,
        expire_on_commit=False,
    )


@pytest.fixture
def shared_mcp_server(
    shared_mcp_session_factory: sessionmaker[Session],
    db_session: Session,
    investigation_submitter: RecordingInvestigationSubmitter,
) -> MCPServer:
    # SQLite's StaticPool shares one physical connection. Avoid an expired
    # seed object opening a transaction while a tool owns its separate session.
    db_session.expire_on_commit = False
    return create_mcp_server(
        session_factory=shared_mcp_session_factory,
        investigation_submitter=investigation_submitter,
    )
