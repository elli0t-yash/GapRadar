"""Read-only MCP tools for persisted independent investigations.

This adapter deliberately depends on the same application services as the
FastAPI routes.  It never calls GapRadar over HTTP and never imports an
acquisition or model provider.
"""

import logging
import uuid
from collections.abc import Callable
from typing import Annotated, TypeVar

from mcp.server import MCPServer
from mcp.types import ToolAnnotations
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.db.models import Investigation
from app.db.session import get_session_factory
from app.domain.enums import ResearchSubjectOrigin
from app.investigations.evidence import (
    DEFAULT_LIMIT as EVIDENCE_DEFAULT_LIMIT,
)
from app.investigations.evidence import MAX_LIMIT as EVIDENCE_MAX_LIMIT
from app.investigations.evidence import get_competitors, get_demand_evidence
from app.investigations.runs import latest_run
from app.investigations.schemas import (
    CompetitorCollection,
    DemandEvidenceCollection,
    InvestigationRead,
    InvestigationRunRead,
)
from app.investigations.service import DEFAULT_LIMIT, MAX_LIMIT
from app.investigations.service import get_investigation as read_investigation
from app.investigations.service import list_investigations as read_investigations
from app.research_intelligence.schemas import ResearchIntelligence
from app.research_intelligence.service import get_subject_research_intelligence

logger = logging.getLogger(__name__)

SessionFactory = Callable[[], Session]
ResultT = TypeVar("ResultT")

READ_ONLY_ANNOTATIONS = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=False,
)


class InvestigationListResult(BaseModel):
    """A bounded list of persisted investigations."""

    model_config = ConfigDict(frozen=True)

    investigations: list[InvestigationRead]
    count: int


class MCPInvestigationRunRead(InvestigationRunRead):
    """The existing run read model with an MCP-stable external id field.

    ``InvestigationRunRead`` accepts an ORM object's ``id`` through a validation
    alias and serializes it as ``run_id``. MCP v2 validates structured output
    against the validation schema, so the nested schema must name the external
    field directly. All other fields and computed flags remain inherited from
    the backend read model.
    """

    run_id: uuid.UUID


class InvestigationStatusResult(BaseModel):
    """An investigation and its latest persisted run, when one exists."""

    model_config = ConfigDict(frozen=True)

    investigation: InvestigationRead
    latest_run: MCPInvestigationRunRead | None


class InvestigationNotFoundError(ValueError):
    """A safe, client-visible error for an unknown investigation id."""


def _not_found(investigation_id: uuid.UUID) -> InvestigationNotFoundError:
    return InvestigationNotFoundError(
        f"Investigation {investigation_id} was not found."
    )


def register_investigation_tools(
    mcp: MCPServer,
    *,
    session_factory: SessionFactory | None = None,
) -> None:
    """Register Phase 1's persisted-read tools on ``mcp``.

    A factory may be supplied by tests.  Production resolves the application's
    cached SQLAlchemy session factory lazily on every invocation, then owns one
    session for exactly the duration of that tool call.
    """

    def run_read(
        tool_name: str,
        operation: Callable[[Session], ResultT],
        *,
        investigation_id: uuid.UUID | None = None,
    ) -> ResultT:
        try:
            factory = session_factory or get_session_factory()
            with factory() as session:
                return operation(session)
        except InvestigationNotFoundError:
            raise
        except Exception as exc:  # noqa: BLE001 - sanitize the MCP boundary
            # Log enough to identify the failing adapter without echoing an
            # exception message that may contain a database URL or SQL text.
            logger.error(
                "mcp_investigation_read_failed",
                extra={
                    "mcp_tool": tool_name,
                    "error_type": type(exc).__name__,
                    "investigation_id": (
                        str(investigation_id) if investigation_id is not None else None
                    ),
                },
            )
            raise RuntimeError(
                "GapRadar could not read persisted investigation data."
            ) from None

    def require_investigation(
        session: Session, investigation_id: uuid.UUID
    ) -> Investigation:
        investigation = read_investigation(session, investigation_id=investigation_id)
        if investigation is None:
            raise _not_found(investigation_id)
        return investigation

    @mcp.tool(
        annotations=READ_ONLY_ANNOTATIONS,
        structured_output=True,
    )
    def list_investigations(
        limit: Annotated[int, Field(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
    ) -> InvestigationListResult:
        """List persisted GapRadar independent investigations.

        This is a read-only operation. It does not create an investigation,
        start analysis, retry a run, or contact a provider.
        """

        def operation(session: Session) -> InvestigationListResult:
            rows = read_investigations(session, limit=limit)
            investigations = [InvestigationRead.model_validate(row) for row in rows]
            return InvestigationListResult(
                investigations=investigations,
                count=len(investigations),
            )

        return run_read("list_investigations", operation)

    @mcp.tool(
        annotations=READ_ONLY_ANNOTATIONS,
        structured_output=True,
    )
    def get_investigation(
        investigation_id: uuid.UUID,
    ) -> InvestigationRead:
        """Return one persisted GapRadar independent investigation.

        This reads the user-supplied hypothesis and current persisted status.
        It does not start or retry analysis and does not contact a provider.
        """

        def operation(session: Session) -> InvestigationRead:
            investigation = require_investigation(session, investigation_id)
            return InvestigationRead.model_validate(investigation)

        return run_read(
            "get_investigation",
            operation,
            investigation_id=investigation_id,
        )

    @mcp.tool(
        annotations=READ_ONLY_ANNOTATIONS,
        structured_output=True,
    )
    def get_investigation_status(
        investigation_id: uuid.UUID,
    ) -> InvestigationStatusResult:
        """Return the latest persisted run state and measured phase progress.

        A never-run investigation has ``latest_run`` set to null. This tool
        does not reconcile, start, or retry work and does not contact a
        provider.
        """

        def operation(session: Session) -> InvestigationStatusResult:
            investigation = require_investigation(session, investigation_id)
            run = latest_run(session, investigation_id=investigation_id)
            run_read = None if run is None else InvestigationRunRead.model_validate(run)
            return InvestigationStatusResult(
                investigation=InvestigationRead.model_validate(investigation),
                latest_run=(
                    None
                    if run_read is None
                    else MCPInvestigationRunRead.model_validate(run_read.model_dump())
                ),
            )

        return run_read(
            "get_investigation_status",
            operation,
            investigation_id=investigation_id,
        )

    @mcp.tool(
        annotations=READ_ONLY_ANNOTATIONS,
        structured_output=True,
    )
    def get_investigation_research(
        investigation_id: uuid.UUID,
    ) -> ResearchIntelligence:
        """Return persisted academic research for an investigation.

        This does not perform a research search, semantic match, analysis run,
        or provider call. A valid investigation with no research returns an
        empty structured result.
        """

        def operation(session: Session) -> ResearchIntelligence:
            require_investigation(session, investigation_id)
            return get_subject_research_intelligence(
                session,
                subject_id=investigation_id,
                origin=ResearchSubjectOrigin.INVESTIGATION,
            )

        return run_read(
            "get_investigation_research",
            operation,
            investigation_id=investigation_id,
        )

    @mcp.tool(
        annotations=READ_ONLY_ANNOTATIONS,
        structured_output=True,
    )
    def get_investigation_demand_evidence(
        investigation_id: uuid.UUID,
        limit: Annotated[
            int, Field(ge=1, le=EVIDENCE_MAX_LIMIT)
        ] = EVIDENCE_DEFAULT_LIMIT,
    ) -> DemandEvidenceCollection:
        """Return persisted demand evidence for an investigation.

        Supporting and contradictory classifications and their search-query
        provenance are preserved. This does not discover pages, rerun an
        investigation, or contact a provider.
        """

        def operation(session: Session) -> DemandEvidenceCollection:
            require_investigation(session, investigation_id)
            return get_demand_evidence(
                session,
                investigation_id=investigation_id,
                limit=limit,
            )

        return run_read(
            "get_investigation_demand_evidence",
            operation,
            investigation_id=investigation_id,
        )

    @mcp.tool(
        annotations=READ_ONLY_ANNOTATIONS,
        structured_output=True,
    )
    def get_investigation_competitors(
        investigation_id: uuid.UUID,
        limit: Annotated[
            int, Field(ge=1, le=EVIDENCE_MAX_LIMIT)
        ] = EVIDENCE_DEFAULT_LIMIT,
    ) -> CompetitorCollection:
        """Return persisted competitor candidates for an investigation.

        Classification and discovery-query provenance come only from stored
        backend records. This does not search the web, rerun an investigation,
        or contact a provider.
        """

        def operation(session: Session) -> CompetitorCollection:
            require_investigation(session, investigation_id)
            return get_competitors(
                session,
                investigation_id=investigation_id,
                limit=limit,
            )

        return run_read(
            "get_investigation_competitors",
            operation,
            investigation_id=investigation_id,
        )
