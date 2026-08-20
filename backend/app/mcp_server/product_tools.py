"""Read-only MCP tool for GapRadar's persisted product overview."""

import logging
from typing import Annotated

from mcp.server import MCPServer
from pydantic import Field

from app.dashboard.service import (
    DEFAULT_TOP_OPPORTUNITIES,
    MAX_TOP_OPPORTUNITIES,
    get_dashboard_read,
)
from app.mcp_server.common import (
    READ_ONLY_ANNOTATIONS,
    SessionFactory,
    run_persisted_read,
)
from app.schemas.dashboard import DashboardRead

logger = logging.getLogger(__name__)


def register_product_tools(
    mcp: MCPServer,
    *,
    session_factory: SessionFactory | None = None,
) -> None:
    """Register the persisted cross-product overview on ``mcp``."""

    @mcp.tool(annotations=READ_ONLY_ANNOTATIONS, structured_output=True)
    def get_gapradar_overview(
        top: Annotated[
            int, Field(ge=1, le=MAX_TOP_OPPORTUNITIES)
        ] = DEFAULT_TOP_OPPORTUNITIES,
    ) -> DashboardRead:
        """Return GapRadar's persisted pipeline, reliability, and signal overview.

        Top opportunities are RecallGuard-trusted and use established scores.
        This does not refresh data, run collection, start analysis, or contact
        any provider.
        """

        return run_persisted_read(
            session_factory=session_factory,
            logger=logger,
            tool_name="get_gapradar_overview",
            operation=lambda session: get_dashboard_read(session, top=top),
        )
