"""Ageing out job rows whose executor no longer exists.

THE PROBLEM, STATED HONESTLY. GapRadar runs background work on FastAPI's
BackgroundTasks, which lives in the Uvicorn process. A reload, a crash,
or a deploy kills the wait while the provider jobs carry on remotely --
leaving a row RUNNING with nothing left to finish it. Because every
active-job table here is guarded by a partial unique index, a stuck row
is not cosmetic: it permanently blocks its subject from ever being run
again, which disables the feature for that subject rather than merely
looking untidy.

This is the reconciler for that, and it is deliberately the dumbest one
that works: age it out. It does NOT resume by provider job id -- that is
a larger piece of work -- it just makes the row terminal so the user can
retry, which is the behaviour the UI needs.

Generic over the run table because the pattern was proven once for
research enrichments and is needed identically for investigation runs.
The alternative -- a second hand-written copy -- is exactly how the two
would come to disagree about what "stale" means.
"""

import logging
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

# How long a job may stay active before it is considered abandoned.
#
# Comfortably above the bounded acquisition budget plus a semantic
# matching pass, so a genuinely slow run is never killed by it; the only
# rows this catches are ones whose executor no longer exists.
DEFAULT_STALE_AFTER_SECONDS = 900.0

# What a reconciled row says happened. Written for a person to read: it
# names the likely cause, states what was NOT done (nothing remote was
# cancelled), and says the action is repeatable.
STALE_RUN_MESSAGE = (
    "the worker running this stopped before it finished (most likely a "
    "backend restart); no provider job was cancelled, and this can be "
    "retried"
)


def reconcile_stale_runs[RunT](
    session: Session,
    *,
    model: type[RunT],
    active_statuses: frozenset[Any] | tuple[Any, ...],
    failed_status: Any,
    now: Callable[[], datetime],
    stale_after_seconds: float = DEFAULT_STALE_AFTER_SECONDS,
    message: str = STALE_RUN_MESSAGE,
    outcome_reason: Any | None = None,
    on_reconciled: Callable[[Session, RunT], None] | None = None,
) -> list[RunT]:
    """Fail every active run older than the budget. Returns the ones failed.

    Safe to call on every status read: it only touches rows older than
    the budget, so an in-flight run is never affected.

    `on_reconciled` runs inside the same transaction as the status write,
    for callers that keep a denormalised copy of the state elsewhere --
    an Investigation's own status, for instance. Doing it here rather
    than in a second pass is what stops the two disagreeing after a
    crash, which is the exact failure this function exists to clean up.
    """
    cutoff = now() - timedelta(seconds=stale_after_seconds)
    stale = list(
        session.execute(
            select(model).where(
                model.status.in_(tuple(active_statuses)),  # type: ignore[attr-defined]
                model.created_at < cutoff,  # type: ignore[attr-defined]
            )
        ).scalars()
    )
    for run in stale:
        run.status = failed_status  # type: ignore[attr-defined]
        run.completed_at = now()  # type: ignore[attr-defined]
        run.error = message  # type: ignore[attr-defined]
        if outcome_reason is not None and hasattr(run, "outcome_reason"):
            run.outcome_reason = outcome_reason  # type: ignore[attr-defined]
        if on_reconciled is not None:
            on_reconciled(session, run)
        logger.warning(
            "stale_run_reconciled",
            extra={"table": model.__tablename__, "run_id": str(run.id)},  # type: ignore[attr-defined]
        )
    if stale:
        session.commit()
    return stale
