"""Claiming, watching and reconciling investigation runs. No provider calls.

    POST /investigations/{id}/run  -> claim + 202   (this module: start_run)
                                      background    (execution: execute_run)
    GET  /investigations/{id}/run  -> watch it      (this module: latest_run)
    GET  /investigations/{id}/research -> the result (research service)

Deliberately the same shape as app.research_intelligence.enrichment's
claiming half, because it is the same problem: one subject, at most one
run in flight, a database constraint doing the real work and the service
turning a lost race into a useful answer.

THE RUN ROW IS AUTHORITATIVE for execution history. `Investigation.status`
is a denormalised mirror of the latest run's state, written ONLY by
`set_run_status` below and always inside the same transaction, so the two
cannot be observed disagreeing at any commit boundary.
"""

import logging
import uuid
from collections.abc import Callable
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.models import Investigation, InvestigationRun
from app.db.models.investigation_run import ACTIVE_INVESTIGATION_RUN_STATUSES
from app.domain.enums import (
    InvestigationRunStatus,
    InvestigationStatus,
    ResearchOutcomeReason,
)
from app.jobs.reconciliation import (
    DEFAULT_STALE_AFTER_SECONDS,
    reconcile_stale_runs,
)

logger = logging.getLogger(__name__)

ACTIVE_STATUSES: frozenset[InvestigationRunStatus] = frozenset(
    ACTIVE_INVESTIGATION_RUN_STATUSES
)

# The ONE mapping from "what the latest run is doing" to "what the
# investigation looks like". Total, so every run state has an answer and
# no combination can be reached by forgetting a branch.
#
# QUEUED maps to READY rather than RUNNING deliberately: work has been
# claimed and nothing has started, and READY is exactly the state Phase 1
# reserved for that. Reporting RUNNING there would claim an execution the
# backend cannot prove is under way -- the same dishonesty the enrichment
# status enum exists to avoid.
RUN_STATUS_TO_INVESTIGATION_STATUS: dict[InvestigationRunStatus, InvestigationStatus] = {
    InvestigationRunStatus.QUEUED: InvestigationStatus.READY,
    InvestigationRunStatus.RUNNING: InvestigationStatus.RUNNING,
    InvestigationRunStatus.SUCCEEDED: InvestigationStatus.SUCCEEDED,
    InvestigationRunStatus.FAILED: InvestigationStatus.FAILED,
}


def _utcnow() -> datetime:
    return datetime.now(UTC)


def set_run_status(
    session: Session,
    run: InvestigationRun,
    status: InvestigationRunStatus,
) -> None:
    """Move a run to a new state and mirror it onto its Investigation.

    THE ONLY WRITER OF `Investigation.status` after creation. Every
    transition goes through here, in one place, so the invariant

        Investigation.status == mapping[latest run.status]

    holds at every commit boundary rather than depending on each call
    site remembering. Does not commit: the caller owns the transaction,
    which is what makes the two writes atomic.
    """
    run.status = status
    investigation = session.get(Investigation, run.investigation_id)
    if investigation is not None:
        investigation.status = RUN_STATUS_TO_INVESTIGATION_STATUS[status]


def active_run(
    session: Session, *, investigation_id: uuid.UUID
) -> InvestigationRun | None:
    """This investigation's run that has not finished, if any."""
    return session.execute(
        select(InvestigationRun)
        .where(
            InvestigationRun.investigation_id == investigation_id,
            InvestigationRun.status.in_(ACTIVE_STATUSES),
        )
        .order_by(InvestigationRun.created_at.desc())
        .limit(1)
    ).scalar()


def latest_run(
    session: Session, *, investigation_id: uuid.UUID
) -> InvestigationRun | None:
    """The most recent run for this investigation, any status."""
    return session.execute(
        select(InvestigationRun)
        .where(InvestigationRun.investigation_id == investigation_id)
        .order_by(InvestigationRun.created_at.desc())
        .limit(1)
    ).scalar()


def start_run(
    session: Session, *, investigation: Investigation
) -> tuple[InvestigationRun, bool]:
    """Claim one run for this investigation.

    Returns `(run, already_running)`. Makes NO provider call, so it is
    safe inside an HTTP request and cannot be the thing that takes
    minutes.

    Deduplication is the point: a second click while one is in flight
    returns the running job untouched. The lookup below is an
    optimisation, NOT the guarantee -- two concurrent requests can both
    find nothing and both try to insert, and the partial unique index
    `uq_investigation_runs_active_investigation` fails the loser, which
    then reads the winner's row. The race costs one rolled-back INSERT
    and can never produce two sets of billable searches, because no
    provider call happens anywhere in this function.
    """
    existing = active_run(session, investigation_id=investigation.id)
    if existing is not None:
        return existing, True

    run = InvestigationRun(
        investigation_id=investigation.id, status=InvestigationRunStatus.QUEUED
    )
    session.add(run)
    session.flush()
    set_run_status(session, run, InvestigationRunStatus.QUEUED)
    try:
        session.commit()
    except IntegrityError:
        # The rollback is required, not cosmetic: the session is unusable
        # for further queries until the failed transaction is discarded.
        session.rollback()
        winner = active_run(session, investigation_id=investigation.id)
        if winner is None:
            # The constraint fired but nothing explains it -- a different
            # violation entirely. Raising beats returning a run that does
            # not correspond to what the caller asked for.
            raise
        logger.info(
            "investigation_run_claim_lost_race",
            extra={
                "investigation_id": str(investigation.id),
                "run_id": str(winner.id),
            },
        )
        return winner, True

    session.refresh(run)
    logger.info(
        "[investigation] queued run=%s investigation=%s", run.id, investigation.id
    )
    return run, False


# -- reconciliation ---------------------------------------------------------

STALE_RUN_AFTER_SECONDS = DEFAULT_STALE_AFTER_SECONDS


def reconcile_stale_investigation_runs(
    session: Session,
    *,
    now: Callable[[], datetime] = _utcnow,
    stale_after_seconds: float = STALE_RUN_AFTER_SECONDS,
) -> int:
    """Fail runs whose executor died, and return how many.

    WHAT HAPPENS IF RAILWAY RESTARTS MID-INVESTIGATION. The background
    task dies with the process. The Bright Data jobs it started keep
    running remotely and their results are simply never collected. The
    run row is left QUEUED or RUNNING, and because the partial unique
    index only permits one active run, the investigation would be
    permanently un-runnable -- not merely untidy.

    This ages such a row out to FAILED with
    ResearchOutcomeReason.INTERRUPTED, which the read model reports as
    retryable, and mirrors that onto the Investigation in the same
    transaction so the subject never reads RUNNING with no active run
    behind it. Papers upsert by arxiv_id and match rows upsert by
    (subject, paper), so the retry re-does the work without duplicating
    anything.

    Safe to call on every status read: it only touches rows older than
    the budget, so an in-flight run is never affected.
    """

    def mirror(session: Session, run: InvestigationRun) -> None:
        # Same transaction as the run's own status write, which is the
        # whole reason the reconciler takes a callback at all.
        investigation = session.get(Investigation, run.investigation_id)
        if investigation is not None:
            investigation.status = InvestigationStatus.FAILED

    return len(
        reconcile_stale_runs(
            session,
            model=InvestigationRun,
            active_statuses=ACTIVE_STATUSES,
            failed_status=InvestigationRunStatus.FAILED,
            now=now,
            stale_after_seconds=stale_after_seconds,
            outcome_reason=ResearchOutcomeReason.INTERRUPTED,
            on_reconciled=mirror,
        )
    )
