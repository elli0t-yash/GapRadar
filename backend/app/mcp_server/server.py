"""GapRadar's official MCP SDK v2 server entry point."""

from collections.abc import Callable

from mcp.server import MCPServer
from sqlalchemy.orm import Session

from app.investigations.actions import InvestigationRunSubmitter
from app.mcp_server.investigation_action_tools import (
    register_investigation_action_tools,
)
from app.mcp_server.investigation_tools import register_investigation_tools
from app.mcp_server.opportunity_tools import register_opportunity_tools
from app.mcp_server.product_tools import register_product_tools
from app.mcp_server.reliability_tools import register_reliability_tools


def create_mcp_server(
    *,
    session_factory: Callable[[], Session] | None = None,
    investigation_submitter: InvestigationRunSubmitter | None = None,
) -> MCPServer:
    """Build the in-process GapRadar MCP adapter.

    ``session_factory`` and ``investigation_submitter`` let the official
    in-memory client exercise real application operations against an isolated
    database and a recording scheduler. Production resolves the configured
    session factory and bounded local Investigation submitter.
    """

    server = MCPServer(
        "GapRadar",
        instructions=(
            "GapRadar exposes persisted Investigation, Opportunity, product, "
            "and RecallGuard intelligence through read-only tools. Creating an "
            "Investigation is provider-free; running one is a separate explicit "
            "action that may spend provider budget. No market refresh, collector "
            "execution, or RecallGuard healing tool is exposed."
        ),
    )
    register_investigation_tools(server, session_factory=session_factory)
    register_investigation_action_tools(
        server,
        session_factory=session_factory,
        investigation_submitter=investigation_submitter,
    )
    register_opportunity_tools(server, session_factory=session_factory)
    register_reliability_tools(server, session_factory=session_factory)
    register_product_tools(server, session_factory=session_factory)
    return server


mcp = create_mcp_server()


if __name__ == "__main__":
    mcp.run()
