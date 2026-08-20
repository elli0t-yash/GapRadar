"""Explicit Investigation actions shared by FastAPI and MCP."""

import logging
import uuid
from collections.abc import Callable
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.domain.enums import InvestigationRunStatus, ResearchOutcomeReason
from app.investigations.runs import (
    reconcile_stale_investigation_runs,
    set_run_status,
    start_run,
)
from app.investigations.schemas import InvestigationRunAccepted
from app.investigations.service import get_investigation

logger = logging.getLogger(__name__)

InvestigationRunSubmitter = Callable[[uuid.UUID], None]


class InvestigationNotFoundError(LookupError):
    """The requested persisted Investigation does not exist."""


def start_investigation_analysis(
    session: Session,
    *,
    investigation_id: uuid.UUID,
    submit: InvestigationRunSubmitter,
) -> InvestigationRunAccepted:
    """Claim or reuse one run and submit a new claim exactly once.

    This is the transport-neutral composition behind both FastAPI and MCP.
    The database's active-run uniqueness constraint remains the concurrency
    guarantee; ``already_running`` is what prevents the winner's executor from
    being submitted twice.
    """
    investigation = get_investigation(session, investigation_id=investigation_id)
    if investigation is None:
        raise InvestigationNotFoundError(
            f"Investigation {investigation_id} was not found."
        )

    reconcile_stale_investigation_runs(session)
    run, already_running = start_run(session, investigation=investigation)

    if not already_running:
        try:
            submit(run.id)
        except Exception as exc:
            # A committed QUEUED claim with no executor would block every later
            # request until it aged out. Close it immediately and preserve the
            # same backend retry authority used for other unexpected failures.
            set_run_status(session, run, InvestigationRunStatus.FAILED)
            run.completed_at = datetime.now(UTC)
            run.error = "the investigation executor could not be scheduled"
            run.outcome_reason = ResearchOutcomeReason.UNEXPECTED_ERROR
            session.commit()
            logger.error(
                "investigation_run_submission_failed",
                extra={
                    "investigation_id": str(investigation_id),
                    "run_id": str(run.id),
                    "error_type": type(exc).__name__,
                },
            )
            raise

    return InvestigationRunAccepted(
        run_id=run.id,
        investigation_id=investigation_id,
        status=run.status,
        already_running=already_running,
    )
