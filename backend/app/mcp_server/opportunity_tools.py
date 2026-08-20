"""Read-only MCP tools for trusted GapRadar opportunities."""

import logging
import uuid
from collections.abc import Callable
from typing import Annotated

from mcp.server import MCPServer
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.mcp_server.common import (
    READ_ONLY_ANNOTATIONS,
    SessionFactory,
    run_persisted_read,
)
from app.opportunity_engine.schemas import Opportunity
from app.opportunity_engine.service import (
    DEFAULT_LIMIT,
)
from app.opportunity_engine.service import (
    get_opportunity as read_opportunity,
)
from app.opportunity_engine.service import list_opportunities as read_opportunities
from app.research_intelligence.schemas import OpportunityResearchIntelligence
from app.research_intelligence.service import get_research_intelligence

logger = logging.getLogger(__name__)
MAX_LIMIT = 200


class OpportunityListResult(BaseModel):
    """A bounded ranked list of persisted, trusted opportunities."""

    model_config = ConfigDict(frozen=True)

    opportunities: list[Opportunity]
    count: int


class OpportunityNotFoundError(ValueError):
    """Safe client-visible result for an unavailable trusted opportunity."""


def register_opportunity_tools(
    mcp: MCPServer,
    *,
    session_factory: SessionFactory | None = None,
) -> None:
    """Register persisted Opportunity reads on the shared MCP server."""

    def require_opportunity(session: Session, opportunity_id: uuid.UUID) -> Opportunity:
        opportunity = read_opportunity(session, signal_id=opportunity_id)
        if opportunity is None:
            raise OpportunityNotFoundError(
                f"Opportunity {opportunity_id} was not found."
            )
        return opportunity

    def run_read[ResultT](
        tool_name: str,
        operation: Callable[[Session], ResultT],
        *,
        opportunity_id: uuid.UUID | None = None,
    ) -> ResultT:
        return run_persisted_read(
            session_factory=session_factory,
            logger=logger,
            tool_name=tool_name,
            operation=operation,
            expected_errors=(OpportunityNotFoundError,),
            resource_id=str(opportunity_id) if opportunity_id is not None else None,
        )

    @mcp.tool(annotations=READ_ONLY_ANNOTATIONS, structured_output=True)
    def list_opportunities(
        limit: Annotated[int, Field(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
    ) -> OpportunityListResult:
        """List GapRadar's strongest persisted, currently trusted opportunities.

        Results preserve established Opportunity scores and source provenance.
        This does not refresh the market, run a collector, enrich research, or
        contact an external provider.
        """

        def operation(session: Session) -> OpportunityListResult:
            opportunities = read_opportunities(session, limit=limit)
            return OpportunityListResult(
                opportunities=opportunities,
                count=len(opportunities),
            )

        return run_read("list_opportunities", operation)

    @mcp.tool(annotations=READ_ONLY_ANNOTATIONS, structured_output=True)
    def get_opportunity(opportunity_id: uuid.UUID) -> Opportunity:
        """Return one persisted, currently trusted GapRadar opportunity.

        The result includes the source identifiers and established Opportunity
        scores. An unknown or RecallGuard-untrusted signal is unavailable.
        This does not run collection, scoring, or provider work.
        """

        def operation(session: Session) -> Opportunity:
            return require_opportunity(session, opportunity_id)

        return run_read(
            "get_opportunity",
            operation,
            opportunity_id=opportunity_id,
        )

    @mcp.tool(annotations=READ_ONLY_ANNOTATIONS, structured_output=True)
    def get_opportunity_research(
        opportunity_id: uuid.UUID,
    ) -> OpportunityResearchIntelligence:
        """Return persisted academic research supporting an opportunity.

        This reads the established Opportunity research contract only. It does
        not search arXiv, perform matching, enrich the opportunity, or contact
        Bright Data, OpenAI, or another provider.
        """

        def operation(session: Session) -> OpportunityResearchIntelligence:
            require_opportunity(session, opportunity_id)
            return OpportunityResearchIntelligence.from_intelligence(
                get_research_intelligence(session, signal_id=opportunity_id)
            )

        return run_read(
            "get_opportunity_research",
            operation,
            opportunity_id=opportunity_id,
        )
