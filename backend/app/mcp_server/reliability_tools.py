"""Read-only MCP tools for persisted RecallGuard evidence."""

import logging
import uuid
from collections.abc import Callable
from typing import Annotated

from mcp.server import MCPServer
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.domain.enums import IncidentStatus
from app.mcp_server.common import (
    READ_ONLY_ANNOTATIONS,
    SessionFactory,
    run_persisted_read,
)
from app.recallguard.demo import read_demo
from app.recallguard.live_evidence import read_live_brightdata_evidence
from app.recallguard.read_service import (
    DEFAULT_INCIDENT_LIMIT,
    MAX_INCIDENT_LIMIT,
)
from app.recallguard.read_service import (
    get_reliability_incident as read_reliability_incident,
)
from app.recallguard.read_service import (
    get_reliability_overview as read_reliability_overview,
)
from app.recallguard.read_service import (
    list_reliability_incidents as read_reliability_incidents,
)
from app.schemas.reliability import (
    LiveBrightDataEvidenceRead,
    RecallGuardDemoRead,
    ReliabilityIncidentRead,
    ReliabilityIncidentSummary,
    ReliabilityOverviewRead,
)

logger = logging.getLogger(__name__)

class ReliabilityIncidentListResult(BaseModel):
    """A bounded list of persisted reliability incidents."""

    model_config = ConfigDict(frozen=True)

    incidents: list[ReliabilityIncidentSummary]
    count: int


class ReliabilityIncidentNotFoundError(ValueError):
    """Safe client-visible result for an unknown incident id."""


def register_reliability_tools(
    mcp: MCPServer,
    *,
    session_factory: SessionFactory | None = None,
) -> None:
    """Register persisted RecallGuard reads on the shared MCP server."""

    def run_read[ResultT](
        tool_name: str,
        operation: Callable[[Session], ResultT],
        *,
        resource_id: uuid.UUID | None = None,
    ) -> ResultT:
        return run_persisted_read(
            session_factory=session_factory,
            logger=logger,
            tool_name=tool_name,
            operation=operation,
            expected_errors=(ReliabilityIncidentNotFoundError,),
            resource_id=str(resource_id) if resource_id is not None else None,
        )

    @mcp.tool(annotations=READ_ONLY_ANNOTATIONS, structured_output=True)
    def get_reliability_overview() -> ReliabilityOverviewRead:
        """Return persisted RecallGuard health across configured collectors.

        This reports stored runs and incidents only. It does not execute a
        scraper, rerun RecallGuard, start healing, or contact a provider.
        """

        return run_read(
            "get_reliability_overview",
            read_reliability_overview,
        )

    @mcp.tool(annotations=READ_ONLY_ANNOTATIONS, structured_output=True)
    def list_reliability_incidents(
        collector_id: uuid.UUID | None = None,
        status: IncidentStatus | None = None,
        limit: Annotated[
            int, Field(ge=1, le=MAX_INCIDENT_LIMIT)
        ] = DEFAULT_INCIDENT_LIMIT,
    ) -> ReliabilityIncidentListResult:
        """List persisted extraction and reliability incidents, newest first.

        Optional filters use existing collector and incident status semantics.
        This does not rerun extraction, detection, verification, or healing.
        """

        def operation(session: Session) -> ReliabilityIncidentListResult:
            incidents = read_reliability_incidents(
                session,
                collector_id=collector_id,
                status=status,
                limit=limit,
            )
            return ReliabilityIncidentListResult(
                incidents=incidents,
                count=len(incidents),
            )

        return run_read("list_reliability_incidents", operation)

    @mcp.tool(annotations=READ_ONLY_ANNOTATIONS, structured_output=True)
    def get_reliability_incident(
        incident_id: uuid.UUID,
    ) -> ReliabilityIncidentRead:
        """Read one persisted extraction or reliability incident in full.

        Evidence, lifecycle timestamps, and recovery proof are returned only
        when stored. This does not rerun extraction, verification, or healing.
        """

        def operation(session: Session) -> ReliabilityIncidentRead:
            incident = read_reliability_incident(session, incident_id=incident_id)
            if incident is None:
                raise ReliabilityIncidentNotFoundError(
                    f"Reliability incident {incident_id} was not found."
                )
            return incident

        return run_read(
            "get_reliability_incident",
            operation,
            resource_id=incident_id,
        )

    @mcp.tool(annotations=READ_ONLY_ANNOTATIONS, structured_output=True)
    def get_recallguard_demo() -> RecallGuardDemoRead:
        """Read the persisted deterministic RecallGuard fixture-replay state.

        Fixture replay is a safe lifecycle demonstration, not a live Bright
        Data run. This tool never starts or advances the replay and performs no
        provider work.
        """

        return run_read("get_recallguard_demo", read_demo)

    @mcp.tool(annotations=READ_ONLY_ANNOTATIONS, structured_output=True)
    def get_live_brightdata_evidence() -> LiveBrightDataEvidenceRead:
        """Return persisted evidence from the isolated real Bright Data test.

        The response honestly preserves the historical detection, repair, and
        safety decision, including rejection when verification failed. It does
        not claim fixture replay is live self-healing, contact Bright Data,
        deploy a repair, or trigger a fresh run.
        """

        return run_read(
            "get_live_brightdata_evidence",
            read_live_brightdata_evidence,
        )
