"""Shared safety boundary for read-only MCP adapters."""

import logging
from collections.abc import Callable

from mcp.types import ToolAnnotations
from sqlalchemy.orm import Session

from app.db.session import get_session_factory

SessionFactory = Callable[[], Session]
READ_ONLY_ANNOTATIONS = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=False,
)


def run_persisted_read[ResultT](
    *,
    session_factory: SessionFactory | None,
    logger: logging.Logger,
    tool_name: str,
    operation: Callable[[Session], ResultT],
    expected_errors: tuple[type[Exception], ...] = (),
    resource_id: str | None = None,
) -> ResultT:
    """Run one read with one owned session and a sanitized error boundary."""
    try:
        factory = session_factory or get_session_factory()
        with factory() as session:
            return operation(session)
    except expected_errors:
        raise
    except Exception as exc:  # noqa: BLE001 - sanitize the public MCP boundary
        logger.error(
            "mcp_persisted_read_failed",
            extra={
                "mcp_tool": tool_name,
                "error_type": type(exc).__name__,
                "resource_id": resource_id,
            },
        )
        raise RuntimeError("GapRadar could not read persisted data.") from None
