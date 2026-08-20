"""Explicit MCP actions for persisted independent Investigations."""

import logging
import uuid
from collections.abc import Callable
from typing import Annotated

from mcp.server import MCPServer
from mcp.types import ToolAnnotations
from pydantic import Field, ValidationError
from sqlalchemy.orm import Session

from app.db.session import get_session_factory
from app.investigations.actions import (
    InvestigationNotFoundError,
    InvestigationRunSubmitter,
    start_investigation_analysis,
)
from app.investigations.background import schedule_investigation_run
from app.investigations.schemas import (
    MAX_INDUSTRY_CHARS,
    MAX_QUERY_CHARS,
    InvestigationCreate,
    InvestigationRead,
    InvestigationRunAccepted,
)
from app.investigations.service import create_investigation as persist_investigation
from app.mcp_server.common import SessionFactory

logger = logging.getLogger(__name__)

CREATE_ANNOTATIONS = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=False,
    idempotentHint=False,
    openWorldHint=False,
)
RUN_ANNOTATIONS = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=False,
    idempotentHint=False,
    openWorldHint=False,
)


class InvestigationInputError(ValueError):
    """Safe client-facing validation failure for an Investigation draft."""


def register_investigation_action_tools(
    mcp: MCPServer,
    *,
    session_factory: SessionFactory | None = None,
    investigation_submitter: InvestigationRunSubmitter | None = None,
) -> None:
    """Register the two explicit Investigation write actions."""

    def run_action[ResultT](
        tool_name: str,
        operation: Callable[[Session], ResultT],
        *,
        resource_id: uuid.UUID | None = None,
    ) -> ResultT:
        try:
            factory = session_factory or get_session_factory()
            with factory() as session:
                return operation(session)
        except (InvestigationInputError, InvestigationNotFoundError):
            raise
        except Exception as exc:  # noqa: BLE001 - sanitize the MCP boundary
            logger.error(
                "mcp_investigation_action_failed",
                extra={
                    "mcp_tool": tool_name,
                    "error_type": type(exc).__name__,
                    "investigation_id": (
                        str(resource_id) if resource_id is not None else None
                    ),
                },
            )
            raise RuntimeError(
                "GapRadar could not complete the Investigation action."
            ) from None

    @mcp.tool(annotations=CREATE_ANNOTATIONS, structured_output=True)
    def create_investigation(
        query: Annotated[str, Field(min_length=1, max_length=MAX_QUERY_CHARS)],
        industry: Annotated[
            str | None, Field(max_length=MAX_INDUSTRY_CHARS)
        ] = None,
    ) -> InvestigationRead:
        """Create a persisted Investigation draft.

        This does not start analysis, schedule execution, or call Bright Data,
        OpenAI, arXiv, or another external provider. Use run_investigation as a
        separate explicit action if analysis is wanted.
        """
        try:
            payload = InvestigationCreate(query=query, industry=industry)
        except ValidationError:
            raise InvestigationInputError(
                "The Investigation query or industry is invalid."
            ) from None

        def operation(session: Session) -> InvestigationRead:
            investigation = persist_investigation(session, payload=payload)
            return InvestigationRead.model_validate(investigation)

        return run_action("create_investigation", operation)

    @mcp.tool(annotations=RUN_ANNOTATIONS, structured_output=True)
    def run_investigation(
        investigation_id: uuid.UUID,
    ) -> InvestigationRunAccepted:
        """Start or reuse analysis for an existing Investigation.

        This action may use external providers including Bright Data, OpenAI,
        and academic research acquisition. Repeated calls while a run is active
        reuse it and do not schedule duplicate provider execution.
        """

        def operation(session: Session) -> InvestigationRunAccepted:
            submitter = investigation_submitter or schedule_investigation_run
            return start_investigation_analysis(
                session,
                investigation_id=investigation_id,
                submit=submitter,
            )

        return run_action(
            "run_investigation",
            operation,
            resource_id=investigation_id,
        )
